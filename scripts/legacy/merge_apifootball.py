"""
Merge API-Football data into ONE match-level table.

Base = every match we downloaded from API-Football (data/apifootball/fixtures.parquet),
NOT the original football-data.co.uk dataset. Onto each fixture we attach:
  * home_/away_ in-match statistics (shots, possession, xG, cards, passes, ...)
  * home_/away_ event aggregates (cards, subs, penalty goals, own goals) + first-goal minute
  * availability flags (has_stats / has_events)

Output: data/apifootball/matches_merged.parquet  (one row per fixture)

Note: in-match stats/events describe what happened DURING the match, so they are not
features for that same match - they are raw material for later rolling FORM features.
"""
from pathlib import Path
import numpy as np, pandas as pd, pyarrow as pa, pyarrow.parquet as pq

OUT = Path("data/apifootball")
def rd(name, cols=None):
    return pq.read_table(OUT / f"{name}.parquet", columns=cols, use_threads=False).to_pandas()

def side_of(df):
    """home/away/None from team_id vs home_id/away_id, NA-safe (nullable Int64)."""
    tid = pd.to_numeric(df.team_id, errors="coerce").to_numpy()
    hid = pd.to_numeric(df.home_id, errors="coerce").to_numpy()
    aid = pd.to_numeric(df.away_id, errors="coerce").to_numpy()
    return np.where(tid == hid, "home", np.where(tid == aid, "away", None))

fx = rd("fixtures"); st = rd("statistics"); ev = rd("events")
print(f"loaded: fixtures {len(fx):,} | statistics {len(st):,} | events {len(ev):,}")
ids = fx[["fixture_id","home_id","away_id"]]

# ---------------------------------------------------------------- statistics
STAT_COLS = ["shots_on_goal","shots_off_goal","total_shots","blocked_shots",
    "shots_insidebox","shots_outsidebox","fouls","corner_kicks","offsides",
    "ball_possession","yellow_cards","red_cards","goalkeeper_saves","total_passes",
    "passes_accurate","passes_pct","expected_goals","goals_prevented","substitutions",
    "free_kicks","assists","counter_attacks","cross_attacks","goals","goal_attempts",
    "throwins","medical_treatment"]
for c in STAT_COLS:
    st[c] = pd.to_numeric(st[c].astype("string").str.replace("%","",regex=False).str.strip(),
                          errors="coerce")
st = st.merge(ids, on="fixture_id", how="left")
st["side"] = side_of(st)
st = st[st.side.notna()]
home_st = st[st.side=="home"].set_index("fixture_id")[STAT_COLS].add_prefix("home_")
away_st = st[st.side=="away"].set_index("fixture_id")[STAT_COLS].add_prefix("away_")

# ---------------------------------------------------------------- events
ev = ev.merge(ids, on="fixture_id", how="left")
ev["side"] = side_of(ev)
d = ev.detail.astype("string").fillna("")
ev["ev_yellow"]    = ((ev.type=="Card") & d.eq("Yellow Card")).astype("int8")
ev["ev_red"]       = ((ev.type=="Card") & (d.str.contains("Red",case=False) | d.str.contains("Second Yellow",case=False))).astype("int8")
ev["ev_subs"]      = (ev.type=="subst").astype("int8")
ev["ev_pen_goals"] = ((ev.type=="Goal") & d.eq("Penalty")).astype("int8")
ev["ev_own_goals"] = ((ev.type=="Goal") & d.eq("Own Goal")).astype("int8")
AGG=["ev_yellow","ev_red","ev_subs","ev_pen_goals","ev_own_goals"]
evs = ev[ev.side.notna()]
agg = evs.groupby(["fixture_id","side"])[AGG].sum()
home_ev = agg.xs("home",level="side").add_prefix("home_")
away_ev = agg.xs("away",level="side").add_prefix("away_")

# first goal minute + side
goals = ev[ev.type=="Goal"].copy()
goals["min_tot"] = goals.minute.fillna(0).astype("float") + goals.minute_extra.fillna(0).astype("float")
first = goals.sort_values("min_tot").groupby("fixture_id").first()
match_ev = pd.DataFrame({"first_goal_minute": first["minute"], "first_goal_side": side_of(first)},
                        index=first.index)

# ---------------------------------------------------------------- assemble
m = fx.set_index("fixture_id").join(home_st).join(away_st).join(home_ev).join(away_ev).join(match_ev)
m["has_stats"]  = m.index.isin(set(st.fixture_id))
m["has_events"] = m.index.isin(set(evs.fixture_id))
ev_cols = [c for c in m.columns if c.startswith(("home_ev_","away_ev_"))]
m.loc[m.has_events, ev_cols] = m.loc[m.has_events, ev_cols].fillna(0)
m = m.reset_index()

pq.write_table(pa.Table.from_pandas(m.convert_dtypes(), preserve_index=False),
               OUT/"matches_merged.parquet", write_statistics=False, version="2.6")

fin = m.status.isin(["FT","AET","PEN"])
print(f"\nmatches_merged.parquet: {len(m):,} rows, {m.shape[1]} cols | finished {fin.sum():,}")
print(f"  with statistics: {m.has_stats.sum():,} ({100*m.has_stats.sum()/fin.sum():.1f}% of finished)")
print(f"  with events    : {m.has_events.sum():,} ({100*m.has_events.sum()/fin.sum():.1f}% of finished)")
chk = pq.read_table(OUT/"matches_merged.parquet", use_threads=False)
print(f"  re-read OK: {chk.num_rows:,} rows, {chk.num_columns} cols")
# tiny sanity peek
s=m[fin & m.has_stats].iloc[0]
print(f"\nsample: {s.HomeTeam} {int(s.FTHG)}-{int(s.FTAG)} {s.AwayTeam} ({s.LeagueCode} {s.Date}) "
      f"shots {s.home_total_shots}-{s.away_total_shots}, xG {s.home_expected_goals}-{s.away_expected_goals}, "
      f"1st goal min {s.first_goal_minute} ({s.first_goal_side})")
