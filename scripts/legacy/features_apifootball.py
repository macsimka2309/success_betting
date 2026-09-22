"""
Build leakage-free features from matches_merged.parquet.

Four families, all computed from strictly PAST matches (shift(1) inside the season,
so the current match never contributes to its own features):

  1. FORM        rolling means over the team's last 1 and 6 matches this season,
                 PLUS an expanding (season-to-date) mean as the long anchor.
                 Window search over 1..30 found MAE improves monotonically with
                 window length (r1 1.3579 -> r7 1.3375 -> r30 1.3287), so a long
                 anchor matters more than a medium window. Expanding ties r30
                 (1.3265 vs 1.3264, noise +/-0.0004) and is cleaner - no arbitrary
                 constant, and it self-adjusts to season length.

  2. VENUE FORM  same, but over the team's last 1 / 4 matches AT THIS VENUE plus a
                 venue expanding mean. Kept small on purpose: against a weak
                 baseline venue form was worth -0.0037 MAE, but once the table
                 features and the expanding anchor are in, the whole v1+v3+v5 block
                 measured EXACTLY 0.0000 and the best single window only -0.0006.
                 The table/strength features already carry that information.

  3. TABLE CLASS where the team sits in its league table BEFORE this match:
                 points-per-game so far, normalised rank (0=top, 1=bottom) and a
                 5-way class (title/upper/mid/lower/relegation). Ranks are computed
                 among teams that have played the SAME number of matches, which is
                 the fair comparison (no games-in-hand distortion), and normalised
                 by league size because leagues here range from 8 to 34 teams.
                 Also the same class for the PREVIOUS season, which is available
                 from matchday 1 whereas the live table is meaningless early on.

  4. PREV SEASON season aggregates (ppg, goals, xG) from the season before.

Output: data/apifootball/matches_features.parquet  (finished matches only)
"""
from pathlib import Path
import numpy as np, pandas as pd, pyarrow as pa, pyarrow.parquet as pq

OUT = Path("data/apifootball")
WIN_ALL   = (1, 6)         # overall-form windows (+ expanding, see below)
WIN_VENUE = (1, 4)         # venue-form windows (+ venue expanding)

m = pq.read_table(OUT/"matches_merged.parquet", use_threads=False).to_pandas()
m = m[m.status.isin(["FT","AET","PEN"])].copy()
m["FTHG"] = pd.to_numeric(m.FTHG, errors="coerce"); m["FTAG"] = pd.to_numeric(m.FTAG, errors="coerce")
m = m[m.FTHG.notna() & m.FTAG.notna()]
print(f"finished matches with result: {len(m):,}")

# ---------------------------------------------------------------- long format
def team_rows(df, venue):
    home = venue == "H"
    gf, ga = (df.FTHG, df.FTAG) if home else (df.FTAG, df.FTHG)
    pre = "home_" if home else "away_"; opp = "away_" if home else "home_"
    num = lambda c: pd.to_numeric(df[c], errors="coerce")
    return pd.DataFrame({
        "fixture_id": df.fixture_id, "team_id": (df.home_id if home else df.away_id),
        "LeagueCode": df.LeagueCode, "Season": df.Season,
        "kickoff": df.kickoff, "Date": df.Date, "venue": venue,
        "gf": gf.astype("float"), "ga": ga.astype("float"),
        "pts": np.where(gf>ga,3.0,np.where(gf==ga,1.0,0.0)),
        "shots": num(pre+"total_shots"), "sot": num(pre+"shots_on_goal"),
        "xg": num(pre+"expected_goals"), "xga": num(opp+"expected_goals"),
        "corners": num(pre+"corner_kicks"), "poss": num(pre+"ball_possession"),
        "fouls": num(pre+"fouls"), "yellows": num(pre+"ev_yellow"), "reds": num(pre+"ev_red"),
    })

long = pd.concat([team_rows(m,"H"), team_rows(m,"A")], ignore_index=True)
long = long[long.team_id.notna() & long.Season.notna()]
long["sortk"] = long.kickoff.fillna(long.Date).astype("string")
long = long.sort_values(["team_id","Season","sortk","fixture_id"]).reset_index(drop=True)

