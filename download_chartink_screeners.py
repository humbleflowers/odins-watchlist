"""
Download Chartink screener results and merge into a single CSV.

Primary method: intercept the /screener/process API payload via browser
network logs and re-post with rows=5000 to fetch all stocks (not just the
first page of 20 shown in the UI).

Fallbacks: UI CSV button, then DOM table scrape (first page only).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
import yaml
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

CHARTINK_HOST = "https://chartink.com"
PROCESS_URL = f"{CHARTINK_HOST}/screener/process"
MAX_SCREENER_ROWS = 5000
PAGE_LOAD_TIMEOUT = 45
DRIVER_RESTART_EVERY = 6  # fresh browser every N screeners to avoid memory hangs
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)

COLUMN_ALIASES = {
    "Symbol": ["Symbol", "nsecode", "symbol"],
    "Stock Name": ["Stock Name", "name", "stockname"],
    "% Chg": ["% Chg", "%Chg", "%_change", "scan-column-default-percent-change"],
    "Price": ["Price", "close", "scan-column-default-close"],
    "Volume": ["Volume", "volume", "scan-column-default-volume"],
}

MERGED_COLUMNS = [
    "Stock Name", "Symbol", "% Chg", "Price", "Volume", "Frequency", "Screener",
]


# ---------------------------------------------------------------------------
# Column normalization
# ---------------------------------------------------------------------------

def normalize_screener_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map Chartink API / DOM / UI CSV column names to a common schema."""
    df = df.copy()
    lower_cols = {c.lower(): c for c in df.columns}
    for standard, aliases in COLUMN_ALIASES.items():
        if standard in df.columns:
            continue
        for alias in aliases:
            if alias in df.columns:
                df = df.rename(columns={alias: standard})
                break
            if alias.lower() in lower_cols:
                df = df.rename(columns={lower_cols[alias.lower()]: standard})
                break
    if "% Chg" in df.columns:
        df["% Chg"] = pd.to_numeric(
            df["% Chg"].astype(str).str.replace("%", "", regex=False),
            errors="coerce",
        )
    for col in ("Price", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )
    return df


def screener_name_from_stem(stem: str) -> str:
    """Strip timestamp suffix from a downloaded CSV filename stem."""
    m = re.match(r"^(.*)_\d{8}_\d{6}$", stem)
    return m.group(1) if m else stem.rsplit("_", 1)[0]


# ---------------------------------------------------------------------------
# Selenium setup
# ---------------------------------------------------------------------------

def load_yaml(file_path: str) -> dict:
    with open(file_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg.get("screener_urls"), dict):
        raise ValueError("YAML must contain top-level 'screener_urls' mapping")
    return cfg


def setup_download_directory(base_dir: str, dt: str) -> str:
    download_dir = os.path.join(base_dir, dt)
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    return download_dir


def setup_driver(download_dir: str, chromedriver_path: Optional[str]) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"  # don't wait for ads/images — API fires earlier
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"user-agent={UA}")
    options.add_experimental_option("prefs", {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    })
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    service = Service(chromedriver_path) if chromedriver_path else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(30)
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": os.path.abspath(download_dir),
        })
    except Exception:
        pass
    return driver


def quit_driver(driver: Optional[webdriver.Chrome]) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def restart_driver(
    driver: Optional[webdriver.Chrome],
    download_dir: str,
    chromedriver_path: Optional[str],
) -> webdriver.Chrome:
    """Kill and recreate the browser — recovers from hung ChromeDriver sessions."""
    quit_driver(driver)
    time.sleep(1)
    print("[INFO] Restarting browser session...")
    return setup_driver(download_dir, chromedriver_path)


def safe_get(driver: webdriver.Chrome, url: str) -> None:
    """Navigate to url; on slow pages stop loading and continue with partial DOM."""
    try:
        driver.get(url)
    except TimeoutException:
        print(f"[INFO] Page load timed out ({PAGE_LOAD_TIMEOUT}s), using partial page...")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backend API fetch (preferred — all rows)
# ---------------------------------------------------------------------------

def clear_perf_logs(driver: webdriver.Chrome) -> None:
    try:
        driver.get_log("performance")
    except Exception:
        pass


