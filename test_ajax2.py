"""Find DataTables AJAX source URL from member list response"""
import requests
import re
import html as html_mod

s = requests.Session()
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
})

# Get memberlist page directly to find AJAX data source
resp = s.get('https://bni.com.tw/zh-TW/memberlist?countryIds=7699&regionId=31901&chapterName=38639', timeout=15)

# Look for ajax/sAjaxSource/data URLs in the page
patterns = [
    r'"sAjaxSource"\s*:\s*"([^"]+)"',
    r'"ajax"\s*:\s*"([^"]+)"',
    r'"ajax"\s*:\s*\{[^}]*"url"\s*:\s*"([^"]+)"',
    r'ajax.*?url[\'"]?\s*[:=]\s*[\'"]([^"\']+)',
    r'sAjaxSource[\'"]?\s*[:=]\s*[\'"]([^"\']+)',
    r'\/web\/open\/[a-zA-Z]+',
    r'\/bnicms\/[a-zA-Z/]+',
]

for pat in patterns:
    matches = re.findall(pat, resp.text)
    if matches:
        print(f'Pattern: {pat}')
        for m in matches[:5]:
            print(f'  Match: {m}')

# Also look in the AJAX widget response
m = re.search(r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text)
mws = html_mod.unescape(m.group(1)) if m else ''

ajax_resp = s.post('https://bni.com.tw/bnicms/v3/frontend/memberlist/display', data={
    'parameters': 'countryIds=7699&regionId=31901&chapterName=38639',
    'languages': '',
    'cmsv3': 'true',
    'mappedWidgetSettings': mws,
    'pageMode': 'Live_Site',
    'website_type': '1',
    'website_id': '10065',
}, timeout=15)

print('\n--- In AJAX response ---')
for pat in patterns:
    matches = re.findall(pat, ajax_resp.text)
    if matches:
        print(f'Pattern: {pat}')
        for m in matches[:5]:
            print(f'  Match: {m}')

# Print any URL-like patterns
print('\n--- All URLs in AJAX response ---')
urls = re.findall(r'(?:https?://|/)[a-zA-Z0-9/_\-\.?&=]+', ajax_resp.text)
for u in set(urls):
    if 'member' in u.lower() or 'data' in u.lower() or 'json' in u.lower() or 'open' in u.lower():
        print(f'  {u}')
