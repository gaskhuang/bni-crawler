"""Test new parser with 2 members"""
import sys, json
sys.path.insert(0, '.')
from bni_scraper_parallel import create_ajax_session, scrape_member_detail_ajax

session, mws = create_ajax_session()

# Test member 1: 林瑞益 (has fax)
m1 = {
    "member_name": "林 瑞益",
    "detail_url": "https://bni.com.tw/zh-TW/memberdetails?encryptedMemberId=wptsgiEi%2FbJbqs9N6rxcVA%3D%3D&name=%E6%9E%97+%E7%91%9E%E7%9B%8AJull+Lin",
}
scrape_member_detail_ajax(session, mws, m1)

print("=== 林瑞益 ===")
for k, v in m1.items():
    if k == "detail_url":
        continue
    val = str(v)[:80] if v else "(空)"
    print(f"  {k}: {val}")

# Test member 2: 黃俊凱 (has phone)
m2 = {
    "member_name": "黃 俊凱",
    "detail_url": "https://bni.com.tw/zh-TW/memberdetails?encryptedMemberId=xkZhBsBOtisUjZBa9dTTNg%3D%3D&name=%E9%BB%83+%E4%BF%8A%E5%87%B1Gask+huang",
}
scrape_member_detail_ajax(session, mws, m2)

print("\n=== 黃俊凱 ===")
for k, v in m2.items():
    if k == "detail_url":
        continue
    val = str(v)[:80] if v else "(空)"
    print(f"  {k}: {val}")
