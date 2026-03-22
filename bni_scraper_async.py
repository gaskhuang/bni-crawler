"""
BNI 全球會員資料爬蟲 - 極致優化版 (Zero Selenium, Pure Async)

完全不使用 Selenium，改用 aiohttp 非同步 HTTP 請求：
- 會員列表: POST /bnicms/v3/frontend/memberlist/display (AJAX)
- 會員詳情: POST /bnicms/v3/frontend/memberdetail/display (AJAX)

支援 44 個國家，啟動時顯示文字選單選擇國家。

用法: python bni_scraper_async.py [並行數，預設30]
"""

import asyncio
import csv
import html as html_mod
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bni_scraper_async.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 常數
BASE_URL = "https://bni.com.tw"
COUNTRY_LIST_API = f"{BASE_URL}/web/open/appsCmsCountryListJson"
REGION_API = f"{BASE_URL}/web/open/appsCmsNationalChapterSearchFilterJson"
MEMBERLIST_PAGE = f"{BASE_URL}/zh-TW/memberlist"
MEMBERLIST_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberlist/display"
MEMBERDETAIL_PAGE = f"{BASE_URL}/zh-TW/memberdetails"
MEMBERDETAIL_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberdetail/display"
MAX_RETRIES = 3

# 已知的 44 個國家 (ID → 名稱, 區域數)
COUNTRIES = {
    2: ("Costa Rica", 1),
    5: ("Cyprus", 5),
    10: ("United States", 213),
    364: ("Antarctica", 66),
    460: ("Slovenia", 1),
    464: ("Barbados", 1),
    470: ("Suriname", 1),
    1010: ("South Africa", 21),
    1138: ("United Arab Emirates", 9),
    1418: ("Kenya", 3),
    1923: ("Romania", 28),
    1924: ("Malta", 1),
    1925: ("Zimbabwe", 1),
    1926: ("Nigeria", 9),
    2777: ("Colombia", 24),
    3278: ("Malaysia", 25),
    3376: ("Denmark", 9),
    3435: ("Finland", 10),
    3445: ("Norway", 9),
    3488: ("Poland", 32),
    3521: ("Mexico", 72),
    3542: ("Brazil", 90),
    3548: ("Switzerland", 9),
    3622: ("Spain", 41),
    3650: ("Belgium (French)", 6),
    3655: ("France", 70),
    3800: ("Portugal", 15),
    3857: ("India", 174),
    3908: ("Italy", 54),
    3979: ("Thailand", 18),
    5723: ("Austria", 7),
    5768: ("Germany", 60),
    6117: ("Sweden", 31),
    7585: ("Singapore", 6),
    7638: ("Japan", 69),
    7699: ("Taiwan", 31),
    7732: ("Vietnam", 24),
    7744: ("South Korea", 25),
    7751: ("Hong Kong", 2),
    7872: ("Ireland", 8),
    7984: ("Netherlands", 21),
    8150: ("United Kingdom", 79),
    9517: ("Tanzania", 1),
    9742: ("Macau", 1),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

CSV_FIELDS = [
    "country_id", "country_name",
    "region_id", "region_name", "chapter_id", "chapter_name",
    "member_name", "title", "company", "industry", "city",
    "phone", "mobile", "address", "postal_code", "website",
    "business_desc", "ideal_referral", "best_product",
    "top_problem_solved", "ideal_referral_partner", "favorite_story",
    "line_url", "detail_url",
]


def show_country_menu():
    """顯示國家選單，回傳選擇的國家 ID 列表"""
    sorted_countries = sorted(COUNTRIES.items(), key=lambda x: x[1][0])

    print("\n" + "=" * 60)
    print("  BNI 全球會員爬蟲 - 國家選擇")
    print("=" * 60)

    # 分欄顯示
    for i, (cid, (name, regions)) in enumerate(sorted_countries, 1):
        print(f"  {i:2d}. {name:<25s} ({regions:>3d} 區域)", end="")
        if i % 2 == 0:
            print()
        else:
            print("  ", end="")
    if len(sorted_countries) % 2 == 1:
        print()

    print(f"\n  {len(sorted_countries)+1:2d}. ** 全部國家 **")
    print("=" * 60)
    print("  輸入編號（可多選，用逗號分隔，例: 1,3,5）")
    print("  直接按 Enter 預設爬取 Taiwan")
    print("=" * 60)

    while True:
        choice = input("\n  請選擇: ").strip()

        if not choice:
            # 預設 Taiwan
            return [7699]

        try:
            nums = [int(x.strip()) for x in choice.split(",")]
        except ValueError:
            print("  ❌ 請輸入數字編號")
            continue

        all_option = len(sorted_countries) + 1
        if all_option in nums:
            return list(COUNTRIES.keys())

        selected = []
        valid = True
        for n in nums:
            if 1 <= n <= len(sorted_countries):
                cid = sorted_countries[n - 1][0]
                selected.append(cid)
            else:
                print(f"  ❌ 無效編號: {n}")
                valid = False
                break

        if valid and selected:
            names = [COUNTRIES[cid][0] for cid in selected]
            print(f"\n  ✅ 已選擇: {', '.join(names)}")
            return selected


async def fetch_regions(session, country_id):
    """取得指定國家的區域列表"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(REGION_API, data={
                "countryId": country_id,
                "request_locale": "en_US",
            }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                return data.get("regions", [])
        except Exception as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)
            else:
                logger.warning("國家 %s 區域取得失敗: %s", country_id, e)
    return []


async def fetch_chapters_for_region(session, country_id, region, settings, sem):
    """用 AJAX 取得一個區域內所有分會的會員（一次 API call 拿整個區域）"""
    region_id = region["id"]
    region_name = region["name"]

    params = (
        f"countryIds={country_id}&regionId={region_id}&chapterName="
        f"&chapterArea=&memberKeywords=&chapterCity="
        f"&memberFirstName=&memberLastName=&memberCompany="
    )

    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(MEMBERLIST_AJAX, data={
                    "parameters": params,
                    "languages": settings["list_langs"],
                    "cmsv3": "true",
                    "mappedWidgetSettings": settings["list_mws"],
                    "pageMode": "Live_Site",
                    "website_type": "1",
                    "website_id": "10065",
                }, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    html_text = await resp.text()

                if len(html_text) < 100:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.5)
                        continue
                    return []

                soup = BeautifulSoup(html_text, "html.parser")
                rows = soup.select("table tbody tr")

                members = []
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) < 6:
                        continue
                    texts = [c.get_text(strip=True) for c in cells]

                    member_name = texts[1]
                    if not member_name:
                        continue

                    # 解析 chapter-region 欄位
                    chapter_region = texts[2]
                    chapter_name = chapter_region.split(" - ")[0].strip() if " - " in chapter_region else chapter_region

                    link = cells[1].find("a", href=re.compile(r"memberdetails"))
                    detail_url = link["href"] if link else ""
                    if detail_url and not detail_url.startswith("http"):
                        detail_url = f"{BASE_URL}/zh-TW/{detail_url}"

                    members.append({
                        "country_id": country_id,
                        "country_name": COUNTRIES.get(country_id, ("?", 0))[0],
                        "region_id": region_id,
                        "region_name": region_name,
                        "chapter_id": "",
                        "chapter_name": chapter_name,
                        "member_name": member_name,
                        "company": texts[6] if len(texts) > 6 else "",
                        "industry": texts[5] if len(texts) > 5 else "",
                        "city": texts[3] if len(texts) > 3 else "",
                        "detail_url": detail_url,
                    })

                return members

            except asyncio.TimeoutError:
                logger.warning("區域 %s 超時 (attempt %d)", region_name, attempt)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.warning("區域 %s 失敗 (attempt %d): %s", region_name, attempt, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.5)

    logger.warning("區域 %s: 最終失敗", region_name)
    return []


async def get_widget_settings(session, country_id):
    """從 memberlist 頁面取得 mappedWidgetSettings 和 languages"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(MEMBERLIST_PAGE, params={"countryIds": country_id}) as resp:
                text = await resp.text()

            mws = ""
            m = re.search(r"var mappedWidgetSettings = '(\[.*?\])';", text)
            if m:
                mws = m.group(1)

            langs = ""
            m2 = re.search(r"var languages = (\{.*?\}\});", text, re.DOTALL)
            if m2:
                langs = m2.group(1)

            # 也取得 memberdetail 的 settings
            async with session.get(MEMBERDETAIL_PAGE) as resp2:
                text2 = await resp2.text()

            detail_mws = ""
            m3 = re.search(r'name="mappedWidgetSettings"\s+value="([^"]*)"', text2)
            if m3:
                detail_mws = html_mod.unescape(m3.group(1))
            else:
                m3 = re.search(r"var mappedWidgetSettings = '(\[.*?\])';", text2)
                if m3:
                    detail_mws = m3.group(1)

            detail_langs = ""
            m4 = re.search(r"var languages = (\{.*?\}\});", text2, re.DOTALL)
            if m4:
                detail_langs = m4.group(1)

            if not mws:
                logger.warning("mappedWidgetSettings not found (attempt %d)", attempt)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                    continue

            logger.info("Widget settings loaded (mws=%d, langs=%d, detail_mws=%d)", len(mws), len(langs), len(detail_mws))
            return {
                "list_mws": mws,
                "list_langs": langs,
                "detail_mws": detail_mws,
                "detail_langs": detail_langs,
            }
        except Exception as e:
            logger.warning("取得 widget settings 失敗 (attempt %d): %s", attempt, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)

    raise RuntimeError("無法取得 widget settings")


def parse_member_detail(html_content, member):
    """解析會員詳情 HTML（同步，CPU-bound）"""
    if not html_content or len(html_content) < 50:
        return member

    soup = BeautifulSoup(html_content, "html.parser")

    # 電話: 從 <a href="tel:...">
    tel_links = soup.find_all("a", href=re.compile(r"^tel:"))
    phones = list(dict.fromkeys(a.get_text(strip=True) for a in tel_links if a.get_text(strip=True)))
    if phones:
        member["phone"] = phones[0]
    if len(phones) > 1:
        member["mobile"] = phones[1]

    # 文字分段解析
    body_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    section_markers = {
        "My Business": "business_desc", "我的業務": "business_desc",
        "Ideal Referral": "ideal_referral", "理想的引薦": "ideal_referral",
        "Top Problem Solved": "top_problem_solved", "解決過的最大難題": "top_problem_solved",
        "My Ideal Referral Partner": "ideal_referral_partner", "我理想中的引薦搭檔": "ideal_referral_partner",
        "Top Product": "best_product", "最佳產品": "best_product",
        "My Favorite BNI Story": "favorite_story",
    }

    sections = {}
    current_section = "header"
    section_lines = []

    for line in lines:
        if line in section_markers:
            if section_lines:
                sections[current_section] = section_lines
            current_section = line
            section_lines = []
        else:
            section_lines.append(line)
    if section_lines:
        sections[current_section] = section_lines

    # Header 解析
    header = sections.get("header", [])
    chapter_idx = -1
    for i, line in enumerate(header):
        if line == "Chapter":
            chapter_idx = i
            break

    if chapter_idx >= 0:
        if chapter_idx >= 3:
            member["industry"] = header[2]

        after_chapter = header[chapter_idx + 1:]
        if after_chapter:
            after_chapter = after_chapter[1:]  # 跳過分會名

        title_idx = -1
        for i, line in enumerate(after_chapter):
            if re.match(r"^(Dr\.|Mr\.|Ms\.|Mrs\.|博士|先生|小姐|女士)", line):
                title_idx = i
                member["title"] = line
                break

        if title_idx >= 0:
            info = after_chapter[title_idx + 1:]
            addr_lines = info[1:] if len(info) > 1 else []
            addr_parts = []
            for line in addr_lines:
                if re.match(r"^https?://|^www\.", line):
                    member["website"] = line
                    break
                if re.match(r"^\d{3,5}$", line):
                    member["postal_code"] = line
                    continue
                if line and not line.startswith(("BNI", "Links")):
                    addr_parts.append(line)
            if addr_parts:
                member["address"] = " ".join(addr_parts)

    # 段落內容
    for marker, field in section_markers.items():
        content = sections.get(marker, [])
        if content and not member.get(field):
            member[field] = "\n".join(content)

    # LINE URL
    line_urls = re.findall(r'https?://line\.me/[^\s"\'<>]+', html_content)
    if line_urls:
        member["line_url"] = line_urls[0]

    return member


async def fetch_member_detail(session, member, settings, sem):
    """用 AJAX 取得單一會員詳情"""
    detail_url = member.get("detail_url", "")
    if not detail_url:
        return member

    # 從 URL 取得 parameters
    if "?" in detail_url:
        parameters = detail_url.split("?", 1)[1]
    else:
        return member

    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(MEMBERDETAIL_AJAX, data={
                    "parameters": parameters,
                    "languages": settings["detail_langs"],
                    "cmsv3": "true",
                    "mappedWidgetSettings": settings["detail_mws"],
                    "pageMode": "Live_Site",
                    "website_type": "1",
                    "website_id": "10065",
                }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    html_content = await resp.text()

                return parse_member_detail(html_content, member)

            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.3)
            except Exception as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.3)
                else:
                    logger.debug("會員 %s 詳情失敗: %s", member.get("member_name", "?"), e)

    return member


