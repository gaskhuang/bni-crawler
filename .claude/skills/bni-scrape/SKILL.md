---
name: bni-scrape
description: BNI 台灣會員資料完整爬蟲流程 - 從區域→分會→會員列表→會員詳情，多線程並行爬取
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
argument-hint: "[workers數量，預設5]"
---

# BNI 台灣會員資料爬蟲

執行完整的 BNI 台灣會員爬蟲流程。使用者可指定並行 workers 數量（預設 5）。

## 參數

- `$ARGUMENTS`: workers 數量（可選，預設 5）

## 執行步驟

### 1. 環境檢查

確認虛擬環境和依賴已安裝：

```bash
cd /Users/gask/Documents/claude-code/bni-crawer
source .venv/bin/activate
pip install -q selenium beautifulsoup4 requests webdriver-manager
```

### 2. 爬取流程概述

整個爬蟲分為 4 個階段：

```
區域 (31個) → 分會 (248個) → 會員列表 → 會員詳情
```

- **區域**: POST API `https://bni.com.tw/web/open/appsCmsNationalChapterSearchFilterJson` with `countryId=7699`
- **分會列表**: Selenium 打開 `findamember` 頁面，選擇區域，讀取 `chapterName` 下拉選單（結果存入 `bni_chapters.csv`）
- **會員列表**: Selenium 載入 `memberlist?chapterName={id}` 頁面，DataTables 渲染後抓取表格（每個分會 < 250 人）
- **會員詳情**: requests POST 到 AJAX 端點 `/bnicms/v3/frontend/memberdetail/display`（不需 Selenium，0.3 秒/位）

### 3. 關鍵技術細節

#### 會員列表 chapterName 參數行為
- `chapterName` URL 參數**不會**在 server side 過濾，只會讓該分會的會員排在最前面
- 必須在 client side 用分會名稱過濾 chapter column 文字
- 每個分會 < 250 人，所以所有該分會會員都在前 250 筆結果中

#### 會員詳情 AJAX 端點
```python
POST https://bni.com.tw/bnicms/v3/frontend/memberdetail/display
data = {
    "parameters": "encryptedMemberId=xxx&name=yyy",  # 從 detail_url 的 query string 取得
    "languages": '{"availableLanguages":[...],"activeLanguage":{...}}',
    "cmsv3": "true",
    "mappedWidgetSettings": "<從 memberdetails 頁面取得>",
    "pageMode": "Live_Site",
    "website_type": "1",
    "website_id": "10065",
}
```

#### AJAX 回應解析結構
```
文字行結構:
[0] 姓名
[1] 公司
[2] 行業
[3+] 電話號碼（從 <a href="tel:..."> 取得）
[?] "Chapter"
[?+1] 分會名稱
[?+2] 稱謂+姓名 (Dr./Mr./Ms./博士)
[?+3] 公司（重複）
[?+4] 街道地址
[?+5] 區域
[?+6] 城市
[?+7] 郵遞區號
[?+8] 網站（可選）

段落標題（英文）:
- "My Business" → 業務描述
- "Ideal Referral" → 理想引薦
- "Top Problem Solved" → 解決過的最大難題
- "My Ideal Referral Partner" → 理想引薦搭檔
- "Top Product" → 最佳產品
- "My Favorite BNI Story" → BNI 故事

LINE URL: 從 HTML 中抓取 https://line.me/... 連結
```

### 4. CSV 輸出欄位

```
region_id, region_name, chapter_id, chapter_name,
member_name, title, company, industry, city,
phone, mobile, address, postal_code, website,
business_desc, ideal_referral, best_product,
top_problem_solved, ideal_referral_partner, favorite_story,
line_url, detail_url
```

### 5. 執行爬蟲

使用 `bni_scraper_parallel.py` 執行多線程爬蟲：

```bash
source .venv/bin/activate
python bni_scraper_parallel.py ${ARGUMENTS:-5}
```

功能特點：
- 多個 Selenium session 並行處理不同分會
- 會員詳情用 requests AJAX（不需 Selenium）
- 支援斷點續爬（從 `bni_members_progress.csv` 恢復）
- 每 5 個分會自動存檔
- Selenium session 失效時自動重啟

### 6. 監控進度

```bash
tail -f bni_scraper.log
```

或查看統計：

```bash
wc -l bni_members_progress.csv
```

### 7. 驗證資料品質

爬取完成後，檢查各欄位的填充率：

```python
import csv
with open('bni_members_YYYYMMDD_HHMMSS.csv', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
total = len(rows)
for field in ['phone', 'address', 'business_desc', 'ideal_referral', 'best_product', 'line_url']:
    count = sum(1 for r in rows if r.get(field))
    print(f'{field}: {count}/{total} ({count*100//total}%)')
```

### 8. 常見問題排除

| 問題 | 解決方式 |
|------|----------|
| Selenium session 失效 (invalid session id) | 程式已內建自動重啟 Chrome |
| 會員列表 250 筆上限 | 按分會查詢，每個分會 < 250 人 |
| AJAX 回應為空 | 檢查 mappedWidgetSettings 是否正確取得 |
| 速度太慢 | 增加 workers 數量（建議 3-8） |
| 被封鎖/限速 | 降低 workers 數量，增加 DETAIL_DELAY |

### 9. 輸出檔案

- `bni_regions.csv` - 31 個區域
- `bni_chapters.csv` - 248 個分會
- `bni_members_progress.csv` - 進度存檔（每 5 個分會更新）
- `bni_members_YYYYMMDD_HHMMSS.csv` - 最終完整資料

## 預期結果

- 約 9,600+ 位會員
- 248 個分會 / 26 個區域
- 5 workers 約需 50 分鐘完成
