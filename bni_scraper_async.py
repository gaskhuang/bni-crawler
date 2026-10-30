"""
BNI 全球會員資料爬蟲 - 極致優化版 (Zero Selenium, Pure Async)

完全不使用 Selenium，改用 aiohttp 非同步 HTTP 請求：
- 會員列表: POST /bnicms/v3/frontend/memberlist/display (AJAX)
- 會員詳情: POST /bnicms/v3/frontend/memberdetail/display (AJAX)

支援 44 個國家，啟動時顯示文字選單選擇國家。

用法: python bni_scraper_async.py [並行數，預設讀 config.yaml]
"""

import asyncio
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import yaml

from bni_core import (
    BASE_URL,
    MEMBER_FIELDS,
    MEMBERLIST_AJAX,
    MEMBER_DETAIL_AJAX,
    MEMBER_DETAIL_URL,
    make_soup,
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
CHAPTER_API = f"{BASE_URL}/web/open/appsCmsNationalMemberSearchFilterJson"
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

# BNI 後端 (2026 改版後) 只接受 jQuery 風格的巢狀表單欄位：
# languages[activeLanguage][id]=8 ... 傳 JSON 字串會讓 PHP 直接 Fatal error。
PHP_FATAL_MARKER = "<b>Fatal error</b>"


def _flatten_form(obj, prefix: str) -> list:
    """把 dict/list 展開成 jQuery $.param 的巢狀表單欄位 [(key, value), ...]。"""
    if isinstance(obj, dict):
        return [kv for k, v in obj.items() for kv in _flatten_form(v, f"{prefix}[{k}]")]
    if isinstance(obj, list):
        return [kv for i, v in enumerate(obj) for kv in _flatten_form(v, f"{prefix}[{i}]")]
    if obj is None:
        return [(prefix, "")]
    if isinstance(obj, bool):
        return [(prefix, "true" if obj else "false")]
    return [(prefix, str(obj))]


def _languages_form(langs_json: str) -> list:
    """把頁面上的 `var languages = {...}` JSON 轉成巢狀表單欄位。"""
    if not langs_json:
        return []
    try:
        return _flatten_form(json.loads(langs_json), "languages")
    except ValueError:
        logger.warning("languages JSON 解析失敗，改用原字串送出")
        return [("languages", langs_json)]


def _cms_form(parameters: str, langs_form: list, mws: str) -> list:
    """組出 memberlist / memberdetail display endpoint 共用的表單內容。"""
    return [
        ("parameters", parameters),
        *langs_form,
        ("cmsv3", "true"),
        ("mappedWidgetSettings", mws),
        ("pageMode", "Live_Site"),
        ("website_type", "1"),
        ("website_id", "10065"),
    ]


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
                text = await resp.text(errors="replace")

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
                text2 = await resp2.text(errors="replace")

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
                "list_langs_form": _languages_form(langs),
                "detail_langs_form": _languages_form(detail_langs),
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


async def _fetch_chapters_for_region(
    session: aiohttp.ClientSession,
    country_id: int,
    region: dict,
    sem: asyncio.Semaphore,
) -> list:
    """單一區域的分會清單，透過 AJAX endpoint 取得。"""
    region_id = str(region["id"])
    region_name = region.get("name", region_id)

    async with sem:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(
                    CHAPTER_API,
                    data={
                        "countryId": country_id,
                        "regionId": region["id"],
                        "request_locale": "en_US",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    chapters = data.get("chapters", []) or []
                    return [
                        {
                            "chapter_id": str(ch["id"]),
                            "chapter_name": ch.get("name", ""),
                            "region_id": region_id,
                            "region_name": region_name,
                        }
                        for ch in chapters
                    ]
            except Exception as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.5)
                else:
                    logger.warning("區域 %s 分會清單取得失敗: %s", region_name, e)
    return []


async def discover_all_chapters_ajax(
    session: aiohttp.ClientSession,
    country_id: int,
    regions: list,
    concurrency: int = 30,
) -> list:
    """
    用純 AJAX 並行取得所有區域的分會清單。

    呼叫 ``appsCmsNationalMemberSearchFilterJson``（傳 countryId+regionId 直接回 chapters），
    取代舊版需要 Selenium + Chrome 開瀏覽器點 dropdown 的做法。
    Taiwan 31 區實測：1.4 秒（Selenium 版本：~5 分鐘，225 倍加速）。

    回傳：[{chapter_id, chapter_name, region_id, region_name}, ...]
    區域沒有任何分會時，會放一筆空 chapter_id 的占位資料，讓會員列表查詢退回到 region-only 模式。
    """
    logger.info("透過 AJAX 並行發現分會清單（共 %d 個區域）...", len(regions))
    sem = asyncio.Semaphore(concurrency)
    tasks = [_fetch_chapters_for_region(session, country_id, r, sem) for r in regions]
    results = await asyncio.gather(*tasks)

    all_chapters: list = []
    for region, chapters in zip(regions, results):
        if chapters:
            all_chapters.extend(chapters)
        else:
            # 區域沒分會（或抓取失敗）：以區域為單位 fallback，避免漏掉會員
            all_chapters.append({
                "chapter_id": "",
                "chapter_name": region.get("name", str(region["id"])),
                "region_id": str(region["id"]),
                "region_name": region.get("name", str(region["id"])),
            })

    logger.info("分會發現完成: 共 %d 個分會", len(all_chapters))
    return all_chapters


class BniApiError(RuntimeError):
    """BNI 後端回傳無法處理的格式（例如 PHP Fatal error）——通常代表 API 又改版了。"""


# 這些狀態碼通常是暫時性的（限流、伺服器忙），退避後重試即可。
RETRY_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}


async def _post_cms_html(
    session: aiohttp.ClientSession,
    url: str,
    form: list,
    label: str,
    timeout: float,
) -> str:
    """
    POST 到 BNI CMS display endpoint 並回傳 HTML。

    - 暫時性錯誤（逾時、連線失敗、429/5xx）以指數退避重試，不會中斷整個國家。
    - 後端回 PHP Fatal error 時丟 BniApiError，讓呼叫端決定要不要中止。
    - 重試用盡回傳空字串。
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(
                url, data=form, timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text(errors="replace")
                if resp.status in RETRY_STATUSES:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status, message=f"HTTP {resp.status}",
                    )
            if PHP_FATAL_MARKER in text[:1000]:
                raise BniApiError(
                    f"{label}: BNI 後端回傳 PHP Fatal error，API 參數格式可能又改版了。"
                    f" 回應開頭: {text[:300]!r}"
                )
            return text
        except BniApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                logger.warning("%s 失敗（已重試 %d 次）: %r", label, attempt, exc)
                break
            await asyncio.sleep(min(0.5 * 2 ** attempt, 8) + random.random() * 0.5)
    return ""


def _parse_member_rows(
    html_text: str,
    country_id: int,
    region_id: str,
    region_name: str,
    chapter_id: str = "",
    chapter_name_filter: str = "",
) -> list:
    """Parse member rows from memberlist HTML and return member dicts."""
    soup = make_soup(html_text)
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


# ─────────────────────────── Member list / detail ────────────────────────────


async def fetch_members_for_chapter(
    session: aiohttp.ClientSession,
    country_id: int,
    region_id: str,
    region_name: str,
    chapter: dict,
    settings: dict,
    sem: asyncio.Semaphore,
) -> list:
    """按分會 ID 抓取該分會所有會員（列表頁欄位，不含詳情）。"""
    ch_id = chapter["chapter_id"]
    ch_name = chapter["chapter_name"]
    label = f"{region_name}/{ch_name}"

    params = (
        f"countryIds={country_id}&regionId={region_id}&chapterName={ch_id}"
        f"&chapterArea=&memberKeywords=&chapterCity="
        f"&memberFirstName=&memberLastName=&memberCompany="
    )

    async with sem:
        html_text = await _post_cms_html(
            session, MEMBERLIST_AJAX,
            _cms_form(params, settings["list_langs_form"], settings["list_mws"]),
            label, timeout=30,
        )

    if len(html_text) < 100:
        return []

    # HTML 解析很吃 CPU，丟到執行緒，讓 event loop（Web UI 的查進度/下載）保持順暢。
    return await asyncio.to_thread(
        _parse_member_rows,
        html_text, country_id, region_id, region_name,
        chapter_id=ch_id, chapter_name_filter=ch_name,
    )


async def fetch_member_detail(
    session: aiohttp.ClientSession,
    member: dict,
    settings: dict,
    sem: asyncio.Semaphore,
) -> dict:
    """
    用 AJAX 取得單一會員詳情，就地補進 *member*。

    詳情頁只用來「補空欄位」：列表頁的產業分類（例如「零售 > 定制服裝 > …」）
    比詳情頁的「定制服裝 | 定制服裝」完整，不能被覆蓋。
    BNI 回 PHP Fatal error 時會丟 BniApiError。
    """
    detail_url = member.get("detail_url", "")
    if "?" not in detail_url:
        return member
    parameters = detail_url.split("?", 1)[1]

    async with sem:
        html_content = await _post_cms_html(
            session, MEMBER_DETAIL_AJAX,
            _cms_form(parameters, settings["detail_langs_form"], settings["detail_mws"]),
            f"詳情 {member.get('member_name', '?')}", timeout=20,
        )

    parsed = await asyncio.to_thread(parse_member_detail_html, html_content)
    for key, value in parsed.items():
        if value and not member.get(key):
            member[key] = value
    return member


# ─────────────────────────── Country scraper ─────────────────────────────────


def _member_key(m: dict) -> tuple:
    return (m.get("detail_url") or "", m.get("chapter_id"), m.get("member_name"))


async def scrape_country(
    session: aiohttp.ClientSession,
    country_id: int,
    concurrency: int,
    progress=None,
) -> list:
    """
    爬取一個國家的所有會員。

    流程：
    1. 取得該國的 widget settings（每國不同）與區域列表
    2. 並行發現每個區域的分會
    3. 管線化：每個分會的會員列表一回來，就立刻開始抓那批會員的詳情，
       不必等所有分會列表都跑完（列表與詳情共用同一個併發上限）
    4. 依「區域 → 分會 → 姓名」排序後回傳，輸出 CSV 順序穩定

    *progress*：選填的回呼 ``progress(**fields)``，Web UI 用來更新進度；
    欄位名稱與 server.py 的 job dict 相同。
    """
    report = progress or (lambda **_: None)
    country_name = COUNTRIES.get(country_id, ("Unknown", 0))[0]
    logger.info("=" * 50)
    logger.info("開始爬取: %s (ID: %d)", country_name, country_id)
    report(current_country=country_name, stage="widget_settings")

    settings = await get_widget_settings(session, country_id)

    report(stage="regions")
    regions = await fetch_regions(session, country_id)
    if not regions:
        logger.warning("%s: 無區域資料", country_name)
        report(log=f"{country_name}: 無區域資料")
        return []
    report(regions_total=len(regions), stage="chapter_discovery")

    all_chapters = await discover_all_chapters_ajax(
        session, country_id, regions, concurrency=concurrency,
    )
    total_chapters = len(all_chapters)
    logger.info("%s: %d 個區域，%d 個分會", country_name, len(regions), total_chapters)
    report(
        chapters_total=total_chapters, stage="member_list",
        log=f"{country_name}: {len(regions)} 個區域，發現 {total_chapters} 個分會",
    )

    sem = asyncio.Semaphore(concurrency)
    all_members: list = []
    seen: set = set()
    counts = {"chapters": 0, "details": 0, "details_total": 0, "detail_errors": 0}

    async def detail_one(member: dict) -> None:
        try:
            await fetch_member_detail(session, member, settings, sem)
        except BniApiError:
            raise
        except Exception:  # 單一會員的怪資料不該拖垮整個國家
            counts["detail_errors"] += 1
            logger.warning(
                "會員 %s 詳情解析失敗，保留列表欄位", member.get("member_name", "?"),
                exc_info=counts["detail_errors"] <= 3,
            )
        counts["details"] += 1
        report(details_done=counts["details"])
        if counts["details"] % 500 == 0:
            logger.info(
                "[%s] 詳情進度: %d/%d（分會 %d/%d）",
                country_name, counts["details"], counts["details_total"],
                counts["chapters"], total_chapters,
            )

    async def process_chapter(ch: dict) -> None:
        members = await fetch_members_for_chapter(
            session, country_id, ch["region_id"], ch["region_name"], ch, settings, sem,
        )
        fresh = []
        for m in members:
            key = _member_key(m)
            if key not in seen:
                seen.add(key)
                fresh.append(m)
        all_members.extend(fresh)
        counts["chapters"] += 1
        counts["details_total"] += len(fresh)
        report(
            chapters_done=counts["chapters"],
            members_count=len(all_members),
            details_total=counts["details_total"],
        )
        if counts["chapters"] == total_chapters:
            logger.info("[%s] 會員列表完成: %d 位", country_name, len(all_members))
            report(
                stage="member_detail",
                log=f"{country_name}: 會員列表 {len(all_members)} 位",
            )
        await asyncio.gather(*(detail_one(m) for m in fresh))

    await asyncio.gather(*(process_chapter(ch) for ch in all_chapters))

    if not all_members:
        logger.warning(
            "[%s] %d 個分會卻抓到 0 位會員——請確認 BNI 網站是否改版",
            country_name, total_chapters,
        )

    if counts["detail_errors"]:
        logger.warning("[%s] %d 位會員詳情失敗", country_name, counts["detail_errors"])

    all_members.sort(key=lambda m: (
        m.get("region_name", ""), m.get("chapter_name", ""), m.get("member_name", ""),
    ))
    logger.info("[%s] 完成! %d 位會員", country_name, len(all_members))
    report(log=f"{country_name}: 完成 {len(all_members)} 位")
    return all_members


# ─────────────────────────── Entry point ─────────────────────────────────────


async def main() -> None:
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
    failed: list = []

    connector = aiohttp.TCPConnector(
        limit=concurrency,
        limit_per_host=concurrency,
        ttl_dns_cache=300,
    )
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        all_members: list = []
        out_dir = Path(OUTPUT_DIR)

        for country_id in selected_ids:
            try:
                members = await scrape_country(session, country_id, concurrency)
            except BniApiError:
                raise  # 格式改版：後面的國家也一定失敗，直接停
            except Exception as exc:
                logger.exception("%s 爬取失敗，略過", COUNTRIES[country_id][0])
                failed.append(f"{COUNTRIES[country_id][0]} ({type(exc).__name__})")
                continue
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
    if failed:
        print(f"  失敗: {', '.join(failed)}")
    print(f"  耗時: {elapsed:.1f} 秒 ({elapsed/60:.1f} 分鐘)")
    if elapsed > 0:
        print(f"  速度: {len(all_members)/elapsed:.1f} 會員/秒")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
