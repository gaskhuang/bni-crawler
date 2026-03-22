"""小範圍測試多線程爬蟲 - 2 workers 抓 4 個分會"""
import csv
import time
import sys
sys.path.insert(0, '.')
from bni_scraper_parallel import (
    scrape_chapter, create_ajax_session, get_driver_path,
    save_members_csv, process_chapter_worker, write_lock,
    all_members, stats, load_chapters_csv
)
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

logger = logging.getLogger(__name__)

# 預先下載 chromedriver
get_driver_path()

# 從 CSV 取幾個未完成的分會測試
chapters = load_chapters_csv()

# 挑 4 個不同區域的分會測試
test_chapters = []
seen_regions = set()
for ch in chapters:
    if ch["region_name"] not in seen_regions and len(test_chapters) < 4:
        test_chapters.append(ch)
        seen_regions.add(ch["region_name"])

print(f"測試 {len(test_chapters)} 個分會:")
for ch in test_chapters:
    print(f"  {ch['region_name']} > {ch['chapter_name']}")

stats["chapters_total"] = len(test_chapters)
stats["chapters_done"] = 0

# 2 workers 並行
start = time.time()
num_workers = 2
print(f"\n開始測試 ({num_workers} workers)...")

with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="W") as executor:
    futures = {executor.submit(process_chapter_worker, ch): ch for ch in test_chapters}
    for future in as_completed(futures):
        ch = futures[future]
        try:
            result = future.result()
        except Exception as e:
            print(f"Error: {ch['chapter_name']}: {e}")

elapsed = time.time() - start
print(f"\n完成! {len(all_members)} 位會員，耗時 {elapsed:.1f} 秒 ({elapsed/60:.1f} 分鐘)")
print(f"平均每分會: {elapsed/len(test_chapters):.1f} 秒")

# 檢查資料品質
has_phone = sum(1 for m in all_members if m.get("phone"))
has_detail = sum(1 for m in all_members if m.get("business_desc"))
print(f"有電話: {has_phone}/{len(all_members)}")
print(f"有業務描述: {has_detail}/{len(all_members)}")
