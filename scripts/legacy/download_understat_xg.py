#!/usr/bin/env python3
"""
Downloads match-level expected goals (xG) data from understat.com for the top-5
European leagues over the last 5 seasons (2021/22 - 2025/26):
    Premier League, La Liga, Bundesliga, Serie A, Ligue 1

Understat's league pages pull their match data from a JSON AJAX endpoint:
    https://understat.com/getLeagueData/<league>/<season>
(header X-Requested-With: XMLHttpRequest). This endpoint sits behind Cloudflare
bot protection that returns an empty body to plain urllib/requests clients even
with correct headers/cookies - it checks the TLS/HTTP fingerprint, not just
headers. `cloudscraper` solves exactly this (it mimics a real browser's TLS
handshake), so this script uses it instead of plain requests/urllib.

Setup (one time):
    pip install cloudscraper

Usage:
    python3 download_understat_xg.py

Output:
    data/understat_xg_top5_leagues.csv
    Columns: League, LeagueCode, Season, Date, HomeTeam, AwayTeam, HG, AG, xG_home, xG_away

    LeagueCode matches the football-data.co.uk codes used in combined_main_leagues.csv
    (E0=Premier League, SP1=La Liga, D1=Bundesliga, I1=Serie A, F1=Ligue 1), so the two
    datasets can be joined later on (LeagueCode, Season, Date, HomeTeam/AwayTeam) after
    normalizing team-name spelling differences between the two sites.
"""

import csv
import json
import os
import sys
import time

try:
    import cloudscraper
except ImportError:
    print("This script needs the 'cloudscraper' package (plain requests/urllib get")
    print("silently blocked by understat.com's bot protection).")
    print("\n    pip install cloudscraper\n")
    sys.exit(1)

# Understat league slug -> (readable name, matching football-data.co.uk LeagueCode)
LEAGUES = {
    "EPL": ("Premier League", "E0"),
    "La_liga": ("La Liga", "SP1"),
    "Bundesliga": ("Bundesliga", "D1"),
    "Serie_A": ("Serie A", "I1"),
    "Ligue_1": ("Ligue 1", "F1"),
}

# Understat's "season" URL param is the year the season started
SEASONS = ["2021", "2022", "2023", "2024", "2025"]
SEASON_LABELS = {
    "2021": "2021/22", "2022": "2022/23", "2023": "2023/24",
    "2024": "2024/25", "2025": "2025/26",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)              # <repo> (scripts/ lives one level down)
OUT_DIR = os.path.join(ROOT_DIR, "data")
OUT_PATH = os.path.join(OUT_DIR, "understat_xg_top5_leagues.csv")

FIELDNAMES = ["League", "LeagueCode", "Season", "Date", "HomeTeam", "AwayTeam",
              "HG", "AG", "xG_home", "xG_away"]

SCRAPER = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})


def get_league_data(league_slug, season, retries=4):
    page_url = f"https://understat.com/league/{league_slug}/{season}"
    api_url = f"https://understat.com/getLeagueData/{league_slug}/{season}"

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # 1) Visit the normal page first to pick up a session cookie, same as
            #    a real browser would before its JS fires the AJAX call below.
            SCRAPER.get(page_url, timeout=30).raise_for_status()

            # 2) Now call the JSON endpoint with that session + matching Referer.
            resp = SCRAPER.get(api_url, timeout=30, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": page_url,
            })
            resp.raise_for_status()
            if not resp.text.strip():
                raise RuntimeError("empty response body")
            return resp.json()
        except Exception as e:
            last_err = e
            wait = 2 * attempt
            print(f"    ! attempt {attempt}/{retries} failed ({e}); retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"failed after {retries} attempts: {last_err}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    ok, failed = [], []

    for league_slug, (league_name, league_code) in LEAGUES.items():
        for season in SEASONS:
            label = f"{league_name} {SEASON_LABELS[season]}"
            print(f"Fetching {label} ...")
            try:
                data = get_league_data(league_slug, season)
                dates = data.get("dates", [])
                count = 0
                for m in dates:
                    if not m.get("isResult"):
                        continue
                    rows.append({
                        "League": league_name,
                        "LeagueCode": league_code,
                        "Season": SEASON_LABELS[season],
                        "Date": m["datetime"][:10],
                        "HomeTeam": m["h"]["title"],
                        "AwayTeam": m["a"]["title"],
                        "HG": m["goals"]["h"],
                        "AG": m["goals"]["a"],
                        "xG_home": round(float(m["xG"]["h"]), 2),
                        "xG_away": round(float(m["xG"]["a"]), 2),
                    })
                    count += 1
                print(f"    -> {count} matches")
                ok.append(label)
            except Exception as e:
                print(f"    ! FAILED {label}: {e}")
                failed.append(label)
            time.sleep(1.5)  # be polite to understat.com

    print("\n=== Summary ===")
    print(f"Succeeded: {len(ok)}/{len(LEAGUES) * len(SEASONS)}")
    if failed:
        print(f"Failed: {failed}")

    if rows:
        with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nSaved {len(rows)} matches to {OUT_PATH}")
    else:
        print("\nNo data collected - nothing saved.")


if __name__ == "__main__":
    main()
