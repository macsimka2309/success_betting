"""Разведочный перебор признаков для голов после отсечки.

Спецификация: specs/разведочный-анализ-поздних-голов.md

Разовый, не входящий в cron скрипт (п. 7 «Принятых решений» спецификации).
Ищет комбинации порогов (владение/удары/ситуация в счёте/замены ведущей и
отстающей команды), при которых доля матчей с голом после отсечки не ниже
0.67 (РА-12) — не ранжирует всё подряд по доходности, хотя EV при заданных
кф 1.7/2.4 считается как вторичная метрика (РА-7). Карточки убраны из
решётки, замены — факт (была/не было), не количество (владелец, 13.10.2026,
«Принятые решения», п. 16).

Только читает `fixtures`, `fixture_statistics`, `fixture_events`,
`fixture_fetch_state`, `teams` и пишет результат в свою таблицу
`goal_after_cutoff_scan_results` (миграция 005) — к остальной схеме не
обращается (НФТ раздел «Безопасность» спецификации).

Использование:
    python3 -m src.jobs.scan_goal_after_cutoff                  # прогон, запись в БД
    python3 -m src.jobs.scan_goal_after_cutoff --cutoff 70       # регрессионная проверка (АП-4)
    python3 -m src.jobs.scan_goal_after_cutoff --no-write        # посчитать, не писать в БД
"""

from __future__ import annotations

import argparse
import itertools
import sys
import uuid
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import psycopg

from src.common.stats import benjamini_hochberg, two_sided_p_value, wilson_interval
from src.db.connection import connect

# --------------------------------------------------------------------- решётка (РА-4)

POSS_GRID = (55, 60, 65, 70, 75, 80)
SHOTS_GRID = tuple(range(0, 11))  # 0..10, шаг 1 (владелец, 13.10.2026)
SOG_GRID = tuple(range(0, 11))  # 0..10, шаг 1 (владелец, 13.10.2026)
SITUATIONS = ("проигрывает_2+", "проигрывает_1", "ничья", "выигрывает_1", "выигрывает_2+")  # чистое разбиение по разнице на отсечке, не пересекаются
BOOL_GRID = (True, False)  # замены ведущей/отстающей команды — факт, не количество (РА-4б)

# Окно для признака замены — от 60-й минуты до отсечки (владелец,
# 13.10.2026: «наличие замены с 60 до 75 минуты»). Верхняя граница равна
# cutoff_minute, а не захардкожена на 75 — при --cutoff 70 (регрессия АП-4)
# окно автоматически становится 60-70, а не остаётся 60-75 поверх другой
# отсечки.
SUBST_WINDOW_START = 60

# Решётка — полное декартово произведение всех осей.
# 6*11*11*5*2*2 = 14520.
GRID_SIZE = len(POSS_GRID) * len(SHOTS_GRID) * len(SOG_GRID) * len(SITUATIONS) * len(BOOL_GRID) * len(BOOL_GRID)
assert GRID_SIZE == 14_520

DEFAULT_CUTOFF_MINUTE = 75
LEGACY_CUTOFF_MINUTE = 70  # только для регрессионной проверки АП-4

ODDS_YES = 1.7
ODDS_NO = 2.4

# РА-8/РА-12: минимальная выборка. 300 — для цели РА-12 (поиск по
# экстремальному порогу на решётке из 14400 комбинаций); 200 — для вторичных
# метрик (EV/ранжирование), как было в версии до смены цели.
MIN_N_TARGET = 300
MIN_N_SECONDARY = 200
MIN_N_HOLDOUT = 150

# РА-12: пороги гейтинга.
TRAIN_SHARE_THRESHOLD = 0.67
TRAIN_STRONG_THRESHOLD = 0.70
HOLDOUT_SHARE_THRESHOLD = 0.60
HOLDOUT_CI_LOW_THRESHOLD = 0.55

# РА-10: train/holdout/monitor по сезонам. Изменено 13.10.2026 («Принятые
# решения», п. 15): train расширен обратно до 2018 (прежнее сужение до 2021
# снято), holdout сдвинут вперёд — начинается с 2023, не с 2024. Все матчи
# базы теперь участвуют в train или holdout, кроме 2026-2027 (monitor).
TRAIN_SEASONS = (2018, 2019, 2020, 2021, 2022)
HOLDOUT_SEASONS = (2023, 2024, 2025)
MONITOR_SEASONS = (2026, 2027)

ALPHA = 0.05  # для поправки Benjamini-Hochberg (РА-9)


def assign_split(season: int) -> str | None:
    if season in TRAIN_SEASONS:
        return "train"
    if season in HOLDOUT_SEASONS:
        return "holdout"
    if season in MONITOR_SEASONS:
        return "monitor"
    return None


