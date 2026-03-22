"""Check if member data is in main page HTML"""
import requests
import re
from bs4 import BeautifulSoup

s = requests.Session()
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
})

resp = s.get('https://bni.com.tw/zh-TW/memberlist?countryIds=7699&regionId=31901&chapterName=38639', timeout=15)
print('Status:', resp.status_code, 'Length:', len(resp.text))

soup = BeautifulSoup(resp.text, 'html.parser')

# Find tables
tables = soup.find_all('table')
print(f'Tables found: {len(tables)}')

for i, table in enumerate(tables):
    rows = table.find_all('tr')
    print(f'  Table {i}: {len(rows)} rows, class={table.get("class")}')
    if rows:
        for row in rows[:3]:
            cells = row.find_all(['td', 'th'])
            texts = [c.get_text(strip=True)[:30] for c in cells]
            print(f'    {texts}')

# Also look for memberdetails links
links = soup.find_all('a', href=re.compile(r'memberdetails'))
print(f'\nMember detail links: {len(links)}')
if links:
    for link in links[:5]:
        print(f'  {link.get_text(strip=True)}: {link["href"][:80]}')

# Look for listtables class
listtables = soup.find_all(class_='listtables')
print(f'\n.listtables elements: {len(listtables)}')
