"""Dump full AJAX response to find data source"""
import requests
import re
import html as html_mod

s = requests.Session()
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
})

resp = s.get('https://bni.com.tw/zh-TW/memberlist?countryIds=7699&regionId=31901&chapterName=38639', timeout=15)

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

# Save full response
with open('ajax_memberlist_response.html', 'w') as f:
    f.write(ajax_resp.text)
print(f'Saved {len(ajax_resp.text)} chars')

# Look for sAjaxSource or data loading URL
for line in ajax_resp.text.split('\n'):
    line = line.strip()
    if any(kw in line.lower() for kw in ['ajaxsource', 'ajax', 'url', 'datasrc', 'aadata', 'serverside']):
        if len(line) < 200:
            print(line)