# --------------------------------------------------------------------- чтение (РА-1, РА-2)


def load_base_sample(conn: psycopg.Connection) -> pd.DataFrame:
    """FT-матчи с полным владением/ударами у обеих команд (РА-1)."""
    sql = """
        WITH stats AS (
            SELECT fixture_id, team_id, ball_possession, shots_on_goal, total_shots
            FROM fixture_statistics
            WHERE ball_possession IS NOT NULL AND shots_on_goal IS NOT NULL
              AND total_shots IS NOT NULL
        )
        SELECT
            f.fixture_id, f.league_id, f.season, f.home_team_id, f.away_team_id,
            f.goals_home, f.goals_away, f.ft90_home, f.ft90_away,
            sh.ball_possession AS home_possession, sh.total_shots AS home_total_shots,
            sh.shots_on_goal AS home_shots_on_goal,
            sa.ball_possession AS away_possession, sa.total_shots AS away_total_shots,
            sa.shots_on_goal AS away_shots_on_goal,
            s.events_fetched_at IS NOT NULL AS events_fetched
        FROM fixtures f
        JOIN stats sh ON sh.fixture_id = f.fixture_id AND sh.team_id = f.home_team_id
        JOIN stats sa ON sa.fixture_id = f.fixture_id AND sa.team_id = f.away_team_id
        LEFT JOIN fixture_fetch_state s ON s.fixture_id = f.fixture_id
        WHERE f.status_short = 'FT'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def load_goal_events(conn: psycopg.Connection, fixture_ids: list[int]) -> pd.DataFrame:
    """Голы основного времени (minute<=90, без незасчитанных пенальти)."""
    if not fixture_ids:
        return pd.DataFrame(columns=["fixture_id", "team_id", "minute"])
    sql = """
        SELECT fixture_id, team_id, minute
        FROM fixture_events
        WHERE fixture_id = ANY(%s) AND type = 'Goal' AND detail != 'Missed Penalty'
          AND minute IS NOT NULL AND minute <= 90
    """
    with conn.cursor() as cur:
        cur.execute(sql, (fixture_ids,))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def load_substitution_events(conn: psycopg.Connection, fixture_ids: list[int]) -> pd.DataFrame:
    """Замены (`subst`) с минутой и командой — нужна команда, чтобы отличить
    замену ведущей команды от замены отстающей (РА-4б, изменено 13.10.2026)."""
    if not fixture_ids:
        return pd.DataFrame(columns=["fixture_id", "team_id", "minute"])
    sql = """
        SELECT fixture_id, team_id, minute
        FROM fixture_events
        WHERE fixture_id = ANY(%s) AND type = 'subst' AND minute IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql, (fixture_ids,))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


# --------------------------------------------------------------------- признаки (чистые функции)


