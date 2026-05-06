# BNI Global Member Scraper

English · [中文](README.md)

Scrape BNI member data across 44 countries, **fully async over plain HTTP — no browser required**.

## Architecture

`bni_scraper_async.py` (recommended) is built on pure aiohttp with no Selenium dependency:

1. **Regions** — `appsCmsNationalChapterSearchFilterJson` returns each country's regions
2. **Chapter discovery** — `appsCmsNationalMemberSearchFilterJson` (parallel) returns chapters per region
3. **Member listing** — `bnicms/v3/frontend/memberlist/display`, queried per chapter to bypass the 250-row cap
4. **Member detail** — `bnicms/v3/frontend/memberdetail/display` (parallel)

Chapters are discovered live on every run; no static data file required.

## Why query per chapter

The public memberlist endpoint **caps responses at 250 rows**. In Taiwan, 13 of 31 regions exceed that cap, so a region-level query silently drops nearly half the members (4,420 vs the real 9,662).

Per-chapter queries recover the full set:

| Query strategy | Taiwan members |
|---------------|----------------|
| Per region | 4,420 ❌ |
| **Per chapter** | **9,662 ✅** |

## Features

- 44 countries supported
- Interactive country menu (single / multi / all)
- Pure aiohttp parallelism — no Selenium / Chrome required
- Live chapter discovery on every run
- Auto-retry (default 3 attempts)
- Fallback: if a region's chapter listing fails, falls back to region-level so members aren't lost
- CSV output with formula-injection guard

## Quick Start

```bash
pip install -r requirements.txt
python bni_scraper_async.py
```

A country menu appears on launch. Press Enter for Taiwan (default), or type comma-separated numbers for multi-select.

### Custom concurrency

```bash
python bni_scraper_async.py 100   # override config.yaml's default of 50
```

## Three execution modes

| File | Mode | Dependencies | Notes |
|------|------|--------------|-------|
| `bni_scraper_async.py` | Pure async (**main**) | aiohttp | All-AJAX, fastest, no browser |
| `bni_scraper_parallel.py` | Threaded | Selenium + Chrome | Multiple parallel Chrome instances (legacy) |
| `bni_scraper.py` | Synchronous | Selenium + Chrome | Single-threaded (legacy, slowest but most stable) |

## Project layout

```
bni_core.py              # Shared core (HTML parser, CSV writer, Chrome driver factory)
bni_scraper_async.py     # Async scraper (main)
bni_scraper_parallel.py  # Threaded scraper (legacy)
bni_scraper.py           # Sync scraper (legacy)
config.yaml              # Settings
requirements.txt         # Python dependencies
```

## Configuration: config.yaml

```yaml
scrape:
  concurrency: 50       # Parallel request count (recommended 30–100)
  delay: 1.0            # Per-chapter pause in seconds (sync mode only)
  retry_attempts: 3     # Auto-retry on failure

output:
  dir: output           # Output directory
  filename: bni_members.csv
```

## Output format

CSV, UTF-8 with BOM, 24 columns:

| Field | Description |
|-------|-------------|
| country_id / country_name | Country |
| region_id / region_name | Region |
| chapter_id / chapter_name | Chapter |
| member_name | Member name |
| title | Title |
| company | Company |
| industry | Industry classification |
| city | City |
| phone / mobile | Phone numbers |
| address / postal_code | Address |
| website | Website |
| business_desc | Business description |
| ideal_referral | Ideal referral |
| best_product | Top product |
| top_problem_solved | Top problem solved |
| ideal_referral_partner | Ideal referral partner |
| favorite_story | Favorite story |
| line_url | LINE link |
| detail_url | Profile URL |

Filename: `output/bni_members_{country}_{timestamp}.csv`. When multiple countries are scraped, an aggregated `bni_members_all_{timestamp}.csv` is also produced.

## Requirements

- Python 3.8+
- `bni_scraper_async.py` does **not** need Chrome
- `bni_scraper.py` / `bni_scraper_parallel.py` need Google Chrome (ChromeDriver auto-managed by Selenium)

## Performance reference (Taiwan, 31 regions)

| Stage | Pure-AJAX | Selenium (legacy) |
|-------|-----------|-------------------|
| Region → chapter discovery | **1.4 s** | ~5 min |
| Member listing (254 chapters) | ~28 s | ~28 s |
| Member detail (9,662 members) | ~2 min | ~2 min |
| **Total** | **~2.6 min** | ~9 min |
| **Members recovered** | **9,662** | 9,647 |

Chapter discovery in the AJAX version is **~225× faster** than the Selenium version.

## License

Use according to the project's actual licensing. This tool only fetches member data already publicly listed on BNI's website.
