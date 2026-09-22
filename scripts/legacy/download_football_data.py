#!/usr/bin/env python3
"""
Downloads 5 years (2021/22 - 2025/26) of football match data from football-data.co.uk:
 - 22 main European league divisions (England, Scotland, Germany, Italy, Spain, France,
   Netherlands, Belgium, Portugal, Turkey, Greece) - full match results, match stats
   (shots, corners, fouls, cards) and betting odds, one .zip per season (all divisions inside).
 - 16 "extra" worldwide leagues (Argentina, Austria, Brazil, China, Denmark, Finland,
   Ireland, Japan, Mexico, Norway, Poland, Romania, Russia, Sweden, Switzerland, USA) -
   results + odds, one combined CSV per country (all seasons; script filters to last 5 years).

Usage:
    python3 download_football_data.py

Requires only the Python standard library (urllib, zipfile, csv). No pip installs needed.
Optionally uses pandas (if installed) to build one consolidated CSV at the end.

Output layout (created in the repo's data/ folder, one level up from this script):
    data/
        main/<SEASON>/<CODE>.csv            <- e.g. data/main/2425/E0.csv
        extra/<CODE>.csv                    <- e.g. data/extra/RUS.csv (all seasons, unfiltered)
        combined_main_leagues.csv           <- all main leagues+seasons, if pandas available
        combined_extra_leagues.csv          <- all extra leagues (last 5 yrs), if pandas available
"""

import os
import re
import io
import csv
import sys
import time
import zipfile
import urllib.request
import urllib.error
import pandas as pd

BASE = "https://www.football-data.co.uk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Last 5 seasons (2021/22 through 2025/26) in football-data.co.uk's URL format
SEASONS = ["2122", "2223", "2324", "2425", "2526", "2627"]
SEASON_LABELS = {
    "2122": "2021-22", "2223": "2022-23", "2324": "2023-24",
    "2425": "2024-25", "2526": "2025-26", "2627": "2026-27",
}

# "Extra" worldwide leagues: (country page slug, human name)
EXTRA_COUNTRIES = [
    ("argentina", "Argentina"), ("austria", "Austria"), ("brazil", "Brazil"),
    ("china", "China"), ("denmark", "Denmark"), ("finland", "Finland"),
    ("ireland", "Ireland"), ("japan", "Japan"), ("mexico", "Mexico"),
    ("norway", "Norway"), ("poland", "Poland"), ("romania", "Romania"),
    ("russia", "Russia"), ("sweden", "Sweden"), ("switzerland", "Switzerland"),
    ("usa", "USA"),
]

# Division code -> (country, league name). The combined files carry these so the
# modelling pipeline can group by country/league without re-deriving them.
LEAGUE_INFO = {
    "B1": ("Belgium", "Pro League"),
    "D1": ("Germany", "Bundesliga"),
    "D2": ("Germany", "Bundesliga 2"),
    "E0": ("England", "Premier League"),
    "E1": ("England", "Championship"),
    "E2": ("England", "League One"),
    "E3": ("England", "League Two"),
    "EC": ("England", "National League"),
    "F1": ("France", "Ligue 1"),
    "F2": ("France", "Ligue 2"),
    "G1": ("Greece", "Super League"),
    "I1": ("Italy", "Serie A"),
    "I2": ("Italy", "Serie B"),
    "N1": ("Netherlands", "Eredivisie"),
    "P1": ("Portugal", "Primeira Liga"),
    "SC0": ("Scotland", "Premiership"),
    "SC1": ("Scotland", "Championship"),
    "SC2": ("Scotland", "League One"),
    "SC3": ("Scotland", "League Two"),
    "SP1": ("Spain", "La Liga"),
    "SP2": ("Spain", "La Liga 2"),
    "T1": ("Turkey", "Super Lig"),
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)              # <repo> (scripts/ lives one level down)
OUT_DIR = os.path.join(ROOT_DIR, "data")
RAW_MAIN_DIR = os.path.join(OUT_DIR, "main")
RAW_EXTRA_DIR = os.path.join(OUT_DIR, "extra")



def _read_csv_any(path):
    """Read a football-data CSV whatever encoding it happens to use.

    These files are mostly UTF-8 with a BOM, but a few (e.g. EC.csv) contain
    cp1252 smart quotes that break a strict UTF-8 read. Whichever encoding wins,
    any BOM left glued to the first column name is stripped, otherwise 'Div'
    silently becomes two different columns across files.
    """
    last = None
    for enc in ("utf-8-sig", "cp1252", "latin1"):
        try:
            df = pd.read_csv(path, encoding=enc, on_bad_lines="skip")
            break
        except UnicodeDecodeError as e:
            last = e
    else:
        raise last
    df.columns = [str(c).lstrip("\ufeff").replace("\u00ef\u00bb\u00bf", "") for c in df.columns]
    return df