def save_members_csv(members, filename):
    """儲存會員資料"""
    if not members:
        return
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for m in members:
            writer.writerow({k: m.get(k, "") for k in CSV_FIELDS})
    logger.info("已儲存 %d 筆至 %s", len(members), filename)


async def scrape_country(session, country_id, settings, concurrency):
    """爬取一個國家的所有會員"""
    country_name = COUNTRIES.get(country_id, ("Unknown", 0))[0]
    logger.info("=" * 50)
    logger.info("開始爬取: %s (ID: %d)", country_name, country_id)

    # 1. 取得區域列表
    regions = await fetch_regions(session, country_id)
    if not regions:
        logger.warning("%s: 無區域資料", country_name)
        return []

    logger.info("%s: %d 個區域", country_name, len(regions))

    # 2. 並行取得所有區域的會員列表
    sem = asyncio.Semaphore(concurrency)
    tasks = [fetch_chapters_for_region(session, country_id, r, settings, sem) for r in regions]

    all_members = []
    regions_done = 0
    regions_total = len(regions)

    for coro in asyncio.as_completed(tasks):
        members = await coro
        all_members.extend(members)
        regions_done += 1
        if regions_done % 10 == 0 or regions_done == regions_total:
            logger.info("[%s] 區域進度: %d/%d (%d%%) - 累計 %d 位",
                        country_name, regions_done, regions_total,
                        int(regions_done / regions_total * 100),
                        len(all_members))

    logger.info("[%s] 會員列表完成: %d 位", country_name, len(all_members))

    # 3. 並行取得所有會員詳情
    if all_members:
        detail_sem = asyncio.Semaphore(concurrency)
        detail_tasks = [fetch_member_detail(session, m, settings, detail_sem) for m in all_members]

        details_done = 0
        details_total = len(detail_tasks)

        for coro in asyncio.as_completed(detail_tasks):
            await coro
            details_done += 1
            if details_done % 500 == 0 or details_done == details_total:
                logger.info("[%s] 詳情進度: %d/%d (%d%%)",
                            country_name, details_done, details_total,
                            int(details_done / details_total * 100))

    logger.info("[%s] 完成! %d 位會員", country_name, len(all_members))
    return all_members


