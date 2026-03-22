"""Test member list AJAX endpoint"""
import requests
import re
import html as html_mod
from bs4 import BeautifulSoup

s = requests.Session()
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
})

# 取得 memberlist 頁面的 mappedWidgetSettings
resp = s.get('https://bni.com.tw/zh-TW/memberlist?countryIds=7699&regionId=31901&chapterName=38639', timeout=15)
print('Page status:', resp.status_code)

# 找 mappedWidgetSettings
m = re.search(r'name="mappedWidgetSettings"\s+value="([^"]*)"', resp.text)
if m:
    mws = html_mod.unescape(m.group(1))
    print('mappedWidgetSettings found, len:', len(mws))
else:
    m = re.search(r"mappedWidgetSettings['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text)
    if m:
        mws = html_mod.unescape(m.group(1))
        print('mappedWidgetSettings (alt), len:', len(mws))
    else:
        print('mappedWidgetSettings NOT found')
        mws = ''

# 找 languages
m2 = re.search(r'name="languages"\s+value="([^"]*)"', resp.text)
if m2:
    langs = html_mod.unescape(m2.group(1))
    print('languages found, len:', len(langs))
else:
    langs = ''
    print('languages NOT found, trying alt...')
    m2 = re.search(r"languages['\"]?\s*[:=]\s*['\"]([^'\"]+)", resp.text)
    if m2:
        langs = html_mod.unescape(m2.group(1))
        print('languages (alt), len:', len(langs))

# AJAX call
params = 'countryIds=7699&regionId=31901&chapterName=38639&chapterArea=&memberKeywords=&chapterCity=&memberFirstName=&memberLastName=&memberCompany='
ajax_resp = s.post('https://bni.com.tw/bnicms/v3/frontend/memberlist/display', data={
    'parameters': params,
    'languages': langs,
    'cmsv3': 'true',
    'mappedWidgetSettings': mws,
    'pageMode': 'Live_Site',
    'website_type': '1',
    'website_id': '10065',
}, timeout=15)
print('\nAJAX status:', ajax_resp.status_code)
print('Response len:', len(ajax_resp.text))

if 'memberdetails' in ajax_resp.text:
    print('Contains member links!')
    links = re.findall(r'href="([^"]*memberdetails[^"]*)"', ajax_resp.text)
    print('Member links:', len(links))

    # Parse with BS4
    soup = BeautifulSoup(ajax_resp.text, 'html.parser')
    rows = soup.select('table tbody tr')
    print(f'Table rows: {len(rows)}')
    if rows:
        for row in rows[:3]:
            cells = row.find_all('td')
            texts = [c.get_text(strip=True) for c in cells]
            print(f'  Row: {texts[:4]}')
else:
    print('No member links found')
    print('First 1000 chars:', ajax_resp.text[:1000])
