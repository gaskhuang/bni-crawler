"""
授權驗證模組 - 透過 Google Apps Script + Google Sheet 遠端驗證授權
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

CACHE_FILE = ".license_cache.json"
CACHE_TTL = 3600  # 1 小時內不重複驗證


def load_license_config(config_path="config.yaml"):
    """從 config.yaml 載入授權設定"""
    path = Path(config_path)
    if not path.exists():
        return None

    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("license")
    except ImportError:
        # 沒有 pyyaml，嘗試手動解析
        text = path.read_text(encoding="utf-8")
        license_cfg = {}
        in_license = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("license:"):
                in_license = True
                continue
            if in_license and stripped.startswith("api_url:"):
                license_cfg["api_url"] = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            elif in_license and stripped.startswith("key:"):
                license_cfg["key"] = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            elif in_license and not stripped.startswith((" ", "-", "")) and ":" in stripped:
                in_license = False
        return license_cfg if license_cfg else None
    except Exception as e:
        logger.warning("載入授權設定失敗: %s", e)
        return None


def _load_cache():
    """載入本地快取"""
    path = Path(CACHE_FILE)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("timestamp", 0) < CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _save_cache(result):
    """儲存驗證結果到本地快取"""
    try:
        data = {**result, "timestamp": time.time()}
        Path(CACHE_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def verify_license(config_path="config.yaml"):
    """
    驗證授權。回傳 (valid: bool, message: str, customer: str)
    若無授權設定則跳過驗證（開發模式）。
    """
    license_cfg = load_license_config(config_path)

    if not license_cfg:
        logger.info("未設定授權，以開發模式執行")
        return True, "開發模式", ""

    api_url = license_cfg.get("api_url", "")
    key = license_cfg.get("key", "")

    if not api_url or not key:
        logger.warning("授權設定不完整（缺少 api_url 或 key）")
        return False, "授權設定不完整", ""

    # 先檢查快取
    cache = _load_cache()
    if cache and cache.get("key") == key and cache.get("valid"):
        logger.info("授權驗證通過（快取）: %s", cache.get("customer", ""))
        return True, cache.get("message", ""), cache.get("customer", "")

    # 線上驗證
    try:
        resp = requests.get(api_url, params={"key": key}, timeout=10)
        resp.raise_for_status()
        result = resp.json()

        valid = result.get("valid", False)
        message = result.get("message", "")
        customer = result.get("customer", "")
        expire_date = result.get("expire_date", "")

        if valid:
            logger.info("授權驗證通過: %s (到期: %s)", customer, expire_date)
            _save_cache({"valid": True, "message": message, "customer": customer,
                         "expire_date": expire_date, "key": key})
        else:
            logger.warning("授權驗證失敗: %s", message)

        return valid, message, customer

    except requests.exceptions.ConnectionError:
        # 離線時用快取（即使過期）
        if cache and cache.get("key") == key and cache.get("valid"):
            logger.info("離線模式，使用快取授權: %s", cache.get("customer", ""))
            return True, "離線模式（快取授權）", cache.get("customer", "")
        logger.error("無法連線到授權伺服器，且無有效快取")
        return False, "無法連線到授權伺服器", ""

    except Exception as e:
        logger.error("授權驗證異常: %s", e)
        # 異常時也嘗試用快取
        if cache and cache.get("key") == key and cache.get("valid"):
            return True, "驗證異常，使用快取授權", cache.get("customer", "")
        return False, f"驗證異常: {e}", ""


def require_license(config_path="config.yaml"):
    """驗證授權，失敗則終止程式"""
    valid, message, customer = verify_license(config_path)
    if not valid:
        print(f"\n{'='*50}")
        print(f"  授權驗證失敗")
        print(f"  原因: {message}")
        print(f"  請聯繫管理員取得有效授權碼")
        print(f"{'='*50}\n")
        sys.exit(1)
    return customer
