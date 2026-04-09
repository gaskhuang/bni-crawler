"""
BNI 台灣會員資料爬蟲 - 多線程並行版本
使用多個 Selenium session 同時爬取不同分會的會員列表
會員詳情使用 requests AJAX (不需 Selenium)

用法: python bni_scraper_parallel.py [workers數量]
"""

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import requests
import yaml
from typing import Optional
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bni_core import (
    AJAX_LANGUAGES,
    AJAX_WEBSITE_ID,
    AJAX_WEBSITE_TYPE,
    BASE_URL,
    COL_CHAPTER_REGION,
    COL_CITY,
    COL_COMPANY_LONG,
    COL_COMPANY_SHORT,
    COL_INDUSTRY_LONG,
    COL_INDUSTRY_SHORT,
    COL_MEMBER_NAME,
    MEMBER_DETAIL_AJAX,
    MEMBER_DETAIL_URL,
    MEMBER_LIST_URL,
    append_members_csv,
    create_driver,
    load_members_csv,
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

MAX_RETRIES: int = int(_SCRAPE_CFG.get("retry_attempts", 3))
REQUEST_DELAY: float = float(_SCRAPE_CFG.get("delay", 2.0))
OUTPUT_DIR: str = _OUTPUT_CFG.get("dir", "output")
OUTPUT_FILENAME: str = _OUTPUT_CFG.get("filename", "bni_members.csv")

COUNTRY_ID = 7699
DETAIL_DELAY = 0.2
WAIT_FOR_TABLE_TIMEOUT = 20

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[
        logging.FileHandler("bni_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────── Shared state (no module-level mutables) ─────────


@dataclass
class ScraperState:
    """
    Encapsulates all mutable shared state for a parallel scrape run.

    Passing this into worker functions (rather than using module-level globals)
    makes the data flow explicit and removes the risk of accidental cross-run
    contamination.
    """
    all_members: list = field(default_factory=list)
    processed_chapters: set = field(default_factory=set)
    failed_chapters: list = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)
    chapters_done: int = 0
    chapters_total: int = 0
    members_count: int = 0


# ─────────────────────────── AJAX session helper ─────────────────────────────


def create_ajax_session():
    """建立 requests Session 並取得 mappedWidgetSettings"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": f"{BASE_URL}/zh-TW/memberdetails",
        "Origin": BASE_URL,
    })

    mapped_widget_settings = ""
    try:
        resp = session.get(MEMBER_DETAIL_URL, timeout=15)
        resp.raise_for_status()
        mapped_widget_settings = _extract_mapped_widget_settings(resp.text)
    except Exception as e:
        logger.warning("取得 mappedWidgetSettings 失敗: %s", e)

    return session, mapped_widget_settings


# ─────────────────────────── Detail scraper (reuses session) ─────────────────


def scrape_member_detail_ajax(
    session: requests.Session,
    mapped_widget_settings: str,
    member: dict,
) -> dict:
    """用 requests AJAX 取得會員詳情 (reuses caller's session)"""
    detail_url = member.get("detail_url", "")
    if not detail_url:
        return member

    parsed = urlparse(detail_url)
    parameters = parsed.query
    if not parameters:
        return member

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(
                MEMBER_DETAIL_AJAX,
                data={
                    "parameters": parameters,
                    "languages": AJAX_LANGUAGES,
                    "cmsv3": "true",
                    "mappedWidgetSettings": mapped_widget_settings,
                    "pageMode": "Live_Site",
                    "website_type": AJAX_WEBSITE_TYPE,
                    "website_id": AJAX_WEBSITE_ID,
                },
                timeout=15,
            )
            resp.raise_for_status()
            html_content = resp.text

            if not html_content or len(html_content) < 50:
                if attempt < MAX_RETRIES:
                    time.sleep(0.5)  # retry back-off
                    continue
                return member

            parsed_detail = parse_member_detail_html(html_content)
            member.update(parsed_detail)
            return member

        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(0.5)  # retry back-off

    return member


# ─────────────────────────── Chapter scraper ─────────────────────────────────


def scrape_chapter(
    chapter: dict,
    driver=None,
    session: Optional[requests.Session] = None,
    mapped_widget_settings: str = "",
) -> list:
    """
    爬取一個分會的所有會員 (列表 + 詳情).

    When *driver* is supplied it is **reused** — the caller owns its lifecycle.
    When *driver* is None a new driver is created and destroyed here.

    Similarly, when *session* / *mapped_widget_settings* are supplied they are
    reused across chapters; otherwise a fresh session is created per chapter.
    """
    region_id = chapter["region_id"]
    chapter_id = chapter["chapter_id"]
    chapter_name = chapter["chapter_name"]
    region_name = chapter["region_name"]

    own_driver = driver is None
    if own_driver:
        driver = create_driver(headless=True)

    own_session = session is None
    if own_session:
        session, mapped_widget_settings = create_ajax_session()

    try:
        url = (
            f"{MEMBER_LIST_URL}?countryIds={COUNTRY_ID}"
            f"&regionId={region_id}&chapterName={chapter_id}"
            f"&chapterArea=&memberKeywords=&chapterCity="
            f"&memberFirstName=&memberLastName=&memberCompany="
        )

        members = []
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                driver.get(url)
                WebDriverWait(driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "memberList"))
                )
                WebDriverWait(driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    lambda d: (
                        d.find_elements(By.CSS_SELECTOR, "#memberList table tbody tr td")
                        or "顯示 0" in d.page_source
                    )
                )
                # Brief pause for DataTables to finish rendering
                WebDriverWait(driver, 5).until(
                    lambda d: not d.find_elements(
                        By.CSS_SELECTOR, ".dataTables_processing"
                    )
                )
                driver.execute_script(
                    'var el = document.querySelector(".cc-window"); if(el) el.remove();'
                )

                rows = driver.find_elements(
                    By.CSS_SELECTOR, "#memberList table tbody tr"
                )

                for row in rows:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 3:
                        continue
                    texts = [c.text.strip() for c in cells]
                    chapter_region_col = (
                        texts[COL_CHAPTER_REGION]
                        if len(texts) > COL_CHAPTER_REGION
                        else ""
                    )

                    if chapter_name not in chapter_region_col:
                        continue

                    member_name = texts[COL_MEMBER_NAME]
                    if not member_name:
                        continue

                    detail_url = ""
                    links = cells[COL_MEMBER_NAME].find_elements(By.TAG_NAME, "a")
                    if links:
                        detail_url = links[0].get_attribute("href") or ""

                    city = texts[COL_CITY] if len(texts) > COL_CITY else ""
                    if len(texts) >= 6:
                        industry = texts[COL_INDUSTRY_LONG]
                        company = texts[COL_COMPANY_LONG]
                    elif len(texts) >= 5:
                        industry = texts[COL_INDUSTRY_SHORT]
                        company = texts[COL_COMPANY_SHORT]
                    else:
                        industry = texts[3] if len(texts) > 3 else ""
                        company = ""

                    members.append({
                        "region_id": region_id,
                        "region_name": region_name,
                        "chapter_id": chapter_id,
                        "chapter_name": chapter_name,
                        "member_name": member_name,
                        "company": company,
                        "industry": industry,
                        "city": city,
                        "detail_url": detail_url,
                    })

                break  # success

            except Exception as e:
                logger.warning(
                    "分會 %s 會員列表失敗 (第 %d 次): %s", chapter_name, attempt, e
                )
                if "invalid session" in str(e).lower():
                    if own_driver:
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        driver = create_driver(headless=True)
                    # If the driver was injected we cannot recreate it — log
                    # and break to avoid an infinite loop.
                    else:
                        break
                if attempt < MAX_RETRIES:
                    time.sleep(REQUEST_DELAY)

        if not members:
            logger.warning("分會 %s: 無會員資料", chapter_name)
            return []

        # Member details — reuse the session across all members in this chapter
        for member in members:
            scrape_member_detail_ajax(session, mapped_widget_settings, member)
            time.sleep(DETAIL_DELAY)

        logger.info("分會 %s: 完成 %d 位會員", chapter_name, len(members))
        return members

    finally:
        if own_driver:
            try:
                driver.quit()
            except Exception:
                pass


# ─────────────────────────── Worker function ─────────────────────────────────


def process_chapter_worker(
    chapter: dict,
    state: ScraperState,
    progress_path: Path,
    driver=None,
    session: Optional[requests.Session] = None,
    mapped_widget_settings: str = "",
) -> list:
    """
    Thread worker: scrape one chapter and update shared *state*.

    *driver*, *session*, and *mapped_widget_settings* are passed in from the
    worker's per-thread initialisation so they are reused across chapters
    rather than created/destroyed per chapter.
    """
    try:
        members = scrape_chapter(
            chapter,
            driver=driver,
            session=session,
            mapped_widget_settings=mapped_widget_settings,
        )
        with state.lock:
            state.all_members.extend(members)
            state.chapters_done += 1
            state.members_count += len(members)
            done = state.chapters_done
            total = state.chapters_total
            logger.info(
                "總進度: %d/%d (%d%%) - 累計 %d 位會員",
                done, total, int(done / total * 100) if total else 0,
                state.members_count,
            )
            # Append-only checkpoint every 5 chapters
            if done % 5 == 0 and members:
                append_members_csv(progress_path, members)
        return members
    except Exception as e:
        logger.error("分會 %s 處理失敗: %s", chapter.get("chapter_name", "?"), e)
        with state.lock:
            state.failed_chapters.append(chapter)
        return []


# ─────────────────────────── Chapter CSV helpers ─────────────────────────────


def load_chapters_csv(filename: str = "bni_chapters.csv") -> list:
    """載入分會列表"""
    import csv
    with open(filename, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_completed_chapters(filename: str = "bni_members_progress.csv"):
    """載入已完成的分會和會員"""
    members = load_members_csv(filename)
    if not members:
        return set(), []
    completed = {row.get("chapter_id", "") for row in members}
    return completed, members


# ─────────────────────────── Main ────────────────────────────────────────────


def main() -> None:
    num_workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    logger.info("啟動並行爬蟲，%d 個 workers", num_workers)

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "bni_members_progress.csv"

    # Load chapter list
    chapters = load_chapters_csv()
    logger.info("載入 %d 個分會", len(chapters))

    # Resume: load already-completed chapters
    completed, existing_members = load_completed_chapters(str(progress_path))

    # Build shared state
    state = ScraperState()
    if completed:
        logger.info(
            "跳過 %d 個已完成分會 (%d 筆會員)",
            len(completed), len(existing_members),
        )
        state.all_members.extend(existing_members)
        state.members_count = len(existing_members)

    pending = [ch for ch in chapters if ch["chapter_id"] not in completed]
    state.chapters_total = len(pending)
    logger.info("待處理: %d 個分會", len(pending))

    if not pending:
        logger.info("所有分會已完成!")
        return

    start_time = time.time()

    # Each thread owns one driver and one requests.Session that it reuses
    # across all the chapters it handles — no per-chapter create/destroy.
    def _thread_init(exec_info):
        """Called in each worker thread to set up per-thread resources."""
        pass  # resources are created inside the worker via thread-local storage

    # We use a simple approach: submit tasks and each worker creates its own
    # driver + session once, passed via a closure over thread-local state.
    import threading

    _thread_local = threading.local()

    def _get_thread_resources():
        if not hasattr(_thread_local, "driver"):
            _thread_local.driver = create_driver(headless=True)
            _thread_local.session, _thread_local.mws = create_ajax_session()
        return _thread_local.driver, _thread_local.session, _thread_local.mws

    def _worker(chapter):
        drv, sess, mws = _get_thread_resources()
        return process_chapter_worker(
            chapter, state, progress_path,
            driver=drv, session=sess, mapped_widget_settings=mws,
        )

    with ThreadPoolExecutor(
        max_workers=num_workers, thread_name_prefix="Worker"
    ) as executor:
        futures = {executor.submit(_worker, ch): ch for ch in pending}
        for future in as_completed(futures):
            ch = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error("分會 %s 異常: %s", ch.get("chapter_name", "?"), e)

    elapsed = time.time() - start_time
    logger.info(
        "全部完成! 耗時 %.1f 分鐘，共 %d 位會員",
        elapsed / 60, len(state.all_members),
    )

    # Final output files
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    write_members_csv(out_dir / f"bni_members_{timestamp}.csv", state.all_members)
    write_members_csv(progress_path, state.all_members)

    print(f"\n{'='*60}")
    print(f"完成! 共 {len(state.all_members)} 位會員，耗時 {elapsed/60:.1f} 分鐘")
    print(f"Workers: {num_workers}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
