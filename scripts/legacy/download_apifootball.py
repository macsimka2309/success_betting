#!/usr/bin/env python3
"""
Fetch squad LINEUPS and INJURIES from API-Football (api-sports.io v3).

Why this source and not FBref
-----------------------------
Everything already in the project (results, shots, odds) is either on-pitch
outcome or the bookmaker's opinion - and the odds already price the shot info in
(see reports/). Lineups and injuries are different: they are known *before* a
match and they are NOT in the football-data.co.uk odds columns. That makes them
the one class of data with a real chance of adding signal the market model does
not already have.

FBref/soccerdata was evaluated and rejected for this: it has no lineups/injuries,
throttles hard and hits CAPTCHAs. API-Football exposes both behind a plain REST
API with a documented free tier.

Account & key (one time)
------------------------
    1. Register at https://dashboard.api-football.com  (or via RapidAPI).
    2. Copy your key from the dashboard.
    3. export APIFOOTBALL_KEY=xxxxxxxxxxxxxxxxxxxx
Free tier = 100 requests/day, ~10/min. Pro ($19/mo) = 7500/day.

This script is DESIGNED for a small daily budget:
  * every response is cached to data/apifootball/raw/ - re-runs never re-spend
    quota on something already fetched (resumable);
  * a hard --max-requests guard stops before the daily cap is hit;
  * it self-throttles from the API's own rate-limit response headers.

Usage
-----
Step 0 - browse what exists, then decide which leagues you actually want:
    python3 download_apifootball.py --status                 # 1 req: plan + remaining quota
    python3 download_apifootball.py --catalog                # ~1-6 req: FULL league catalog (all ~1200) + coverage

Step 1 - cheap, per (league, season):
    python3 download_apifootball.py --resolve                # ~1 req/country: map OUR 38 codes -> api ids
    python3 download_apifootball.py --injuries               # 1 req/(league,season)
    python3 download_apifootball.py --fixtures               # 1 req/(league,season): fixture ids to join on

Step 2 - expensive, 1 req PER FIXTURE each (resumable; obey --max-requests):
    python3 download_apifootball.py --events     --max-requests 70000
    python3 download_apifootball.py --statistics --max-requests 70000
    python3 download_apifootball.py --lineups    --max-requests 70000

History depth defaults to the last 8 seasons (see DEFAULT_SEASONS); override with
--seasons. Restrict which leagues are pulled by editing LEAGUE_MAP (used by
--resolve/--injuries/--fixtures); --catalog ignores LEAGUE_MAP and lists everything.

Outputs (data/apifootball/)
    leagues_catalog.parquet    one row per league in the API (id, country, type, seasons, coverage flags)
    apifootball_leagues.json   OUR LeagueCode -> {api id, name, coverage{...}}
    injuries.parquet           one row per player-injury (league, season, team, player, reason, date)
    fixtures.parquet           one row per fixture: fixture_id + our join keys (Country/Div/Date/teams)
    events.parquet             one row per in-match event (goal/card/subst/VAR: minute, team, player, detail)
    statistics.parquet         one row per (fixture, team): shots, possession, corners, fouls, xG, ...
    lineups.parquet            one row per player in a starting XI / bench, with formation & position

Only the Python standard library is required (urllib, json). pandas is used, if
present, to write .parquet (falls back to .csv otherwise).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
API_HOST = "v3.football.api-sports.io"          # direct api-sports.io endpoint
BASE = f"https://{API_HOST}"

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
OUT_DIR = ROOT_DIR / "data" / "apifootball"
RAW_DIR = OUT_DIR / "raw"
LEAGUES_JSON = OUT_DIR / "apifootball_leagues.json"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Season = the calendar year a season STARTS in (API-Football's convention).
# Split-year European leagues: 2021 == 2021/22. Calendar leagues (Brazil, MLS,
# Japan, the Nordics, ...) already use the single year.
# Default = the last 8 seasons (the user asked for 8 years, no more).
DEFAULT_SEASONS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]

# Our football-data.co.uk LeagueCode  ->  (API-Football country, [name hints]).
# We resolve the numeric league id by querying /leagues for that country and
# matching the name, so a hint that is a unique substring is enough. The ids are
# NOT hard-coded on purpose - coverage and ids are read live in --resolve.
LEAGUE_MAP = {
    # main divisions (with in-match stats on football-data)
    "E0":  ("England", ["Premier League"]),
    "E1":  ("England", ["Championship"]),
    "E2":  ("England", ["League One"]),
    "E3":  ("England", ["League Two"]),
    "EC":  ("England", ["National League"]),
    "SC0": ("Scotland", ["Premiership"]),
    "SC1": ("Scotland", ["Championship"]),
    "SC2": ("Scotland", ["League One"]),
    "SC3": ("Scotland", ["League Two"]),
    "D1":  ("Germany", ["Bundesliga"]),           # exclude "2. Bundesliga" in matcher
    "D2":  ("Germany", ["2. Bundesliga"]),
    "I1":  ("Italy", ["Serie A"]),
    "I2":  ("Italy", ["Serie B"]),
    "SP1": ("Spain", ["La Liga", "Primera División"]),
    "SP2": ("Spain", ["Segunda División"]),
    "F1":  ("France", ["Ligue 1"]),
    "F2":  ("France", ["Ligue 2"]),
    "N1":  ("Netherlands", ["Eredivisie"]),
    "B1":  ("Belgium", ["Jupiler", "Pro League", "First Division A"]),
    "P1":  ("Portugal", ["Primeira Liga"]),
    "T1":  ("Turkey", ["Süper Lig", "Super Lig"]),
    "G1":  ("Greece", ["Super League"]),
    # extra leagues (results+odds only on football-data - this fills them out)
    "ARG": ("Argentina", ["Liga Profesional", "Primera División", "Primera Division"]),
    "AUT": ("Austria", ["Bundesliga"]),
    "BRA": ("Brazil", ["Serie A"]),
    "CHN": ("China", ["Super League"]),
    "DNK": ("Denmark", ["Superliga", "Superligaen"]),
    "FIN": ("Finland", ["Veikkausliiga"]),
    "IRL": ("Ireland", ["Premier Division"]),
    "JPN": ("Japan", ["J1 League", "J. League", "J1"]),
    "MEX": ("Mexico", ["Liga MX", "Liga BBVA MX", "Primera"]),
    "NOR": ("Norway", ["Eliteserien"]),
    "POL": ("Poland", ["Ekstraklasa"]),
    "ROU": ("Romania", ["Liga I"]),
    "RUS": ("Russia", ["Premier League"]),
    "SWE": ("Sweden", ["Allsvenskan"]),
    "SWZ": ("Switzerland", ["Super League"]),
    "USA": ("USA", ["Major League Soccer"]),
}

# Codes whose season is the calendar year (kick off in spring, end in autumn).
CALENDAR_LEAGUES = {"BRA", "CHN", "FIN", "IRL", "JPN", "NOR", "SWE", "USA"}

# API-Football league id -> our football-data LeagueCode, for the 34 leagues that
# overlap our dataset. --select keeps these codes so events/stats/lineups still
# join back to the football-data match table; every OTHER selected league gets the
# synthetic code "L<id>" (no football-data counterpart to join to).
FD_CODE_BY_APIID = {
    39: "E0", 40: "E1", 41: "E2", 42: "E3", 43: "EC",
    179: "SC0", 180: "SC1", 183: "SC2", 184: "SC3",
    78: "D1", 79: "D2", 135: "I1", 136: "I2", 140: "SP1", 141: "SP2",
    61: "F1", 62: "F2", 88: "N1", 144: "B1", 94: "P1", 203: "T1", 197: "G1",
    128: "ARG", 218: "AUT", 71: "BRA", 169: "CHN", 119: "DNK", 244: "FIN",
    357: "IRL", 98: "JPN", 262: "MEX", 103: "NOR", 106: "POL", 283: "ROU",
    235: "RUS", 113: "SWE", 207: "SWZ", 253: "USA",
}


# --------------------------------------------------------------------------
# HTTP with quota discipline
# --------------------------------------------------------------------------
def _rate_limited(body: dict) -> bool:
    """API-Football reports a per-minute overshoot as HTTP 200 with
    errors={'rateLimit': 'Too many requests...'} rather than a 429 status."""
    errs = body.get("errors")
    if isinstance(errs, dict):
        for k, v in errs.items():
            if "ratelimit" in str(k).lower() or "too many requests" in str(v).lower():
                return True
    return False


def _daily_limit_hit(body: dict) -> bool:
    """The DAILY quota is reported as errors={'requests': '...reached the request
    limit for the day...'}. Unlike the per-minute limit this will not clear until
    the 00:00 UTC reset, so the whole run must stop rather than retry or spin."""
    errs = body.get("errors")
    if isinstance(errs, dict):
        for v in errs.values():
            if "limit for the day" in str(v).lower():
                return True
    return False


class Api:
    def __init__(self, key: str, max_requests: int, rpm: int, verbose: bool = True):
        self.key = key
        self.max_requests = max_requests
        self.min_interval = 60.0 / max(1, rpm)   # spacing to respect per-minute cap
        self.sent = 0
        self.verbose = verbose
        self._last = 0.0
        self.daily_remaining = None
        # thread-safety for the parallel per-fixture pulls: one lock guards the
        # daily-budget counter, one paces dispatch to the per-minute cap (shared by
        # all workers), one serialises progress prints. Cache files are one-per-call
        # so their reads/writes need no lock.
        self._budget_lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._budget_warned = False
        self._daily_exhausted = False        # set when the API's daily quota is hit

    def _cache_path(self, endpoint: str, params: dict) -> Path:
        q = urllib.parse.urlencode(sorted(params.items()))
        safe = (endpoint.strip("/") + "__" + q).replace("/", "_").replace("&", "_").replace("=", "-")
        return RAW_DIR / f"{safe}.json"

    def _pace(self):
        """Block until at least min_interval has passed since the last dispatch.
        Sleeping while holding the lock serialises dispatch timing across all worker
        threads, so total throughput never exceeds the per-minute cap (--rpm) no
        matter how many workers there are. The network call happens after release,
        so requests still overlap in flight."""
        with self._pace_lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get(self, endpoint: str, params: dict, use_cache: bool = True) -> dict | None:
        """One API call, cached. Returns the parsed 'response' payload (full dict).
        Returns None (and does not spend a request) if the budget is exhausted.
        Thread-safe: safe to call from many worker threads at once."""
        cache = self._cache_path(endpoint, params)
        if use_cache and cache.exists():
            try:
                return json.loads(cache.read_text())
            except (ValueError, OSError):
                pass                         # corrupt/partial cache file -> refetch

        if self._daily_exhausted:            # API daily quota hit -> stop hitting the wire
            return None

        # reserve one slot from the daily budget (atomic across threads)
        with self._budget_lock:
            if self.sent >= self.max_requests:
                if self.verbose and not self._budget_warned:
                    self._budget_warned = True
                    print(f"  [budget] reached --max-requests={self.max_requests}; stopping. "
                          f"Re-run later to resume (cached calls are free).")
                return None
            self.sent += 1
            my_n = self.sent

        url = f"{BASE}{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"x-apisports-key": self.key})
        body = None
        max_attempts = 7
        for attempt in range(1, max_attempts + 1):
            self._pace()                     # throttle every send (first try AND retries)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.daily_remaining = resp.headers.get("x-ratelimit-requests-remaining")
                    body = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 429:            # per-minute cap as HTTP status: back off + retry
                    body = None; time.sleep(2 * attempt); continue
                with self._print_lock:
                    print(f"  ! HTTP {e.code} on {endpoint} {params}: {e.reason}")
                return None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                body = None
                if attempt == max_attempts:
                    with self._print_lock:
                        print(f"  ! network error on {endpoint} {params}: {e}")
                    return None
                time.sleep(3 * attempt); continue
            # API signals a per-minute overshoot as HTTP 200 with errors:{rateLimit:...}.
            # Treat it like a 429: wait for the window to clear and retry (don't skip the
            # fixture, don't cache the error). A short sleep lets the rolling minute drain.
            if _rate_limited(body):
                body = None; time.sleep(2 * attempt); continue
            if _daily_limit_hit(body):
                # hard stop for the whole run - the daily quota won't clear until 00:00 UTC
                with self._print_lock:
                    if not self._daily_exhausted:
                        self._daily_exhausted = True
                        print("  [daily limit] API daily request quota reached - stopping this "
                              "run. Resume after the 00:00 UTC reset; cached calls are free.")
                return None
            break
        if body is None:
            with self._print_lock:
                print(f"  ! gave up on {endpoint} {params} after {max_attempts} tries "
                      f"(rate limit / network); will retry next run")
            return None

        if body.get("errors"):
            # Non-rate-limit API error (auth/plan/bad param). Do NOT cache - otherwise a
            # transient/param error would stick forever.
            with self._print_lock:
                print(f"  ! API error on {endpoint} {params}: {body['errors']}")
            return body
        # Caching is an optimisation, not a requirement: this project lives in an
        # iCloud-synced folder whose raw/ directory already holds ~385k files, and
        # a write there can time out. Losing a cache write only costs a re-fetch
        # later; letting it raise would abandon an hours-long pull.
        try:
            cache.write_text(json.dumps(body))
        except OSError as e:
            with self._print_lock:
                print(f"  ~ кэш не записан ({type(e).__name__}), продолжаю: {cache.name}")
        if self.verbose and my_n % 50 == 0:
            rem = self.daily_remaining if self.daily_remaining is not None else "?"
            with self._print_lock:
                print(f"  [req {my_n}/{self.max_requests}] {endpoint} "
                      f"-> daily left: {rem}")
        return body

    def paged(self, endpoint: str, params: dict):
        """Yield every item across all pages. The FIRST call omits `page` entirely:
        some endpoints (e.g. /leagues) reject a `page` param and return everything in
        one response. `page` is only added from page 2 onwards, for endpoints that
        actually paginate (/fixtures, /injuries, ...)."""
        page = 1
        while True:
            call = dict(params) if page == 1 else dict(params, page=page)
            body = self.get(endpoint, call)
            if not body or body.get("errors"):
                return
            for item in body.get("response", []):
                yield item
            paging = body.get("paging", {}) or {}
            if page >= (paging.get("total", 1) or 1):
                return
            page += 1

    def pull_per_fixture(self, endpoint, fixtures, parse, label, workers=1):
        """Fetch a per-fixture endpoint (events/statistics/lineups) for every finished
        fixture and return the parsed rows. `parse(fx_row, body) -> list[dict]`.

        Pass 1 reads everything already cached (free - no budget, no rate limit) so the
        output table always contains every fixture on disk, in any run. Pass 2 fetches
        the uncached remainder with `workers` threads until the request budget is hit;
        the shared pacer keeps total dispatch under --rpm. Fully resumable: a run that
        stops at the budget just leaves the rest for next time."""
        recs = [fx for _, fx in fixtures.iterrows()]
        rows, rows_lock = [], threading.Lock()

        todo = []
        for fx in recs:
            cache = self._cache_path(endpoint, {"fixture": int(fx["fixture_id"])})
            if cache.exists():
                try:
                    rows.extend(parse(fx, json.loads(cache.read_text())) or [])
                    continue
                except (ValueError, OSError):
                    pass                     # corrupt cache -> refetch below
            todo.append(fx)
        print(f"  {label}: {len(recs) - len(todo):,} already cached, "
              f"{len(todo):,} to fetch (workers={workers}, budget={self.max_requests:,})")

        def work(fx):
            body = self.get(endpoint, {"fixture": int(fx["fixture_id"])})
            if body is None:                 # budget exhausted
                return False
            r = parse(fx, body)
            if r:
                with rows_lock:
                    rows.extend(r)
            return True

        if todo and self.sent < self.max_requests:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                inflight = set()
                for fx in todo:
                    if self.sent >= self.max_requests or self._daily_exhausted:
                        break
                    inflight.add(ex.submit(work, fx))
                    if len(inflight) >= workers * 4:
                        done, inflight = concurrent.futures.wait(
                            inflight, return_when=concurrent.futures.FIRST_COMPLETED)
                for f in inflight:
                    f.result()
        _write_table(rows, OUT_DIR / label, label)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _write_table(rows: list[dict], path_noext: Path, label: str):
    if not rows:
        print(f"  {label}: no rows collected.")
        return
    try:
        import pandas as pd
        # convert_dtypes -> nullable Int64/boolean/string, AND write_statistics=False:
        # pyarrow 19 otherwise emits per-column stats that its own reader rejects with
        # "Repetition level histogram size mismatch" on nullable columns. This bit us on
        # events.parquet and statistics.parquet, which had to be rebuilt from the cache.
        df = pd.DataFrame(rows).convert_dtypes()
        try:
            import pyarrow as _pa, pyarrow.parquet as _pq
            _pq.write_table(_pa.Table.from_pandas(df, preserve_index=False),
                            path_noext.with_suffix(".parquet"),
                            write_statistics=False, version="2.6")
            print(f"  {label}: wrote {len(df):,} rows -> {path_noext.with_suffix('.parquet')}")
        except Exception:
            df.to_csv(path_noext.with_suffix(".csv"), index=False)
            print(f"  {label}: wrote {len(df):,} rows -> {path_noext.with_suffix('.csv')} (no parquet engine)")
    except ImportError:
        import csv
        with open(path_noext.with_suffix(".csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader(); w.writerows(rows)
        print(f"  {label}: wrote {len(rows):,} rows -> {path_noext.with_suffix('.csv')} (pandas missing)")


def _match_league(hints: list[str], leagues_in_country: list[dict]) -> dict | None:
    """Pick the league object whose name matches a hint. Special-cases the
    'Bundesliga' vs '2. Bundesliga' style overlap by preferring an exact-ish hit."""
    want_second = any(h[0].isdigit() for h in hints)   # e.g. "2. Bundesliga"
    best = None
    for item in leagues_in_country:
        lg = item["league"]
        if lg.get("type") != "League":
            continue
        name = lg["name"]
        for h in hints:
            if h.lower() in name.lower():
                is_second = name.strip()[0].isdigit()
                if is_second != want_second:
                    continue                    # don't match "2. Bundesliga" for "Bundesliga"
                # prefer the shortest name (closest to exact) among candidates
                if best is None or len(name) < len(best["league"]["name"]):
                    best = item
    return best


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------
def cmd_status(api: Api):
    body = api.get("/status", {}, use_cache=False)
    if not body:
        return
    resp = body.get("response", {})
    acc = resp.get("account", {}); sub = resp.get("subscription", {}); req = resp.get("requests", {})
    print("\nAccount :", acc.get("firstname", ""), acc.get("lastname", ""), "-", acc.get("email", ""))
    print("Plan    :", sub.get("plan"), "| active:", sub.get("active"), "| ends:", sub.get("end"))
    print("Requests:", req.get("current"), "/", req.get("limit_day"), "used today")


def cmd_resolve(api: Api, seasons: list[int]):
    season_probe = max(seasons)
    countries = sorted({c for c, _ in LEAGUE_MAP.values()})
    per_country: dict[str, list[dict]] = {}
    for country in countries:
        body = api.get("/leagues", {"country": country})
        if body is None:
            print("  (budget/limit reached during resolve - partial map saved)")
            break
        per_country[country] = body.get("response", [])

    resolved = {}
    for code, (country, hints) in LEAGUE_MAP.items():
        cand = per_country.get(country)
        if not cand:
            continue
        m = _match_league(hints, cand)
        if not m:
            print(f"  ? {code}: no league matched hints {hints} in {country}")
            continue
        lg = m["league"]
        # coverage lives on the most recent season entry
        seasons_cov = m.get("seasons", [])
        cov = (seasons_cov[-1].get("coverage", {}) if seasons_cov else {})
        fx = cov.get("fixtures", {}) if isinstance(cov, dict) else {}
        resolved[code] = {
            "id": lg["id"], "name": lg["name"], "country": country,
            "coverage": {"lineups": fx.get("lineups"), "injuries": cov.get("injuries"),
                         "statistics_fixtures": fx.get("statistics_fixtures")},
        }
    LEAGUES_JSON.write_text(json.dumps(resolved, indent=2, ensure_ascii=False))
    print(f"\nResolved {len(resolved)}/{len(LEAGUE_MAP)} leagues -> {LEAGUES_JSON}")
    print("  coverage (lineups / injuries):")
    for code, v in sorted(resolved.items()):
        c = v["coverage"]
        print(f"    {code:4s} id={v['id']:<5} {v['name'][:28]:28s} "
              f"lineups={str(c['lineups']):5s} injuries={str(c['injuries'])}")


def cmd_catalog(api: Api, seasons: list[int]):
    """Dump the ENTIRE league catalogue the API knows about (all ~1200), so you
    can browse it and decide which leagues you actually want. Cheap: /leagues with
    no filter returns everything (1-6 paged requests). Ignores LEAGUE_MAP.

    For each league we keep only the seasons within the requested window and record
    whether the LATEST such season has events / statistics / lineups coverage."""
    min_year = min(seasons)
    rows = []
    for item in api.paged("/leagues", {}):
        lg = item.get("league", {}); co = item.get("country", {})
        yrs = [s for s in item.get("seasons", []) if s.get("year", 0) >= min_year]
        latest = max(yrs, key=lambda s: s["year"]) if yrs else {}
        cov = latest.get("coverage", {}) if isinstance(latest.get("coverage"), dict) else {}
        fx = cov.get("fixtures", {}) if isinstance(cov, dict) else {}
        rows.append({
            "id": lg.get("id"), "name": lg.get("name"), "type": lg.get("type"),
            "country": co.get("name"), "country_code": co.get("code"),
            "seasons_in_window": len(yrs),
            "first_year": min((s["year"] for s in yrs), default=None),
            "last_year": max((s["year"] for s in yrs), default=None),
            "events": fx.get("events"), "statistics": fx.get("statistics_fixtures"),
            "lineups": fx.get("lineups"), "players": fx.get("statistics_players"),
            "injuries": cov.get("injuries"), "odds": cov.get("odds"),
            "standings": cov.get("standings"),
        })
    _write_table(rows, OUT_DIR / "leagues_catalog", "catalog")
    # quick printed summary so you can eyeball coverage without opening the file
    leagues = [r for r in rows if r["type"] == "League" and r["seasons_in_window"]]
    full = [r for r in leagues if r["events"] and r["statistics"] and r["lineups"]]
    print(f"\n  catalogue: {len(rows)} competitions total "
          f"({len(leagues)} domestic leagues with data in the last {len(seasons)} yrs; "
          f"{len(full)} of them have events+stats+lineups).")
    by_country = {}
    for r in full:
        by_country.setdefault(r["country"], 0)
        by_country[r["country"]] += 1
    print("  countries with full-coverage leagues (top 20 by count):")
    for c, n in sorted(by_country.items(), key=lambda x: -x[1])[:20]:
        print(f"    {c:22s} {n}")
    print("  -> open data/apifootball/leagues_catalog.parquet, pick the ids you want, "
          "then put them in LEAGUE_MAP (or a --only filter) before the big pull.")


def cmd_select(api: Api, seasons: list[int], require: list[str]):
    """Build apifootball_leagues.json for EVERY league whose latest season has the
    required coverage flags (default: events + statistics). This is the bulk-scope
    alternative to --resolve: it targets leagues by catalogue coverage, not by our
    38-league LEAGUE_MAP. Reuses the cached /leagues response if present (no request).

    Each entry also stores the exact list of available seasons within the window, so
    the per-(league,season) pulls never waste a request on a season with no data."""
    require = [r.lower() for r in require]
    alias = {"stats": "statistics", "statistics_fixtures": "statistics"}
    require = [alias.get(r, r) for r in require]
    min_year = min(seasons)

    cache = RAW_DIR / "leagues__.json"
    if cache.exists():
        items = json.loads(cache.read_text()).get("response", [])
        print(f"  (using cached catalogue {cache.name} - no request spent)")
    else:
        items = list(api.paged("/leagues", {}))

    resolved = {}
    for it in items:
        lg = it.get("league", {}); co = it.get("country", {})
        if lg.get("type") != "League":
            continue
        # window = seasons in [min_year .. newest requested]. Capping at the newest
        # requested year avoids reading a just-created future season whose coverage
        # flags are still all false because it has not kicked off yet.
        win = [s for s in it.get("seasons", []) if min_year <= s.get("year", 0) <= max(seasons)]
        if not win:
            continue
        yrs = sorted(s["year"] for s in win)
        latest = max(win, key=lambda s: s["year"])
        cov = latest.get("coverage", {}) or {}; fx = cov.get("fixtures", {}) or {}
        flags = {"events": bool(fx.get("events")), "statistics": bool(fx.get("statistics_fixtures")),
                 "lineups": bool(fx.get("lineups")), "injuries": bool(cov.get("injuries"))}
        if not all(flags.get(r) for r in require):
            continue
        code = FD_CODE_BY_APIID.get(lg["id"], f"L{lg['id']}")
        resolved[code] = {
            "id": lg["id"], "name": lg["name"], "country": co.get("name"),
            "seasons": [y for y in yrs if y in set(seasons)],
            "coverage": {"events": flags["events"], "statistics_fixtures": flags["statistics"],
                         "lineups": flags["lineups"], "injuries": flags["injuries"]},
        }
    LEAGUES_JSON.write_text(json.dumps(resolved, indent=2, ensure_ascii=False))
    mine = sum(1 for c in resolved if not c.startswith("L"))
    with_lin = sum(1 for v in resolved.values() if v["coverage"]["lineups"])
    tot_ls = sum(len(v["seasons"]) for v in resolved.values())
    print(f"\nSelected {len(resolved)} leagues (require={require}) -> {LEAGUES_JSON}")
    print(f"  {mine} overlap our dataset (keep football-data codes); {with_lin} have lineups too.")
    print(f"  {tot_ls} league-seasons in window {min(seasons)}-{max(seasons)}. Rough per-fixture pull:")
    print(f"    ~{tot_ls*306:,} matches -> events {tot_ls*306:,} + statistics {tot_ls*306:,} "
          f"(+ lineups where available) requests. Run --fixtures first for the exact count.")


def _load_resolved() -> dict:
    if not LEAGUES_JSON.exists():
        sys.exit("Run --select (bulk) or --resolve (our 38) first - need apifootball_leagues.json.")
    return json.loads(LEAGUES_JSON.read_text())


# Set by --force-seasons: ask for the requested seasons even when the cached
# catalogue does not list them. The catalogue is a snapshot, so a season that
# started after it was taken would otherwise be invisible forever.
FORCE_SEASONS = False


def _league_seasons(v: dict, seasons: list[int]) -> list[int]:
    """Seasons to actually request for one league: the requested window narrowed to
    the seasons the catalogue says exist (falls back to the full window if unknown)."""
    avail = v.get("seasons")
    if FORCE_SEASONS or avail is None:
        return list(seasons)
    return [s for s in seasons if s in avail]


def _load_fixtures():
    """Shared loader for the per-fixture pulls (events/statistics/lineups).
    Returns finished fixtures only - unplayed games have nothing to fetch.
    Reads parquet single-threaded (pyarrow's multithreaded reader can throw
    'Repetition level histogram size mismatch' on nullable columns) and falls
    back to the CSV copy if the parquet is unreadable - so a per-fixture pull
    can never be blocked by a flaky fixtures file."""
    import pandas as pd
    pq_path = OUT_DIR / "fixtures.parquet"; csv_path = OUT_DIR / "fixtures.csv"
    fixtures = None
    if pq_path.exists():
        try:
            import pyarrow.parquet as _pq
            fixtures = _pq.read_table(pq_path, use_threads=False).to_pandas()
        except Exception as e:
            print(f"  ! could not read {pq_path.name} ({e}); falling back to fixtures.csv")
    if fixtures is None and csv_path.exists():
        fixtures = pd.read_csv(csv_path)
    if fixtures is None:
        sys.exit("Run --fixtures first (need fixture ids for per-fixture pulls).")
    return fixtures[fixtures["status"].isin(["FT", "AET", "PEN"])]


def cmd_injuries(api: Api, seasons: list[int]):
    resolved = _load_resolved()
    rows = []
    for code, v in resolved.items():
        for season in _league_seasons(v, seasons):
            for item in api.paged("/injuries", {"league": v["id"], "season": season}):
                p = item.get("player", {}); t = item.get("team", {}); fx = item.get("fixture", {})
                rows.append({
                    "LeagueCode": code, "Season": season, "api_league_id": v["id"],
                    "team": t.get("name"), "team_id": t.get("id"),
                    "player": p.get("name"), "player_id": p.get("id"),
                    "type": p.get("type"), "reason": p.get("reason"),
                    "fixture_id": fx.get("id"), "date": (fx.get("date") or "")[:10],
                })
    _write_table(rows, OUT_DIR / "injuries", "injuries")


def cmd_fixtures(api: Api, seasons: list[int]):
    resolved = _load_resolved()
    rows = []
    for code, v in resolved.items():
        for season in _league_seasons(v, seasons):
            for item in api.paged("/fixtures", {"league": v["id"], "season": season}):
                fx = item.get("fixture", {}); tm = item.get("teams", {}); gl = item.get("goals", {})
                rows.append({
                    "fixture_id": fx.get("id"),
                    "LeagueCode": code, "Country": v["country"], "Season": season,
                    "Date": (fx.get("date") or "")[:10], "kickoff": fx.get("date"),
                    "HomeTeam": tm.get("home", {}).get("name"),
                    "AwayTeam": tm.get("away", {}).get("name"),
                    "home_id": tm.get("home", {}).get("id"),
                    "away_id": tm.get("away", {}).get("id"),
                    "FTHG": gl.get("home"), "FTAG": gl.get("away"),
                    "status": fx.get("status", {}).get("short"),
                })
    _write_table(rows, OUT_DIR / "fixtures", "fixtures")


def cmd_date(api: Api, dates: list[str]):
    """Every fixture in the world for a given date - ONE cheap request per date.

    /fixtures?date=YYYY-MM-DD ignores the league filter, so this is how a daily
    slate is obtained without asking per league. Fixtures whose league is in our
    selection get our LeagueCode; the rest are tagged "L<id>" so they are still
    visible (they just have no history to predict from yet).
    """
    resolved = _load_resolved()
    by_id = {int(v["id"]): (code, v.get("country", "")) for code, v in resolved.items()}
    rows = []
    for d in dates:
        n_before = len(rows)
        for item in api.paged("/fixtures", {"date": d}):
            fx = item.get("fixture", {}); tm = item.get("teams", {})
            gl = item.get("goals", {}); lg = item.get("league", {})
            lid = lg.get("id")
            code, country = by_id.get(int(lid), (f"L{lid}", lg.get("country", "")))
            rows.append({
                "fixture_id": fx.get("id"),
                "LeagueCode": code, "Country": country, "Season": lg.get("season"),
                "league_id": lid, "league_name": lg.get("name"),
                "in_portfolio": int(int(lid) in by_id),
                "Date": (fx.get("date") or "")[:10], "kickoff": fx.get("date"),
                "HomeTeam": tm.get("home", {}).get("name"),
                "AwayTeam": tm.get("away", {}).get("name"),
                "home_id": tm.get("home", {}).get("id"),
                "away_id": tm.get("away", {}).get("id"),
                "FTHG": gl.get("home"), "FTAG": gl.get("away"),
                "status": fx.get("status", {}).get("short"),
            })
        got = len(rows) - n_before
        known = sum(r["in_portfolio"] for r in rows[n_before:])
        print(f"  {d}: {got} fixtures ({known} in our portfolio)")
    _write_table(rows, OUT_DIR / "fixtures_by_date", "fixtures_by_date")


def _parse_events(fx, body):
    out = []
    for ev in body.get("response", []):
        t = ev.get("time", {}); tm = ev.get("team", {})
        pl = ev.get("player", {}); asst = ev.get("assist", {})
        out.append({
            "fixture_id": int(fx["fixture_id"]), "LeagueCode": fx["LeagueCode"],
            "Season": fx["Season"], "Date": fx["Date"],
            "minute": t.get("elapsed"), "minute_extra": t.get("extra"),
            "team": tm.get("name"), "team_id": tm.get("id"),
            "type": ev.get("type"), "detail": ev.get("detail"),
            "player": pl.get("name"), "player_id": pl.get("id"),
            "assist": asst.get("name"), "comments": ev.get("comments"),
        })
    return out


def _parse_statistics(fx, body):
    out = []
    for team_block in body.get("response", []):
        tm = team_block.get("team", {})
        row = {
            "fixture_id": int(fx["fixture_id"]), "LeagueCode": fx["LeagueCode"],
            "Season": fx["Season"], "Date": fx["Date"],
            "team": tm.get("name"), "team_id": tm.get("id"),
        }
        for st in team_block.get("statistics", []):
            key = str(st.get("type", "")).strip().lower().replace(" ", "_").replace("%", "pct")
            row[key] = st.get("value")
        out.append(row)
    return out


def _parse_lineups(fx, body):
    out = []
    for team_block in body.get("response", []):
        team = team_block.get("team", {})
        formation = team_block.get("formation")
        coach = (team_block.get("coach") or {}).get("name")
        for role, players in (("start", team_block.get("startXI", [])),
                              ("sub", team_block.get("substitutes", []))):
            for pl in players:
                p = pl.get("player", {})
                out.append({
                    "fixture_id": int(fx["fixture_id"]), "LeagueCode": fx["LeagueCode"],
                    "Season": fx["Season"], "Date": fx["Date"],
                    "team": team.get("name"), "team_id": team.get("id"),
                    "formation": formation, "coach": coach, "role": role,
                    "player": p.get("name"), "player_id": p.get("id"),
                    "number": p.get("number"), "pos": p.get("pos"), "grid": p.get("grid"),
                })
    return out


def cmd_events(api: Api, workers: int = 1):
    """Per-fixture in-match events (goal/card/subst/VAR). 1 req/fixture, resumable."""
    api.pull_per_fixture("/fixtures/events", _load_fixtures(), _parse_events, "events", workers)


def cmd_statistics(api: Api, workers: int = 1):
    """Per-fixture team stats (shots, possession, corners, fouls, xG). 1 req/fixture."""
    api.pull_per_fixture("/fixtures/statistics", _load_fixtures(), _parse_statistics, "statistics", workers)


def cmd_lineups(api: Api, workers: int = 1):
    """Per-fixture lineups (formation, starting XI, subs, coach). 1 req/fixture."""
    api.pull_per_fixture("/fixtures/lineups", _load_fixtures(), _parse_lineups, "lineups", workers)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="show plan + remaining quota (1 req)")
    ap.add_argument("--catalog", action="store_true", help="dump the FULL league catalogue to browse (cheap)")
    ap.add_argument("--select", action="store_true",
                    help="bulk-target EVERY league with required coverage (see --require); uses cached catalogue")
    ap.add_argument("--require", nargs="*", default=["events", "statistics"],
                    help="coverage flags a league must have to be selected (default: events statistics)")
    ap.add_argument("--resolve", action="store_true", help="resolve only OUR 38 league ids + coverage")
    ap.add_argument("--injuries", action="store_true", help="pull injuries per (league, season)")
    ap.add_argument("--fixtures", action="store_true", help="pull fixture ids (join keys)")
    ap.add_argument("--force-seasons", action="store_true",
                    help="request seasons even if the cached catalogue lacks them")
    ap.add_argument("--date", nargs="+", metavar="YYYY-MM-DD",
                    help="pull EVERY fixture worldwide for these dates (1 req/date)")
    ap.add_argument("--events", action="store_true", help="pull per-fixture events (expensive)")
    ap.add_argument("--statistics", action="store_true", help="pull per-fixture team stats (expensive)")
    ap.add_argument("--lineups", action="store_true", help="pull per-fixture lineups (expensive)")
    ap.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS,
                    help=f"start-year seasons (default = last 8: {DEFAULT_SEASONS})")
    ap.add_argument("--max-requests", type=int, default=90,
                    help="hard cap on network calls this run (free daily=100; set high on a paid plan)")
    ap.add_argument("--rpm", type=int, default=10,
                    help="requests/minute self-throttle (free=10, Pro=300, Ultra=450, Mega=900)")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel threads for per-fixture pulls (events/statistics/lineups). "
                         "1=serial; try 8 on a paid plan. Total rate is still capped by --rpm.")
    args = ap.parse_args()

    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        sys.exit("Set your key first:  export APIFOOTBALL_KEY=xxxx   "
                 "(get one at https://dashboard.api-football.com)")

    modes = [args.status, args.catalog, args.select, args.resolve, args.injuries,
             args.fixtures, args.events, args.statistics, args.lineups, args.date]
    if not any(modes):
        ap.print_help(); return

    global FORCE_SEASONS
    FORCE_SEASONS = args.force_seasons
    api = Api(key, max_requests=args.max_requests, rpm=args.rpm)
    if args.status:     cmd_status(api)
    if args.catalog:    cmd_catalog(api, args.seasons)
    if args.select:     cmd_select(api, args.seasons, args.require)
    if args.resolve:    cmd_resolve(api, args.seasons)
    if args.injuries:   cmd_injuries(api, args.seasons)
    if args.date:       cmd_date(api, args.date)
    if args.fixtures:   cmd_fixtures(api, args.seasons)
    if args.events:     cmd_events(api, args.workers)
    if args.statistics: cmd_statistics(api, args.workers)
    if args.lineups:    cmd_lineups(api, args.workers)
    print(f"\nDone. Network requests spent this run: {api.sent}.")


if __name__ == "__main__":
    main()