def capture_screener_process_payload(driver: webdriver.Chrome, timeout: int = 15) -> Optional[dict]:
    """Capture the JSON body Chartink posts to /screener/process after page load."""
    end = time.time() + timeout
    while time.time() < end:
        for entry in driver.get_log("performance"):
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.requestWillBeSent":
                continue
            req = msg.get("params", {}).get("request", {})
            if PROCESS_URL not in req.get("url", "") or not req.get("postData"):
                continue
            try:
                return json.loads(req["postData"])
            except Exception:
                continue
        time.sleep(0.5)
    return None


def _extract_csrf(html: str) -> Optional[str]:
    m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', html, re.I)
    return m.group(1) if m else None


def _requests_session_from_driver(driver: webdriver.Chrome) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    for cookie in driver.get_cookies():
        if "chartink" in (cookie.get("domain") or ""):
            sess.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain") or "chartink.com",
                path=cookie.get("path") or "/",
            )
    return sess


def _save_screener_csv(df: pd.DataFrame, name: str, download_dir: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    out = os.path.join(download_dir, f"{safe}_{ts}.csv")
    df.to_csv(out, index=False)
    return out


def backend_fetch_current_scan(driver: webdriver.Chrome, name: str, download_dir: str) -> Optional[str]:
    """Fetch all screener rows via Chartink's /screener/process API."""
    html = driver.page_source
    csrf = _extract_csrf(html)
    payload = capture_screener_process_payload(driver)
    if not payload:
        return None

    payload = dict(payload)
    payload["rows"] = MAX_SCREENER_ROWS

    sess = _requests_session_from_driver(driver)
    sess.headers.update({
        "Content-Type": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "Referer": driver.current_url,
    })
    if csrf:
        sess.headers["x-csrf-token"] = csrf

    resp = sess.post(PROCESS_URL, json=payload, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    if not rows:
        return None

    out = _save_screener_csv(normalize_screener_columns(pd.DataFrame(rows)), name, download_dir)
    print(f"Downloaded {len(rows)} rows via backend: {out}")
    return out


# ---------------------------------------------------------------------------
# UI / DOM fallbacks (first page only)
# ---------------------------------------------------------------------------

def click_csv_button(driver: webdriver.Chrome, timeout: int = 25) -> bool:
    wait = WebDriverWait(driver, timeout)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
    except Exception:
        pass
    for by, sel in [
        (By.XPATH, "//div[contains(@class,'secondary-button')]//span[normalize-space()='CSV']"),
        (By.CSS_SELECTOR, "div.secondary-button i.fa-file-csv"),
    ]:
        try:
            el = wait.until(EC.element_to_be_clickable((by, sel)))
            try:
                btn = el.find_element(By.XPATH, "./ancestor::div[contains(@class,'secondary-button')]")
            except Exception:
                btn = el
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            driver.execute_script("arguments[0].click();", btn)
            return True
        except Exception:
            continue
    return False


def wait_for_download(download_dir: str, after_ts: float, timeout: int = 90) -> Optional[str]:
    end = time.time() + timeout
    last_size, stable = -1, 0
    while time.time() < end:
        files = [
            os.path.join(download_dir, f) for f in os.listdir(download_dir)
            if f.endswith((".csv", ".crdownload")) and os.path.getmtime(
                os.path.join(download_dir, f)
            ) >= after_ts
        ]
        if files:
            target = max(files, key=os.path.getmtime)
            size = os.path.getsize(target)
            if size == last_size and target.endswith(".csv"):
                stable += 1
                if stable >= 2:
                    return target
            else:
                last_size, stable = size, 0
        time.sleep(1)
    return None


def dom_scrape_to_csv(driver: webdriver.Chrome, name: str, download_dir: str) -> Optional[str]:
    js = """
    const tables = Array.from(document.querySelectorAll('table'));
    const t = tables.find(tbl => tbl.querySelectorAll('thead th').length >= 2
        && tbl.querySelectorAll('tbody td').length > 0);
    if (!t) return null;
    const headers = [...t.querySelectorAll('thead th')].map(th => th.innerText.trim());
    const rows = [...t.querySelectorAll('tbody tr')].map(tr =>
        [...tr.querySelectorAll('td')].map(td => td.innerText.trim()));
    return JSON.stringify({headers, rows});
    """
    try:
        raw = driver.execute_script(js)
        if not raw:
            return None
        obj = json.loads(raw)
        headers, rows = obj.get("headers") or [], obj.get("rows") or []
        if not rows:
            return None
        fixed = [
            r + [""] * (len(headers) - len(r)) if len(r) < len(headers) else r[:len(headers)]
            for r in rows
        ]
        return _save_screener_csv(
            normalize_screener_columns(pd.DataFrame(fixed, columns=headers)),
            name, download_dir,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Download orchestration
# ---------------------------------------------------------------------------

def download_csvs(driver: webdriver.Chrome, screener_urls: Dict[str, str], download_dir: str) -> None:
    for name, url in screener_urls.items():
        print(f"Visiting: {name} -> {url}")
        clear_perf_logs(driver)
        driver.get(url)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )
        except Exception:
            pass

        # 1) Backend API (all rows)
        try:
            out = backend_fetch_current_scan(driver, name, download_dir)
            if out and os.path.exists(out):
                continue
            print(f"[INFO] {name}: Backend unavailable; trying UI CSV.")
        except Exception as exc:
            print(f"[INFO] {name}: Backend error ({exc}); trying UI CSV.")

        # 2) UI CSV button (first page)
        start = time.time()
        if click_csv_button(driver):
            out_path = wait_for_download(download_dir, after_ts=start, timeout=120)
            if out_path:
                new_name = _save_screener_csv(
                    normalize_screener_columns(pd.read_csv(out_path)), name, download_dir
                )
                os.remove(out_path)
                print(f"Downloaded via UI: {new_name}")
                continue
            print(f"[INFO] {name}: UI CSV failed; scraping DOM.")

        # 3) DOM scrape (first page)
        out = dom_scrape_to_csv(driver, name, download_dir)
        if out:
            print(f"Saved via DOM scrape: {out}")
        else:
            print(f"[WARN] {name}: All download methods failed.")


def merge_downloaded_csvs(download_dir: str) -> str:
    """Merge per-screener CSVs into one deduplicated file grouped by Symbol."""
    frames: List[pd.DataFrame] = []
    for path in Path(download_dir).glob("*.csv"):
        if path.name == "merged_chartink_output.csv":
            continue
        try:
            df = normalize_screener_columns(pd.read_csv(path))
            df.insert(0, "Screener", screener_name_from_stem(path.stem))
            frames.append(df)
        except Exception as exc:
            print(f"[WARN] Skipped {path.name}: {exc}")

    if not frames:
        raise RuntimeError("No CSVs to merge.")

    df_all = pd.concat(frames, ignore_index=True)
    if "Symbol" not in df_all.columns:
        out_csv = os.path.join(download_dir, "merged_chartink_output.csv")
        df_all.to_csv(out_csv, index=False)
        return out_csv

    agg = {"Screener": lambda x: ", ".join(sorted(set(x)))}
    for col in ("Stock Name", "% Chg", "Price", "Volume"):
        if col in df_all.columns:
            agg[col] = "first"

    grouped = df_all.groupby("Symbol", as_index=False).agg(agg)
    grouped["Frequency"] = grouped["Screener"].str.count(",") + 1
    final_df = grouped[[c for c in MERGED_COLUMNS if c in grouped.columns]]

    out_csv = os.path.join(download_dir, "merged_chartink_output.csv")
    final_df.to_csv(out_csv, index=False)
    print(f"Merged CSV saved to: {out_csv} ({len(final_df)} symbols)")
    return out_csv


def process_chartink_download(
    chromedriver_path: Optional[str],
    config_path: str,
    base_dir: str,
    dt: str,
) -> str:
    """
    Download all configured Chartink screeners for date folder `dt`.

    Returns:
        Path to merged_chartink_output.csv
    """
    screener_urls: Dict[str, str] = load_yaml(config_path)["screener_urls"]
    download_dir = setup_download_directory(base_dir, dt)
    driver = setup_driver(download_dir, chromedriver_path)
    try:
        download_csvs(driver, screener_urls, download_dir)
        return merge_downloaded_csvs(download_dir)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download Chartink screener results")
    parser.add_argument("--config", default="config/chartink_screeners_url.yaml")
    parser.add_argument("--chromedriver", default=None)
    parser.add_argument("--outdir", default="chartink_downloads")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-07-06")
    args = parser.parse_args()
    process_chartink_download(args.chromedriver, args.config, args.outdir, args.date)
