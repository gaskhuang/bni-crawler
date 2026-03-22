"""分析 AJAX HTML 完整結構 - 找出所有欄位"""
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

# Test 2 members: 林瑞益 (has fax) and 黃俊凱 (has phone)
urls = [
    "encryptedMemberId=wptsgiEi%2FbJbqs9N6rxcVA%3D%3D&name=%E6%9E%97+%E7%91%9E%E7%9B%8AJull+Lin",
    "encryptedMemberId=xkZhBsBOtisUjZBa9dTTNg%3D%3D&name=%E9%BB%83+%E4%BF%8A%E5%87%B1Gask+huang",
]

for params in urls:
    ajax_resp = s.post("https://bni.com.tw/bnicms/v3/frontend/memberdetail/display", data={
        "parameters": params, "languages": AJAX_LANGUAGES, "cmsv3": "true",
        "mappedWidgetSettings": mws, "pageMode": "Live_Site",
        "website_type": "1", "website_id": "10065",
    }, timeout=15)

    soup = BeautifulSoup(ajax_resp.text, "html.parser")

    # Find all <ul> with phone/fax info
    print(f"\n{'='*60}")
    name_tag = soup.find("h3")
    if name_tag:
        print(f"會員: {name_tag.get_text(strip=True)}")

    # Find ALL <li> elements that contain tel: links or labels
    print("\n--- All <li> with contact info ---")
    for li in soup.find_all("li"):
        text = li.get_text(strip=True)
        # Look for phone/fax/mobile related
        a_tag = li.find("a", href=re.compile(r"^tel:"))
        strong = li.find("strong")
        if a_tag or (strong and strong.get_text(strip=True)):
            label = strong.get_text(strip=True) if strong else ""
            value = a_tag.get_text(strip=True) if a_tag else ""
            print(f"  label='{label}' value='{value}' full='{text[:60]}'")

    # Show the contact section HTML (around tel: links)
    print("\n--- Contact section HTML ---")
    for ul in soup.find_all("ul"):
        if ul.find("a", href=re.compile(r"^tel:")):
            print(ul.prettify()[:1000])
            break
