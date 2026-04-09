# BNI 全球會員爬蟲

支援 44 個國家的 BNI 會員資料爬取，提供三種執行模式。

## 架構

採用 Selenium + aiohttp 混合架構：

1. **Selenium（headless Chrome）** — 動態發現各區域的分會清單（章節下拉選單為 JS 動態渲染）
2. **aiohttp 非同步 HTTP** — 高併發抓取會員列表與詳細資料

每次執行時自動從 BNI 網站即時發現所有分會，不依賴靜態資料檔。

## 功能

- 44 個國家的 BNI 會員資料爬取
- 互動式國家選單（單選 / 多選 / 全選）
- 動態分會發現（每次執行自動偵測最新分會）
- 逐章節查詢，避免 API 200 筆上限截斷
- 可調整併發數（預設 50）
- 自動重試機制（預設 3 次）
- CSV 輸出含公式注入防護

## 快速開始

```bash
pip install -r requirements.txt
python bni_scraper_async.py
```

啟動後會顯示國家選單，直接按 Enter 預設爬取台灣。

### 指定併發數

```bash
python bni_scraper_async.py 100
```

## 執行模式

| 檔案 | 模式 | 說明 |
|------|------|------|
| `bni_scraper_async.py` | 非同步（推薦） | Selenium 發現分會 + aiohttp 高併發抓取，速度最快 |
| `bni_scraper_parallel.py` | 多線程 | 多個 Chrome 實例並行，適合需要完整瀏覽器環境的場景 |
| `bni_scraper.py` | 同步 | 單線程逐一抓取，最穩定但最慢 |

## 專案結構

```
bni_core.py              # 共用核心模組（driver 建立、HTML 解析、CSV 輸出）
bni_scraper_async.py     # 非同步版爬蟲（主力）
bni_scraper_parallel.py  # 多線程版爬蟲
bni_scraper.py           # 同步版爬蟲
config.yaml              # 設定檔（併發數、重試次數、輸出路徑）
requirements.txt         # Python 依賴
```

## 設定檔 config.yaml

```yaml
scrape:
  concurrency: 50       # 並行請求數
  delay: 1.0            # 分會間等待秒數（同步版用）
  retry_attempts: 3     # 失敗自動重試次數

output:
  dir: output           # 輸出目錄
  filename: bni_members.csv
```

## 輸出格式

CSV 檔案，包含 24 個欄位：

| 欄位 | 說明 |
|------|------|
| country_id / country_name | 國家 |
| region_id / region_name | 區域 |
| chapter_id / chapter_name | 分會 |
| member_name | 會員姓名 |
| title | 職稱 |
| company | 公司名稱 |
| industry | 產業分類 |
| city | 城市 |
| phone / mobile | 電話 |
| address / postal_code | 地址 |
| website | 網站 |
| business_desc | 業務描述 |
| ideal_referral | 理想引薦 |
| best_product | 最佳產品 |
| top_problem_solved | 解決的問題 |
| ideal_referral_partner | 理想引薦夥伴 |
| favorite_story | 故事 |
| line_url | LINE 連結 |
| detail_url | 詳情頁連結 |

輸出檔名格式：`bni_members_{country}_{timestamp}.csv`

## 系統需求

- Python 3.8+
- Google Chrome（Selenium 自動管理 ChromeDriver）

## 效能參考（台灣）

| 項目 | 數值 |
|------|------|
| 區域數 | 31 |
| 分會數 | ~257 |
| 會員數 | ~9,800 |
| 分會發現 | ~2 分鐘（Selenium） |
| 會員抓取 + 詳情 | ~2 分鐘（concurrency=50） |
| 總耗時 | ~4 分鐘 |
