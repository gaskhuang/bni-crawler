"""
BNI 台灣會員資料爬蟲系統
自動化爬取 BNI 台灣所有區域、分會及會員的完整資料

爬取流程:
1. 取得所有區域 (透過 API)
2. 取得每個區域的分會列表 (透過 findamember 頁面下拉選單)
3. 按分會查詢會員列表 (chapterName 參數讓該分會排第一，再前端過濾)
4. 進入每位會員的詳情頁面，提取完整資料 (電話、地址、業務描述等)
5. 匯出為 CSV
"""

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from typing import Optional
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

import bni_core
from bni_core import (
    AjaxClient,
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
    FIND_A_MEMBER_URL,
    MEMBER_DETAIL_AJAX,
    MEMBER_FIELDS,
    MEMBER_LIST_URL,
    REGION_API,
    append_members_csv,
    create_driver,
    load_members_csv,
    parse_member_detail_html,
    write_members_csv,
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

REQUEST_DELAY: float = float(_SCRAPE_CFG.get("delay", 2.0))
DETAIL_DELAY: float = 0.3           # AJAX 間隔 (秒) — intentionally short, not rate-limited
MAX_RETRIES: int = int(_SCRAPE_CFG.get("retry_attempts", 3))
OUTPUT_DIR: str = _OUTPUT_CFG.get("dir", "output")
OUTPUT_FILENAME: str = _OUTPUT_CFG.get("filename", "bni_members.csv")

COUNTRY_ID = 7699  # Taiwan
WAIT_FOR_TABLE_TIMEOUT = 20

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bni_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────── Scraper class ───────────────────────────────────


class BNIMemberScraper:
    """BNI 台灣會員資料爬蟲"""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self.driver = None
        self._ajax: AjaxClient = AjaxClient()
        self.all_members: list = []
        self.regions: list = []
        self.chapters: list = []
        self.failed_chapters: list = []
        self.stats = {"regions": 0, "chapters": 0, "members": 0, "details_scraped": 0}

    # ── driver lifecycle ──────────────────────────────────────────────────────

    def start_driver(self) -> None:
        """啟動 Chrome 瀏覽器"""
        self.driver = create_driver(headless=self.headless)

    def close_driver(self) -> None:
        """關閉瀏覽器"""
        if self.driver:
            self.driver.quit()
            self.driver = None
            logger.info("瀏覽器已關閉")

    def _restart_driver(self) -> None:
        """重啟 Selenium 瀏覽器 (session 失效時自動恢復)"""
        logger.info("正在重啟瀏覽器...")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        self.start_driver()

    # ── Step 1: regions ───────────────────────────────────────────────────────

    def fetch_regions(self) -> list:
        """透過 API 取得所有區域資料"""
        logger.info("正在取得區域列表...")
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = self._ajax.fetch_regions(COUNTRY_ID, locale="zh_TW")
                self.regions = [
                    {"region_id": str(r["id"]), "region_name": r["name"]}
                    for r in raw
                ]
                self.stats["regions"] = len(self.regions)
                logger.info("成功取得 %d 個區域", len(self.regions))
                return self.regions
            except Exception as e:
                logger.warning("取得區域列表失敗 (第 %d 次): %s", attempt, e)
                if attempt < MAX_RETRIES:
                    time.sleep(REQUEST_DELAY)
        logger.error("無法取得區域列表，已重試 %d 次", MAX_RETRIES)
        return []

    # ── Step 2: chapters ──────────────────────────────────────────────────────

    def _dismiss_cookie(self) -> None:
        """關閉 cookie 同意彈窗"""
        assert self.driver is not None
        self.driver.execute_script(
            'var el = document.querySelector(".cc-window"); if(el) el.remove();'
        )

    def fetch_chapters_for_region(self, region_id: str, region_name: str) -> list:
        """從 findamember 頁面的下拉選單取得指定區域的所有分會"""
        assert self.driver is not None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.driver.get(FIND_A_MEMBER_URL)
                # Wait for page to be ready instead of fixed sleep
                WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'a[href="#search-advanced-tab"]')
                    )
                )
                self._dismiss_cookie()

                # Switch to advanced-search tab
                self.driver.execute_script(
                    'document.querySelector(\'a[href="#search-advanced-tab"]\').click();'
                )
                WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    EC.visibility_of_element_located(
                        (By.CSS_SELECTOR, '#search-advanced select[name="regionId"]')
                    )
                )

                # Select region
                region_sel = Select(
                    self.driver.find_element(
                        By.CSS_SELECTOR, '#search-advanced select[name="regionId"]'
                    )
                )
                region_sel.select_by_value(region_id)

                # Wait for chapter dropdown to be populated
                WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    lambda d: len(
                        Select(
                            d.find_element(
                                By.CSS_SELECTOR,
                                '#search-advanced select[name="chapterName"]',
                            )
                        ).options
                    ) > 1
                )

                chapter_sel = Select(
                    self.driver.find_element(
                        By.CSS_SELECTOR, '#search-advanced select[name="chapterName"]'
                    )
                )
                chapters = []
                for opt in chapter_sel.options:
                    val = opt.get_attribute("value")
                    text = opt.text.strip()
                    if val:
                        chapters.append({
                            "region_id": region_id,
                            "region_name": region_name,
                            "chapter_id": val,
                            "chapter_name": text,
                        })

                logger.info("區域 %s: 取得 %d 個分會", region_name, len(chapters))
                return chapters

            except Exception as e:
                logger.warning(
                    "取得區域 %s 分會列表失敗 (第 %d 次): %s",
                    region_name, attempt, e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(REQUEST_DELAY)

        logger.error("無法取得區域 %s 的分會列表", region_name)
        return []

    def fetch_all_chapters(self) -> list:
        """取得所有區域的分會列表"""
        if not self.regions:
            self.fetch_regions()

        total = len(self.regions)
        logger.info("開始取得 %d 個區域的分會列表", total)

        for i, region in enumerate(self.regions, 1):
            logger.info("取得分會 [%d/%d]: %s", i, total, region["region_name"])
            chapters = self.fetch_chapters_for_region(
                region["region_id"], region["region_name"]
            )
            self.chapters.extend(chapters)
            time.sleep(REQUEST_DELAY)

        self.stats["chapters"] = len(self.chapters)
        logger.info("共取得 %d 個分會", len(self.chapters))
        return self.chapters

    # ── Step 3: member list ───────────────────────────────────────────────────

    def _wait_for_member_table(self) -> None:
        """等待 DataTables 會員表格載入完成"""
        assert self.driver is not None
        WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "memberList"))
        )
        WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
            lambda d: (
                d.find_elements(By.CSS_SELECTOR, "#memberList table tbody tr td")
                or "顯示 0" in d.page_source
            )
        )
        # Brief pause to let DataTables fully render rows after the spinner
        # clears.  This is a genuine render-wait, not a rate-limit sleep.
        WebDriverWait(self.driver, 5).until(
            lambda d: not d.find_elements(By.CSS_SELECTOR, ".dataTables_processing")
        )

    def scrape_chapter_members(self, chapter: dict) -> list:
        """爬取指定分會的會員列表，並取得每位會員的詳情頁連結"""
        assert self.driver is not None
        region_id = chapter["region_id"]
        chapter_id = chapter["chapter_id"]
        chapter_name = chapter["chapter_name"]
        region_name = chapter["region_name"]

        url = (
            f"{MEMBER_LIST_URL}?countryIds={COUNTRY_ID}"
            f"&regionId={region_id}&chapterName={chapter_id}"
            f"&chapterArea=&memberKeywords=&chapterCity="
            f"&memberFirstName=&memberLastName=&memberCompany="
        )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.driver.get(url)
                self._wait_for_member_table()
                self._dismiss_cookie()

                rows = self.driver.find_elements(
                    By.CSS_SELECTOR, "#memberList table tbody tr"
                )

                members = []
                for row in rows:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 3:
                        continue

                    texts = [c.text.strip() for c in cells]
                    chapter_region_col = texts[COL_CHAPTER_REGION] if len(texts) > COL_CHAPTER_REGION else ""

                    # Front-end filter: keep only members in the target chapter
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

                logger.info("分會 %s: 取得 %d 位會員", chapter_name, len(members))
                return members

            except Exception as e:
                logger.warning(
                    "爬取分會 %s 失敗 (第 %d 次): %s",
                    chapter_name, attempt, e,
                )
                if "invalid session" in str(e).lower() or "no such session" in str(e).lower():
                    self._restart_driver()
                if attempt < MAX_RETRIES:
                    time.sleep(REQUEST_DELAY)

        logger.error("無法爬取分會 %s", chapter_name)
        self.failed_chapters.append(chapter)
        return []

    # ── Step 4: member details ────────────────────────────────────────────────

    def scrape_member_detail(self, member: dict) -> dict:
        """
        透過 AJAX 端點取得會員詳情 (不需 Selenium，約 0.3 秒/位)
        """
        detail_url = member.get("detail_url", "")
        if not detail_url:
            return member

        # Ensure widget settings are loaded
        self._ajax.get_widget_settings()

        parsed = urlparse(detail_url)
        parameters = parsed.query
        if not parameters:
            return member

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._ajax.session.post(
                    MEMBER_DETAIL_AJAX,
                    data={
                        "parameters": parameters,
                        "languages": AJAX_LANGUAGES,
                        "cmsv3": "true",
                        "mappedWidgetSettings": self._ajax.get_widget_settings(),
                        "pageMode": "Live_Site",
                        "website_type": AJAX_WEBSITE_TYPE,
                        "website_id": AJAX_WEBSITE_ID,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                html_content = resp.text

                if not html_content or len(html_content) < 50:
                    logger.warning("會員 %s AJAX 回應為空", member.get("member_name", "?"))
                    if attempt < MAX_RETRIES:
                        time.sleep(1)  # genuine retry back-off
                        continue
                    return member

                parsed_detail = parse_member_detail_html(html_content)
                member.update(parsed_detail)
                self.stats["details_scraped"] += 1
                return member

            except Exception as e:
                logger.warning(
                    "爬取會員 %s 詳情失敗 (第 %d 次): %s",
                    member.get("member_name", "?"), attempt, e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(1)  # genuine retry back-off

        return member

    # ── Progress helpers ──────────────────────────────────────────────────────

    def _load_chapters_from_csv(self, filename: str = "bni_chapters.csv") -> bool:
        """從已存的 CSV 載入分會列表 (避免重新爬取)"""
        path = Path(filename)
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                self.chapters = list(reader)
            self.stats["chapters"] = len(self.chapters)
            logger.info("從 %s 載入 %d 個分會", filename, len(self.chapters))
            return True
        except Exception as e:
            logger.warning("載入分會 CSV 失敗: %s", e)
            return False

    def _load_completed_chapters(self, filename: str = "bni_members_progress.csv") -> set:
        """從進度 CSV 載入已完成的分會 ID 集合"""
        members = load_members_csv(filename)
        if not members:
            return set()
        completed: set = set()
        for row in members:
            completed.add(row.get("chapter_id", ""))
            self.all_members.append(row)
        logger.info(
            "從 %s 載入 %d 筆已完成會員，涵蓋 %d 個分會",
            filename, len(self.all_members), len(completed),
        )
        self.stats["members"] = len(self.all_members)
        self.stats["details_scraped"] = len(self.all_members)
        return completed

    # ── Main flow ─────────────────────────────────────────────────────────────

    def scrape_all(self, scrape_details: bool = True, resume: bool = True) -> None:
        """
        完整爬取流程:
        1. 區域 → 2. 分會 → 3. 會員列表 → 4. 會員詳情
        支援從中斷處繼續 (resume=True)
        """
        # 1. Regions
        regions = self.fetch_regions()
        if not regions:
            logger.error("無法取得區域資料，程式結束")
            return
        self.save_regions_csv()

        # 2. Chapters (load from CSV cache or scrape)
        if not (resume and self._load_chapters_from_csv()):
            self.start_driver()
            self.fetch_all_chapters()
            self.save_chapters_csv()
        else:
            self.start_driver()

        # 3. Resume: figure out already-completed chapters
        completed_chapters: set = set()
        progress_path = Path(OUTPUT_DIR) / "bni_members_progress.csv"
        if resume:
            completed_chapters = self._load_completed_chapters(str(progress_path))

        # 4. Scrape each chapter
        total_chapters = len(self.chapters)
        skipped = 0
        logger.info(
            "開始爬取 %d 個分會的會員資料 (已完成 %d 個)",
            total_chapters, len(completed_chapters),
        )

        for i, chapter in enumerate(self.chapters, 1):
            if chapter["chapter_id"] in completed_chapters:
                skipped += 1
                if skipped % 20 == 0:
                    logger.info("已跳過 %d 個已完成分會...", skipped)
                continue

            logger.info(
                "=" * 40 + "\n分會進度: %d/%d (%d%%) - %s > %s",
                i, total_chapters, int(i / total_chapters * 100),
                chapter["region_name"], chapter["chapter_name"],
            )

            members = self.scrape_chapter_members(chapter)

            if scrape_details and members:
                logger.info("開始爬取 %d 位會員詳情 (AJAX 模式)...", len(members))
                for j, member in enumerate(members, 1):
                    if j % 10 == 0:
                        logger.info("  會員詳情進度: %d/%d", j, len(members))
                    self.scrape_member_detail(member)
                    time.sleep(DETAIL_DELAY)

            if members:
                self.all_members.extend(members)
                self.stats["members"] += len(members)

                # Append-only checkpoint every 5 chapters — avoids rewriting
                # the whole file each time.
                if (i - skipped) % 5 == 0:
                    append_members_csv(progress_path, members)

            time.sleep(REQUEST_DELAY)

        logger.info("所有分會爬取完成，共 %d 位會員", len(self.all_members))

    # ── CSV output ────────────────────────────────────────────────────────────

    def save_to_csv(self, filename: Optional[str] = None) -> Optional[str]:
        """將會員資料儲存為 CSV"""
        if not self.all_members:
            logger.warning("沒有會員資料可儲存")
            return None

        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(OUTPUT_DIR)
            filename = str(out_dir / f"bni_members_{timestamp}.csv")

        write_members_csv(filename, self.all_members)
        return str(filename)

    def save_regions_csv(self, filename: str = "bni_regions.csv") -> Optional[str]:
        """儲存區域資料為 CSV"""
        if not self.regions:
            return None
        out = Path(OUTPUT_DIR) / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["region_id", "region_name"])
            writer.writeheader()
            writer.writerows(self.regions)
        logger.info("已儲存 %d 個區域至 %s", len(self.regions), out)
        return str(out)

    def save_chapters_csv(self, filename: str = "bni_chapters.csv") -> Optional[str]:
        """儲存分會資料為 CSV"""
        if not self.chapters:
            return None
        fieldnames = ["region_id", "region_name", "chapter_id", "chapter_name"]
        out = Path(OUTPUT_DIR) / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for ch in self.chapters:
                writer.writerow({k: ch.get(k, "") for k in fieldnames})
        logger.info("已儲存 %d 個分會至 %s", len(self.chapters), out)
        return str(out)

    # ── Summary ───────────────────────────────────────────────────────────────

    def print_summary(self) -> None:
        """輸出統計報告"""
        print("\n" + "=" * 60)
        print("BNI 台灣會員資料爬蟲 - 執行報告")
        print("=" * 60)
        print(f"區域總數: {self.stats['regions']}")
        print(f"分會總數: {self.stats['chapters']}")
        print(f"會員總數: {self.stats['members']}")
        print(f"已爬詳情: {self.stats['details_scraped']}")
        print(f"失敗分會: {len(self.failed_chapters)}")

        if self.failed_chapters:
            print("\n失敗的分會:")
            for ch in self.failed_chapters:
                print(f"  - {ch['region_name']} > {ch['chapter_name']}")

        region_stats: dict = {}
        for m in self.all_members:
            name = m.get("region_name", "未知")
            region_stats[name] = region_stats.get(name, 0) + 1

        if region_stats:
            print("\n各區域會員數:")
            for name, count in sorted(region_stats.items(), key=lambda x: -x[1]):
                print(f"  {name}: {count}")

        print("=" * 60)


def main() -> None:
    """主程式"""
    scraper = BNIMemberScraper(headless=True)

    try:
        scraper.scrape_all(scrape_details=True)

        out_dir = Path(OUTPUT_DIR)
        output_file = scraper.save_to_csv(str(out_dir / OUTPUT_FILENAME))
        scraper.print_summary()

        if output_file:
            print(f"\n資料已儲存至: {output_file}")

    except KeyboardInterrupt:
        logger.info("使用者中斷執行")
        if scraper.all_members:
            scraper.save_to_csv(str(Path(OUTPUT_DIR) / "bni_members_partial.csv"))
            logger.info("已儲存部分資料")
    except Exception as e:
        logger.error("執行錯誤: %s", e, exc_info=True)
        if scraper.all_members:
            scraper.save_to_csv(str(Path(OUTPUT_DIR) / "bni_members_partial.csv"))
    finally:
        scraper.close_driver()


if __name__ == "__main__":
    main()
