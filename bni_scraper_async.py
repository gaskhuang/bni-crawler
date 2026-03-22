"""
BNI 台灣會員資料爬蟲 - 極致優化版 (Zero Selenium, Pure Async)

完全不使用 Selenium，改用 aiohttp 非同步 HTTP 請求：
- 會員列表: POST /bnicms/v3/frontend/memberlist/display (AJAX)
- 會員詳情: POST /bnicms/v3/frontend/memberdetail/display (AJAX)

預估 248 分會 / ~9,651 會員，3-5 分鐘完成（原版 48 分鐘）

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
REGION_API = f"{BASE_URL}/web/open/appsCmsNationalChapterSearchFilterJson"
MEMBERLIST_PAGE = f"{BASE_URL}/zh-TW/memberlist"
MEMBERLIST_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberlist/display"
MEMBERDETAIL_PAGE = f"{BASE_URL}/zh-TW/memberdetails"
MEMBERDETAIL_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberdetail/display"
COUNTRY_ID = 7699
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

CSV_FIELDS = [
    "region_id", "region_name", "chapter_id", "chapter_name",
    "member_name", "title", "company", "industry", "city",
    "phone", "mobile", "address", "postal_code", "website",
    "business_desc", "ideal_referral", "best_product",
    "top_problem_solved", "ideal_referral_partner", "favorite_story",
    "line_url", "detail_url",
]


async def get_widget_settings(session):
    """從 memberlist 頁面取得 mappedWidgetSettings 和 languages"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(MEMBERLIST_PAGE, params={"countryIds": COUNTRY_ID}) as resp:
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


async def fetch_chapter_members(session, chapter, settings, sem):
    """用 AJAX 取得一個分會的會員列表"""
    region_id = chapter["region_id"]
    chapter_id = chapter["chapter_id"]
    chapter_name = chapter["chapter_name"]
    region_name = chapter["region_name"]

    params = (
        f"countryIds={COUNTRY_ID}&regionId={region_id}&chapterName={chapter_id}"
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
                }, timeout=aiohttp.ClientTimeout(total=20)) as resp:
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

                    # Column 0 (hidden): group ("Exact Matches" or "More Results...")
                    # 只取 Exact Matches
                    if texts[0] != "Exact Matches":
                        continue

                    member_name = texts[1]
                    if not member_name:
                        continue

                    # 從 chapter-region column 確認是目標分會
                    chapter_region = texts[2]
                    if chapter_name not in chapter_region:
                        continue

                    link = cells[1].find("a", href=re.compile(r"memberdetails"))
                    detail_url = link["href"] if link else ""
                    if detail_url and not detail_url.startswith("http"):
                        detail_url = f"{BASE_URL}/zh-TW/{detail_url}"

                    members.append({
                        "region_id": region_id,
                        "region_name": region_name,
                        "chapter_id": chapter_id,
                        "chapter_name": chapter_name,
                        "member_name": member_name,
                        "company": texts[6] if len(texts) > 6 else "",
                        "industry": texts[5] if len(texts) > 5 else "",
                        "city": texts[3] if len(texts) > 3 else "",
                        "detail_url": detail_url,
                    })

                return members

            except asyncio.TimeoutError:
                logger.warning("分會 %s 超時 (attempt %d)", chapter_name, attempt)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.warning("分會 %s 失敗 (attempt %d): %s", chapter_name, attempt, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.5)

    logger.warning("分會 %s: 最終失敗", chapter_name)
    return []


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


def load_chapters_csv(filename="bni_chapters.csv"):
    """載入分會列表"""
    with open(filename, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_completed_chapters(filename="bni_members_progress_async.csv"):
    """載入已完成的分會"""
    path = Path(filename)
    if not path.exists():
        return set(), []
    completed = set()
    members = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            completed.add(row.get("chapter_id", ""))
            members.append(row)
    return completed, members


async def main():
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    logger.info("啟動非同步爬蟲，並行數: %d", concurrency)

    # 載入分會列表
    chapters = load_chapters_csv()
    logger.info("載入 %d 個分會", len(chapters))

    # 載入已完成
    completed, existing_members = load_completed_chapters()
    if completed:
        logger.info("跳過 %d 個已完成分會 (%d 筆會員)", len(completed), len(existing_members))

    pending = [ch for ch in chapters if ch["chapter_id"] not in completed]
    logger.info("待處理: %d 個分會", len(pending))

    if not pending:
        logger.info("所有分會已完成!")
        return

    start_time = time.time()

    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        # 1. 取得 widget settings
        settings = await get_widget_settings(session)

        # 2. 並行取得所有分會的會員列表
        logger.info("=== 階段一：取得會員列表 ===")
        list_sem = asyncio.Semaphore(concurrency)
        list_tasks = [fetch_chapter_members(session, ch, settings, list_sem) for ch in pending]

        all_members = list(existing_members)
        chapters_done = 0
        chapters_total = len(pending)

        for coro in asyncio.as_completed(list_tasks):
            members = await coro
            all_members.extend(members)
            chapters_done += 1
            if chapters_done % 20 == 0 or chapters_done == chapters_total:
                logger.info("會員列表進度: %d/%d (%d%%) - 累計 %d 位",
                            chapters_done, chapters_total,
                            int(chapters_done / chapters_total * 100),
                            len(all_members))

        list_time = time.time() - start_time
        new_members = all_members[len(existing_members):]
        logger.info("會員列表完成: %d 位新會員，耗時 %.1f 秒", len(new_members), list_time)

        # 3. 並行取得所有會員詳情
        logger.info("=== 階段二：取得會員詳情 ===")
        detail_sem = asyncio.Semaphore(concurrency)
        detail_tasks = [fetch_member_detail(session, m, settings, detail_sem) for m in new_members]

        details_done = 0
        details_total = len(detail_tasks)

        for coro in asyncio.as_completed(detail_tasks):
            await coro
            details_done += 1
            if details_done % 200 == 0 or details_done == details_total:
                logger.info("會員詳情進度: %d/%d (%d%%)",
                            details_done, details_total,
                            int(details_done / details_total * 100))

        # 中途存檔
        save_members_csv(all_members, "bni_members_progress_async.csv")

    elapsed = time.time() - start_time
    logger.info("全部完成! 耗時 %.1f 秒 (%.1f 分鐘)，共 %d 位會員", elapsed, elapsed / 60, len(all_members))

    # 最終存檔
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_members_csv(all_members, f"bni_members_{timestamp}.csv")
    save_members_csv(all_members, "bni_members_progress_async.csv")

    # 統計
    print(f"\n{'='*60}")
    print(f"完成! 共 {len(all_members)} 位會員")
    print(f"耗時: {elapsed:.1f} 秒 ({elapsed/60:.1f} 分鐘)")
    print(f"並行數: {concurrency}")
    print(f"速度: {len(all_members)/elapsed:.1f} 會員/秒")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
