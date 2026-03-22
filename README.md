# BNI 全球會員爬蟲

支援 44 個國家的 BNI 會員資料爬取，純非同步 HTTP（不需瀏覽器），內建授權驗證機制。

## 功能

- 44 個國家的 BNI 會員資料爬取
- 互動式國家選單（單選/多選/全選）
- 純 aiohttp 非同步，無需 Selenium/Chrome
- Google Sheet 遠端授權控制
- 支援 Windows .exe 打包

## 快速開始

```bash
pip install -r requirements.txt
python bni_scraper_async.py
```

## 授權驗證機制

### 1. 建立 Google Sheet

| 授權碼 | 客戶名稱 | 狀態 | 到期日 |
|--------|----------|------|--------|
| ABC-12345 | 王先生 | 1 | 2026-12-31 |
| XYZ-99999 | 李小姐 | 0 | 2026-06-30 |

- 狀態: `1` = 啟用, `0` = 停用
- 到期日過期也會自動擋

### 2. 部署 Google Apps Script

1. 在 Sheet 上方 → **擴充功能** → **Apps Script**
2. 將 `google_apps_script.js` 的內容貼進去
3. **部署** → **新增部署** → **網頁應用程式**
   - 執行身分: 我
   - 誰可以存取: **所有人**
4. 複製部署後的 URL

### 3. 設定 config.yaml

```yaml
license:
  api_url: "你部署的 Apps Script URL"
  key: "客戶的授權碼"
```

### 管理方式

在 Google Sheet 中直接修改即可遠端控制：
- 改狀態為 `0` → 立即停用
- 改到期日 → 控制使用期限
- E/F 欄自動記錄存取時間和結果

## Windows 打包

```bash
# 在 Windows 上執行
build_windows.bat

# 或手動
pip install pyinstaller
python build_windows.py
```

產出在 `dist/BNI_Scraper/` 資料夾，交付給客戶前記得填入 `config.yaml` 的授權碼。