def fetch(url, retries=4, timeout=30):
    """GET a URL with retries + backoff. Returns raw bytes."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            wait = 2 * attempt
            print(f"    ! attempt {attempt}/{retries} failed ({e}); retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def download_main_leagues():
    print("\n=== Main leagues (22 divisions x 5 seasons, via season ZIPs) ===")
    os.makedirs(RAW_MAIN_DIR, exist_ok=True)
    ok, failed = [], []
    for season in SEASONS:
        url = f"{BASE}/mmz4281/{season}/data.zip"
        dest_dir = os.path.join(RAW_MAIN_DIR, season)
        os.makedirs(dest_dir, exist_ok=True)
        print(f"  Season {SEASON_LABELS[season]}: downloading {url}")
        try:
            data = fetch(url)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                zf.extractall(dest_dir, members=names)
            print(f"    -> extracted {len(names)} CSV files to {dest_dir}")
            ok.append(season)
        except Exception as e:
            print(f"    ! FAILED: {e}")
            failed.append(season)
    return ok, failed


def find_extra_csv_code(country_slug):
    """Scrape the country page to find the exact CSV code, e.g. 'new/RUS.csv' -> 'RUS'."""
    page_url = f"{BASE}/{country_slug}.php"
    html = fetch(page_url).decode("utf-8", errors="ignore")
    m = re.search(r'new/([A-Za-z]+)\.csv', html)
    if not m:
        raise RuntimeError(f"Could not find CSV code on {page_url}")
    return m.group(1)


def download_extra_leagues():
    print("\n=== Extra leagues (16 countries, combined-history CSV each) ===")
    os.makedirs(RAW_EXTRA_DIR, exist_ok=True)
    ok, failed = [], []
    for slug, name in EXTRA_COUNTRIES:
        try:
            code = find_extra_csv_code(slug)
            url = f"{BASE}/new/{code}.csv"
            print(f"  {name}: found code '{code}', downloading {url}")
            data = fetch(url)
            dest = os.path.join(RAW_EXTRA_DIR, f"{code}.csv")
            with open(dest, "wb") as f:
                f.write(data)
            print(f"    -> saved {dest} ({len(data):,} bytes)")
            ok.append((name, code))
        except Exception as e:
            print(f"    ! FAILED for {name}: {e}")
            failed.append(name)
    return ok, failed


def try_combine():
    """Optional: build consolidated CSVs if pandas is available."""
    try:
        import pandas as pd
    except ImportError:
        print("\n(pandas not installed - skipping auto-combine step. "
              "Run 'pip install pandas' and re-run this script to also get "
              "combined_main_leagues.csv / combined_extra_leagues.csv.)")
        return

    print("\n=== Combining files with pandas ===")

    # --- Main leagues ---
    main_frames = []
    if os.path.isdir(RAW_MAIN_DIR):
        for season in sorted(os.listdir(RAW_MAIN_DIR)):
            season_dir = os.path.join(RAW_MAIN_DIR, season)
            if not os.path.isdir(season_dir):
                continue
            for fname in sorted(os.listdir(season_dir)):
                if not fname.lower().endswith(".csv"):
                    continue
                fpath = os.path.join(season_dir, fname)
                try:
                    # utf-8-sig strips the BOM these files ship with; latin1 would turn
                    # it into a visible prefix and split the first column in two.
                    df = _read_csv_any(fpath)
                except Exception as e:
                    print(f"  ! could not read {fpath}: {e}")
                    continue
                code = fname.replace(".csv", "")
                df["Season"] = SEASON_LABELS.get(season, season)
                df["LeagueCode"] = code
                country, league = LEAGUE_INFO.get(code, ("", ""))
                df["Country"], df["League"] = country, league
                df["MatchDate"] = pd.to_datetime(df.get("Date"), dayfirst=True,
                                                 errors="coerce")
                main_frames.append(df)
    if main_frames:
        combined = pd.concat(main_frames, ignore_index=True, sort=False)
        out_path = os.path.join(OUT_DIR, "combined_main_leagues.csv")
        combined.to_csv(out_path, index=False)
        print(f"  -> {out_path} ({len(combined):,} rows, {combined.shape[1]} columns)")

    # --- Extra leagues (filter to last 5 years by Season column, which is a year like 2021,2022...) ---
    extra_frames = []
    if os.path.isdir(RAW_EXTRA_DIR):
        for fname in sorted(os.listdir(RAW_EXTRA_DIR)):
            if not fname.lower().endswith(".csv"):
                continue
            fpath = os.path.join(RAW_EXTRA_DIR, fname)
            try:
                df = _read_csv_any(fpath)
            except Exception as e:
                print(f"  ! could not read {fpath}: {e}")
                continue
            df["MatchDate"] = pd.to_datetime(df.get("Date"), dayfirst=True,
                                             errors="coerce")
            if "Season" in df.columns:
                # Season is either a calendar year ("2021", southern hemisphere) or a
                # split year ("2021/2022"). Take the leading year of either form -
                # a plain to_numeric would turn every split-year season into NaN and
                # silently drop those leagues entirely.
                start = df["Season"].astype(str).str.extract(r"(\d{4})")[0]
                df = df[pd.to_numeric(start, errors="coerce") >= 2021]
            extra_frames.append(df)
    if extra_frames:
        combined_extra = pd.concat(extra_frames, ignore_index=True, sort=False)
        out_path = os.path.join(OUT_DIR, "combined_extra_leagues.csv")
        combined_extra.to_csv(out_path, index=False)
        print(f"  -> {out_path} ({len(combined_extra):,} rows, {combined_extra.shape[1]} columns)")


def main():
    print(f"Output folder: {OUT_DIR}")
    main_ok, main_failed = download_main_leagues()
    extra_ok, extra_failed = download_extra_leagues()

    print("\n=== Summary ===")
    print(f"Main league seasons downloaded: {len(main_ok)}/{len(SEASONS)}"
          + (f"  (failed: {main_failed})" if main_failed else ""))
    print(f"Extra leagues downloaded: {len(extra_ok)}/{len(EXTRA_COUNTRIES)}"
          + (f"  (failed: {extra_failed})" if extra_failed else ""))

    try_combine()

    print("\nDone. Raw files are under:", OUT_DIR)
    print("Point Claude at this 'data' folder (or its parent) to continue "
          "building the consolidated dataset (add xG, clean up, feature engineering).")


if __name__ == "__main__":
    main()
