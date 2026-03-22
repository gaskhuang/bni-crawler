"""Debug AJAX detail scraping"""
import requests
import re
import json
import html as html_mod
from bs4 import BeautifulSoup
from urllib.parse import urlparse

BASE_URL = "https://bni.com.tw"
MEMBER_DETAIL_URL = f"{BASE_URL}/zh-TW/memberdetails"
MEMBER_DETAIL_AJAX = f"{BASE_URL}/bnicms/v3/frontend/memberdetail/display"

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

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": f"{BASE_URL}/zh-TW/memberdetails",
    "Origin": BASE_URL,
})

# Get mappedWidgetSettings
resp = s.get(MEMBER_DETAIL_URL, timeout=15)
print("Page status:", resp.status_code)

m = re.search(r'name="mappedWidgetSettings"\s+value="([^"]*)"', resp.text)
if m:
    mws = html_mod.unescape(m.group(1))
    print(f"MWS (primary): len={len(mws)}, value={mws[:100]}")
else:
    m = re.search(r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text)
    if m:
        mws = html_mod.unescape(m.group(1))
        print(f"MWS (alt): len={len(mws)}, value={mws[:100]}")
    else:
        mws = ""
        print("MWS: NOT FOUND")

# Test with a known member URL
test_url = "https://bni.com.tw/zh-TW/memberdetails?encryptedMemberId=xkZhBsBOtisUjZBa9dTTNg%3D%3D&name=%E9%BB%83+%E4%BF%8A%E5%87%B1Gask+huang"
parsed = urlparse(test_url)
params = parsed.query
print(f"\nParameters: {params[:80]}...")

ajax_resp = s.post(MEMBER_DETAIL_AJAX, data={
    "parameters": params,
    "languages": AJAX_LANGUAGES,
    "cmsv3": "true",
    "mappedWidgetSettings": mws,
    "pageMode": "Live_Site",
    "website_type": "1",
    "website_id": "10065",
}, timeout=15)

print(f"AJAX status: {ajax_resp.status_code}")
print(f"Response len: {len(ajax_resp.text)}")

if len(ajax_resp.text) > 50:
    soup = BeautifulSoup(ajax_resp.text, "html.parser")
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    print(f"Text lines: {len(lines)}")
    for line in lines[:15]:
        print(f"  {line}")

    # Phone
    phone = re.findall(r"電話\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*(\+\d[\d\s\-]+)", ajax_resp.text)
    print(f"\nPhone matches: {phone}")
else:
    print("Response too short:", ajax_resp.text)