def build_feature_table(
    base_df: pd.DataFrame,
    goals_df: pd.DataFrame,
    subst_df: pd.DataFrame,
    cutoff_minute: int,
) -> pd.DataFrame:
    """Собирает одну строку на матч с признаками для перебора.

    Вход — уже отфильтрованный по надёжности `base_df` (РА-2: либо
    `events_fetched=True`, либо итоговый счёт 0:0 — решает вызывающий код,
    см. `filter_reliable`). Эта функция сама по себе не обращается к БД —
    тестируется на синтетических DataFrame (Тестовые сценарии спецификации).
    """
    df = base_df.copy()
    df["final_home"] = pd.to_numeric(df["ft90_home"], errors="coerce").fillna(df["goals_home"]).astype(float)
    df["final_away"] = pd.to_numeric(df["ft90_away"], errors="coerce").fillna(df["goals_away"]).astype(float)

    home_dominant = df["home_possession"] >= df["away_possession"]
    df["dominant_side"] = np.where(home_dominant, "H", "A")
    df["dominant_possession"] = df[["home_possession", "away_possession"]].max(axis=1)
    df["dominant_total_shots"] = np.where(home_dominant, df["home_total_shots"], df["away_total_shots"])
    df["dominant_sog"] = np.where(home_dominant, df["home_shots_on_goal"], df["away_shots_on_goal"])

    if goals_df.empty:
        before = pd.DataFrame({"fixture_id": [], "home_goals_cutoff": [], "away_goals_cutoff": []})
    else:
        g = goals_df[goals_df["minute"] <= cutoff_minute]
        home_goals = (
            g.merge(df[["fixture_id", "home_team_id"]], on="fixture_id")
            .loc[lambda x: x["team_id"] == x["home_team_id"]]
            .groupby("fixture_id")
            .size()
            .rename("home_goals_cutoff")
        )
        away_goals = (
            g.merge(df[["fixture_id", "away_team_id"]], on="fixture_id")
            .loc[lambda x: x["team_id"] == x["away_team_id"]]
            .groupby("fixture_id")
            .size()
            .rename("away_goals_cutoff")
        )
        before = pd.concat([home_goals, away_goals], axis=1).reset_index()

    df = df.merge(before, on="fixture_id", how="left")
    df["home_goals_cutoff"] = df["home_goals_cutoff"].fillna(0).astype(int)
    df["away_goals_cutoff"] = df["away_goals_cutoff"].fillna(0).astype(int)

    df["dominant_goals_cutoff"] = np.where(home_dominant, df["home_goals_cutoff"], df["away_goals_cutoff"])
    df["other_goals_cutoff"] = np.where(home_dominant, df["away_goals_cutoff"], df["home_goals_cutoff"])
    df["situation_diff"] = df["dominant_goals_cutoff"] - df["other_goals_cutoff"]

    home_after = df["final_home"] - df["home_goals_cutoff"]
    away_after = df["final_away"] - df["away_goals_cutoff"]
    df["any_after"] = (home_after > 0) | (away_after > 0)
    df["dominant_after"] = np.where(home_dominant, home_after, away_after) > 0

    if subst_df.empty:
        df["dominant_subst_window"] = False
        df["other_subst_window"] = False
    else:
        in_window = (subst_df["minute"] >= SUBST_WINDOW_START) & (subst_df["minute"] <= cutoff_minute)
        s = subst_df[in_window].merge(
            df[["fixture_id", "home_team_id"]], on="fixture_id", how="inner"
        )
        is_home_event = s["team_id"] == s["home_team_id"]
        home_has_subst = s.loc[is_home_event].groupby("fixture_id").size()
        away_has_subst = s.loc[~is_home_event].groupby("fixture_id").size()
        df["_home_subst_60"] = df["fixture_id"].map(home_has_subst).notna()
        df["_away_subst_60"] = df["fixture_id"].map(away_has_subst).notna()
        df["dominant_subst_window"] = np.where(home_dominant, df["_home_subst_60"], df["_away_subst_60"])
        df["other_subst_window"] = np.where(home_dominant, df["_away_subst_60"], df["_home_subst_60"])
        df = df.drop(columns=["_home_subst_60", "_away_subst_60"])

    df["split"] = df["season"].map(assign_split)
    return df


def filter_reliable(base_df: pd.DataFrame) -> pd.DataFrame:
    """РА-2: матч исключается, если итог ненулевой, а события не собраны.

    Матчи 0:0 остаются независимо от `events_fetched` — им просто нечего
    было бы искажать (ноль голов — ноль голов после отсечки в любом случае).
    """
    final_home = pd.to_numeric(base_df["ft90_home"], errors="coerce").fillna(base_df["goals_home"]).astype(float)
    final_away = pd.to_numeric(base_df["ft90_away"], errors="coerce").fillna(base_df["goals_away"]).astype(float)
    total_goals = final_home.fillna(0) + final_away.fillna(0)
    unreliable = (total_goals > 0) & (~base_df["events_fetched"])
    return base_df.loc[~unreliable].copy()


# --------------------------------------------------------------------- решётка и подсчёт


def situation_mask(diff: pd.Series, situation: str) -> pd.Series:
    """Чистое разбиение по разнице (доминирующая минус другая команда) на
    отсечке — пять взаимоисключающих категорий, в сумме дают весь диапазон.
    Раньше `разрыв<=1`/`любая` пересекались с тремя остальными значениями
    (владелец, 13.10.2026: разделить выигрыш/проигрыш по марже в 1 мяч)."""
    if situation == "проигрывает_2+":
        return diff <= -2
    if situation == "проигрывает_1":
        return diff == -1
    if situation == "ничья":
        return diff == 0
    if situation == "выигрывает_1":
        return diff == 1
    if situation == "выигрывает_2+":
        return diff >= 2
    raise ValueError(f"неизвестная ситуация: {situation}")


@dataclass(frozen=True)
class Combo:
    poss_threshold: int
    shots_threshold: int
    sog_threshold: int
    situation: str
    dominant_subst_window: bool
    other_subst_window: bool


def generate_grid() -> list[Combo]:
    combos = [
        Combo(poss, shots, sog, situation, dom_subst, other_subst)
        for poss, shots, sog, situation, dom_subst, other_subst in itertools.product(
            POSS_GRID, SHOTS_GRID, SOG_GRID, SITUATIONS, BOOL_GRID, BOOL_GRID
        )
    ]
    assert len(combos) == GRID_SIZE
    return combos


