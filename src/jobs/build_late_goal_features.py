"""Признаки для анализа поздних голов — группы А и Б.

Источник требований: сообщение чата «Анализ данных» 17.10.2026 (список
признаков с формулами). Разовый экспортный скрипт по образцу
`scan_goal_after_cutoff.py` — не входит в cron, только читает БД.

"Доминирующая сторона" = команда с бОльшим `ball_possession` за весь матч
(та же логика, что в `scan_goal_after_cutoff.build_feature_table`):
`dominant_side = home if home_possession >= away_possession else away`.

Группа А — из `fixture_statistics_halves` (`half=1`, честно без утечки за
75-ю минуту): требует наличия строки `half=1` у обеих команд, покрытие пока
частичное (проверяется отдельно перед использованием, сбор продолжается).

Группа Б — из `fixture_events`, минута ≤ 75 (или заданное окно); не
зависит от наполнения `fixture_statistics_halves`, доступна на всей базе.

Использование:
    python3 -m src.jobs.build_late_goal_features              # вывод в CSV
    python3 -m src.jobs.build_late_goal_features --out exports/late_goal_features.csv
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd
import psycopg

from src.db.connection import connect

DEFAULT_CUTOFF_MINUTE = 75
SUBST_YELLOW_WINDOW_START = 60

# Группа А: метрики half=1, которые считаются и атрибутируются напрямую
# (без специальной обработки, в отличие от xg/xg_overperformance).
HALF1_SIMPLE_METRICS = (
    "total_shots",
    "shots_on_goal",
    "shots_off_goal",
    "blocked_shots",
    "shots_insidebox",
    "shots_outsidebox",
    "ball_possession",
    "corner_kicks",
    "offsides",
    "passes_pct",
    "goalkeeper_saves",
    "yellow_cards",
)

# Группа А: переименование для признаков (ball_possession -> possession).
HALF1_FEATURE_NAME = {"ball_possession": "possession"}


def load_dominant_base(conn: psycopg.Connection) -> pd.DataFrame:
    """FT-матчи с полным владением/ударами у обеих команд (как РА-1
    в `scan_goal_after_cutoff`) — база для определения доминирующей стороны.
    """
    sql = """
        WITH stats AS (
            SELECT fixture_id, team_id, ball_possession
            FROM fixture_statistics
            WHERE ball_possession IS NOT NULL
        )
        SELECT
            f.fixture_id, f.league_id, f.season, f.home_team_id, f.away_team_id,
            f.ht_home, f.ht_away,
            sh.ball_possession AS home_possession,
            sa.ball_possession AS away_possession
        FROM fixtures f
        JOIN stats sh ON sh.fixture_id = f.fixture_id AND sh.team_id = f.home_team_id
        JOIN stats sa ON sa.fixture_id = f.fixture_id AND sa.team_id = f.away_team_id
        WHERE f.status_short = 'FT'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def load_halves_half1(conn: psycopg.Connection, fixture_ids: list[int]) -> pd.DataFrame:
    """Строки `fixture_statistics_halves` при `half=1` для списка матчей."""
    cols = ["fixture_id", "team_id", *HALF1_SIMPLE_METRICS, "expected_goals"]
    if not fixture_ids:
        return pd.DataFrame(columns=cols)
    sql = f"""
        SELECT fixture_id, team_id, {", ".join(HALF1_SIMPLE_METRICS)}, expected_goals
        FROM fixture_statistics_halves
        WHERE fixture_id = ANY(%s) AND half = 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (fixture_ids,))
        result_cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=result_cols)
    # NUMERIC из psycopg приходит как decimal.Decimal — не арифметируется с float.
    df["expected_goals"] = pd.to_numeric(df["expected_goals"], errors="coerce")
    return df


def load_events_for_group_b(conn: psycopg.Connection, fixture_ids: list[int]) -> pd.DataFrame:
    """События, нужные для признаков группы Б (голы/карточки/замены до 75')."""
    cols = ["fixture_id", "team_id", "type", "detail", "minute"]
    if not fixture_ids:
        return pd.DataFrame(columns=cols)
    sql = """
        SELECT fixture_id, team_id, type, detail, minute
        FROM fixture_events
        WHERE fixture_id = ANY(%s)
          AND minute IS NOT NULL
          AND (
                (type = 'Goal' AND (detail IS NULL OR detail != 'Missed Penalty'))
             OR (type = 'Card')
             OR (type = 'subst')
          )
    """
    with conn.cursor() as cur:
        cur.execute(sql, (fixture_ids,))
        result_cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=result_cols)


