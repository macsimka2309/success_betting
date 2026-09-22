"""
Join football-data.co.uk odds onto the API-Football match table, WITHOUT biasing
the sample.

An earlier version keyed on (LeagueCode, Date, FTHG, FTAG) and kept only keys
unique on both sides. That silently selected high-scoring matches: a 1-1 collides
with other 1-1s on the same matchday and gets dropped, a 3-1 survives. The joined
sample came out at 55.8% Over 2.5 against 50.4% in the population, which is enough
to manufacture a fake +4% ROI on "always bet Over".

The fix is a two-pass join that never uses the score as a key:
  pass 1  learn a team-name map from the unambiguous score-keyed matches
          (those pairings were verified 95.4% consistent)
  pass 2  extend the map with fuzzy name matching inside each league
  pass 3  join on (LeagueCode, Date, home_name, away_name) only
The score is then used purely as a CHECK that the pairing is right.

Output: data/apifootball/odds_joined.parquet
"""
import difflib, numpy as np, pandas as pd, pyarrow as pa, pyarrow.parquet as pq
from pathlib import Path
OUT=Path("data/apifootball")

api=pq.read_table(OUT/"matches_features.parquet",use_threads=False).to_pandas()
api["FTHG"]=pd.to_numeric(api.FTHG);api["FTAG"]=pd.to_numeric(api.FTAG)

mn=pd.read_csv("data/combined_main_leagues.csv",low_memory=False)
mn["D"]=pd.to_datetime(mn.Date,format="%d/%m/%Y",errors="coerce").dt.strftime("%Y-%m-%d")
main=mn.rename(columns={"LeagueCode":"LC"})[["LC","D","HomeTeam","AwayTeam","FTHG","FTAG",
      "AvgH","AvgD","AvgA","Avg>2.5","Avg<2.5","B365H","B365D","B365A"]].copy()
main.columns=["LC","D","hn","an","hg","ag","oH","oD","oA","oOver","oUnder","bH","bD","bA"]
ex=pd.read_csv("data/combined_extra_leagues.csv",low_memory=False)
ex["D"]=pd.to_datetime(ex.MatchDate,errors="coerce").dt.strftime("%Y-%m-%d")
CC={"Argentina":"ARG","Austria":"AUT","Brazil":"BRA","China":"CHN","Denmark":"DNK","Finland":"FIN",
    "Ireland":"IRL","Japan":"JPN","Mexico":"MEX","Norway":"NOR","Poland":"POL","Romania":"ROU",
    "Russia":"RUS","Sweden":"SWE","Switzerland":"SWZ","USA":"USA"}
ex["LC"]=ex.Country.map(CC)
extra=ex[["LC","D","Home","Away","HG","AG","AvgCH","AvgCD","AvgCA"]].copy()
extra.columns=["LC","D","hn","an","hg","ag","oH","oD","oA"]
for c in ["oOver","oUnder","bH","bD","bA"]: extra[c]=np.nan
fd=pd.concat([main,extra],ignore_index=True)
for c in ["oH","oD","oA","oOver","oUnder","bH","bD","bA"]: fd[c]=pd.to_numeric(fd[c],errors="coerce")
fd=fd[fd.LC.notna()&fd.D.notna()&fd.hg.notna()&fd.ag.notna()].copy()
fd["hg"]=fd.hg.astype(int); fd["ag"]=fd.ag.astype(int)
print(f"football-data строк: {len(fd):,} | api матчей: {len(api):,}")

# ---- pass 1: learn name map from unambiguous score-keyed pairs ----
K=["LC","D","hg","ag"]
ak=api.assign(LC=api.LeagueCode,D=api.Date,hg=api.FTHG,ag=api.FTAG)
seed=(ak[~ak.duplicated(K,keep=False)].merge(fd[~fd.duplicated(K,keep=False)],on=K,how="inner"))
pairs=pd.concat([seed[["LC","hn","HomeTeam"]].rename(columns={"hn":"fdname","HomeTeam":"apiname"}),
                 seed[["LC","an","AwayTeam"]].rename(columns={"an":"fdname","AwayTeam":"apiname"})])
NAME_MAP=(pairs.groupby(["LC","fdname"]).apiname.agg(lambda s:s.value_counts().index[0]).to_dict())
print(f"pass 1: выучено {len(NAME_MAP):,} соответствий имён из {len(seed):,} однозначных пар")

# ---- pass 2: fuzzy-match names the seed never covered ----
api_by_lc=api.groupby("LeagueCode").apply(lambda d:sorted(set(d.HomeTeam)|set(d.AwayTeam)),include_groups=False).to_dict()
fd_names=pd.concat([fd[["LC","hn"]].rename(columns={"hn":"n"}),fd[["LC","an"]].rename(columns={"an":"n"})]).drop_duplicates()
added=0
for lc,n in fd_names.itertuples(index=False):
    if (lc,n) in NAME_MAP or lc not in api_by_lc: continue
    m=difflib.get_close_matches(str(n),api_by_lc[lc],n=1,cutoff=0.72)
    if m: NAME_MAP[(lc,n)]=m[0]; added+=1
print(f"pass 2: добавлено {added:,} соответствий по похожести имён -> всего {len(NAME_MAP):,}")

# ---- pass 3: join on names + date only (score NOT in the key) ----
fd["h_api"]=[NAME_MAP.get((l,n)) for l,n in zip(fd.LC,fd.hn)]
fd["a_api"]=[NAME_MAP.get((l,n)) for l,n in zip(fd.LC,fd.an)]
fd2=fd[fd.h_api.notna()&fd.a_api.notna()]
KEY=["LC","D","h_api","a_api"]
fd2=fd2[~fd2.duplicated(KEY,keep=False)]
ak2=api.assign(LC=api.LeagueCode,D=api.Date,h_api=api.HomeTeam,a_api=api.AwayTeam)
ak2=ak2[~ak2.duplicated(KEY,keep=False)]
j=ak2.merge(fd2,on=KEY,how="inner",suffixes=("","_fd"))

agree=((j.FTHG==j.hg)&(j.FTAG==j.ag)).mean()
print(f"\nсовпало матчей: {len(j):,}")
print(f"ПРОВЕРКА: счёт из двух источников совпадает у {100*agree:.2f}% (счёт НЕ был ключом)")
j=j[(j.FTHG==j.hg)&(j.FTAG==j.ag)].copy()          # drop the few genuine mismatches
tot=j.FTHG+j.FTAG
MAIN=set("E0 E1 E2 E3 SC0 D1 D2 I1 I2 SP1 SP2 F1 F2 N1 B1 P1 T1 G1".split())
pop=api[api.LeagueCode.isin(MAIN)&(api.Date>="2021-01-01")]
print(f"\nПРОВЕРКА СМЕЩЕНИЯ:")
print(f"  доля Over 2.5 в джойне    : {(tot>2.5).mean():.3f}")
print(f"  доля Over 2.5 в популяции : {((pd.to_numeric(pop.FTHG)+pd.to_numeric(pop.FTAG))>2.5).mean():.3f}")
print(f"  (в старой версии было 0.558 против 0.504 - смещение)")
print(f"\nс Over/Under 2.5: {j.oOver.notna().sum():,} | с 1X2: {j.oH.notna().sum():,} | лиг: {j.LeagueCode.nunique()}")
pq.write_table(pa.Table.from_pandas(j.convert_dtypes(),preserve_index=False),
               OUT/"odds_joined.parquet",write_statistics=False,version="2.6")
print(f"-> odds_joined.parquet ({len(j):,} строк)")