def combo_mask(df: pd.DataFrame, combo: Combo) -> pd.Series:
    """Владение — строго больше порога (РА-3). Удары/в створ — не меньше
    порога (`>=`), как во всём ручном анализе этого чата (АП-4: "≥9/4
    ударов"); спецификация явно не называла оператор для этих двух осей,
    `>=` выбран, чтобы АП-4 воспроизводился буквально."""
    return (
        (df["dominant_possession"] > combo.poss_threshold)
        & (df["dominant_total_shots"] >= combo.shots_threshold)
        & (df["dominant_sog"] >= combo.sog_threshold)
        & situation_mask(df["situation_diff"], combo.situation)
        & (df["dominant_subst_window"] == combo.dominant_subst_window)
        & (df["other_subst_window"] == combo.other_subst_window)
    )


@dataclass
class ComboResult:
    combo: Combo
    target: str
    split: str
    n: int
    successes: int
    share: float | None
    ci_low: float | None
    ci_high: float | None
    ev_yes: float | None
    ev_no: float | None
    p_value: float | None


def evaluate_combo(df: pd.DataFrame, combo: Combo, target: str, split: str) -> ComboResult:
    subset = df[(df["split"] == split)]
    mask = combo_mask(subset, combo)
    matched = subset.loc[mask]
    n = len(matched)
    outcome_col = "any_after" if target == "any_team" else "dominant_after"
    successes = int(matched[outcome_col].sum()) if n else 0

    if n == 0:
        return ComboResult(combo, target, split, 0, 0, None, None, None, None, None, None)

    share = successes / n
    ci_low, ci_high = wilson_interval(successes, n)
    ev_yes = share * ODDS_YES - 1 if target == "any_team" else None
    ev_no = (1 - share) * ODDS_NO - 1 if target == "any_team" else None
    p_value = two_sided_p_value(successes, n, 1 / ODDS_YES) if target == "any_team" else None
    return ComboResult(combo, target, split, n, successes, share, ci_low, ci_high, ev_yes, ev_no, p_value)


# --------------------------------------------------------------------- гейтинг (РА-12)


@dataclass
class GatedResult:
    combo: Combo
    target: str
    train: ComboResult
    holdout: ComboResult | None
    meets_threshold_train: bool
    is_strong_train: bool
    meets_threshold_holdout: bool
    holdout_status: str  # "подтверждено" / "не подтверждено" / "не проверено"


def apply_gate(train: ComboResult, holdout: ComboResult | None) -> GatedResult:
    meets_train = (
        train.n >= MIN_N_TARGET and train.ci_low is not None and train.ci_low >= TRAIN_SHARE_THRESHOLD
    )
    is_strong = meets_train and train.share is not None and train.share >= TRAIN_STRONG_THRESHOLD

    meets_holdout = False
    status = "не применимо"
    if meets_train:
        if holdout is None or holdout.n < MIN_N_HOLDOUT:
            status = "не проверено"
        else:
            meets_holdout = (
                holdout.share is not None
                and holdout.share >= HOLDOUT_SHARE_THRESHOLD
                and holdout.ci_low is not None
                and holdout.ci_low >= HOLDOUT_CI_LOW_THRESHOLD
            )
            status = "подтверждено" if meets_holdout else "не подтверждено"

    return GatedResult(
        combo=train.combo,
        target=train.target,
        train=train,
        holdout=holdout,
        meets_threshold_train=meets_train,
        is_strong_train=is_strong,
        meets_threshold_holdout=meets_holdout,
        holdout_status=status,
    )


# --------------------------------------------------------------------- прогон


@dataclass
class ScanRun:
    run_id: str
    cutoff_minute: int
    phase: str
    gated: list[GatedResult] = field(default_factory=list)
    secondary: list[ComboResult] = field(default_factory=list)  # для EV-ранжирования, порог 200


def run_scan(feature_df: pd.DataFrame, cutoff_minute: int, phase: str = "full_match") -> ScanRun:
    run = ScanRun(run_id=str(uuid.uuid4()), cutoff_minute=cutoff_minute, phase=phase)
    grid = generate_grid()

    for target in ("any_team", "dominant_team"):
        train_results = [evaluate_combo(feature_df, c, target, "train") for c in grid]
        holdout_results = [evaluate_combo(feature_df, c, target, "holdout") for c in grid]

        p_values = [r.p_value for r in train_results if r.p_value is not None and r.n >= MIN_N_SECONDARY]
        if p_values:
            corrected, _ = benjamini_hochberg(p_values, ALPHA)
            corrected_iter = iter(corrected)
        for train_r, holdout_r in zip(train_results, holdout_results):
            run.gated.append(apply_gate(train_r, holdout_r))
            if train_r.n >= MIN_N_SECONDARY:
                run.secondary.append(train_r)

    return run


