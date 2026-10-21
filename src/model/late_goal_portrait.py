"""Модель (не перебор порогов) для «портрета» матча с голом после 75-й минуты.

Спецификация: specs/разведочный-анализ-поздних-голов.md (чат «Анализ
данных»/«Обработка данных»). Продолжение их работы: честный перебор порогов
(src/jobs/scan_goal_after_cutoff.py) не прошёл гейт 0.67 на holdout — следующий
шаг, предложенный «Анализ данных» владельцу, — простая модель на новых
признаках без утечки (`exports/late_goal_features_group_a.csv`,
`exports/late_goal_features_group_b.csv`, build_late_goal_features.py).

Цели — те же, что в scan_goal_after_cutoff.build_feature_table (cutoff=75):
`any_after` (гол любой команды после 75') и `dominant_after` (гол команды-
лидера по владению за весь матч). Атрибуция dominant/other и сплит
train(2018-2022)/holdout(2023-2025)/monitor(2026-2027) — переиспользованы
оттуда, не пересчитаны заново.

Модели: неглубокое дерево решений (читаемый «портрет» — явные пороги) и
логрегрессия со стандартизацией (ранжирование признаков по значимости,
устойчивость знака коэффициента проверяется бутстрэпом на train). Гейтинг —
train n>=300, holdout n>=150, доля на holdout не ниже, чем в scan (0.60/0.55
на ДИ-нижнюю), чтобы не повторить переобучение перебора.

Использование:
    python3 -m src.model.late_goal_portrait [--group a|b|both]
"""

from __future__ import annotations

import argparse
import random

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, export_text

from src.common.stats import wilson_interval
from src.db.connection import connect, database_url
from src.jobs.scan_goal_after_cutoff import (
    DEFAULT_CUTOFF_MINUTE,
    build_feature_table,
    load_base_sample,
    load_goal_events,
)

RANDOM_STATE = 42
MIN_N_TRAIN = 300
MIN_N_HOLDOUT = 150
HOLDOUT_SHARE_THRESHOLD = 0.60
HOLDOUT_CI_LOW_THRESHOLD = 0.55
N_BOOTSTRAP = 200

TARGETS = ("any_after", "dominant_after")

GROUP_A_PATH = "exports/late_goal_features_group_a.csv"
GROUP_B_PATH = "exports/late_goal_features_group_b.csv"

GROUP_A_FEATURES: tuple[str, ...] = (
    "h1_total_shots_diff", "h1_shots_on_goal_diff", "h1_shots_off_goal_diff",
    "h1_blocked_shots_diff", "h1_shots_insidebox_diff", "h1_shots_outsidebox_diff",
    "h1_possession_diff", "h1_corner_kicks_diff", "h1_offsides_diff",
    "h1_passes_pct_diff", "h1_goalkeeper_saves_diff", "h1_yellow_cards_diff",
    "h1_xg_diff", "h1_xg_overperformance_diff",
)
GROUP_B_FEATURES: tuple[str, ...] = (
    "goals_46_75_dominant", "goals_46_75_other",
    "red_card_before_75_dominant", "red_card_before_75_other",
    "subst_count_before_75_dominant", "subst_count_before_75_other",
    "yellow_cards_60_75_dominant", "yellow_cards_60_75_other",
)


def load_targets(conn) -> pd.DataFrame:
    """Таргеты/сплит из scan_goal_after_cutoff, cutoff=75 — не пересчитаны заново."""
    base = load_base_sample(conn)
    base = base[base["events_fetched"].fillna(False)]
    goals = load_goal_events(conn, base["fixture_id"].tolist())
    table = build_feature_table(base, goals, pd.DataFrame(columns=["fixture_id", "team_id", "minute"]), DEFAULT_CUTOFF_MINUTE)
    return table[["fixture_id", "league_id", "season", "split", "any_after", "dominant_after"]]


def load_group_features(path: str, columns: tuple[str, ...]) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["fixture_id", *columns]]


def _wilson_report(y: pd.Series) -> dict:
    n = int(len(y))
    successes = int(y.sum())
    share = successes / n if n else None
    low, high = wilson_interval(successes, n)
    return {"n": n, "share": share, "ci_low": low, "ci_high": high}


def fit_tree_portrait(
    X_train: pd.DataFrame, y_train: pd.Series, X_holdout: pd.DataFrame, y_holdout: pd.Series, feature_names: list[str]
) -> dict:
    tree = DecisionTreeClassifier(max_depth=3, min_samples_leaf=MIN_N_TRAIN, random_state=RANDOM_STATE)
    tree.fit(X_train, y_train)

    leaf_train = tree.apply(X_train)
    leaf_holdout = tree.apply(X_holdout)
    rules = export_text(tree, feature_names=feature_names)

    leaves = []
    for leaf_id in sorted(set(leaf_train)):
        train_mask = leaf_train == leaf_id
        train_report = _wilson_report(y_train[train_mask])
        holdout_mask = leaf_holdout == leaf_id
        holdout_report = _wilson_report(y_holdout[holdout_mask]) if holdout_mask.any() else {"n": 0, "share": None, "ci_low": None, "ci_high": None}

        passes_train = train_report["n"] >= MIN_N_TRAIN
        passes_holdout = (
            holdout_report["n"] >= MIN_N_HOLDOUT
            and holdout_report["share"] is not None
            and holdout_report["share"] >= HOLDOUT_SHARE_THRESHOLD
            and holdout_report["ci_low"] >= HOLDOUT_CI_LOW_THRESHOLD
        )
        leaves.append(
            {
                "leaf_id": int(leaf_id),
                "train": train_report,
                "holdout": holdout_report,
                "confirmed_on_holdout": bool(passes_train and passes_holdout),
            }
        )
    return {"rules": rules, "leaves": leaves}


