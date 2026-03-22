"""
Windows 打包腳本 - 使用 PyInstaller 產生 .exe

在 Windows 上執行：
    pip install pyinstaller pyyaml aiohttp beautifulsoup4 requests
    python build_windows.py

產出：
    dist/BNI_Scraper/
    ├── BNI_Scraper.exe
    ├── config.yaml        (需手動填入授權碼)
    └── ...
"""

import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "bni_scraper_async.py")
DIST_DIR = os.path.join(SCRIPT_DIR, "dist", "BNI_Scraper")


def build():
    print("=" * 50)
    print("  BNI Scraper - Windows 打包")
    print("=" * 50)

    # 檢查 PyInstaller
    try:
        import PyInstaller
        print(f"  PyInstaller: {PyInstaller.__version__}")
    except ImportError:
        print("  安裝 PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # PyInstaller 打包
    print("\n  正在打包...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "BNI_Scraper",
        "--onedir",
        "--console",
        "--noconfirm",
        "--clean",
        # 加入額外檔案
        "--add-data", f"config.yaml{os.pathsep}.",
        "--add-data", f"license_check.py{os.pathsep}.",
        # Hidden imports
        "--hidden-import", "aiohttp",
        "--hidden-import", "bs4",
        "--hidden-import", "yaml",
        "--hidden-import", "requests",
        "--hidden-import", "license_check",
        MAIN_SCRIPT,
    ]

    subprocess.check_call(cmd, cwd=SCRIPT_DIR)

    # 複製設定檔到 dist（確保有最新版）
    config_src = os.path.join(SCRIPT_DIR, "config.yaml")
    config_dst = os.path.join(DIST_DIR, "config.yaml")
    if os.path.exists(config_src):
        shutil.copy2(config_src, config_dst)
        print(f"  已複製 config.yaml")

    # 複製 bni_chapters.csv（如果有的話）
    chapters_src = os.path.join(SCRIPT_DIR, "bni_chapters.csv")
    chapters_dst = os.path.join(DIST_DIR, "bni_chapters.csv")
    if os.path.exists(chapters_src):
        shutil.copy2(chapters_src, chapters_dst)
        print(f"  已複製 bni_chapters.csv")

    # 複製 bni_regions.csv
    regions_src = os.path.join(SCRIPT_DIR, "bni_regions.csv")
    regions_dst = os.path.join(DIST_DIR, "bni_regions.csv")
    if os.path.exists(regions_src):
        shutil.copy2(regions_src, regions_dst)
        print(f"  已複製 bni_regions.csv")

    # 建立啟動批次檔
    bat_path = os.path.join(DIST_DIR, "啟動爬蟲.bat")
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write('@echo off\n')
        f.write('chcp 65001 >nul\n')
        f.write('echo.\n')
        f.write('echo  BNI 全球會員爬蟲\n')
        f.write('echo  ==================\n')
        f.write('echo.\n')
        f.write('BNI_Scraper.exe\n')
        f.write('echo.\n')
        f.write('echo  按任意鍵關閉...\n')
        f.write('pause >nul\n')
    print(f"  已建立 啟動爬蟲.bat")

    print(f"\n{'='*50}")
    print(f"  打包完成!")
    print(f"  輸出: {DIST_DIR}")
    print(f"")
    print(f"  交付給客戶前，請先在 config.yaml 填入：")
    print(f"    license:")
    print(f'      api_url: "你的 Apps Script URL"')
    print(f'      key: "客戶的授權碼"')
    print(f"{'='*50}")


if __name__ == "__main__":
    build()
