"""Прогноз исходов будущих матчей и отбор ставок по коэффициентам.

Спецификация: specs/модель-предсказаний.md, «Инференс и отбор ставок».

Модели — LightGBM из `models/full_stats/` (обучены на полной статистике).
Коэффициенты — последний снимок Pinnacle; если по конкретному исходу у
Pinnacle снимка нет, берётся 1xBet (помечается колонкой `bookmaker` — это
менее точный букмекер, см. decision log в спецификации). Тотал сверяется
только по точной линии `league_total_line`. Результат — Parquet в
`models/bets/`, в Git не попадает.

Использование:
    python3 -m src.model.predict [--models-dir models/full_stats] [--out-dir models/bets]
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

import joblib
import pandas as pd
import psycopg

from src.db.connection import connect, database_url
from src.model.features import FEATURE_COLUMNS
from src.model.train import coerce_feature_columns

ODDS_MIN = 1.5
ODDS_MAX = 2.5
# Порядок — приоритет: Pinnacle точнее 1xBet, берём его первым и используем
# 1xBet только как откат для исходов без снимка Pinnacle (08.10.2026 решение
# владельца: без этого слишком много матчей выпадало из-за отсутствия
# Pinnacle, хотя 1xBet котировка была).
BOOKMAKERS_BY_PRIORITY = ("Pinnacle", "1xBet")
LINE_PATTERN = re.compile(r"^(Over|Under) ([+-]?\d+(?:\.\d+)?)$")

RESULT_CLASSES = ("A", "D", "H")  # порядок колонок predict_proba LightGBM (sorted)


def load_upcoming_features(conn: psycopg.Connection) -> pd.DataFrame:
    """ФТ-1а (specs/проверка-ставок-новостями.md, чат «Поиск ставок»):
    `kickoff_at`/`status_short` добавлены в выгрузку, чтобы соседний чат не
    запрашивал время и статус матчей отдельным чтением."""
    feature_names = dict.fromkeys(["fixture_id", "match_date", "league_total_line", *FEATURE_COLUMNS])
    columns = ", ".join(
        [f"f.{c}" for c in feature_names]
        + ["th.name AS home_team", "ta.name AS away_team", "fx.kickoff_at", "fx.status_short"]
    )
    sql = f"""
        SELECT {columns}
        FROM ml_match_features f
        JOIN teams th ON th.team_id = f.home_team_id
        JOIN teams ta ON ta.team_id = f.away_team_id
        JOIN fixtures fx ON fx.fixture_id = f.fixture_id
        WHERE f.reg_home IS NULL AND f.match_date >= current_date
        ORDER BY f.match_date, f.fixture_id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


ODDS_COLUMNS = ["fixture_id", "bet_type", "value", "odd", "bookmaker"]


def prefer_bookmaker(df: pd.DataFrame, priority: tuple[str, ...] = BOOKMAKERS_BY_PRIORITY) -> pd.DataFrame:
    """Из нескольких строк на один (fixture_id, bet_type, value) — от разных
    букмекеров — оставляет одну, по приоритету `priority` (первый найденный
    выигрывает). Букмекеры вне `priority` отбрасываются."""
    if df.empty:
        return pd.DataFrame(columns=ODDS_COLUMNS)
    rank = {name: i for i, name in enumerate(priority)}
    df = df[df["bookmaker"].isin(rank)].copy()
    if df.empty:
        return pd.DataFrame(columns=ODDS_COLUMNS)
    df["_priority"] = df["bookmaker"].map(rank)
    df = df.sort_values("_priority").drop_duplicates(subset=["fixture_id", "bet_type", "value"], keep="first")
    return df[ODDS_COLUMNS].reset_index(drop=True)


