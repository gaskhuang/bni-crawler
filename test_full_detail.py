"""用指定會員 URL 測試完整資料擷取"""
import requests, re, json, html as html_mod
from bs4 import BeautifulSoup
from urllib.parse import urlparse

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://bni.com.tw/zh-TW/memberdetails",
    "Origin": "https://bni.com.tw",
})

resp = s.get("https://bni.com.tw/zh-TW/memberdetails", timeout=15)
m = re.search(r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text)
mws = html_mod.unescape(m.group(1)) if m else ""

AJAX_LANGUAGES = json.dumps({
    "availableLanguages": [{"type": "published", "url": "http://bni.com.tw/zh-TW/memberdetails",
        "descriptionKey": "繁體中文", "id": 8, "localeCode": "zh_TW"}],
    "activeLanguage": {"id": 8, "localeCode": "zh_TW", "descriptionKey": "繁體中文", "cookieBotCode": "zh"},
})

# 測試用戶指定的 URL
test_url = "https://bni.com.tw/zh-TW/memberdetails?encryptedMemberId=wptsgiEi%2FbJbqs9N6rxcVA%3D%3D&name=%E6%9E%97+%E7%91%9E%E7%9B%8AJull+Lin"
params = urlparse(test_url).query

ajax_resp = s.post("https://bni.com.tw/bnicms/v3/frontend/memberdetail/display", data={
    "parameters": params, "languages": AJAX_LANGUAGES, "cmsv3": "true",
    "mappedWidgetSettings": mws, "pageMode": "Live_Site",
    "website_type": "1", "website_id": "10065",
}, timeout=15)

print(f"AJAX status: {ajax_resp.status_code}, len: {len(ajax_resp.text)}")

soup = BeautifulSoup(ajax_resp.text, "html.parser")

# 顯示所有文字行
text = soup.get_text(separator="\n")
lines = [l.strip() for l in text.split("\n") if l.strip()]
print(f"\n=== 所有文字行 ({len(lines)} 行) ===")
for i, line in enumerate(lines):
    print(f"  [{i:2d}] {line[:80]}")

# 顯示所有 <a href="tel:">
print("\n=== tel: 連結 ===")
for a in soup.find_all("a", href=re.compile(r"^tel:")):
    print(f"  href={a['href']}, text={a.get_text(strip=True)}")

# 顯示所有 <strong> 標籤 (可能是欄位標題)
print("\n=== <strong> 標籤 ===")
for s_tag in soup.find_all("strong"):
    txt = s_tag.get_text(strip=True)
    if txt:
        # 取得同一 <li> 中後面的文字
        parent_li = s_tag.find_parent("li")
        if parent_li:
            full = parent_li.get_text(strip=True)
            print(f"  {txt}: {full}")
        else:
            print(f"  {txt}")

# 顯示 HTML 中含「傳真」的部分
print("\n=== 傳真相關 HTML ===")
for m in re.finditer(r'傳真', ajax_resp.text):
    start = max(0, m.start() - 50)
    end = min(len(ajax_resp.text), m.end() + 150)
    print(ajax_resp.text[start:end])
    print("---")
