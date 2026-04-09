"""
BNI 全球會員資料爬蟲 - 極致優化版 (Zero Selenium, Pure Async)

完全不使用 Selenium，改用 aiohttp 非同步 HTTP 請求：
- 會員列表: POST /bnicms/v3/frontend/memberlist/display (AJAX)
- 會員詳情: POST /bnicms/v3/frontend/memberdetail/display (AJAX)

支援 44 個國家，啟動時顯示文字選單選擇國家。

用法: python bni_scraper_async.py [並行數，預設5]
"""

import asyncio
import html as html_mod
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import yaml
from bs4 import BeautifulSoup

from bni_core import (
    BASE_URL,
    FIND_A_MEMBER_URL,
    MEMBER_FIELDS,
    MEMBER_LIST_URL,
    MEMBERLIST_AJAX,
    MEMBER_DETAIL_AJAX,
    MEMBER_DETAIL_URL,
    append_members_csv,
    create_driver,
    parse_member_detail_html,
    write_members_csv,
    _extract_mapped_widget_settings,
)

# ── config ────────────────────────────────────────────────────────────────────

def _load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}

_CFG = _load_config()
_SCRAPE_CFG = _CFG.get("scrape", {})
_OUTPUT_CFG = _CFG.get("output", {})

DEFAULT_CONCURRENCY: int = int(_SCRAPE_CFG.get("concurrency", 5))
MAX_RETRIES: int = int(_SCRAPE_CFG.get("retry_attempts", 3))
OUTPUT_DIR: str = _OUTPUT_CFG.get("dir", "output")
OUTPUT_FILENAME: str = _OUTPUT_CFG.get("filename", "bni_members.csv")

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bni_scraper_async.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

COUNTRY_LIST_API = f"{BASE_URL}/web/open/appsCmsCountryListJson"
REGION_API = f"{BASE_URL}/web/open/appsCmsNationalChapterSearchFilterJson"
MEMBERLIST_PAGE = f"{BASE_URL}/zh-TW/memberlist"
MEMBERDETAIL_PAGE = MEMBER_DETAIL_URL

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

# ─────────────────────────── Country menu ────────────────────────────────────


def show_country_menu():
    """顯示國家選單，回傳選擇的國家 ID 列表"""
    sorted_countries = sorted(COUNTRIES.items(), key=lambda x: x[1][0])

    print("\n" + "=" * 60)
    print("  BNI 全球會員爬蟲 - 國家選擇")
    print("=" * 60)

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
            return [7699]

        try:
            nums = [int(x.strip()) for x in choice.split(",")]
        except ValueError:
            print("  請輸入數字編號")
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
                print(f"  無效編號: {n}")
                valid = False
                break

        if valid and selected:
            names = [COUNTRIES[cid][0] for cid in selected]
            print(f"\n  已選擇: {', '.join(names)}")
            return selected


# ─────────────────────────── Widget settings (per-country) ───────────────────


async def get_widget_settings(session: aiohttp.ClientSession, country_id: int) -> dict:
    """
    Fetch widget settings from the member-list and member-detail pages for the
    given *country_id*.

    Widget settings are country-specific (the memberlist page is parameterised
    with ``countryIds``), so this must be called once per country rather than
    once globally.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                MEMBERLIST_PAGE,
                params={"countryIds": country_id},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status not in (200, 304):
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status,
                        message=f"Unexpected status {resp.status} fetching memberlist page",
                    )
                text = await resp.text()

            mws = ""
            m = re.search(r"var mappedWidgetSettings = '(\[.*?\])';", text)
            if m:
                mws = m.group(1)

            langs = ""
            m2 = re.search(r"var languages = (\{.*?\}\});", text, re.DOTALL)
            if m2:
                langs = m2.group(1)

            async with session.get(
                MEMBERDETAIL_PAGE,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp2:
                if resp2.status not in (200, 304):
                    raise aiohttp.ClientResponseError(
                        resp2.request_info, resp2.history,
                        status=resp2.status,
                        message=f"Unexpected status {resp2.status} fetching memberdetail page",
                    )
                text2 = await resp2.text()

            detail_mws = _extract_mapped_widget_settings(text2)

            detail_langs = ""
            m4 = re.search(r"var languages = (\{.*?\}\});", text2, re.DOTALL)
            if m4:
                detail_langs = m4.group(1)

            if not mws:
                logger.warning(
                    "mappedWidgetSettings not found for country %d (attempt %d)",
                    country_id, attempt,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                    continue

            logger.info(
                "Widget settings loaded for country %d "
                "(mws=%d, langs=%d, detail_mws=%d)",
                country_id, len(mws), len(langs), len(detail_mws),
            )
            return {
                "list_mws": mws,
                "list_langs": langs,
                "detail_mws": detail_mws,
                "detail_langs": detail_langs,
            }
        except aiohttp.ClientResponseError:
            raise
        except Exception as e:
            logger.warning(
                "取得 widget settings 失敗 country=%d attempt=%d: %s",
                country_id, attempt, e,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)

    raise RuntimeError(f"無法取得 widget settings (country={country_id})")


# ─────────────────────────── Region fetching ─────────────────────────────────


async def fetch_regions(session: aiohttp.ClientSession, country_id: int) -> list:
    """取得指定國家的區域列表"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(
                REGION_API,
                data={"countryId": country_id, "request_locale": "en_US"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 304):
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status,
                        message=f"Unexpected status {resp.status} fetching regions",
                    )
                data = await resp.json()
                return data.get("regions", [])
        except aiohttp.ClientResponseError as exc:
            if exc.status in (403, 429, 500):
                raise
            logger.warning("國家 %s 區域取得失敗 attempt=%d: %s", country_id, attempt, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)
        except Exception as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)
            else:
                logger.warning("國家 %s 區域取得失敗: %s", country_id, e)
    return []