METRICS = ["gf","ga","pts","shots","sot","xg","xga","corners","poss","fouls","yellows","reds"]

# ---------------------------------------------------------------- 1) overall form
key = [long.team_id, long.Season]
g = long.groupby(key, sort=False)
for met in METRICS:
    past = g[met].shift(1)
    pg = past.groupby(key, sort=False)
    for w in WIN_ALL:
        long[f"{met}_r{w}"] = pg.rolling(w, min_periods=1).mean().reset_index(level=[0,1], drop=True)
    # season-to-date mean: the long anchor. Ties a 30-match window and needs no constant.
    long[f"{met}_exp"] = pg.expanding().mean().reset_index(level=[0,1], drop=True)

# ---------------------------------------------------------------- 2) venue form
vkey = [long.team_id, long.Season, long.venue]
vg = long.groupby(vkey, sort=False)
for met in METRICS:
    past = vg[met].shift(1)
    pg = past.groupby(vkey, sort=False)
    for w in WIN_VENUE:
        long[f"{met}_v{w}"] = pg.rolling(w, min_periods=1).mean().reset_index(level=[0,1,2], drop=True)
    long[f"{met}_vexp"] = pg.expanding().mean().reset_index(level=[0,1,2], drop=True)
long["venue_matches"] = vg.cumcount()

dt = pd.to_datetime(long.Date, errors="coerce")
long["rest_days"] = (dt - g["Date"].shift(1).pipe(pd.to_datetime, errors="coerce")).dt.days
long["matches_this_season"] = g.cumcount()

# ---------------------------------------------------------------- 3) live league table
# cumulative record BEFORE this match, within (team, league, season)
lkey = [long.team_id, long.LeagueCode, long.Season]
lg = long.groupby(lkey, sort=False)
long["cum_pts"]    = lg["pts"].cumsum() - long["pts"]        # strictly before
long["cum_gd"]     = (lg["gf"].cumsum() - long["gf"]) - (lg["ga"].cumsum() - long["ga"])
long["played"]     = lg.cumcount()
long["table_ppg"]  = np.where(long.played > 0, long.cum_pts / long.played.replace(0, np.nan), np.nan)

# rank among teams of the same league-season that have played the SAME number of
# matches -> a fair table snapshot, then normalise by how many teams that is.
grp = long.groupby(["LeagueCode","Season","played"], sort=False)
long["_n_peers"] = grp["team_id"].transform("size")
order = long.cum_pts.fillna(-1) * 1000 + long.cum_gd.fillna(0)     # points, then goal difference
long["_rank"] = order.groupby([long.LeagueCode, long.Season, long.played]).rank(ascending=False, method="average")
long["table_pos_pct"] = np.where(long._n_peers > 1,
                                 (long._rank - 1) / (long._n_peers - 1), np.nan)   # 0=top, 1=bottom
long.loc[long.played == 0, ["table_pos_pct","table_ppg"]] = np.nan                 # no table yet
CLASS_EDGES = [0, .15, .35, .65, .85, 1.0]        # title / upper / mid / lower / relegation
long["table_class"] = pd.cut(long.table_pos_pct, CLASS_EDGES, labels=[0,1,2,3,4],
                             include_lowest=True).astype("Float64")

# ---------------------------------------------------------------- 4) previous season
season_agg = (long.groupby(["team_id","LeagueCode","Season"])
                  .agg(ps_ppg=("pts","mean"), ps_gf=("gf","mean"), ps_ga=("ga","mean"),
                       ps_xg=("xg","mean"), ps_shots=("shots","mean"),
                       _fin_pts=("pts","sum"), _fin_gd=("gf","sum"), _fin_ga=("ga","sum"),
                       _n=("pts","size")).reset_index())
