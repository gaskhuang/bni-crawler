@echo off
chcp 65001 >nul
echo.
echo  BNI Scraper - Windows 打包工具
echo  ================================
echo.

REM 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [錯誤] 找不到 Python，請先安裝 Python 3.10+
    echo  下載: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 安裝依賴
echo  安裝依賴套件...
pip install pyinstaller aiohttp beautifulsoup4 requests pyyaml

echo.
echo  開始打包...
python build_windows.py

echo.
pause