# ─────────────────────────── Chapter/member list ─────────────────────────────


def discover_all_chapters_selenium(country_id: int, regions: list) -> list:
    """
    用 Selenium（headless Chrome）一次性爬取所有區域的分會清單。
    Chapter dropdown 是 JS 動態載入，只有真實瀏覽器能看到。

    流程：開啟 findamember → Advanced Search tab → 逐一選 region → 讀 chapter dropdown
    回傳：[{chapter_id, chapter_name, region_id, region_name}, ...]
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC

    logger.info("使用 Selenium 發現分會清單（共 %d 個區域）...", len(regions))
    driver = create_driver(headless=True)
    all_chapters = []

    try:
        # Load findamember page once
        driver.get(FIND_A_MEMBER_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'a[href="#search-advanced-tab"]')
            )
        )
        # Click Advanced Search tab
        driver.execute_script(
            'document.querySelector(\'a[href="#search-advanced-tab"]\').click();'
        )
        WebDriverWait(driver, 15).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, '#search-advanced select[name="regionId"]')
            )
        )
        # Dismiss cookie if present
        try:
            btn = driver.find_element(By.CSS_SELECTOR, 'button[id*="cookie"], .cookie-accept, #onetrust-accept-btn-handler')
            btn.click()
        except Exception:
            pass

        for region in regions:
            region_id = str(region["id"])
            region_name = region.get("name", region_id)
            try:
                # Select region
                region_sel = Select(
                    driver.find_element(By.CSS_SELECTOR, '#search-advanced select[name="regionId"]')
                )
                region_sel.select_by_value(region_id)

                # Wait for chapter dropdown to populate (>1 option means it loaded)
                WebDriverWait(driver, 15).until(
                    lambda d: len(
                        Select(d.find_element(By.CSS_SELECTOR, '#search-advanced select[name="chapterName"]')).options
                    ) > 1
                )

                chapter_sel = Select(
                    driver.find_element(By.CSS_SELECTOR, '#search-advanced select[name="chapterName"]')
                )
                chapters = []
                for opt in chapter_sel.options:
                    val = opt.get_attribute("value") or ""
                    text = opt.text.strip()
                    if val:
                        chapters.append({
                            "chapter_id": val,
                            "chapter_name": text,
                            "region_id": region_id,
                            "region_name": region_name,
                        })

                if chapters:
                    all_chapters.extend(chapters)
                    logger.info("  區域 %s: %d 個分會", region_name, len(chapters))
                else:
                    all_chapters.append({
                        "chapter_id": "",
                        "chapter_name": region_name,
                        "region_id": region_id,
                        "region_name": region_name,
                    })
            except Exception as e:
                logger.warning("區域 %s 分會發現失敗: %s，以區域代替", region_name, e)
                all_chapters.append({
                    "chapter_id": "",
                    "chapter_name": region_name,
                    "region_id": region_id,
                    "region_name": region_name,
                })
    finally:
        driver.quit()

    logger.info("分會發現完成: 共 %d 個分會", len(all_chapters))
    return all_chapters


async def _ajax_memberlist(
    session: aiohttp.ClientSession,
    settings: dict,
    params_str: str,
    label: str,
) -> str:
    """POST to MEMBERLIST_AJAX and return the HTML text. Raises on HTTP errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(
                MEMBERLIST_AJAX,
                data={
                    "parameters": params_str,
                    "languages": settings["list_langs"],
                    "cmsv3": "true",
                    "mappedWidgetSettings": settings["list_mws"],
                    "pageMode": "Live_Site",
                    "website_type": "1",
                    "website_id": "10065",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (403, 429, 500):
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status,
                        message=f"HTTP {resp.status} on memberlist for {label}",
                    )
                return await resp.text()
        except aiohttp.ClientResponseError:
            raise
        except asyncio.TimeoutError:
            logger.warning("%s 超時 (attempt %d)", label, attempt)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1)
        except Exception as e:
            logger.warning("%s 失敗 (attempt %d): %s", label, attempt, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5)
    return ""