def fit_logreg_ranking(
    X_train: pd.DataFrame, y_train: pd.Series, feature_names: list[str], n_bootstrap: int = N_BOOTSTRAP
) -> list[dict]:
    """Коэффициенты логрегрессии на стандартизованных признаках + устойчивость
    знака бутстрэпом (без statsmodels — доля бутстрэп-повторов с тем же знаком,
    что и на полном train, как грубая замена p-value)."""
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X_train)
    model = LogisticRegression(max_iter=1000)
    model.fit(Xs, y_train)
    coefs = model.coef_[0]

    rng = random.Random(RANDOM_STATE)
    n = len(X_train)
    sign_matches = np.zeros(len(feature_names))
    for _ in range(n_bootstrap):
        idx = [rng.randrange(n) for _ in range(n)]
        Xb, yb = Xs[idx], y_train.to_numpy()[idx]
        if len(set(yb)) < 2:
            continue
        m = LogisticRegression(max_iter=1000)
        m.fit(Xb, yb)
        sign_matches += (np.sign(m.coef_[0]) == np.sign(coefs)).astype(int)

    out = []
    for name, coef, matches in zip(feature_names, coefs, sign_matches):
        out.append({"feature": name, "coef_std": float(coef), "sign_stability": float(matches / n_bootstrap)})
    return sorted(out, key=lambda r: abs(r["coef_std"]), reverse=True)


def run_group(targets_df: pd.DataFrame, features_df: pd.DataFrame, feature_cols: tuple[str, ...], group_name: str) -> dict:
    merged = targets_df.merge(features_df, on="fixture_id", how="inner")
    merged = merged.dropna(subset=list(feature_cols))
    report = {"group": group_name, "n_merged": int(len(merged)), "targets": {}}

    for target in TARGETS:
        train = merged[merged["split"] == "train"]
        holdout = merged[merged["split"] == "holdout"]
        if len(train) < MIN_N_TRAIN or len(holdout) < MIN_N_HOLDOUT:
            report["targets"][target] = {"skipped": True, "n_train": len(train), "n_holdout": len(holdout)}
            continue

        X_train, y_train = train[list(feature_cols)], train[target].astype(int)
        X_holdout, y_holdout = holdout[list(feature_cols)], holdout[target].astype(int)

        tree_result = fit_tree_portrait(X_train, y_train, X_holdout, y_holdout, list(feature_cols))
        logreg_result = fit_logreg_ranking(X_train, y_train, list(feature_cols))

        report["targets"][target] = {
            "n_train": len(train),
            "n_holdout": len(holdout),
            "base_rate_train": _wilson_report(y_train),
            "base_rate_holdout": _wilson_report(y_holdout),
            "tree": tree_result,
            "logreg_ranking": logreg_result,
        }
    return report


def print_report(report: dict) -> None:
    print(f"\n=== Группа признаков: {report['group']} (n={report['n_merged']}) ===")
    for target, result in report["targets"].items():
        print(f"\n--- Цель: {target} ---")
        if result.get("skipped"):
            print(f"  пропущено: n_train={result['n_train']}, n_holdout={result['n_holdout']} (меньше гейта)")
            continue
        br_tr, br_ho = result["base_rate_train"], result["base_rate_holdout"]
        print(f"  база (без признаков): train доля={br_tr['share']:.3f} (n={br_tr['n']}), "
              f"holdout доля={br_ho['share']:.3f} (n={br_ho['n']})")

        print("  Топ-5 признаков по |коэффициенту| логрегрессии (станд.), устойчивость знака на бутстрэпе:")
        for row in result["logreg_ranking"][:5]:
            print(f"    {row['feature']}: coef={row['coef_std']:+.3f}, устойчивость знака={row['sign_stability']:.2f}")

        confirmed = [leaf for leaf in result["tree"]["leaves"] if leaf["confirmed_on_holdout"]]
        print(f"  Листья дерева, подтверждённые на holdout (train n>={MIN_N_TRAIN}, "
              f"holdout n>={MIN_N_HOLDOUT}, доля>={HOLDOUT_SHARE_THRESHOLD}, ДИ-нижняя>={HOLDOUT_CI_LOW_THRESHOLD}): "
              f"{len(confirmed)} из {len(result['tree']['leaves'])}")
        for leaf in confirmed:
            print(f"    leaf {leaf['leaf_id']}: train доля={leaf['train']['share']:.3f} (n={leaf['train']['n']}), "
                  f"holdout доля={leaf['holdout']['share']:.3f} (n={leaf['holdout']['n']}, "
                  f"ДИ-нижняя={leaf['holdout']['ci_low']:.3f})")
        if not confirmed:
            print("    (ничего не подтвердилось на holdout)")
        print("  Правила дерева (train):")
        print("    " + result["tree"]["rules"].replace("\n", "\n    "))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("a", "b", "both"), default="both")
    args = parser.parse_args()

    with connect(database_url()) as conn:
        targets_df = load_targets(conn)

    if args.group in ("b", "both"):
        group_b = load_group_features(GROUP_B_PATH, GROUP_B_FEATURES)
        print_report(run_group(targets_df, group_b, GROUP_B_FEATURES, "B (события, 134к)"))

    if args.group in ("a", "both"):
        group_a = load_group_features(GROUP_A_PATH, GROUP_A_FEATURES)
        print_report(run_group(targets_df, group_a, GROUP_A_FEATURES, "A (1-й тайм + xG, 26к)"))

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
