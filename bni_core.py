"""
bni_core.py — 共用核心模組
提供所有 BNI 爬蟲共享的工具函式與類別：
- Chrome driver 工廠
- AJAX 客戶端層
- 會員詳情 HTML 解析器
- CSV 讀寫工具 (含公式注入防護)
- 共用常數
"""

import csv
import html as html_mod
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

# ─────────────────────────── Constants ───────────────────────────

BASE_URL = "https://bni.com.tw"
REGION_API = f"{BASE_URL}/web/open/appsCmsNationalChapterSearchFilterJson"
MEMBER_LIST_URL = f"{BASE_URL}/zh-TW/memberlist"
FIND_A_MEMBER_URL = f"{BASE_URL}/zh-TW/findamember"
MEMBER_DETAIL_URL = f"{BASE_URL}/zh-TW/memberdetails"
MEMBER_DETAIL_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberdetail/display"
MEMBERLIST_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberlist/display"

PAGE_LOAD_TIMEOUT = 30

# Member-list table column indices (0-based)
COL_MEMBER_NAME = 0
COL_CHAPTER_REGION = 1
COL_CITY = 2
COL_INDUSTRY_SHORT = 3   # when only 5 columns
COL_COMPANY_SHORT = 4    # when only 5 columns
COL_INDUSTRY_LONG = 4    # when 6+ columns
COL_COMPANY_LONG = 5     # when 6+ columns

# Section markers in detail HTML (both Chinese and English)
SECTION_MARKERS: Dict[str, str] = {
    "My Business": "business_desc",
    "我的業務": "business_desc",
    "Ideal Referral": "ideal_referral",
    "理想的引薦": "ideal_referral",
    "Top Problem Solved": "top_problem_solved",
    "解決過的最大難題": "top_problem_solved",
    "My Ideal Referral Partner": "ideal_referral_partner",
    "我理想中的引薦搭檔": "ideal_referral_partner",
    "Top Product": "best_product",
    "最佳產品": "best_product",
    "My Favorite BNI Story": "favorite_story",
    "Links": "__links__",  # sentinel so we know to stop
}

AJAX_LANGUAGES = json.dumps({
    "availableLanguages": [{
        "type": "published",
        "url": "http://bni.com.tw/zh-TW/memberdetails",
        "descriptionKey": "繁體中文",
        "id": 8,
        "localeCode": "zh_TW",
    }],
    "activeLanguage": {
        "id": 8,
        "localeCode": "zh_TW",
        "descriptionKey": "繁體中文",
        "cookieBotCode": "zh",
    },
})
AJAX_WEBSITE_TYPE = "1"
AJAX_WEBSITE_ID = "10065"

# CSV column order — canonical schema for all three scrapers
MEMBER_FIELDS: List[str] = [
    "country_id", "country_name",
    "region_id", "region_name", "chapter_id", "chapter_name",
    "member_name", "title", "company", "industry", "city",
    "phone", "mobile", "address", "postal_code", "website",
    "business_desc", "ideal_referral", "best_product",
    "top_problem_solved", "ideal_referral_partner", "favorite_story",
    "line_url", "detail_url",
]

# Characters that begin a spreadsheet formula — prefix with single-quote to
# neutralise formula injection when writing to CSV.
_FORMULA_PREFIXES = ("=", "+", "-", "@")


# ─────────────────────────── Chrome Driver Factory ───────────────────────────

# Cache the chromedriver path so each worker process only downloads it once.
_chromedriver_path: Optional[str] = None


def _get_chromedriver_path() -> str:
    global _chromedriver_path
    if _chromedriver_path is None:
        _chromedriver_path = ChromeDriverManager().install()
    return _chromedriver_path