# --------------------------------------------------------------------- признаки (чистые функции)


def _attribute(home_dominant: pd.Series, home_values: pd.Series, away_values: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Разносит пару home/away-значений по dominant/other согласно `home_dominant`."""
    dominant = np.where(home_dominant, home_values, away_values)
    other = np.where(home_dominant, away_values, home_values)
    return pd.Series(dominant, index=home_dominant.index), pd.Series(other, index=home_dominant.index)


def compute_dominant_side(base_df: pd.DataFrame) -> pd.DataFrame:
    df = base_df.copy()
    df["home_dominant"] = df["home_possession"] >= df["away_possession"]
    return df


def build_group_a_features(base_df: pd.DataFrame, halves_df: pd.DataFrame) -> pd.DataFrame:
    """Признаки по `half=1` (без утечки за 75'). Строки без half=1 у обеих
    команд не попадают в результат (inner join) — проверь покрытие перед
    использованием, сбор `fixture_statistics_halves` продолжается.
    """
    df = compute_dominant_side(base_df)

    home = halves_df.merge(
        df[["fixture_id", "home_team_id"]], left_on=["fixture_id", "team_id"], right_on=["fixture_id", "home_team_id"]
    ).drop(columns=["team_id", "home_team_id"])
    away = halves_df.merge(
        df[["fixture_id", "away_team_id"]], left_on=["fixture_id", "team_id"], right_on=["fixture_id", "away_team_id"]
    ).drop(columns=["team_id", "away_team_id"])

    value_cols = [*HALF1_SIMPLE_METRICS, "expected_goals"]
    home = home.rename(columns={c: f"home_{c}" for c in value_cols})
    away = away.rename(columns={c: f"away_{c}" for c in value_cols})

    merged = df.merge(home, on="fixture_id", how="inner").merge(away, on="fixture_id", how="inner")

    out = merged[["fixture_id", "league_id", "season", "home_team_id", "away_team_id"]].copy()

    for metric in HALF1_SIMPLE_METRICS:
        feature_name = HALF1_FEATURE_NAME.get(metric, metric)
        dominant, other = _attribute(merged["home_dominant"], merged[f"home_{metric}"], merged[f"away_{metric}"])
        out[f"h1_{feature_name}_dominant"] = dominant
        out[f"h1_{feature_name}_other"] = other
        out[f"h1_{feature_name}_diff"] = dominant - other

    # xg — оставляем NaN как есть (заполнено не у всех строк), без fillna.
    home_xg = merged["home_expected_goals"]
    away_xg = merged["away_expected_goals"]
    xg_dominant, xg_other = _attribute(merged["home_dominant"], home_xg, away_xg)
    out["h1_xg_dominant"] = xg_dominant
    out["h1_xg_other"] = xg_other
    out["h1_xg_diff"] = xg_dominant - xg_other

    # xg_overperformance — голы к перерыву своей же команды минус её h1_xg.
    home_ht_goals = pd.to_numeric(merged["ht_home"], errors="coerce")
    away_ht_goals = pd.to_numeric(merged["ht_away"], errors="coerce")
    home_overperf = home_ht_goals - home_xg
    away_overperf = away_ht_goals - away_xg
    overperf_dominant, overperf_other = _attribute(merged["home_dominant"], home_overperf, away_overperf)
    out["h1_xg_overperformance_dominant"] = overperf_dominant
    out["h1_xg_overperformance_other"] = overperf_other
    out["h1_xg_overperformance_diff"] = overperf_dominant - overperf_other

    return out


def build_group_b_features(
    base_df: pd.DataFrame,
    events_df: pd.DataFrame,
    cutoff_minute: int = DEFAULT_CUTOFF_MINUTE,
) -> pd.DataFrame:
    """Признаки из `fixture_events`, не зависящие от `fixture_statistics_halves`."""
    df = compute_dominant_side(base_df)
    out = df[["fixture_id", "league_id", "season", "home_team_id", "away_team_id"]].copy()

    def counts_by_team(mask: pd.Series) -> pd.DataFrame:
        """Считает число событий `mask` по (fixture_id, team_id), затем
        переводит в home/away-колонки, 0 там, где событий не было."""
        if events_df.empty or not mask.any():
            return pd.DataFrame({"fixture_id": [], "home_count": [], "away_count": []})
        subset = events_df.loc[mask]
        counted = subset.groupby(["fixture_id", "team_id"]).size().rename("n").reset_index()
        merged = counted.merge(df[["fixture_id", "home_team_id", "away_team_id"]], on="fixture_id")
        home_n = merged.loc[merged["team_id"] == merged["home_team_id"]].set_index("fixture_id")["n"]
        away_n = merged.loc[merged["team_id"] == merged["away_team_id"]].set_index("fixture_id")["n"]
        result = pd.DataFrame({"home_count": home_n, "away_count": away_n}).reset_index()
        return result

    def attach_count_feature(name: str, mask: pd.Series) -> None:
        counts = counts_by_team(mask)
        merged = out[["fixture_id"]].merge(counts, on="fixture_id", how="left")
        home_count = merged["home_count"].fillna(0).astype(int).reset_index(drop=True)
        away_count = merged["away_count"].fillna(0).astype(int).reset_index(drop=True)
        dominant, other = _attribute(df["home_dominant"].reset_index(drop=True), home_count, away_count)
        out[f"{name}_dominant"] = dominant.to_numpy()
        out[f"{name}_other"] = other.to_numpy()

    def attach_bool_feature(name: str, mask: pd.Series) -> None:
        counts = counts_by_team(mask)
        merged = out[["fixture_id"]].merge(counts, on="fixture_id", how="left")
        home_flag = merged["home_count"].fillna(0).astype(int).gt(0).reset_index(drop=True)
        away_flag = merged["away_count"].fillna(0).astype(int).gt(0).reset_index(drop=True)
        dominant, other = _attribute(df["home_dominant"].reset_index(drop=True), home_flag, away_flag)
        out[f"{name}_dominant"] = dominant.to_numpy().astype(bool)
        out[f"{name}_other"] = other.to_numpy().astype(bool)

    if events_df.empty:
        goal_mask = card_mask = subst_mask = yellow_mask = pd.Series([], dtype=bool)
    else:
        minute = events_df["minute"]
        goal_mask = (
            (events_df["type"] == "Goal")
            & (events_df["detail"].fillna("") != "Missed Penalty")
            & minute.between(46, cutoff_minute)
        )
        card_mask = (
            (events_df["type"] == "Card")
            & events_df["detail"].isin(["Red Card", "Second Yellow Card"])
            & (minute <= cutoff_minute)
        )
        subst_mask = (events_df["type"] == "subst") & (minute <= cutoff_minute)
        yellow_mask = (
            (events_df["type"] == "Card")
            & (events_df["detail"] == "Yellow Card")
            & minute.between(SUBST_YELLOW_WINDOW_START, cutoff_minute)
        )

    attach_count_feature(f"goals_46_{cutoff_minute}", goal_mask)
    attach_bool_feature(f"red_card_before_{cutoff_minute}", card_mask)
    attach_count_feature(f"subst_count_before_{cutoff_minute}", subst_mask)
    attach_count_feature(f"yellow_cards_{SUBST_YELLOW_WINDOW_START}_{cutoff_minute}", yellow_mask)

    return out


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Признаки для анализа поздних голов (группы А и Б)")
    parser.add_argument("--cutoff", type=int, default=DEFAULT_CUTOFF_MINUTE, help="минута отсечки (по умолчанию 75)")
    parser.add_argument(
        "--group",
        choices=["a", "b", "both"],
        default="both",
        help="какую группу признаков строить (по умолчанию обе, отдельными файлами при both)",
    )
    parser.add_argument("--out", default="exports/late_goal_features", help="путь/префикс для CSV (без расширения при --group both)")
    args = parser.parse_args(argv)

    with connect() as conn:
        base = load_dominant_base(conn)
        fixture_ids = base["fixture_id"].tolist()
        print(f"Базовая выборка (доминирующая сторона определена): {len(base)} матчей")

        if args.group in ("a", "both"):
            halves = load_halves_half1(conn, fixture_ids)
            group_a = build_group_a_features(base, halves)
            path_a = args.out if args.group == "a" else f"{args.out}_group_a.csv"
            group_a.to_csv(path_a, index=False)
            print(f"Группа А: {len(group_a)} строк (из них с half=1 у обеих команд) -> {path_a}")

        if args.group in ("b", "both"):
            events = load_events_for_group_b(conn, fixture_ids)
            group_b = build_group_b_features(base, events, args.cutoff)
            path_b = args.out if args.group == "b" else f"{args.out}_group_b.csv"
            group_b.to_csv(path_b, index=False)
            print(f"Группа Б: {len(group_b)} строк -> {path_b}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