# --------------------------------------------------------------------- запись в БД


def write_results(conn: psycopg.Connection, run: ScanRun) -> int:
    rows = []
    for g in run.gated:
        for split_name, result in (("train", g.train), ("holdout", g.holdout)):
            if result is None or result.n == 0:
                continue
            rows.append(
                (
                    run.run_id,
                    run.cutoff_minute,
                    run.phase,
                    g.target,
                    split_name,
                    g.combo.poss_threshold,
                    g.combo.shots_threshold,
                    g.combo.sog_threshold,
                    g.combo.situation,
                    g.combo.dominant_subst_window,
                    g.combo.other_subst_window,
                    result.n,
                    result.share,
                    result.ci_low,
                    result.ci_high,
                    result.ev_yes,
                    result.ev_no,
                    result.p_value,
                    None,  # p_value_corrected — заполняется отдельно при необходимости ранжирования
                    result.n >= MIN_N_SECONDARY,
                    g.meets_threshold_train if split_name == "train" else False,
                    g.meets_threshold_holdout if split_name == "holdout" else False,
                )
            )

    if not rows:
        return 0

    sql = """
        INSERT INTO goal_after_cutoff_scan_results (
            run_id, cutoff_minute, phase, target, split,
            poss_threshold, shots_threshold, sog_threshold, situation,
            dominant_subst_window, other_subst_window,
            n, share, ci_low, ci_high, ev_yes, ev_no, p_value, p_value_corrected,
            is_candidate, meets_threshold_train, meets_threshold_holdout
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """
    BATCH = 2_000
    with conn.cursor() as cur:
        for start in range(0, len(rows), BATCH):
            with conn.transaction():
                cur.executemany(sql, rows[start : start + BATCH])
    return len(rows)


# --------------------------------------------------------------------- отчёт


def summarize(run: ScanRun) -> str:
    lines = [f"Прогон {run.run_id} (отсечка {run.cutoff_minute}', фаза {run.phase})"]
    for target in ("any_team", "dominant_team"):
        target_results = [g for g in run.gated if g.target == target]
        n_train_pass = sum(1 for g in target_results if g.meets_threshold_train)
        n_strong = sum(1 for g in target_results if g.is_strong_train)
        n_confirmed = sum(1 for g in target_results if g.holdout_status == "подтверждено")
        n_not_confirmed = sum(1 for g in target_results if g.holdout_status == "не подтверждено")
        n_not_tested = sum(1 for g in target_results if g.holdout_status == "не проверено" and g.meets_threshold_train)
        lines.append(
            f"  {target}: протестировано {len(target_results)} комбинаций; "
            f"прошли train-гейт (n>={MIN_N_TARGET}, ДИ-нижняя>={TRAIN_SHARE_THRESHOLD}): {n_train_pass} "
            f"(из них сильных >=0.70: {n_strong}); "
            f"подтверждено на holdout: {n_confirmed}; не подтверждено: {n_not_confirmed}; "
            f"не проверено (мало holdout-данных): {n_not_tested}"
        )
        if n_train_pass == 0:
            lines.append(f"    -> по {target} на пороге 0.67/0.70 кандидатов на train нет")
    return "\n".join(lines)


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перебор признаков для голов после отсечки")
    parser.add_argument("--cutoff", type=int, default=DEFAULT_CUTOFF_MINUTE, help="минута отсечки (по умолчанию 75)")
    parser.add_argument("--phase", choices=["full_match", "half2_approx"], default="full_match")
    parser.add_argument("--no-write", action="store_true", help="посчитать и напечатать отчёт, не писать в БД")
    args = parser.parse_args(argv)

    with connect() as conn:
        base = load_base_sample(conn)
        reliable = filter_reliable(base)
        fixture_ids = reliable["fixture_id"].tolist()
        goals = load_goal_events(conn, fixture_ids)
        subst = load_substitution_events(conn, fixture_ids)
        features = build_feature_table(reliable, goals, subst, args.cutoff)

        print(f"Базовая выборка: {len(base)}, исключено ненадёжных: {len(base) - len(reliable)}")
        run = run_scan(features, args.cutoff, args.phase)
        print(summarize(run))

        if not args.no_write:
            written = write_results(conn, run)
            print(f"Записано строк в goal_after_cutoff_scan_results: {written}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