def create_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Build and return a configured Chrome WebDriver instance.

    Parameters
    ----------
    headless : bool
        Run Chrome in headless mode (default True).

    Returns
    -------
    webdriver.Chrome
        A ready-to-use driver with page_load_strategy='eager' and a 30-second
        page-load timeout.
    """
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.page_load_strategy = "eager"

    service = Service(_get_chromedriver_path())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    logger.info("Chrome 瀏覽器已啟動 (headless=%s)", headless)
    return driver


# ─────────────────────────── AJAX Client Layer ───────────────────────────────


class AjaxClient:
    """
    Thin wrapper around a requests.Session that handles BNI AJAX endpoints.

    The instance lazily fetches ``mappedWidgetSettings`` on first use and
    reuses the same session for all subsequent requests.
    """

    _USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self.session: requests.Session = requests.Session()
        self.session.headers.update({
            "User-Agent": self._USER_AGENT,
            "Referer": f"{BASE_URL}/zh-TW/memberdetails",
            "Origin": BASE_URL,
        })
        self._mapped_widget_settings: Optional[str] = None

    # ------------------------------------------------------------------
    # Widget settings
    # ------------------------------------------------------------------

    def get_widget_settings(self, driver: Optional[webdriver.Chrome] = None) -> str:
        """
        Return the ``mappedWidgetSettings`` value required by AJAX endpoints.

        Fetches the member-detail page with the internal requests.Session and
        extracts the value from the HTML.  If ``driver`` is supplied the
        method falls back to the already-loaded page source from Selenium.

        The result is cached so subsequent calls are free.
        """
        if self._mapped_widget_settings is not None:
            return self._mapped_widget_settings

        logger.info("正在取得 AJAX 所需的 mappedWidgetSettings...")
        try:
            resp = self.session.get(MEMBER_DETAIL_URL, timeout=15)
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            logger.warning("取得 mappedWidgetSettings 失敗 (HTTP): %s", exc)
            if driver is not None:
                text = driver.page_source
            else:
                self._mapped_widget_settings = ""
                return ""

        value = _extract_mapped_widget_settings(text)
        if value:
            logger.info("成功取得 mappedWidgetSettings (%d 字元)", len(value))
        else:
            logger.warning("無法從頁面取得 mappedWidgetSettings，使用空值")
        self._mapped_widget_settings = value
        return value

    # ------------------------------------------------------------------
    # Country / region fetching
    # ------------------------------------------------------------------

    def fetch_regions(self, country_id: int, locale: str = "zh_TW") -> List[Dict[str, Any]]:
        """
        POST to the region API and return the list of region dicts.

        Parameters
        ----------
        country_id : int
            BNI numeric country ID.
        locale : str
            Locale string passed as ``request_locale`` (default ``"zh_TW"``).

        Returns
        -------
        list[dict]
            Raw region dicts from the API, each containing at least ``id`` and
            ``name`` keys.  Returns an empty list on failure.
        """
        try:
            resp = self.session.post(
                REGION_API,
                data={"countryId": country_id, "request_locale": locale},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("regions", [])
        except Exception as exc:
            logger.warning("fetch_regions(%s) 失敗: %s", country_id, exc)
            return []


# ─────────────────────────── Widget-Settings Helpers ─────────────────────────


def _extract_mapped_widget_settings(html_text: str) -> str:
    """Extract mappedWidgetSettings value from HTML using multiple patterns."""
    # Pattern 1: <input name="mappedWidgetSettings" value="...">
    m = re.search(r'name="mappedWidgetSettings"\s+value="([^"]*)"', html_text)
    if m:
        return html_mod.unescape(m.group(1))

    # Pattern 2: var mappedWidgetSettings = '[...]';
    m = re.search(r"var mappedWidgetSettings = '(\[.*?\])';", html_text)
    if m:
        return m.group(1)

    # Pattern 3: generic assignment
    m = re.search(r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", html_text)
    if m:
        return html_mod.unescape(m.group(1))

    return ""


# ─────────────────────────── Member-Detail Parser ────────────────────────────


def parse_member_detail_html(html_content: str) -> Dict[str, Any]:
    """
    Parse the HTML returned by the ``memberdetail/display`` AJAX endpoint.

    Returns a dict with the fields that can be extracted from the HTML:
    ``phone``, ``mobile``, ``title``, ``address``, ``postal_code``,
    ``website``, ``industry``, ``business_desc``, ``ideal_referral``,
    ``best_product``, ``top_problem_solved``, ``ideal_referral_partner``,
    ``favorite_story``, ``line_url``.

    Only keys whose values were actually found are included; callers should
    merge the result with the base member dict (``member.update(parsed)``).
    """
    result: Dict[str, Any] = {}

    if not html_content or len(html_content) < 50:
        return result

    soup = BeautifulSoup(html_content, "html.parser")

    # ── Phone / mobile ──────────────────────────────────────────────
    tel_links = soup.find_all("a", href=re.compile(r"^tel:"))
    phones = list(dict.fromkeys(
        a.get_text(strip=True) for a in tel_links if a.get_text(strip=True)
    ))
    if phones:
        result["phone"] = phones[0]
    if len(phones) > 1:
        result["mobile"] = phones[1]

    # ── Section split ────────────────────────────────────────────────
    body_text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in body_text.split("\n") if ln.strip()]

    sections: Dict[str, List[str]] = {}
    current_section = "header"
    section_lines: List[str] = []

    for line in lines:
        if line in SECTION_MARKERS:
            if section_lines:
                sections[current_section] = section_lines
            current_section = line
            section_lines = []
        else:
            section_lines.append(line)
    if section_lines:
        sections[current_section] = section_lines

    # ── Header: title / address / postal_code / website / industry ──
    header = sections.get("header", [])

    # Try to locate the "Chapter" anchor
    chapter_idx = -1
    for i, line in enumerate(header):
        if line == "Chapter":
            chapter_idx = i
            break

    if chapter_idx >= 0:
        # Industry is at index 2 (before "Chapter")
        if chapter_idx >= 3:
            result["industry"] = header[2]

        after_chapter = header[chapter_idx + 1:]
        if after_chapter:
            after_chapter = after_chapter[1:]  # skip chapter name

        # Find salutation line
        title_idx = -1
        for i, line in enumerate(after_chapter):
            if re.match(r"^(Dr\.|Mr\.|Ms\.|Mrs\.|博士|先生|小姐|女士)", line):
                title_idx = i
                result["title"] = line
                break

        if title_idx >= 0:
            info = after_chapter[title_idx + 1:]
            addr_lines = info[1:] if len(info) > 1 else []
            addr_parts: List[str] = []
            for line in addr_lines:
                if re.match(r"^https?://|^www\.", line):
                    result["website"] = line
                    break
                if re.match(r"^\d{3,5}$", line):
                    result["postal_code"] = line
                    continue
                if line and not line.startswith(("BNI", "Links")):
                    addr_parts.append(line)
            if addr_parts:
                result["address"] = " ".join(addr_parts)
    else:
        # Fallback for pages without "Chapter" anchor (older layout)
        info_start = 0
        for i, line in enumerate(header):
            if line.startswith(("Mr.", "Ms.", "Mrs.", "先生", "小姐", "女士")):
                info_start = i + 1
                break

        if info_start > 0 and info_start < len(header):
            remaining = header[info_start:]
            addr_parts = []
            for line in remaining[1:]:  # skip duplicate company name
                if re.match(r"^https?://|^www\.", line):
                    result["website"] = line
                    break
                if re.match(r"^\d{3,5}$", line):
                    continue
                if line and not line.startswith(("BNI", "Links")):
                    addr_parts.append(line)
            if addr_parts:
                result["address"] = " ".join(addr_parts)

    # ── Sections ────────────────────────────────────────────────────
    for marker, field in SECTION_MARKERS.items():
        if field == "__links__":
            continue
        content = sections.get(marker, [])
        if content and field not in result:
            result[field] = "\n".join(content)

    # ── LINE URL ────────────────────────────────────────────────────
    line_urls = re.findall(r'https?://line\.me/[^\s"\'<>]+', html_content)
    if line_urls:
        result["line_url"] = line_urls[0]

    return result


# ─────────────────────────── CSV Helpers ─────────────────────────────────────


def _sanitize_cell(value: Any) -> str:
    """
    Convert a value to string and prepend a single-quote to strings that
    start with a spreadsheet formula prefix (=, +, -, @) to prevent
    formula injection when the CSV is opened in Excel / Google Sheets.
    """
    s = str(value) if value is not None else ""
    if s and s[0] in _FORMULA_PREFIXES:
        s = "'" + s
    return s


def write_members_csv(path: Union[str, Path], members: List[Dict[str, Any]]) -> None:
    """
    Write *members* to a CSV file at *path* using the canonical MEMBER_FIELDS
    column order.

    - Creates parent directories if they do not exist.
    - Sanitizes every cell value to prevent formula injection.
    - Writes with UTF-8 BOM (utf-8-sig) so Excel opens it correctly.
    - Ignores extra keys in member dicts (``extrasaction='ignore'``).
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=MEMBER_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for member in members:
            row = {k: _sanitize_cell(member.get(k, "")) for k in MEMBER_FIELDS}
            writer.writerow(row)

    logger.info("已儲存 %d 筆會員資料至 %s", len(members), out)


def append_members_csv(path: Union[str, Path], members: List[Dict[str, Any]]) -> None:
    """
    Append *members* to an existing CSV file at *path*.

    If the file does not yet exist (or is empty) a header row is written first.
    This is the preferred save method during an in-progress scrape run so that
    the entire list does not need to be rewritten on every checkpoint.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    write_header = not out.exists() or out.stat().st_size == 0

    with open(out, "a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=MEMBER_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for member in members:
            row = {k: _sanitize_cell(member.get(k, "")) for k in MEMBER_FIELDS}
            writer.writerow(row)

    logger.debug("已附加 %d 筆至 %s", len(members), out)


def load_members_csv(path: Union[str, Path]) -> List[Dict[str, str]]:
    """
    Load members from a previously written CSV file.

    Returns a list of dicts keyed by the CSV header row.  Returns an empty
    list if the file does not exist.
    """
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader)