season_agg["_fin_gd"] = season_agg._fin_gd - season_agg._fin_ga
# final table class of that season
o = season_agg._fin_pts*1000 + season_agg._fin_gd
season_agg["_r"] = o.groupby([season_agg.LeagueCode, season_agg.Season]).rank(ascending=False)
season_agg["_np"] = season_agg.groupby(["LeagueCode","Season"]).team_id.transform("size")
season_agg["ps_pos_pct"] = np.where(season_agg._np>1,(season_agg._r-1)/(season_agg._np-1),np.nan)
season_agg["ps_class"] = pd.cut(season_agg.ps_pos_pct, CLASS_EDGES, labels=[0,1,2,3,4],
                                include_lowest=True).astype("Float64")
PS = ["ps_ppg","ps_gf","ps_ga","ps_xg","ps_shots","ps_pos_pct","ps_class"]
prev = season_agg.sort_values(["team_id","Season"]).copy()
prev["Season"] = prev.groupby("team_id").Season.shift(-1)     # describes the season BEFORE
prev = prev[prev.Season.notna()][["team_id","Season"]+PS].drop_duplicates(["team_id","Season"])
long = long.merge(prev, on=["team_id","Season"], how="left")

# ---------------------------------------------------------------- assemble
FEAT = ([f"{met}_r{w}" for met in METRICS for w in WIN_ALL]
        + [f"{met}_exp" for met in METRICS]
        + [f"{met}_v{w}" for met in METRICS for w in WIN_VENUE]
        + [f"{met}_vexp" for met in METRICS]
        + ["rest_days","matches_this_season","venue_matches",
           "table_ppg","table_pos_pct","table_class"] + PS)
home = long[long.venue=="H"].set_index("fixture_id")[FEAT].add_prefix("home_")
away = long[long.venue=="A"].set_index("fixture_id")[FEAT].add_prefix("away_")

base = m.set_index("fixture_id")[["LeagueCode","Country","Season","Date","kickoff",
        "HomeTeam","AwayTeam","home_id","away_id","FTHG","FTAG","status","has_stats","has_events"]]
feat = base.join(home).join(away).reset_index()

feat["diff_pts_r6"]    = feat.home_pts_r6 - feat.away_pts_r6
feat["diff_gf_r6"]     = feat.home_gf_r6  - feat.away_gf_r6
feat["diff_xg_exp"]    = feat.home_xg_exp - feat.away_xg_exp
feat["diff_pts_exp"]   = feat.home_pts_exp - feat.away_pts_exp
feat["diff_ps_ppg"]    = feat.home_ps_ppg - feat.away_ps_ppg
feat["diff_table_pos"] = feat.home_table_pos_pct - feat.away_table_pos_pct
feat["diff_table_ppg"] = feat.home_table_ppg - feat.away_table_ppg
feat["exp_gf_home_r6"]  = (feat.home_gf_r6  + feat.away_ga_r6)  / 2
feat["exp_gf_away_r6"]  = (feat.away_gf_r6  + feat.home_ga_r6)  / 2
feat["exp_gf_home_szn"] = (feat.home_gf_exp + feat.away_ga_exp) / 2   # season-to-date
feat["exp_gf_away_szn"] = (feat.away_gf_exp + feat.home_ga_exp) / 2

pq.write_table(pa.Table.from_pandas(feat.convert_dtypes(), preserve_index=False),
               OUT/"matches_features.parquet", write_statistics=False, version="2.6")

print(f"\nmatches_features.parquet: {len(feat):,} rows, {feat.shape[1]} cols")
print(f"  overall form r{WIN_ALL} + expanding : {feat.home_gf_r6.notna().sum():,} rows")
print(f"  venue form  v{WIN_VENUE} + expanding : {feat.home_gf_v4.notna().sum():,} rows")
print(f"  live table class          : {feat.home_table_class.notna().sum():,} rows")
print(f"  prev-season class         : {feat.home_ps_class.notna().sum():,} rows")
chk = pq.read_table(OUT/"matches_features.parquet", use_threads=False)
print(f"  re-read OK: {chk.num_rows:,} rows")
