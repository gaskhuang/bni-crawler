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
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bni_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 常數
BASE_URL = "https://bni.com.tw"
COUNTRY_ID = 7699  # Taiwan
REGION_API = f"{BASE_URL}/web/open/appsCmsNationalChapterSearchFilterJson"
MEMBER_LIST_URL = f"{BASE_URL}/zh-TW/memberlist"
FIND_A_MEMBER_URL = f"{BASE_URL}/zh-TW/findamember"
MEMBER_DETAIL_URL = f"{BASE_URL}/zh-TW/memberdetails"
MEMBER_DETAIL_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberdetail/display"
REQUEST_DELAY = 2  # 秒
DETAIL_DELAY = 0.3  # 會員詳情 AJAX 間隔 (秒)
MAX_RETRIES = 3
PAGE_LOAD_TIMEOUT = 30
WAIT_FOR_TABLE_TIMEOUT = 20

# AJAX 固定參數
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


class BNIMemberScraper:
    """BNI 台灣會員資料爬蟲"""

    def __init__(self, headless=True):
        self.headless = headless
        self.driver = None
        self.session = None  # requests.Session for AJAX calls
        self.mapped_widget_settings = ""  # 從頁面取得的 widget 設定
        self.all_members = []
        self.regions = []
        self.chapters = []  # [{region_id, region_name, chapter_id, chapter_name}]
        self.failed_chapters = []
        self.stats = {"regions": 0, "chapters": 0, "members": 0, "details_scraped": 0}

    def start_driver(self):
        """啟動 Chrome 瀏覽器"""
        options = Options()
        if self.headless:
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

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        logger.info("Chrome 瀏覽器已啟動 (headless=%s)", self.headless)

    def close_driver(self):
        """關閉瀏覽器"""
        if self.driver:
            self.driver.quit()
            logger.info("瀏覽器已關閉")

    # ========== 第一步: 取得區域列表 ==========

    def fetch_regions(self):
        """透過 API 取得所有區域資料"""
        logger.info("正在取得區域列表...")
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    REGION_API,
                    data={"countryId": COUNTRY_ID, "request_locale": "zh_TW"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                self.regions = [
                    {"region_id": str(r["id"]), "region_name": r["name"]}
                    for r in data.get("regions", [])
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

    # ========== 第二步: 取得每個區域的分會列表 ==========

    def fetch_chapters_for_region(self, region_id, region_name):
        """從 findamember 頁面的下拉選單取得指定區域的所有分會"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.driver.get(FIND_A_MEMBER_URL)
                time.sleep(3)
                self._dismiss_cookie()

                # 切換到進階搜尋頁籤
                self.driver.execute_script(
                    'document.querySelector(\'a[href="#search-advanced-tab"]\').click();'
                )
                time.sleep(1)

                # 選擇區域
                region_sel = Select(
                    self.driver.find_element(
                        By.CSS_SELECTOR, '#search-advanced select[name="regionId"]'
                    )
                )
                region_sel.select_by_value(region_id)
                time.sleep(3)

                # 取得分會下拉選單的選項
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

                logger.info(
                    "區域 %s: 取得 %d 個分會", region_name, len(chapters)
                )
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

    def fetch_all_chapters(self):
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

    # ========== 第三步: 按分會爬取會員列表 ==========

    def _dismiss_cookie(self):
        """關閉 cookie 同意彈窗"""
        self.driver.execute_script(
            'var el = document.querySelector(".cc-window"); if(el) el.remove();'
        )

    def _wait_for_member_table(self):
        """等待 DataTables 會員表格載入完成"""
        WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "memberList"))
        )
        WebDriverWait(self.driver, WAIT_FOR_TABLE_TIMEOUT).until(
            lambda d: (
                d.find_elements(By.CSS_SELECTOR, "#memberList table tbody tr td")
                or "顯示 0" in d.page_source
            )
        )
        time.sleep(2)

    def _restart_driver(self):
        """重啟 Selenium 瀏覽器 (session 失效時自動恢復)"""
        logger.info("正在重啟瀏覽器...")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        self.start_driver()

    def scrape_chapter_members(self, chapter):
        """爬取指定分會的會員列表，並取得每位會員的詳情頁連結"""
        region_id = chapter["region_id"]
        chapter_id = chapter["chapter_id"]
        chapter_name = chapter["chapter_name"]
        region_name = chapter["region_name"]

        # 用 chapterName 參數讓該分會的會員排在最前面
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
                    chapter_region_col = texts[1] if len(texts) > 1 else ""

                    # 前端過濾: 只保留屬於目標分會的會員
                    if chapter_name not in chapter_region_col:
                        continue

                    member_name = texts[0]
                    if not member_name:
                        continue

                    # 取得會員詳情頁連結
                    detail_url = ""
                    links = cells[0].find_elements(By.TAG_NAME, "a")
                    if links:
                        detail_url = links[0].get_attribute("href") or ""

                    city = texts[2] if len(texts) > 2 else ""
                    if len(texts) >= 6:
                        industry = texts[4]
                        company = texts[5]
                    elif len(texts) >= 5:
                        industry = texts[3]
                        company = texts[4]
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

                logger.info(
                    "分會 %s: 取得 %d 位會員", chapter_name, len(members)
                )
                return members

            except Exception as e:
                logger.warning(
                    "爬取分會 %s 失敗 (第 %d 次): %s",
                    chapter_name, attempt, e,
                )
                # 如果是 session 失效，自動重啟瀏覽器
                if "invalid session" in str(e).lower() or "no such session" in str(e).lower():
                    self._restart_driver()
                if attempt < MAX_RETRIES:
                    time.sleep(REQUEST_DELAY)

        logger.error("無法爬取分會 %s", chapter_name)
        self.failed_chapters.append(chapter)
        return []

    # ========== 第四步: 爬取會員詳情 (使用 requests AJAX，快 10 倍以上) ==========

    def _init_ajax_session(self):
        """初始化 requests Session 並取得 mappedWidgetSettings"""
        if self.session and self.mapped_widget_settings:
            return

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"{BASE_URL}/zh-TW/memberdetails",
            "Origin": BASE_URL,
        })

        # 從會員詳情頁面取得 mappedWidgetSettings
        logger.info("正在取得 AJAX 所需的 mappedWidgetSettings...")
        try:
            resp = self.session.get(MEMBER_DETAIL_URL, timeout=15)
            resp.raise_for_status()
            # 從 HTML 中提取 mappedWidgetSettings
            match = re.search(
                r'name="mappedWidgetSettings"\s+value="([^"]*)"', resp.text
            )
            if match:
                import html as html_mod
                self.mapped_widget_settings = html_mod.unescape(match.group(1))
                logger.info("成功取得 mappedWidgetSettings (%d 字元)", len(self.mapped_widget_settings))
            else:
                # 嘗試其他格式
                match = re.search(
                    r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text
                )
                if match:
                    import html as html_mod
                    self.mapped_widget_settings = html_mod.unescape(match.group(1))
                    logger.info("成功取得 mappedWidgetSettings (備用方式)")
                else:
                    logger.warning("無法從頁面取得 mappedWidgetSettings，使用空值")
                    self.mapped_widget_settings = ""
        except Exception as e:
            logger.warning("取得 mappedWidgetSettings 失敗: %s", e)
            self.mapped_widget_settings = ""

    def scrape_member_detail(self, member):
        """
        透過 AJAX 端點取得會員詳情 (不需 Selenium，約 0.3 秒/位)

        使用 POST 到 /bnicms/v3/frontend/memberdetail/display
        回傳 HTML 包含電話、地址、業務描述等資料
        """
        detail_url = member.get("detail_url", "")
        if not detail_url:
            return member

        self._init_ajax_session()

        # 從 detail_url 提取 parameters (問號後面的部分)
        parsed = urlparse(detail_url)
        parameters = parsed.query  # e.g. "encryptedMemberId=xxx&name=yyy"

        if not parameters:
            return member

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    MEMBER_DETAIL_AJAX,
                    data={
                        "parameters": parameters,
                        "languages": AJAX_LANGUAGES,
                        "cmsv3": "true",
                        "mappedWidgetSettings": self.mapped_widget_settings,
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
                        time.sleep(1)
                        continue
                    return member

                # 使用 BeautifulSoup 解析 HTML
                soup = BeautifulSoup(html_content, "html.parser")

                # 電話: 從 <a href="tel:+886..."> 取得
                tel_links = soup.find_all("a", href=re.compile(r"^tel:"))
                if tel_links:
                    member["phone"] = tel_links[0].get_text(strip=True)
                    if len(tel_links) > 1:
                        member["mobile"] = tel_links[1].get_text(strip=True)

                # 取得純文字，按行分析
                body_text = soup.get_text(separator="\n")
                lines = [l.strip() for l in body_text.split("\n") if l.strip()]

                # 提取地址、網站、業務描述 (AJAX 回應段落標題可能是英文)
                sections = {}
                current_section = "header"
                section_lines = []

                section_markers = [
                    "My Business", "Ideal Referral", "Top Problem Solved",
                    "My Ideal Referral Partner", "Top Product",
                    "我的業務", "理想的引薦", "解決過的最大難題",
                    "我理想中的引薦搭檔", "最佳產品", "Links",
                ]

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

                # 從 header 區段取得地址和網站
                header = sections.get("header", [])
                info_start = 0
                for i, line in enumerate(header):
                    if line.startswith(("Mr.", "Ms.", "Mrs.", "先生", "小姐", "女士")):
                        info_start = i + 1
                        break

                if info_start > 0 and info_start < len(header):
                    remaining = header[info_start:]
                    addr_parts = []
                    for line in remaining[1:]:  # 跳過公司名重複
                        if re.match(r"^https?://|^www\.", line):
                            member["website"] = line
                            break
                        if re.match(r"^\d{3,5}$", line):
                            continue
                        if line and not line.startswith(("BNI", "Links")):
                            addr_parts.append(line)
                    if addr_parts:
                        member["address"] = " ".join(addr_parts)

                # 業務描述
                biz = sections.get("My Business") or sections.get("我的業務") or []
                if biz:
                    member["business_desc"] = "\n".join(biz)

                # 理想引薦
                referral = sections.get("Ideal Referral") or sections.get("理想的引薦") or []
                if referral:
                    member["ideal_referral"] = "\n".join(referral)

                # 最佳產品
                product = sections.get("Top Product") or sections.get("最佳產品") or []
                if product:
                    member["best_product"] = "\n".join(product)

                self.stats["details_scraped"] += 1
                return member

            except Exception as e:
                logger.warning(
                    "爬取會員 %s 詳情失敗 (第 %d 次): %s",
                    member.get("member_name", "?"), attempt, e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(1)

        return member

    # ========== 主流程 ==========

    def _load_chapters_from_csv(self, filename="bni_chapters.csv"):
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

    def _load_completed_chapters(self, filename="bni_members_progress.csv"):
        """從進度 CSV 載入已完成的分會 ID 集合"""
        path = Path(filename)
        if not path.exists():
            return set()
        try:
            completed = set()
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    completed.add(row.get("chapter_id", ""))
                    self.all_members.append(row)
            logger.info("從 %s 載入 %d 筆已完成會員，涵蓋 %d 個分會",
                        filename, len(self.all_members), len(completed))
            self.stats["members"] = len(self.all_members)
            self.stats["details_scraped"] = len(self.all_members)
            return completed
        except Exception as e:
            logger.warning("載入進度 CSV 失敗: %s", e)
            return set()

    def scrape_all(self, scrape_details=True, resume=True):
        """
        完整爬取流程:
        1. 區域 → 2. 分會 → 3. 會員列表 → 4. 會員詳情
        支援從中斷處繼續 (resume=True)
        """
        # 1. 取得區域
        regions = self.fetch_regions()
        if not regions:
            logger.error("無法取得區域資料，程式結束")
            return
        self.save_regions_csv()

        # 2. 取得分會列表 (優先從 CSV 載入)
        if not (resume and self._load_chapters_from_csv()):
            self.start_driver()
            self.fetch_all_chapters()
            self.save_chapters_csv()
        else:
            # 仍需啟動瀏覽器 (用於會員列表頁面)
            self.start_driver()

        # 3. 載入已完成的分會 (用於跳過)
        completed_chapters = set()
        if resume:
            completed_chapters = self._load_completed_chapters()

        # 4. 按分會爬取會員
        total_chapters = len(self.chapters)
        skipped = 0
        logger.info("開始爬取 %d 個分會的會員資料 (已完成 %d 個)",
                    total_chapters, len(completed_chapters))

        for i, chapter in enumerate(self.chapters, 1):
            # 跳過已完成的分會
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
                        logger.info(
                            "  會員詳情進度: %d/%d", j, len(members)
                        )
                    self.scrape_member_detail(member)
                    time.sleep(DETAIL_DELAY)  # AJAX 間隔 0.3 秒

            if members:
                self.all_members.extend(members)
                self.stats["members"] += len(members)

            # 每 5 個分會存檔一次 (防止中斷丟失資料)
            if (i - skipped) % 5 == 0 and self.all_members:
                self.save_to_csv("bni_members_progress.csv")

            time.sleep(REQUEST_DELAY)

        logger.info("所有分會爬取完成，共 %d 位會員", len(self.all_members))

    def save_to_csv(self, filename=None):
        """將會員資料儲存為 CSV"""
        if not self.all_members:
            logger.warning("沒有會員資料可儲存")
            return None

        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"bni_members_{timestamp}.csv"

        output_path = Path(filename)
        fieldnames = [
            "region_id", "region_name", "chapter_id", "chapter_name",
            "member_name", "company", "industry", "city",
            "phone", "mobile", "address", "website",
            "business_desc", "ideal_referral", "best_product",
            "detail_url",
        ]

        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for member in self.all_members:
                row = {k: member.get(k, "") for k in fieldnames}
                writer.writerow(row)

        logger.info("已儲存 %d 筆會員資料至 %s", len(self.all_members), output_path)
        return str(output_path)

    def save_regions_csv(self, filename="bni_regions.csv"):
        """儲存區域資料為 CSV"""
        if not self.regions:
            return None

        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["region_id", "region_name"])
            writer.writeheader()
            writer.writerows(self.regions)

        logger.info("已儲存 %d 個區域至 %s", len(self.regions), filename)
        return filename

    def save_chapters_csv(self, filename="bni_chapters.csv"):
        """儲存分會資料為 CSV"""
        if not self.chapters:
            return None

        fieldnames = ["region_id", "region_name", "chapter_id", "chapter_name"]
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for ch in self.chapters:
                writer.writerow({k: ch.get(k, "") for k in fieldnames})

        logger.info("已儲存 %d 個分會至 %s", len(self.chapters), filename)
        return filename

    def print_summary(self):
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

        # 按區域統計
        region_stats = {}
        for m in self.all_members:
            name = m.get("region_name", "未知")
            region_stats[name] = region_stats.get(name, 0) + 1

        if region_stats:
            print("\n各區域會員數:")
            for name, count in sorted(region_stats.items(), key=lambda x: -x[1]):
                print(f"  {name}: {count}")

        print("=" * 60)


def main():
    """主程式"""
    scraper = BNIMemberScraper(headless=True)

    try:
        scraper.scrape_all(scrape_details=True)

        # 儲存最終結果
        output_file = scraper.save_to_csv()
        scraper.print_summary()

        if output_file:
            print(f"\n資料已儲存至: {output_file}")

    except KeyboardInterrupt:
        logger.info("使用者中斷執行")
        if scraper.all_members:
            scraper.save_to_csv("bni_members_partial.csv")
            logger.info("已儲存部分資料")
    except Exception as e:
        logger.error("執行錯誤: %s", e, exc_info=True)
        if scraper.all_members:
            scraper.save_to_csv("bni_members_partial.csv")
    finally:
        scraper.close_driver()


if __name__ == "__main__":
    main()