def _parse_chapter_options(html_text: str) -> list:
    """
    Extract chapter IDs and names from the chapterName <select> dropdown.
    Returns list of {chapter_id, chapter_name} dicts. Empty if no dropdown found.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    sel = soup.find("select", {"name": "chapterName"})
    if not sel:
        return []
    chapters = []
    for opt in sel.find_all("option"):
        val = opt.get("value", "").strip()
        text = opt.get_text(strip=True)
        if val and val != "0":
            chapters.append({"chapter_id": val, "chapter_name": text})
    return chapters


def _parse_member_rows(
    html_text: str,
    country_id: int,
    region_id: str,
    region_name: str,
    chapter_id: str = "",
    chapter_name_filter: str = "",
) -> list:
    """Parse member rows from memberlist HTML and return member dicts."""
    soup = BeautifulSoup(html_text, "html.parser")
    rows = soup.select("table tbody tr")
    members = []
    country_name = COUNTRIES.get(country_id, ("?", 0))[0]

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        texts = [c.get_text(strip=True) for c in cells]

        member_name = texts[1]
        if not member_name:
            continue

        chapter_region = texts[2]
        parsed_chapter_name = (
            chapter_region.split(" - ")[0].strip()
            if " - " in chapter_region
            else chapter_region
        )

        # Front-end filter when fetching by chapter_id to avoid cross-contamination
        if chapter_name_filter and chapter_name_filter not in chapter_region:
            continue

        link = cells[1].find("a", href=re.compile(r"memberdetails"))
        detail_url = link["href"] if link else ""
        if detail_url and not detail_url.startswith("http"):
            detail_url = f"{BASE_URL}/zh-TW/{detail_url}"

        members.append({
            "country_id": country_id,
            "country_name": country_name,
            "region_id": region_id,
            "region_name": region_name,
            "chapter_id": chapter_id,
            "chapter_name": parsed_chapter_name,
            "member_name": member_name,
            "company": texts[6] if len(texts) > 6 else "",
            "industry": texts[5] if len(texts) > 5 else "",
            "city": texts[3] if len(texts) > 3 else "",
            "detail_url": detail_url,
        })
    return members




# ─────────────────────────── Member detail ───────────────────────────────────


async def fetch_member_detail(
    session: aiohttp.ClientSession,
    member: dict,
    settings: dict,
    sem: asyncio.Semaphore,
) -> dict:
    """用 AJAX 取得單一會員詳情"""
    detail_url = member.get("detail_url", "")
    if not detail_url:
        return member

    if "?" in detail_url:
        parameters = detail_url.split("?", 1)[1]
    else:
        return member

    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(
                    MEMBER_DETAIL_AJAX,
                    data={
                        "parameters": parameters,
                        "languages": settings["detail_langs"],
                        "cmsv3": "true",
                        "mappedWidgetSettings": settings["detail_mws"],
                        "pageMode": "Live_Site",
                        "website_type": "1",
                        "website_id": "10065",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (403, 429, 500):
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status,
                            message=f"HTTP {resp.status} on memberdetail for {member.get('member_name')}",
                        )
                    html_content = await resp.text()

                parsed = parse_member_detail_html(html_content)
                member.update(parsed)
                return member

            except aiohttp.ClientResponseError:
                raise
            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.3)
            except Exception as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.3)
                else:
                    logger.debug(
                        "會員 %s 詳情失敗: %s", member.get("member_name", "?"), e
                    )

    return member


# ─────────────────────────── Country scraper ─────────────────────────────────



async def fetch_members_for_chapter(
    session: aiohttp.ClientSession,
    country_id: int,
    region_id: str,
    region_name: str,
    chapter: dict,
    settings: dict,
    sem: asyncio.Semaphore,
) -> list:
    """按分會 ID 抓取該分會所有會員"""
    ch_id = chapter["chapter_id"]
    ch_name = chapter["chapter_name"]
    label = f"{region_name}/{ch_name}"

    params = (
        f"countryIds={country_id}&regionId={region_id}&chapterName={ch_id}"
        f"&chapterArea=&memberKeywords=&chapterCity="
        f"&memberFirstName=&memberLastName=&memberCompany="
    )

    async with sem:
        html_text = await _ajax_memberlist(session, settings, params, label)

    if len(html_text) < 100:
        return []

    return _parse_member_rows(
        html_text, country_id, region_id, region_name,
        chapter_id=ch_id, chapter_name_filter=ch_name,
    )


async def scrape_country(
    session: aiohttp.ClientSession,
    country_id: int,
    concurrency: int,
) -> list:
    """
    爬取一個國家的所有會員。

    Widget settings are fetched **per country** so that the correct
    ``mappedWidgetSettings`` is used for each country's pages.

    流程：
    1. 取得區域列表
    2. 動態發現每個區域的分會（GET memberlist 頁面解析 dropdown）
    3. 對每個分會呼叫 AJAX 取得會員（避免 API ~200 筆截斷）
    4. 抓每位會員的詳情
    """
    country_name = COUNTRIES.get(country_id, ("Unknown", 0))[0]
    logger.info("=" * 50)
    logger.info("開始爬取: %s (ID: %d)", country_name, country_id)

    # Fetch widget settings specifically for this country
    settings = await get_widget_settings(session, country_id)

    # 1. Regions
    regions = await fetch_regions(session, country_id)
    if not regions:
        logger.warning("%s: 無區域資料", country_name)
        return []

    logger.info("%s: %d 個區域，正在用 Selenium 發現分會...", country_name, len(regions))

    # 2. Discover chapters via Selenium (chapter dropdown is JS-rendered)
    loop = asyncio.get_event_loop()
    all_chapters = await loop.run_in_executor(
        None, discover_all_chapters_selenium, country_id, regions
    )
    total_chapters = len(all_chapters)
    logger.info("%s: 發現 %d 個分會", country_name, total_chapters)

    sem = asyncio.Semaphore(concurrency)

    # 3. Build member-fetch tasks (one per chapter)
    tasks = [
        fetch_members_for_chapter(
            session, country_id,
            ch["region_id"], ch["region_name"],
            ch, settings, sem,
        )
        for ch in all_chapters
    ]
    total_tasks = len(tasks)

    all_members: list = []
    done = 0

    for coro in asyncio.as_completed(tasks):
        members = await coro
        all_members.extend(members)
        done += 1
        if done % 10 == 0 or done == total_tasks:
            logger.info(
                "[%s] 進度: %d/%d (%d%%) - 累計 %d 位",
                country_name, done, total_tasks,
                int(done / total_tasks * 100),
                len(all_members),
            )

    logger.info("[%s] 會員列表完成: %d 位", country_name, len(all_members))

    # 3. Parallel detail fetch, bounded by semaphore
    if all_members:
        detail_sem = asyncio.Semaphore(concurrency)
        detail_tasks = [
            fetch_member_detail(session, m, settings, detail_sem)
            for m in all_members
        ]

        details_done = 0
        details_total = len(detail_tasks)

        for coro in asyncio.as_completed(detail_tasks):
            await coro
            details_done += 1
            if details_done % 500 == 0 or details_done == details_total:
                logger.info(
                    "[%s] 詳情進度: %d/%d (%d%%)",
                    country_name, details_done, details_total,
                    int(details_done / details_total * 100),
                )

    logger.info("[%s] 完成! %d 位會員", country_name, len(all_members))
    return all_members


# ─────────────────────────── Entry point ─────────────────────────────────────


async def main() -> None:
    # 授權驗證（暫時停用，測試用）
    # from license_check import require_license
    # customer = require_license()
    # if customer:
    #     print(f"  歡迎, {customer}!\n")
    pass

    # Concurrency from CLI arg, falling back to config, then hardcoded default
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONCURRENCY

    # 國家選擇選單
    selected_ids = show_country_menu()
    selected_names = [COUNTRIES[cid][0] for cid in selected_ids]
    total_regions = sum(COUNTRIES[cid][1] for cid in selected_ids)

    print(f"\n  準備爬取 {len(selected_ids)} 個國家, {total_regions} 個區域")
    print(f"  並行數: {concurrency}")
    print()

    start_time = time.time()

    connector = aiohttp.TCPConnector(
        limit=concurrency,
        limit_per_host=concurrency,
        ttl_dns_cache=300,
    )
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        all_members: list = []
        out_dir = Path(OUTPUT_DIR)

        for country_id in selected_ids:
            members = await scrape_country(session, country_id, concurrency)
            all_members.extend(members)

            # Per-country output
            if members:
                country_name = COUNTRIES[country_id][0].lower().replace(" ", "_")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                write_members_csv(
                    out_dir / f"bni_members_{country_name}_{timestamp}.csv",
                    members,
                )

    elapsed = time.time() - start_time

    # Combined output when multiple countries were scraped
    if len(selected_ids) > 1 and all_members:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        write_members_csv(
            Path(OUTPUT_DIR) / f"bni_members_all_{timestamp}.csv",
            all_members,
        )

    # Summary
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
