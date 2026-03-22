"""Debug AJAX HTML structure"""
import requests, re, json, html as html_mod
from bs4 import BeautifulSoup
from urllib.parse import urlparse

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0",
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

params = "encryptedMemberId=xkZhBsBOtisUjZBa9dTTNg%3D%3D&name=%E9%BB%83+%E4%BF%8A%E5%87%B1Gask+huang"
ajax_resp = s.post("https://bni.com.tw/bnicms/v3/frontend/memberdetail/display", data={
    "parameters": params, "languages": AJAX_LANGUAGES, "cmsv3": "true",
    "mappedWidgetSettings": mws, "pageMode": "Live_Site",
    "website_type": "1", "website_id": "10065",
}, timeout=15)

# Save raw HTML
with open("ajax_detail_response.html", "w") as f:
    f.write(ajax_resp.text)

soup = BeautifulSoup(ajax_resp.text, "html.parser")
text = soup.get_text(separator="\n")
lines = [l.strip() for l in text.split("\n") if l.strip()]
print("=== All text lines ===")
for i, line in enumerate(lines):
    print(f"  [{i}] {line}")

# Find phone in HTML context
print("\n=== Phone context in HTML ===")
for m in re.finditer(r'\+886', ajax_resp.text):
    start = max(0, m.start() - 100)
    end = min(len(ajax_resp.text), m.end() + 50)
    print(ajax_resp.text[start:end])
    print("---")
