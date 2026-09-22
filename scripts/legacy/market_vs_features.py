"""
Does our feature set add anything on top of the bookmaker's price?

Three models, same split, same learner:
  A. MARKET only      - odds-implied probabilities
  B. FEATURES only    - our rolling form / table / prev-season features
  C. MARKET+FEATURES  - both
plus the bookmaker's own de-vigged Over-2.5 probability as a zero-model reference.

If C ~= A, the market already prices everything we computed.
"""
import numpy as np, pandas as pd, pyarrow.parquet as pq, warnings
warnings.filterwarnings("ignore")
from sklearn.ensemble import HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.metrics import mean_absolute_error, log_loss

d=pq.read_table("data/apifootball/odds_joined.parquet",use_threads=False).to_pandas()
for c in ["FTHG","FTAG","oH","oD","oA","oOver","oUnder"]: d[c]=pd.to_numeric(d[c],errors="coerce")
d["tot"]=d.FTHG+d.FTAG; d["over25"]=(d.tot>2.5).astype(int)

# market features: de-vigged probabilities
inv=lambda x: 1/x
s=inv(d.oH)+inv(d.oD)+inv(d.oA)
d["mkt_pH"]=inv(d.oH)/s; d["mkt_pD"]=inv(d.oD)/s; d["mkt_pA"]=inv(d.oA)/s
d["mkt_logit"]=np.log(d.mkt_pH/d.mkt_pA)
so=inv(d.oOver)+inv(d.oUnder)
d["mkt_pOver"]=inv(d.oOver)/so
MKT=["mkt_pH","mkt_pD","mkt_pA","mkt_logit"]
MKT_OU=MKT+["mkt_pOver"]

FE=[c for c in d.columns if c.startswith(("home_","away_","diff_","exp_gf_"))
    and c not in("home_id","away_id") and pd.api.types.is_numeric_dtype(d[c])]
d=d.sort_values("Date")
print(f"фичей: {len(FE)} | матчей с 1X2: {d.mkt_pH.notna().sum():,} | с O/U: {d.mkt_pOver.notna().sum():,}\n")

def evaluate(df,mkt_cols,label):
    df=df.dropna(subset=mkt_cols+["tot"])
    tr=df[df.Date<"2024-06-01"]; te=df[df.Date>="2024-06-01"]
    if len(te)<800: print(f"  {label}: мало данных в тесте ({len(te)})"); return
    print(f"\n{'='*74}\n{label}: train {len(tr):,} | test {len(te):,}\n{'='*74}")
    has_ou = "mkt_pOver" in mkt_cols
    print(f"{'модель':26s} {'MAE тотал':>10s} {'Δ':>8s}" + (f" {'logloss O2.5':>13s} {'Δ':>8s}" if has_ou else ""))
    base_mae=base_ll=None
    for name,cols in [("A. только рынок",mkt_cols),("B. только наши фичи",FE),("C. рынок + фичи",mkt_cols+FE)]:
        maes=[];lls=[]
        for sd in (0,1):
            m1=HistGradientBoostingRegressor(loss="poisson",max_iter=300,learning_rate=.06,random_state=sd)
            m1.fit(tr[cols],tr.tot); maes.append(mean_absolute_error(te.tot,m1.predict(te[cols])))
            if has_ou:
                m2=HistGradientBoostingClassifier(max_iter=300,learning_rate=.06,random_state=sd)
                m2.fit(tr[cols],tr.over25); lls.append(log_loss(te.over25,m2.predict_proba(te[cols])[:,1]))
        mae=np.mean(maes); ll=np.mean(lls) if has_ou else None
        if base_mae is None: base_mae,base_ll=mae,ll
        line=f"{name:26s} {mae:>10.4f} {mae-base_mae:>+8.4f}"
        if has_ou: line+=f" {ll:>13.4f} {ll-base_ll:>+8.4f}"
        print(line)
    if has_ou:
        llb=log_loss(te.over25,te.mkt_pOver)
        print(f"{'--- линия букмекера O2.5':26s} {'':>10s} {'':>8s} {llb:>13.4f}  <- эталон")
        print(f"    (базовая доля Over 2.5 в тесте: {te.over25.mean():.3f})")

MAIN=set("E0 E1 E2 E3 SC0 D1 D2 I1 I2 SP1 SP2 F1 F2 N1 B1 P1 T1 G1".split())
evaluate(d[d.LeagueCode.isin(MAIN)], MKT_OU, "MAIN-лиги (есть Over/Under 2.5)")
evaluate(d[~d.LeagueCode.isin(MAIN)], MKT, "EXTRA-лиги (только 1X2, рынок тоньше)")
evaluate(d, MKT, "ВСЕ 34 лиги (1X2)")
