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

# Selenium / webdriver_manager are imported lazily inside the functions that
# actually need them. This keeps the async (pure-aiohttp) execution path and
# the Cloud Run container free of a hard dependency on Chrome.

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
    "我最喜歡的BNI故事": "favorite_story",
    "Links": "__links__",  # sentinel so we know to stop
}

# Contact-detail labels (English + 繁中). Fax / freephone are ignored.
PHONE_LABELS: Dict[str, set] = {
    "phone": {"Phone", "電話", "Direct", "董事"},
    "mobile": {"Mobile", "行動電話", "手機"},
}

# Salutation at the start of the name line (old English / current Chinese layout)
SALUTATION_RE = re.compile(r"^(Dr\.|Mr\.|Ms\.|Mrs\.|Prof\.|博士|先生|小姐|女士|教授|醫師|律師)\s*")

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
    """Lazy import — only when the Selenium scrapers actually need a driver."""
    from webdriver_manager.chrome import ChromeDriverManager  # noqa: WPS433

    global _chromedriver_path
    if _chromedriver_path is None:
        _chromedriver_path = ChromeDriverManager().install()
    return _chromedriver_path


def create_driver(headless: bool = True):
    """
    Build and return a configured Chrome WebDriver instance.

    Selenium is imported lazily here so the async (pure-aiohttp) scraper and
    the Cloud Run container don't need Chrome at all to start up.

    Parameters
    ----------
    headless : bool
        Run Chrome in headless mode (default True).

    Returns
    -------
    selenium.webdriver.Chrome
        A ready-to-use driver with page_load_strategy='eager' and a 30-second
        page-load timeout.
    """
    from selenium import webdriver  # noqa: WPS433
    from selenium.webdriver.chrome.options import Options  # noqa: WPS433
    from selenium.webdriver.chrome.service import Service  # noqa: WPS433

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

    def get_widget_settings(self, driver: Optional[Any] = None) -> str:
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


def make_soup(html_content: str) -> BeautifulSoup:
    """BeautifulSoup with lxml when available (~5x faster), html.parser otherwise."""
    try:
        return BeautifulSoup(html_content, "lxml")
    except Exception:  # lxml not installed
        return BeautifulSoup(html_content, "html.parser")


_POSTAL_ONLY = re.compile(r"^\d{3,6}$")
_TRAILING_POSTAL = re.compile(r"^(.*\S)\s+(\d{3,6})$")


def _lines(tag) -> List[str]:
    """Visible text of *tag* split on <br>/newlines, blank lines removed."""
    return [ln.strip() for ln in tag.get_text("\n").split("\n") if ln.strip()]


def parse_member_detail_html(html_content: str) -> Dict[str, Any]:
    """
    Parse the HTML returned by the ``memberdetail/display`` AJAX endpoint.

    Works off the page's HTML structure (CSS classes) rather than the position
    of text lines, so it handles both the old English labels and the current
    localized layout (中文稱謂在姓名前、電話有標籤、區塊標題為中文).

    Returns only the keys that were found: ``phone``, ``mobile``, ``title``,
    ``address``, ``postal_code``, ``website``, ``industry``, ``business_desc``,
    ``ideal_referral``, ``best_product``, ``top_problem_solved``,
    ``ideal_referral_partner``, ``favorite_story``, ``line_url``.
    """
    result: Dict[str, Any] = {}
    if not html_content or len(html_content) < 50:
        return result

    soup = make_soup(html_content)
    for junk in soup(["script", "style"]):
        junk.decompose()

    # ── Top card: name / company / industry ─────────────────────────
    info = soup.select_one(".memberProfileInfo")
    name = company = ""
    if info:
        h2 = info.find("h2")
        name = h2.get_text(strip=True) if h2 else ""
        p = info.find("p")
        company = p.get_text(strip=True) if p else ""
        h6 = info.find("h6")
        if h6 and h6.get_text(strip=True):
            result["industry"] = h6.get_text(strip=True)

    # ── Phones: labelled <li><strong>電話</strong><a href=tel:..> ─────
    phones: List[str] = []
    for li in soup.select(".memberContactDetails li"):
        a = li.find("a", href=re.compile(r"^tel:"))
        if not a or not a.get_text(strip=True):
            continue
        number = a.get_text(strip=True)
        strong = li.find("strong")
        label = strong.get_text(strip=True) if strong else ""
        if label in PHONE_LABELS["mobile"]:
            result.setdefault("mobile", number)
        elif label in PHONE_LABELS["phone"]:
            result.setdefault("phone", number)
        elif not label:
            phones.append(number)  # unlabelled (older layout) → positional
    if not phones and "phone" not in result and "mobile" not in result:
        # Last resort: any tel: link on the page
        phones = [a.get_text(strip=True) for a in soup.find_all("a", href=re.compile(r"^tel:"))
                  if a.get_text(strip=True)]
    phones = [p for p in dict.fromkeys(phones)
              if p not in (result.get("phone"), result.get("mobile"))]
    if phones and "phone" not in result:
        result["phone"] = phones.pop(0)
    if phones and "mobile" not in result:
        result["mobile"] = phones.pop(0)

    # ── Company card: 稱謂+姓名 / 公司 / 地址… / 郵遞區號, website ─────
    holder = soup.select_one(".widgetMemberCompanyDetail .textHolder")
    if holder:
        h6 = holder.find("h6")
        lines = _lines(h6) if h6 else []
        if lines:
            first = lines[0]
            if name and name in first:
                title = first.replace(name, "").strip()
                if title:
                    result["title"] = title
                lines = lines[1:]
            elif SALUTATION_RE.match(first):
                result["title"] = SALUTATION_RE.match(first).group(0).strip()
                lines = lines[1:]
        if lines and company and lines[0] == company:
            lines = lines[1:]

        addr_parts: List[str] = []
        for ln in lines:
            if re.fullmatch(r"\d{1,2}", ln):
                continue  # 佔位用的 "0" 之類
            if _POSTAL_ONLY.match(ln):
                result.setdefault("postal_code", ln)
                continue
            m = _TRAILING_POSTAL.match(ln)
            if m:
                addr_parts.append(m.group(1))
                result.setdefault("postal_code", m.group(2))
                continue
            addr_parts.append(ln)
        if addr_parts:
            result["address"] = " ".join(addr_parts)

        site = holder.select_one("p a[href]")
        if site:
            result["website"] = site["href"].strip() or site.get_text(strip=True)
    if "website" not in result and info:
        a = info.select_one("p a[href^=http]")
        if a:
            result["website"] = a["href"]

    # ── Text sections (h2/h3 heading → following text) ──────────────
    sections: Dict[str, List[str]] = {}
    for sec in soup.select("section.widgetMemberTxtVideo, section.widgetProfile"):
        current: Optional[str] = None
        for node in sec.find_all(string=True):
            text = node.strip()
            if not text:
                continue
            if node.parent.name in ("h2", "h3"):
                current = SECTION_MARKERS.get(text)
                if current is None:
                    logger.debug("未知的詳情區塊標題: %s", text)
                continue
            if current and current != "__links__":
                sections.setdefault(current, []).append(text)
    for field, content in sections.items():
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
