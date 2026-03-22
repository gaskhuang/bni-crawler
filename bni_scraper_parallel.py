"""
BNI 台灣會員資料爬蟲 - 多線程並行版本
使用多個 Selenium session 同時爬取不同分會的會員列表
會員詳情使用 requests AJAX (不需 Selenium)

用法: python bni_scraper_parallel.py [workers數量]
"""

import csv
import html as html_mod
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    handlers=[
        logging.FileHandler("bni_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 常數
BASE_URL = "https://bni.com.tw"
COUNTRY_ID = 7699
REGION_API = f"{BASE_URL}/web/open/appsCmsNationalChapterSearchFilterJson"
MEMBER_LIST_URL = f"{BASE_URL}/zh-TW/memberlist"
MEMBER_DETAIL_URL = f"{BASE_URL}/zh-TW/memberdetails"
MEMBER_DETAIL_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberdetail/display"
DETAIL_DELAY = 0.2  # AJAX 間隔
MAX_RETRIES = 3
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

# 全域鎖和共享資料
write_lock = Lock()
all_members = []
stats = {"chapters_done": 0, "chapters_total": 0, "members": 0}

# 快取 chromedriver 路徑
_driver_path = None


def get_driver_path():
    global _driver_path
    if _driver_path is None:
        _driver_path = ChromeDriverManager().install()
    return _driver_path


def create_driver():
    """建立一個新的 headless Chrome"""
    options = Options()
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
    service = Service(get_driver_path())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    return driver


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
        m = re.search(r'name="mappedWidgetSettings"\s+value="([^"]*)"', resp.text)
        if m:
            mapped_widget_settings = html_mod.unescape(m.group(1))
        else:
            m = re.search(r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text)
            if m:
                mapped_widget_settings = html_mod.unescape(m.group(1))
    except Exception as e:
        logger.warning("取得 mappedWidgetSettings 失敗: %s", e)

    return session, mapped_widget_settings


def scrape_member_detail_ajax(session, mapped_widget_settings, member):
    """用 requests AJAX 取得會員詳情"""
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
                    "website_type": "1",
                    "website_id": "10065",
                },
                timeout=15,
            )
            resp.raise_for_status()
            html_content = resp.text

            if not html_content or len(html_content) < 50:
                if attempt < MAX_RETRIES:
                    time.sleep(0.5)
                    continue
                return member

            soup = BeautifulSoup(html_content, "html.parser")

            # === 電話/傳真: 從 <a href="tel:..."> 取得所有號碼 ===
            tel_links = soup.find_all("a", href=re.compile(r"^tel:"))
            phones = [a.get_text(strip=True) for a in tel_links if a.get_text(strip=True)]
            # 去重但保持順序
            seen = set()
            unique_phones = []
            for p in phones:
                if p not in seen:
                    seen.add(p)
                    unique_phones.append(p)
            if unique_phones:
                member["phone"] = unique_phones[0]
            if len(unique_phones) > 1:
                member["mobile"] = unique_phones[1]

            # === 文字分段解析 ===
            body_text = soup.get_text(separator="\n")
            lines = [l.strip() for l in body_text.split("\n") if l.strip()]

            # 段落標題 (中英文都支援)
            section_markers = {
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

            # === Header 解析: 名字/公司/行業/電話/Chapter/稱謂/地址/郵遞區號/網站 ===
            header = sections.get("header", [])

            # 找 "Chapter" 行作為錨點
            chapter_idx = -1
            for i, line in enumerate(header):
                if line == "Chapter":
                    chapter_idx = i
                    break

            if chapter_idx >= 0:
                # Chapter 前面: 名字、公司、行業、電話號碼
                # Chapter 後面: 分會名、稱謂+名字、公司(重複)、地址、區域、城市、郵遞區號、網站

                # Chapter 前: 行業在第3行 (index 2)
                if chapter_idx >= 3:
                    member["industry"] = header[2]

                # Chapter 後面的資訊
                after_chapter = header[chapter_idx + 1:]  # 跳過 "Chapter"
                if after_chapter:
                    after_chapter = after_chapter[1:]  # 跳過分會名

                # 找稱謂行 (Dr./Mr./Ms./博士/先生/小姐)
                title_idx = -1
                for i, line in enumerate(after_chapter):
                    if re.match(r"^(Dr\.|Mr\.|Ms\.|Mrs\.|博士|先生|小姐|女士)", line):
                        title_idx = i
                        member["title"] = line
                        break

                if title_idx >= 0:
                    info = after_chapter[title_idx + 1:]  # 稱謂行之後
                    # 跳過公司名重複 (第一行)
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

            # === 各段落內容 ===
            for marker, field in section_markers.items():
                content = sections.get(marker, [])
                if content and not member.get(field):
                    member[field] = "\n".join(content)

            # === LINE 聯絡方式: 從 HTML 或文字中提取 line.me URL ===
            line_urls = re.findall(r'https?://line\.me/[^\s"\'<>]+', html_content)
            if line_urls:
                member["line_url"] = line_urls[0]

            return member

        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(0.5)
    return member


def scrape_chapter(chapter, driver=None):
    """
    爬取一個分會的所有會員 (列表 + 詳情)
    driver: 可選的 Selenium driver，若為 None 會自動建立
    """
    region_id = chapter["region_id"]
    chapter_id = chapter["chapter_id"]
    chapter_name = chapter["chapter_name"]
    region_name = chapter["region_name"]

    own_driver = driver is None
    if own_driver:
        driver = create_driver()

    try:
        # 1. 用 Selenium 取得會員列表
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
                # 等待表格載入
                WebDriverWait(driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "memberList"))
                )
                WebDriverWait(driver, WAIT_FOR_TABLE_TIMEOUT).until(
                    lambda d: (
                        d.find_elements(By.CSS_SELECTOR, "#memberList table tbody tr td")
                        or "顯示 0" in d.page_source
                    )
                )
                time.sleep(2)
                # 關閉 cookie
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
                    chapter_region_col = texts[1] if len(texts) > 1 else ""

                    if chapter_name not in chapter_region_col:
                        continue

                    member_name = texts[0]
                    if not member_name:
                        continue

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

                break  # 成功

            except Exception as e:
                logger.warning("分會 %s 會員列表失敗 (第 %d 次): %s", chapter_name, attempt, e)
                if "invalid session" in str(e).lower():
                    driver.quit()
                    driver = create_driver()
                if attempt < MAX_RETRIES:
                    time.sleep(2)

        if not members:
            logger.warning("分會 %s: 無會員資料", chapter_name)
            return []

        # 2. 用 requests AJAX 取得所有會員詳情
        session, mws = create_ajax_session()
        for j, member in enumerate(members, 1):
            scrape_member_detail_ajax(session, mws, member)
            time.sleep(DETAIL_DELAY)

        logger.info("分會 %s: 完成 %d 位會員", chapter_name, len(members))
        return members

    finally:
        if own_driver:
            try:
                driver.quit()
            except Exception:
                pass


