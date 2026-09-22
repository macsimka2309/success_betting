"""
Value betting backtest on Over/Under 2.5.

Model P(Over) from our features only (no odds -> an independent opinion), compare
with the bookmaker's de-vigged probability, and bet when our edge exceeds a
threshold. Stakes are flat (1 unit); returns use the ACTUAL offered price, not the
de-vigged one, because that is what you would really be paid.

The decisive diagnostic is not accuracy - it is whether, inside each edge bucket,
the real outcome rate beats what the price implies. Accuracy can look fine while
losing money, and a small number of bets makes ROI extremely noisy, so binomial
confidence intervals are reported alongside.
"""
import numpy as np, pandas as pd, pyarrow.parquet as pq, warnings
warnings.filterwarnings("ignore")
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import log_loss
from sklearn.calibration import CalibratedClassifierCV

full=pq.read_table("data/apifootball/matches_features.parquet",use_threads=False).to_pandas()
full["tot"]=pd.to_numeric(full.FTHG)+pd.to_numeric(full.FTAG)
full["over25"]=(full.tot>2.5).astype(int)
od=pq.read_table("data/apifootball/odds_joined.parquet",use_threads=False).to_pandas()
for c in ["oOver","oUnder","FTHG","FTAG"]: od[c]=pd.to_numeric(od[c],errors="coerce")
od["tot"]=od.FTHG+od.FTAG; od["over25"]=(od.tot>2.5).astype(int)
od=od.dropna(subset=["oOver","oUnder","tot"])
inv=lambda x:1/x; s=inv(od.oOver)+inv(od.oUnder)
od["p_mkt_over"]=inv(od.oOver)/s; od["p_mkt_under"]=inv(od.oUnder)/s
od["overround"]=s
FE=[c for c in full.columns if c.startswith(("home_","away_","diff_","exp_gf_"))
    and c not in("home_id","away_id") and pd.api.types.is_numeric_dtype(full[c])]

tr=full[full.Date<"2024-06-01"]
te=od[od.Date>="2024-06-01"].copy()
print(f"обучение: {len(tr):,} матчей | тест (с кэфами O/U): {len(te):,}")
print(f"средняя маржа букмекера: {100*(te.overround.mean()-1):.2f}%\n")

# модель на фичах + калибровка (важно для value: нужна честная вероятность)
base=HistGradientBoostingClassifier(max_iter=300,learning_rate=.06,random_state=0)
clf=CalibratedClassifierCV(base,method="isotonic",cv=3).fit(tr[FE],tr.over25)
te["p_model"]=clf.predict_proba(te[FE])[:,1]
print(f"logloss наша модель {log_loss(te.over25,te.p_model):.4f} | рынок {log_loss(te.over25,te.p_mkt_over):.4f}")
print(f"-> модель {'ЛУЧШЕ' if log_loss(te.over25,te.p_model)<log_loss(te.over25,te.p_mkt_over) else 'ХУЖЕ'} рынка\n")

te["edge_over"] =te.p_model-te.p_mkt_over
te["edge_under"]=(1-te.p_model)-te.p_mkt_under

def backtest(df,thr):
    bets=[]
    o=df[df.edge_over>=thr];  bets.append(pd.DataFrame({"win":o.over25,"price":o.oOver,"side":"OVER"}))
    u=df[df.edge_under>=thr]; bets.append(pd.DataFrame({"win":1-u.over25,"price":u.oUnder,"side":"UNDER"}))
    b=pd.concat(bets)
    if len(b)==0: return None
    ret=np.where(b.win==1,b.price-1,-1.0)
    roi=ret.mean(); n=len(b)
    se=ret.std(ddof=1)/np.sqrt(n) if n>1 else np.nan
    return dict(n=n,hit=b.win.mean(),avg_price=b.price.mean(),roi=100*roi,
                lo=100*(roi-1.96*se),hi=100*(roi+1.96*se))

print(f"{'порог value':>12s} {'ставок':>7s} {'% захода':>9s} {'ср.кэф':>7s} {'ROI %':>8s} {'95% интервал':>18s}")
for thr in [0.02,0.03,0.05,0.07,0.10,0.15]:
    r=backtest(te,thr)
    if r: print(f"{thr:>12.0%} {r['n']:>7,} {100*r['hit']:>8.1f}% {r['avg_price']:>7.2f} "
                f"{r['roi']:>+8.2f} {'['+format(r['lo'],'+.1f')+' .. '+format(r['hi'],'+.1f')+']':>18s}")

print("\nДИАГНОСТИКА: в каждой корзине value — реальная доля Over против того, что заложено в цене")
te["bucket"]=pd.cut(te.edge_over,[-1,-.10,-.05,-.02,.02,.05,.10,1],
                    labels=["<-10%","-10..-5%","-5..-2%","±2%","+2..5%","+5..10%",">+10%"])
g=te.groupby("bucket",observed=True).apply(lambda x:pd.Series({
    "n":len(x),"наша p":x.p_model.mean(),"цена подразум.":x.p_mkt_over.mean(),
    "факт Over":x.over25.mean()}),include_groups=False)
g["кто прав"]=np.where((g["факт Over"]-g["цена подразум."]).abs()<(g["факт Over"]-g["наша p"]).abs(),"РЫНОК","модель")
print(g.to_string(float_format=lambda v:f"{v:7.3f}"))