async def main():
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    # 國家選擇選單
    selected_ids = show_country_menu()
    selected_names = [COUNTRIES[cid][0] for cid in selected_ids]
    total_regions = sum(COUNTRIES[cid][1] for cid in selected_ids)

    print(f"\n  準備爬取 {len(selected_ids)} 個國家, {total_regions} 個區域")
    print(f"  並行數: {concurrency}")
    print()

    start_time = time.time()

    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        # 取得 widget settings
        settings = await get_widget_settings(session, selected_ids[0])

        # 逐國爬取
        all_members = []
        for country_id in selected_ids:
            members = await scrape_country(session, country_id, settings, concurrency)
            all_members.extend(members)

            # 每個國家存一份
            if members:
                country_name = COUNTRIES[country_id][0].lower().replace(" ", "_")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                save_members_csv(members, f"bni_members_{country_name}_{timestamp}.csv")

    elapsed = time.time() - start_time

    # 全部存一份總檔
    if len(selected_ids) > 1 and all_members:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_members_csv(all_members, f"bni_members_all_{timestamp}.csv")

    # 統計
    print(f"\n{'=' * 60}")
    print(f"  爬取完成!")
    print(f"  國家: {', '.join(selected_names)}")
    print(f"  會員總數: {len(all_members):,}")
    print(f"  耗時: {elapsed:.1f} 秒 ({elapsed/60:.1f} 分鐘)")
    if elapsed > 0:
        print(f"  速度: {len(all_members)/elapsed:.1f} 會員/秒")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
