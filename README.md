# BNI 全球會員爬蟲

[English](README.en.md) · 中文

支援 44 個國家的 BNI 會員資料爬取，**純非同步 HTTP（Zero Selenium）**。

## 架構

`bni_scraper_async.py`（推薦使用）採用純 aiohttp 架構，完全不需要瀏覽器：

1. **區域列表**：呼叫 `appsCmsNationalChapterSearchFilterJson` 取得各國區域
2. **分會發現**：呼叫 `appsCmsNationalMemberSearchFilterJson`（並行）取得各區域分會
3. **會員列表**：呼叫 `bnicms/v3/frontend/memberlist/display`（每分會獨立查詢，避免 250 筆截斷）
4. **會員詳情**：呼叫 `bnicms/v3/frontend/memberdetail/display`（並行）

每次執行自動發現所有分會，不依賴靜態資料檔。

## 為什麼按分會查詢

BNI 公開 API 的會員列表 endpoint **每次最多回傳 250 筆**。台灣 31 個區域中有多達 13 個會員數超過 250，若按區域查詢會漏抓近 50% 的會員（4,420 vs 真實 9,662）。

改成按分會（chapter）查詢後可拿到完整資料：

| 查詢方式 | 台灣會員數 |
|---------|-----------|
| 按區域 | 4,420 ❌ |
| **按分會** | **9,662 ✅** |

## 功能

- 44 個國家的 BNI 會員資料爬取
- 互動式國家選單（單選 / 多選 / 全選）
- 純 aiohttp 並行抓取，無需 Selenium / Chrome
- 動態分會發現（每次執行自動偵測最新分會）
- 自動重試機制（預設 3 次）
- 失敗 fallback：若某區域分會查詢失敗，退回 region 模式避免漏抓
- CSV 輸出含公式注入防護

## 快速開始

```bash
pip install -r requirements.txt
python bni_scraper_async.py
```

啟動後顯示國家選單，直接按 Enter 預設爬取台灣，輸入編號（可逗號分隔）多選。

### 指定併發數

```bash
python bni_scraper_async.py 100   # 100 並發（預設讀 config.yaml 的 50）
```

## 三種執行模式

| 檔案 | 模式 | 依賴 | 說明 |
|------|------|------|------|
| `bni_scraper_async.py` | 純非同步（**主力**） | aiohttp | 純 AJAX，速度最快，無需瀏覽器 |
| `bni_scraper_parallel.py` | 多線程 | Selenium + Chrome | 多 Chrome 實例並行（舊版） |
| `bni_scraper.py` | 同步 | Selenium + Chrome | 單線程逐一抓取（舊版，最穩定但最慢） |

## 專案結構

```
bni_core.py              # 共用核心模組（HTML 解析、CSV 輸出、Chrome driver factory）
bni_scraper_async.py     # 非同步版（主力）
bni_scraper_parallel.py  # 多線程版（舊）
bni_scraper.py           # 同步版（舊）
config.yaml              # 設定檔
requirements.txt         # Python 依賴
```

## 設定檔 config.yaml

```yaml
scrape:
  concurrency: 50       # 並行請求數（建議 30–100）
  delay: 1.0            # 分會間等待秒數（同步版用）
  retry_attempts: 3     # 失敗自動重試次數

output:
  dir: output           # 輸出目錄
  filename: bni_members.csv
```

## 輸出格式

CSV，UTF-8 with BOM，包含 24 個欄位：

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

輸出檔名：`output/bni_members_{country}_{timestamp}.csv`，多國時另外輸出 `bni_members_all_{timestamp}.csv`。

## 系統需求

- Python 3.8+
- `bni_scraper_async.py` 不需要 Chrome
- `bni_scraper.py` / `bni_scraper_parallel.py` 需要 Google Chrome（Selenium 自動管理 ChromeDriver）

## 效能參考（台灣，31 區）

| 階段 | 純 AJAX 版 | Selenium 版（舊） |
|------|-----------|------------------|
| 區域 → 分會發現 | **1.4 秒** | ~5 分鐘 |
| 會員列表（254 個分會） | ~28 秒 | ~28 秒 |
| 會員詳情（9,662 位） | ~2 分鐘 | ~2 分鐘 |
| **總耗時** | **~2.6 分鐘** | ~9 分鐘 |
| **會員回收率** | **9,662** | 9,647 |

純 AJAX 版本在 chapter discovery 階段比 Selenium 版**快 225 倍**。

## License

請依專案實際授權使用，本工具僅抓取 BNI 公開網站上已公開揭露的會員資料。