def process_chapter_worker(chapter):
    """線程工作函數：建立自己的 driver，爬一個分會"""
    try:
        members = scrape_chapter(chapter)
        with write_lock:
            all_members.extend(members)
            stats["chapters_done"] += 1
            stats["members"] += len(members)
            done = stats["chapters_done"]
            total = stats["chapters_total"]
            logger.info(
                "總進度: %d/%d (%d%%) - 累計 %d 位會員",
                done, total, int(done / total * 100) if total else 0,
                stats["members"],
            )
            # 每 5 個分會存檔
            if done % 5 == 0:
                save_members_csv(all_members, "bni_members_progress.csv")
        return members
    except Exception as e:
        logger.error("分會 %s 處理失敗: %s", chapter.get("chapter_name", "?"), e)
        return []


def save_members_csv(members, filename):
    """儲存會員資料"""
    if not members:
        return
    fieldnames = [
        "region_id", "region_name", "chapter_id", "chapter_name",
        "member_name", "title", "company", "industry", "city",
        "phone", "mobile", "address", "postal_code", "website",
        "business_desc", "ideal_referral", "best_product",
        "top_problem_solved", "ideal_referral_partner", "favorite_story",
        "line_url", "detail_url",
    ]
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for m in members:
            writer.writerow({k: m.get(k, "") for k in fieldnames})
    logger.info("已儲存 %d 筆至 %s", len(members), filename)


def load_chapters_csv(filename="bni_chapters.csv"):
    """載入分會列表"""
    with open(filename, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_completed_chapters(filename="bni_members_progress.csv"):
    """載入已完成的分會和會員"""
    path = Path(filename)
    if not path.exists():
        return set(), []
    completed = set()
    members = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            completed.add(row.get("chapter_id", ""))
            members.append(row)
    return completed, members


def main():
    num_workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    logger.info("啟動並行爬蟲，%d 個 workers", num_workers)

    # 預先下載 chromedriver
    get_driver_path()

    # 載入分會列表
    chapters = load_chapters_csv()
    logger.info("載入 %d 個分會", len(chapters))

    # 載入已完成的
    completed, existing_members = load_completed_chapters()
    if completed:
        logger.info("跳過 %d 個已完成分會 (%d 筆會員)", len(completed), len(existing_members))
        all_members.extend(existing_members)
        stats["members"] = len(existing_members)

    # 過濾未完成的分會
    pending = [ch for ch in chapters if ch["chapter_id"] not in completed]
    stats["chapters_total"] = len(pending)
    stats["chapters_done"] = 0
    logger.info("待處理: %d 個分會", len(pending))

    if not pending:
        logger.info("所有分會已完成!")
        return

    # 並行處理
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="Worker") as executor:
        futures = {executor.submit(process_chapter_worker, ch): ch for ch in pending}
        for future in as_completed(futures):
            ch = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error("分會 %s 異常: %s", ch.get("chapter_name", "?"), e)

    elapsed = time.time() - start_time
    logger.info("全部完成! 耗時 %.1f 分鐘，共 %d 位會員", elapsed / 60, len(all_members))

    # 最終存檔
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_members_csv(all_members, f"bni_members_{timestamp}.csv")
    save_members_csv(all_members, "bni_members_progress.csv")

    # 統計
    print(f"\n{'='*60}")
    print(f"完成! 共 {len(all_members)} 位會員，耗時 {elapsed/60:.1f} 分鐘")
    print(f"Workers: {num_workers}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