def load_odds(conn: psycopg.Connection, fixture_ids: list[int]) -> pd.DataFrame:
    """Последний снимок по каждому (матч, рынок, исход): Pinnacle, а если у
    Pinnacle снимка нет — откат на 1xBet (колонка `bookmaker` показывает,
    какой источник использован; см. `prefer_bookmaker`)."""
    if not fixture_ids:
        return pd.DataFrame(columns=ODDS_COLUMNS)
    sql = """
        SELECT DISTINCT ON (s.fixture_id, bt.name, v.value, b.name)
               s.fixture_id, bt.name AS bet_type, v.value, v.odd, b.name AS bookmaker
        FROM odds_values v
        JOIN odds_snapshots s ON s.snapshot_id = v.snapshot_id
        JOIN bookmakers b ON b.bookmaker_id = v.bookmaker_id
        JOIN bet_types bt ON bt.bet_type_id = v.bet_type_id
        WHERE b.name = ANY(%s)
          AND bt.name IN ('Match Winner', 'Goals Over/Under')
          AND s.fixture_id = ANY(%s)
        ORDER BY s.fixture_id, bt.name, v.value, b.name, s.taken_at DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (list(BOOKMAKERS_BY_PRIORITY), fixture_ids))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return prefer_bookmaker(pd.DataFrame(rows, columns=cols))


def predict_predictions(
    features: pd.DataFrame, result_model, total_model
) -> pd.DataFrame:
    """По строке на матч: вероятности H/D/A и Over/Under на линии матча."""
    X = coerce_feature_columns(features)[list(FEATURE_COLUMNS)]
    proba_1x2 = result_model.predict_proba(X)
    columns = list(result_model.classes_)
    p = {c: proba_1x2[:, columns.index(c)] for c in RESULT_CLASSES}
    p_over = total_model.predict_proba(X)[:, 1]

    out = features[
        ["fixture_id", "match_date", "home_team", "away_team", "league_total_line", "kickoff_at", "status_short"]
    ].copy()
    out["p_home"] = p["H"]
    out["p_draw"] = p["D"]
    out["p_away"] = p["A"]
    out["p_over"] = p_over
    out["p_under"] = 1.0 - p_over
    return out.reset_index(drop=True)


def _parse_line(value: str) -> tuple[str, float] | None:
    match = LINE_PATTERN.match(value)
    if match is None:
        return None
    return match.group(1), float(match.group(2))


def build_bets(predictions: pd.DataFrame, odds: pd.DataFrame) -> pd.DataFrame:
    """Исходы с коэффициентом (Pinnacle, либо 1xBet в откате) в
    [ODDS_MIN; ODDS_MAX] и EV = p × кф − 1."""
    lines = predictions.set_index("fixture_id")["league_total_line"].to_dict()
    probs = predictions.set_index("fixture_id")
    rows = []
    for odd_row in odds.itertuples(index=False):
        if odd_row.fixture_id not in probs.index:
            continue
        if not (ODDS_MIN <= float(odd_row.odd) <= ODDS_MAX):
            continue
        pred = probs.loc[odd_row.fixture_id]
        if odd_row.bet_type == "Match Winner":
            side_prob = {"Home": pred["p_home"], "Draw": pred["p_draw"], "Away": pred["p_away"]}.get(odd_row.value)
            if side_prob is None:
                continue
            market, outcome = "1X2", {"Home": "H", "Draw": "D", "Away": "A"}[odd_row.value]
        else:
            parsed = _parse_line(odd_row.value)
            line = lines.get(odd_row.fixture_id)
            if parsed is None or line is None or pd.isna(line):
                continue
            side, value = parsed
            if abs(value - float(line)) > 1e-9:
                continue
            market, outcome = "ТБ/ТМ", odd_row.value
            side_prob = pred["p_over"] if side == "Over" else pred["p_under"]

        odd = float(odd_row.odd)
        rows.append(
            {
                "fixture_id": odd_row.fixture_id,
                "match_date": pred["match_date"],
                "home_team": pred["home_team"],
                "away_team": pred["away_team"],
                "kickoff_at": pred["kickoff_at"],
                "status_short": pred["status_short"],
                "market": market,
                "outcome": outcome,
                "model_prob": float(side_prob),
                "odds": odd,
                "bookmaker": odd_row.bookmaker,
                "ev": float(side_prob) * odd - 1.0,
            }
        )
    columns = [
        "fixture_id", "match_date", "home_team", "away_team", "kickoff_at", "status_short",
        "market", "outcome", "model_prob", "odds", "bookmaker", "ev",
    ]
    return pd.DataFrame(rows, columns=columns).sort_values("ev", ascending=False).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default="models/full_stats")
    parser.add_argument("--out-dir", default="models/bets")
    args = parser.parse_args()

    models_dir = Path(args.models_dir)
    result_model = joblib.load(models_dir / "result_1x2_lightgbm.joblib")
    total_model = joblib.load(models_dir / "league_total_lightgbm.joblib")

    with connect(database_url()) as conn:
        features = load_upcoming_features(conn)
        odds = load_odds(conn, features["fixture_id"].tolist())

    predictions = predict_predictions(features, result_model, total_model)
    bets = build_bets(predictions, odds)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    predictions.to_parquet(out_dir / f"predictions_{stamp}.parquet", index=False)
    bets.to_parquet(out_dir / f"bets_{stamp}.parquet", index=False)

    print(f"матчей с прогнозом: {len(predictions)}; исходов с кф {ODDS_MIN}–{ODDS_MAX}: {len(bets)}")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print("\nТоп-20 по EV:")
        print(bets.head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
