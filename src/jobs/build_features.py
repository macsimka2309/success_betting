"""Препроцессинг для модели: построение `ml_match_features`.

Спецификация: specs/препроцессинг-для-модели.md

Стадия — чтение сырых таблиц, построение признаков в хронологическом
порядке (нужно для Эло, ДП-8) и запись в `ml_match_features` пакетами
через `ON CONFLICT DO UPDATE`. Только чтение сырых таблиц и запись
в свои — к API не обращается (ДП-11).

Режим по умолчанию сейчас — полный пересчёт: инкрементальность (ДП-11)
пока не оптимизирована отдельно, `--incremental` вычисляет всё то же
самое и лишь ограничивает то, что записывается, новыми матчами — это
корректно, но не быстрее полного прохода. Оптимизация — на будущее,
когда/если станет ощутимо медленно для еженедельного запуска (ДП-12).

Режим `--upcoming` — инференс на ещё не сыгранные матчи (ДП-9 расширение):
считает те же признаки для несыгранных матчей в горизонте `--horizon-days`
от текущего момента, используя всю прошлую историю как основание для
скользящих метрик (утечки нет — история идёт только до текущего момента,
а сам факт будущего матча не даёт ничего вперёд), но не записывает и не
меняет исторические строки и состояние Эло: целевые колонки (`reg_home`,
`result_1x2`, `btts` и т.д.) у таких строк остаются NULL, потому что
результата ещё нет. Строка перезаписывается тем же upsert'ом, когда матч
будет сыгран и рассчитан обычным (не `--upcoming`) прогоном.

Использование:
    python3 -m src.jobs.build_features                 # полный пересчёт
    python3 -m src.jobs.build_features --incremental
    python3 -m src.jobs.build_features --upcoming [--horizon-days 2]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import numpy as np
import pandas as pd
import psycopg

from src.common.feature_columns import all_columns, eval_columns
from src.db.connection import connect, database_url

# Значения по умолчанию — specs/препроцессинг-для-модели.md, «Принятые решения».
MIN_LEAGUE_MATCHES = 20
MIN_TEAM_MATCHES = 3
SHORT_WINDOW = 3
LONG_WINDOW = 5
H2H_WINDOW = 5
FINISHED_STATUSES = ("FT", "AET", "PEN")
UPCOMING_STATUS = "NS"
DEFAULT_UPCOMING_HORIZON_DAYS = 2

ELO_DEFAULT = 1500.0
ELO_K = 20.0
ELO_HOME_ADVANTAGE = 60.0

STAT_METRICS = ("shots_on_goal", "total_shots", "corner_kicks", "ball_possession", "expected_goals")
STAT_COLUMN_MAP = {
    "shots_on_goal": "shots_on_goal",
    "total_shots": "total_shots",
    "corner_kicks": "corner_kicks",
    "ball_possession": "ball_possession",
    "expected_goals": "expected_goals",
}

EVAL_BOOKMAKER_NAMES = {"1xbet": "1xBet", "pinnacle": "Pinnacle"}


def half_line(x: pd.Series) -> pd.Series:
    """`floor(x) + 0.5` — та же формула для всех линий (ДП-2, ДП-13):
    результат никогда не целое число, поэтому «пуш» невозможен."""
    return np.floor(x) + 0.5


# --------------------------------------------------------------------- чтение


def load_fixtures(
    conn: psycopg.Connection, include_upcoming: bool = False, horizon_days: int | None = None
) -> pd.DataFrame:
    """Завершённые матчи (для целей и истории) плюс, при `include_upcoming`,
    ещё не сыгранные матчи в горизонте `horizon_days` дней вперёд от текущего
    момента (`--upcoming`, инференс) — им не хватает счёта, поэтому дальше
    по пайплайну они остаются с NULL в целевых колонках."""
    sql = """
        SELECT fixture_id, league_id, season, match_date, kickoff_at,
               home_team_id, away_team_id, status_short,
               goals_home, goals_away, ft90_home, ft90_away
        FROM fixtures
        WHERE status_short = ANY(%(finished)s)
    """
    params: dict = {"finished": list(FINISHED_STATUSES)}
    if include_upcoming:
        sql += """
            OR (status_short = %(upcoming_status)s
                AND kickoff_at >= now()
                AND kickoff_at < now() + (%(horizon_days)s || ' days')::interval)
        """
        params["upcoming_status"] = UPCOMING_STATUS
        params["horizon_days"] = str(horizon_days or DEFAULT_UPCOMING_HORIZON_DAYS)
    sql += " ORDER BY kickoff_at, fixture_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    df["kickoff_at"] = pd.to_datetime(df["kickoff_at"], utc=True)
    return df


def load_statistics(conn: psycopg.Connection) -> pd.DataFrame:
    sql = """
        SELECT fixture_id, team_id, shots_on_goal, total_shots, corner_kicks,
               ball_possession, expected_goals
        FROM fixture_statistics
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def load_injuries(conn: psycopg.Connection) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id, team_id FROM injuries")
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def load_odds(conn: psycopg.Connection) -> pd.DataFrame:
    """Снимки коэффициентов до кикоффа, для ДП-10 (eval-колонки)."""
    sql = """
        SELECT s.fixture_id, s.taken_at, b.name AS bookmaker, bt.name AS bet_type, v.value, v.odd
        FROM odds_values v
        JOIN odds_snapshots s ON s.snapshot_id = v.snapshot_id
        JOIN bookmakers b ON b.bookmaker_id = v.bookmaker_id
        JOIN bet_types bt ON bt.bet_type_id = v.bet_type_id
        WHERE b.name IN ('1xBet', 'Pinnacle')
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["taken_at"] = pd.to_datetime(df["taken_at"], utc=True)
    return df


def load_elo_state(conn: psycopg.Connection) -> dict[tuple[int, int], float]:
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, league_id, rating FROM ml_team_elo_state")
        return {(row[0], row[1]): float(row[2]) for row in cur.fetchall()}


# ------------------------------------------------------------------- цели


def compute_targets(fx: pd.DataFrame) -> pd.DataFrame:
    """ДП-0, ДП-14: регулярный счёт, результат, обе забьют, двойной шанс."""
    fx = fx.copy()
    fx["reg_home"] = fx["ft90_home"].where(fx["ft90_home"].notna(), fx["goals_home"])
    fx["reg_away"] = fx["ft90_away"].where(fx["ft90_away"].notna(), fx["goals_away"])
    fx["reg_home"] = fx["reg_home"].astype("Int64")
    fx["reg_away"] = fx["reg_away"].astype("Int64")

    # Матч отмечен завершённым (FT/AET/PEN), но без счёта — дефект данных
    # (найдено на продовых данных 29.09.2026: 4 таких строки). Без счёта
    # нет цели ни по одному рынку, поэтому строка исключается целиком,
    # а не протаскивается дальше с NA — иначе np.select ниже упал бы
    # на нечистом булевом массиве (нашлось именно так, тестами на
    # синтетических данных со всегда полным счётом это не поймать).
    # Отсутствие счёта — дефект данных только у завершённых матчей; у ещё
    # не сыгранных (--upcoming, ДП-9-расширение) счёта нет по определению,
    # такие строки не выбрасываем.
    is_finished = fx["status_short"].isin(FINISHED_STATUSES)
    missing_score = is_finished & (fx["reg_home"].isna() | fx["reg_away"].isna())
    if missing_score.any():
        print(
            f"  пропущено матчей без счёта при завершённом статусе: {int(missing_score.sum())} "
            f"(fixture_id: {fx.loc[missing_score, 'fixture_id'].tolist()})"
        )
        fx = fx.loc[~missing_score].copy()

    # Сыгран ли матч — единственный признак, по которому целевые колонки
    # ниже либо считаются, либо остаются NULL (--upcoming): np.select и
    # сравнения с NaN сами по себе дают False/"D" вместо NULL, поэтому
    # результат явно маскируется через `played`.
    played = fx["reg_home"].notna() & fx["reg_away"].notna()

    # fillna(False): reg_home/reg_away — Int64 (nullable), сравнение с NA
    # (--upcoming) даёт pandas nullable boolean с <NA>, а не bool ndarray,
    # на котором падает np.select; итог всё равно перезатирается None ниже
    # через `played`, поэтому подстановка False тут безопасна.
    conditions = [
        (fx["reg_home"] > fx["reg_away"]).fillna(False).to_numpy(dtype=bool),
        (fx["reg_home"] < fx["reg_away"]).fillna(False).to_numpy(dtype=bool),
    ]
    fx["result_1x2"] = np.select(conditions, ["H", "A"], default="D")
    fx["result_1x2"] = fx["result_1x2"].astype(object).where(played, None)
    fx["total_goals"] = fx["reg_home"] + fx["reg_away"]
    fx["btts"] = ((fx["reg_home"] > 0) & (fx["reg_away"] > 0)).astype(object).where(played, None)

    fx["dc_1x"] = fx["result_1x2"].isin(["H", "D"]).astype(object).where(played, None)
    fx["dc_x2"] = fx["result_1x2"].isin(["A", "D"]).astype(object).where(played, None)
    fx["dc_12"] = fx["result_1x2"].isin(["H", "A"]).astype(object).where(played, None)
    return fx


# -------------------------------------------------------- скользящие расчёты


def _shifted_rolling(
    series: pd.Series,
    groups: pd.Series,
    window: int | None,
    order_key: pd.Series,
    min_periods: int = 1,
):
    """Среднее по прошлым матчам этой группы, без утечки текущего (ДП-1).

    Тонкая обёртка над `_shifted_rolling_batch` для одной колонки — используется
    там, где батчить нечего (ДП-2, ДП-7). Для ДП-3/ДП-4, где на одну и ту же
    группу считаются 8-11 метрик сразу, нужен именно batch-вариант: иначе
    каждая метрика пересортировывает и перегруппировывает те же самые строки
    заново — на реальном объёме (найдено на сервере 30.09.2026, ~1,1 млн
    матчей) это ощутимо и по времени, и по памяти.
    """
    frame = series.to_frame("value")
    means, counted = _shifted_rolling_batch(frame, ["value"], groups, window, order_key, min_periods)
    return means["value"], counted


def _shifted_rolling_batch(
    df: pd.DataFrame,
    value_cols: list[str],
    groups: pd.Series,
    window: int | None,
    order_key: pd.Series,
    min_periods: int = 1,
):
    """Как `_shifted_rolling`, но для нескольких колонок сразу за один проход.

    `shift`/`rolling` внутри `groupby` опираются на порядок строк, а не на
    какой-либо ключ сортировки — поэтому функция сортирует по `order_key`
    сама, один раз для всех переданных колонок, и возвращает результат,
    выровненный по меткам исходного индекса `df` (`reindex`, не позиционно).

    `window=None` — накопительно (`expanding`), иначе — последние `window`.
    Возвращает (DataFrame средних по каждой колонке, Series числа учтённых
    матчей — одна на все колонки: у них общая группа наблюдений).
    """
    order = order_key.loc[df.index].sort_values().index
    sub = df.loc[order, value_cols]
    g = groups.loc[order]

    shifted = sub.groupby(g)[value_cols].shift(1)
    grouped = shifted.groupby(g)
    if window is None:
        means = grouped.transform(lambda x: x.expanding(min_periods=min_periods).mean())
        counted = grouped[value_cols[0]].transform(lambda x: x.expanding(min_periods=0).count())
    else:
        means = grouped.transform(lambda x: x.rolling(window, min_periods=min_periods).mean())
        counted = grouped[value_cols[0]].transform(lambda x: x.rolling(window, min_periods=0).count())
    return means.reindex(df.index), counted.reindex(df.index)


def compute_league_lines(fx: pd.DataFrame) -> pd.DataFrame:
    """ДП-2 (лига), ДП-7 (базовые частоты): накопительно с начала сезона."""
    fx = fx.sort_values(["league_id", "season", "kickoff_at", "fixture_id"]).copy()
    key = fx["league_id"].astype(str) + "|" + fx["season"].astype(str)

    work = fx.assign(
        _is_home_win=(fx["result_1x2"] == "H").astype(float),
        _is_draw=(fx["result_1x2"] == "D").astype(float),
        _is_away_win=(fx["result_1x2"] == "A").astype(float),
    )
    cols = ["total_goals", "_is_home_win", "_is_draw", "_is_away_win"]
    means, count = _shifted_rolling_batch(work, cols, key, window=None, order_key=fx["kickoff_at"])

    enough = count >= MIN_LEAGUE_MATCHES
    fx["league_total_line"] = half_line(means["total_goals"]).where(enough)
    # league_total_line — предсказательный признак, валиден и для ещё не
    # сыгранных матчей (--upcoming); а вот league_total_over — наблюдение
    # по факту счёта, поэтому дополнительно маскируется по total_goals.
    fx["league_total_over"] = (fx["total_goals"] > fx["league_total_line"]).where(
        fx["league_total_line"].notna() & fx["total_goals"].notna()
    )
    fx["league_home_win_rate"] = means["_is_home_win"].where(enough)
    fx["league_draw_rate"] = means["_is_draw"].where(enough)
    fx["league_away_win_rate"] = means["_is_away_win"].where(enough)
    return fx


def _team_long_format(fx: pd.DataFrame) -> pd.DataFrame:
    """Одна строка на (матч, команда): по одной на хозяев и на гостей.

    Нужен, чтобы считать «форму команды» одним групповым проходом вместо
    отдельного кода для хозяев и для гостей.
    """
    home = pd.DataFrame(
        {
            "fixture_id": fx["fixture_id"],
            "league_id": fx["league_id"],
            "season": fx["season"],
            "kickoff_at": fx["kickoff_at"],
            "team_id": fx["home_team_id"],
            "opponent_id": fx["away_team_id"],
            "is_home": True,
            "goals_for": fx["reg_home"],
            "goals_against": fx["reg_away"],
            "result": fx["result_1x2"],
        }
    )
    away = pd.DataFrame(
        {
            "fixture_id": fx["fixture_id"],
            "league_id": fx["league_id"],
            "season": fx["season"],
            "kickoff_at": fx["kickoff_at"],
            "team_id": fx["away_team_id"],
            "opponent_id": fx["home_team_id"],
            "is_home": False,
            "goals_for": fx["reg_away"],
            "goals_against": fx["reg_home"],
            "result": fx["result_1x2"],
        }
    )
    long_df = pd.concat([home, away], ignore_index=True)
    long_df["points"] = np.select(
        [
            (long_df["is_home"] & (long_df["result"] == "H")) | (~long_df["is_home"] & (long_df["result"] == "A")),
            long_df["result"] == "D",
        ],
        [3, 1],
        default=0,
    )
    long_df["win"] = long_df["points"] == 3
    long_df["draw"] = long_df["points"] == 1
    long_df["loss"] = long_df["points"] == 0
    long_df["clean_sheet"] = long_df["goals_against"] == 0
    long_df["failed_to_score"] = long_df["goals_for"] == 0

    # Матч ещё не сыгран (--upcoming) — все наблюдённые по факту исхода
    # метрики должны остаться NULL, а не 0/False, иначе они молча войдут
    # в скользящее среднее следующего матча той же команды в горизонте
    # (например, если команда играет дважды за неделю).
    played = long_df["goals_for"].notna() & long_df["goals_against"].notna()
    for col in ("points", "win", "draw", "loss", "clean_sheet", "failed_to_score"):
        long_df[col] = long_df[col].astype(float).where(played)

    long_df["team_season_key"] = (
        long_df["team_id"].astype(str) + "|" + long_df["league_id"].astype(str) + "|" + long_df["season"].astype(str)
    )
    return long_df.sort_values(["team_season_key", "kickoff_at", "fixture_id"])


def compute_team_lines(fx: pd.DataFrame, long_df: pd.DataFrame) -> pd.DataFrame:
    """ДП-2 (команда), ДП-13 (фора): накопительная линия дома/в гостях."""
    lines = {}
    for venue, is_home in (("home", True), ("away", False)):
        subset = long_df[long_df["is_home"] == is_home]
        mean, count = _shifted_rolling(
            subset["goals_for"], subset["team_season_key"], window=None, order_key=subset["kickoff_at"]
        )
        line = half_line(mean).where(count >= MIN_TEAM_MATCHES)
        lines[venue] = pd.Series(line.values, index=subset["fixture_id"].values)

    fx = fx.copy()
    fx["home_team_line"] = fx["fixture_id"].map(lines["home"])
    fx["away_team_line"] = fx["fixture_id"].map(lines["away"])
    # *_team_line — предсказательный признак, валиден и для несыгранных
    # матчей (--upcoming); *_total_over — по факту счёта, поэтому требует
    # ещё и reg_home/reg_away.
    fx["home_total_over"] = (fx["reg_home"] > fx["home_team_line"]).where(
        fx["home_team_line"].notna() & fx["reg_home"].notna()
    )
    fx["away_total_over"] = (fx["reg_away"] > fx["away_team_line"]).where(
        fx["away_team_line"].notna() & fx["reg_away"].notna()
    )

    played = fx["reg_home"].notna() & fx["reg_away"].notna()
    both_known = fx["home_team_line"].notna() & fx["away_team_line"].notna()
    raw = fx["home_team_line"] - fx["away_team_line"]
    fx["handicap_favorite"] = np.where(raw >= 0, "home", "away")
    fx.loc[~both_known, "handicap_favorite"] = None
    fx["handicap_line"] = half_line(raw.abs()).where(both_known)
    margin = np.where(
        fx["handicap_favorite"] == "home", fx["reg_home"] - fx["reg_away"], fx["reg_away"] - fx["reg_home"]
    )
    # dtype="object", не bool: где линия неизвестна, значение остаётся None,
    # а не приводится к False — иначе pandas выдаёт FutureWarning на присвоение
    # None булевой колонке (и в будущей версии это станет ошибкой).
    covers = pd.Series(margin, index=fx.index) > fx["handicap_line"]
    fx["handicap_favorite_covers"] = covers.astype(object).where(both_known & played, None)
    return fx


_FORM_METRIC_SOURCE_COLUMNS = {
    "goals_scored_avg": "goals_for",
    "goals_conceded_avg": "goals_against",
    "points_avg": "points",
    "win_rate": "win",
    "draw_rate": "draw",
    "loss_rate": "loss",
    "clean_sheet_rate": "clean_sheet",
    "failed_to_score_rate": "failed_to_score",
}


def compute_form(long_df: pd.DataFrame) -> pd.DataFrame:
    """ДП-3: форма, окна SHORT/LONG, в рамках сезона, без MIN_WINDOW-отсечения.

    Все метрики группы (scope, window) считаются одним проходом
    `_shifted_rolling_batch`, а не по отдельному вызову на каждую — иначе
    на реальном объёме (~1,1 млн матчей) 3 scope × 2 window × 9 метрик =
    54 отдельные пересортировки одних и тех же строк съедали память и время
    несоразмерно (найдено на сервере 30.09.2026, пришлось прерывать прогон).
    """
    out = pd.DataFrame({"fixture_id": long_df["fixture_id"], "team_id": long_df["team_id"], "is_home": long_df["is_home"]})

    work = long_df.copy()
    for source_col in ("win", "draw", "loss", "clean_sheet", "failed_to_score"):
        work[source_col] = work[source_col].astype(float)
    source_cols = list(_FORM_METRIC_SOURCE_COLUMNS.values())

    scopes = {
        "overall": work.index,
        "home": work.index[work["is_home"]],
        "away": work.index[~work["is_home"]],
    }
    for scope, idx in scopes.items():
        sub_key = work.loc[idx, "team_season_key"]
        order_key = work.loc[idx, "kickoff_at"]
        for window_name, window in (("short", SHORT_WINDOW), ("long", LONG_WINDOW)):
            means, counted = _shifted_rolling_batch(work.loc[idx], source_cols, sub_key, window, order_key)
            out.loc[idx, f"form__{scope}__{window_name}__matches_played"] = counted.values
            for metric, source_col in _FORM_METRIC_SOURCE_COLUMNS.items():
                out.loc[idx, f"form__{scope}__{window_name}__{metric}"] = means[source_col].values
    return out


def compute_statistics_features(long_df: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    """ДП-4: та же механика, что ДП-3, плюс coverage — доля матчей окна со статистикой.

    «Соперник» берётся из уже готовой колонки `long_df.opponent_id`
    (заполняется в `_team_long_format` напрямую из home/away_team_id той
    же строки fixtures) — без self-join по fixture_id: на реальном объёме
    (~1,1 млн матчей → 2,2 млн строк long_df) такой join давал 4 строки на
    матч до фильтрации, лишний расход памяти без необходимости (найдено
    вместе с проблемой в compute_form, 30.09.2026).

    Все метрики группы (scope, window) считаются одним проходом
    `_shifted_rolling_batch`, как в ДП-3 — по той же причине.
    """
    merged = long_df.merge(stats, on=["fixture_id", "team_id"], how="left")  # свои — "for"
    merged["has_stats"] = merged["shots_on_goal"].notna().astype(float)

    opp_stats = stats.rename(columns={m: f"{m}_against" for m in STAT_METRICS})
    opp_stats = opp_stats.rename(columns={"team_id": "opponent_id"})
    merged = merged.merge(opp_stats, on=["fixture_id", "opponent_id"], how="left")  # соперника — "against"

    out = pd.DataFrame({"fixture_id": long_df["fixture_id"], "team_id": long_df["team_id"], "is_home": long_df["is_home"]})

    source_cols = ["has_stats"] + [
        f"{metric}{suffix}" for metric in STAT_METRICS for suffix in ("", "_against")
    ]
    scopes = {
        "overall": merged.index,
        "home": merged.index[merged["is_home"]],
        "away": merged.index[~merged["is_home"]],
    }
    for scope, idx in scopes.items():
        sub_key = merged.loc[idx, "team_season_key"]
        order_key = merged.loc[idx, "kickoff_at"]
        for window_name, window in (("short", SHORT_WINDOW), ("long", LONG_WINDOW)):
            means, _ = _shifted_rolling_batch(merged.loc[idx], source_cols, sub_key, window, order_key)
            out.loc[idx, f"stats__{scope}__{window_name}__stats_coverage"] = means["has_stats"].values
            for metric in STAT_METRICS:
                for direction, col in (("for", metric), ("against", f"{metric}_against")):
                    out.loc[idx, f"stats__{scope}__{window_name}__{metric}_{direction}_avg"] = means[col].values
    return out


# ---------------------------------------------------------- личные встречи


def compute_h2h(fx: pd.DataFrame) -> pd.DataFrame:
    """ДП-5: сквозной расчёт (не в рамках сезона), последние H2H_WINDOW встреч."""
    # np.minimum/maximum вместо apply(axis=1): на 1M+ строк построчный apply
    # заметно медленнее векторизованной пары операций с тем же результатом.
    pair_key = list(
        zip(
            np.minimum(fx["home_team_id"], fx["away_team_id"]),
            np.maximum(fx["home_team_id"], fx["away_team_id"]),
        )
    )
    fx = fx.assign(_pair=pair_key).sort_values(["_pair", "kickoff_at", "fixture_id"])

    results = []
    for _, group in fx.groupby("_pair"):
        history: list[dict] = []
        for row in group.itertuples():
            recent = history[-H2H_WINDOW:]
            if len(recent) >= 2:
                total_goals = sum(h["total_goals"] for h in recent)
                home_wins = sum(1 for h in recent if h["winner_team_id"] == row.home_team_id)
                away_wins = sum(1 for h in recent if h["winner_team_id"] == row.away_team_id)
                draws = sum(1 for h in recent if h["winner_team_id"] is None)
                n = len(recent)
                results.append(
                    {
                        "fixture_id": row.fixture_id,
                        "h2h_matches_played": n,
                        "h2h_avg_total_goals": total_goals / n,
                        "h2h_home_team_win_rate": home_wins / n,
                        "h2h_draw_rate": draws / n,
                        "h2h_away_team_win_rate": away_wins / n,
                    }
                )
            else:
                results.append(
                    {
                        "fixture_id": row.fixture_id,
                        "h2h_matches_played": len(recent) if recent else 0,
                        "h2h_avg_total_goals": None,
                        "h2h_home_team_win_rate": None,
                        "h2h_draw_rate": None,
                        "h2h_away_team_win_rate": None,
                    }
                )
            # Несыгранный матч (--upcoming) не добавляем в историю пары —
            # у него ещё нет исхода, а fixture_id одного и того же горизонта
            # мог включать две ещё не сыгранные встречи подряд.
            if pd.notna(row.total_goals):
                winner_team_id = (
                    row.home_team_id if row.result_1x2 == "H" else row.away_team_id if row.result_1x2 == "A" else None
                )
                history.append({"total_goals": row.total_goals, "winner_team_id": winner_team_id})
    return pd.DataFrame(results)


# --------------------------------------------------------------------- травмы


def compute_injuries(fx: pd.DataFrame, injuries: pd.DataFrame) -> pd.DataFrame:
    """ДП-6: число травмированных к матчу, по fixture_id (не утечка, ДП-1)."""
    if injuries.empty:
        return pd.DataFrame({"fixture_id": fx["fixture_id"], "injuries_home_count": 0, "injuries_away_count": 0})
    counts = injuries.groupby(["fixture_id", "team_id"]).size().reset_index(name="n")
    merged = fx[["fixture_id", "home_team_id", "away_team_id"]].merge(
        counts, left_on=["fixture_id", "home_team_id"], right_on=["fixture_id", "team_id"], how="left"
    )
    merged["injuries_home_count"] = merged["n"].fillna(0).astype(int)
    merged = merged.drop(columns=["n", "team_id"]).merge(
        counts, left_on=["fixture_id", "away_team_id"], right_on=["fixture_id", "team_id"], how="left"
    )
    merged["injuries_away_count"] = merged["n"].fillna(0).astype(int)
    return merged[["fixture_id", "injuries_home_count", "injuries_away_count"]]


# ------------------------------------------------------------- турнирная таблица


def compute_standings(fx: pd.DataFrame, long_df: pd.DataFrame) -> pd.DataFrame:
    """ДП-7: место в таблице и разрыв в очках, по датам матчей, в рамках сезона."""
    long_df = long_df.copy()
    long_df["goal_diff"] = long_df["goals_for"] - long_df["goals_against"]

    records = []
    for (league_id, season), group in long_df.groupby(["league_id", "season"]):
        group = group.sort_values(["kickoff_at", "fixture_id"])
        cum_points: dict[int, float] = {}
        cum_gd: dict[int, float] = {}
        rows_by_fixture: dict[int, list[tuple]] = {}
        for row in group.itertuples():
            rows_by_fixture.setdefault(row.fixture_id, []).append(row)
        for fixture_id, rows in rows_by_fixture.items():
            snapshot_points = dict(cum_points)
            snapshot_gd = dict(cum_gd)
            ranking = sorted(snapshot_points, key=lambda t: (-snapshot_points[t], -snapshot_gd.get(t, 0)))
            rank_of = {team: i + 1 for i, team in enumerate(ranking)}
            for row in rows:
                records.append(
                    {
                        "fixture_id": fixture_id,
                        "team_id": row.team_id,
                        "rank": rank_of.get(row.team_id),
                        "points": snapshot_points.get(row.team_id, 0),
                    }
                )
            for row in rows:
                # Несыгранный матч (--upcoming) не двигает турнирную таблицу
                # вперёд — row.points/goal_diff тут NaN, а NaN в cum_points
                # безвозвратно испортил бы все последующие снимки для команды.
                if pd.notna(row.points):
                    cum_points[row.team_id] = cum_points.get(row.team_id, 0) + row.points
                    cum_gd[row.team_id] = cum_gd.get(row.team_id, 0) + row.goal_diff
    standings = pd.DataFrame(records)

    home = fx[["fixture_id", "home_team_id"]].merge(
        standings, left_on=["fixture_id", "home_team_id"], right_on=["fixture_id", "team_id"], how="left"
    )[["fixture_id", "rank", "points"]].rename(columns={"rank": "home_team_rank", "points": "home_team_points"})
    away = fx[["fixture_id", "away_team_id"]].merge(
        standings, left_on=["fixture_id", "away_team_id"], right_on=["fixture_id", "team_id"], how="left"
    )[["fixture_id", "rank", "points"]].rename(columns={"rank": "away_team_rank", "points": "away_team_points"})
    result = home.merge(away, on="fixture_id")
    result["points_gap"] = result["home_team_points"] - result["away_team_points"]
    return result


# --------------------------------------------------------------------- Эло


def compute_elo(fx: pd.DataFrame, initial_state: dict[tuple[int, int], float] | None = None) -> tuple[pd.DataFrame, dict]:
    """ДП-8: сквозной по сезонам, последовательный проход в хронологическом порядке."""
    ratings: dict[tuple[int, int], float] = dict(initial_state or {})
    # Средний рейтинг лиги инициализируется уже известными рейтингами
    # (initial_state) — иначе на инкрементальном запуске «средний рейтинг
    # лиги» для новой команды считался бы по пустому списку и откатывался
    # бы к ELO_DEFAULT, даже если у лиги уже есть история из прошлых запусков.
    league_avg: dict[int, list[float]] = {}
    for (_team_id, league_id), rating in ratings.items():
        league_avg.setdefault(league_id, []).append(rating)
    home_elo, away_elo = [], []

    for row in fx.sort_values(["kickoff_at", "fixture_id"]).itertuples():
        league_id = row.league_id
        home_key, away_key = (row.home_team_id, league_id), (row.away_team_id, league_id)
        current_avg = (sum(league_avg.get(league_id, [ELO_DEFAULT])) / max(1, len(league_avg.get(league_id, [1]))))

        if home_key not in ratings:
            ratings[home_key] = current_avg
        if away_key not in ratings:
            ratings[away_key] = current_avg

        r_home, r_away = ratings[home_key], ratings[away_key]
        home_elo.append(r_home)
        away_elo.append(r_away)

        # Несыгранный матч (--upcoming) не обновляет рейтинг — у него нет
        # исхода; home_elo/away_elo (до этой строки) уже зафиксированы выше
        # как текущее, известное на момент матча значение.
        if pd.notna(row.result_1x2):
            expected_home = 1.0 / (1.0 + 10 ** (-(r_home + ELO_HOME_ADVANTAGE - r_away) / 400.0))
            actual_home = {"H": 1.0, "D": 0.5, "A": 0.0}[row.result_1x2]
            delta = ELO_K * (actual_home - expected_home)
            ratings[home_key] = r_home + delta
            ratings[away_key] = r_away - delta

            league_avg.setdefault(league_id, []).append(ratings[home_key])
            league_avg[league_id].append(ratings[away_key])

    elo_df = fx.sort_values(["kickoff_at", "fixture_id"])[["fixture_id"]].copy()
    elo_df["home_team_elo"] = home_elo
    elo_df["away_team_elo"] = away_elo
    elo_df["elo_diff"] = elo_df["home_team_elo"] - elo_df["away_team_elo"]
    return elo_df, ratings


# ---------------------------------------------------------------- eval-колонки


def _nearest_line_odds(rows: pd.DataFrame, target_line: float | None, side: str) -> float | None:
    """Ближайшая к нашей линии котировка на `side` ('Over'/'Under'/'Home +'/…)."""
    if target_line is None or rows.empty:
        return None
    numeric = rows["value"].str.extract(r"([+-]?\d+(?:\.\d+)?)$")[0].astype(float)
    candidates = rows.assign(_num=numeric, _diff=(numeric - target_line).abs())
    candidates = candidates[candidates["value"].str.startswith(side)]
    if candidates.empty:
        return None
    return float(candidates.sort_values("_diff").iloc[0]["odd"])


def compute_eval_odds(fx: pd.DataFrame, odds: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """ДП-10: ближайший к матчу снимок линии, не признак — только для сверки.

    Колонки существуют всегда, даже когда котировок вообще нет (пустой
    `odds`): иначе write_features упал бы с KeyError на отсутствующей
    колонке, которую ожидает схема `ml_match_features`.
    """
    eval_col_names = [name for name, _ in eval_columns()]
    empty_result = pd.DataFrame({"fixture_id": fx["fixture_id"], **{c: None for c in eval_col_names}})
    if odds.empty:
        return empty_result

    out_rows = []
    latest = odds.sort_values("taken_at").groupby(["fixture_id", "bookmaker", "bet_type", "value"]).tail(1)
    by_fixture = {fid: g for fid, g in latest.groupby("fixture_id")}
    lines_by_fixture = lines.set_index("fixture_id")

    for fixture_id, group in by_fixture.items():
        row = {"fixture_id": fixture_id}
        line_row = lines_by_fixture.loc[fixture_id] if fixture_id in lines_by_fixture.index else None
        for key, bm_name in EVAL_BOOKMAKER_NAMES.items():
            bm_rows = group[group["bookmaker"] == bm_name]
            mw = bm_rows[bm_rows["bet_type"] == "Match Winner"]
            for value, col in (("Home", "home"), ("Draw", "draw"), ("Away", "away")):
                m = mw[mw["value"] == value]
                row[f"eval_{key}_odds_{col}"] = float(m.iloc[0]["odd"]) if not m.empty else None

            bts = bm_rows[bm_rows["bet_type"] == "Both Teams Score"]
            for value, col in (("Yes", "btts_yes"), ("No", "btts_no")):
                m = bts[bts["value"] == value]
                row[f"eval_{key}_odds_{col}"] = float(m.iloc[0]["odd"]) if not m.empty else None

            dc = bm_rows[bm_rows["bet_type"] == "Double Chance"]
            for value, col in (("Home/Draw", "dc_1x"), ("Draw/Away", "dc_x2"), ("Home/Away", "dc_12")):
                m = dc[dc["value"] == value]
                row[f"eval_{key}_odds_{col}"] = float(m.iloc[0]["odd"]) if not m.empty else None

            if line_row is not None:
                gou = bm_rows[bm_rows["bet_type"] == "Goals Over/Under"]
                row[f"eval_{key}_odds_league_total_over"] = _nearest_line_odds(gou, line_row.get("league_total_line"), "Over")
                row[f"eval_{key}_odds_league_total_under"] = _nearest_line_odds(gou, line_row.get("league_total_line"), "Under")

                th = bm_rows[bm_rows["bet_type"] == "Total - Home"]
                row[f"eval_{key}_odds_home_total_over"] = _nearest_line_odds(th, line_row.get("home_team_line"), "Over")
                row[f"eval_{key}_odds_home_total_under"] = _nearest_line_odds(th, line_row.get("home_team_line"), "Under")

                ta = bm_rows[bm_rows["bet_type"] == "Total - Away"]
                row[f"eval_{key}_odds_away_total_over"] = _nearest_line_odds(ta, line_row.get("away_team_line"), "Over")
                row[f"eval_{key}_odds_away_total_under"] = _nearest_line_odds(ta, line_row.get("away_team_line"), "Under")

                ah = bm_rows[bm_rows["bet_type"] == "Asian Handicap"]
                favorite = line_row.get("handicap_favorite")
                handicap_line = line_row.get("handicap_line")
                if favorite and pd.notna(handicap_line):
                    side = "Home" if favorite == "home" else "Away"
                    row[f"eval_{key}_odds_handicap_favorite"] = _nearest_line_odds(
                        ah, -abs(handicap_line), side
                    )
        out_rows.append(row)
    result = pd.DataFrame(out_rows)
    # Гарантия полного набора колонок даже если какой-то из рынков не
    # встретился ни в одной строке (например, в тестовых данных нет
    # Asian Handicap) — иначе схема ml_match_features не совпала бы.
    for c in eval_col_names:
        if c not in result.columns:
            result[c] = None
    return result


# --------------------------------------------------------------------- сборка


def build(conn: psycopg.Connection, include_upcoming: bool = False, horizon_days: int | None = None) -> pd.DataFrame:
    """Строит полную таблицу признаков (без записи в базу).

    `include_upcoming` (--upcoming) добавляет к истории ещё не сыгранные
    матчи в горизонте `horizon_days` дней — их целевые колонки останутся
    NULL, историю это не меняет (см. модульный докстринг)."""
    fx = load_fixtures(conn, include_upcoming=include_upcoming, horizon_days=horizon_days)
    fx = compute_targets(fx)
    fx = compute_league_lines(fx)

    long_df = _team_long_format(fx)
    fx = compute_team_lines(fx, long_df)

    form = compute_form(long_df)
    stats_raw = load_statistics(conn)
    stats = compute_statistics_features(long_df, stats_raw)
    h2h = compute_h2h(fx)
    injuries_raw = load_injuries(conn)
    injuries = compute_injuries(fx, injuries_raw)
    standings = compute_standings(fx, long_df)
    elo_df, final_ratings = compute_elo(fx)
    odds_raw = load_odds(conn)
    eval_odds = compute_eval_odds(
        fx, odds_raw, fx[["fixture_id", "league_total_line", "home_team_line", "away_team_line", "handicap_favorite", "handicap_line"]]
    )

    wide_form = _pivot_side_features(form, "form")
    wide_stats = _pivot_side_features(stats, "stats")

    result = fx.merge(wide_form, on="fixture_id", how="left")
    result = result.merge(wide_stats, on="fixture_id", how="left")
    result = result.merge(h2h, on="fixture_id", how="left")
    result = result.merge(injuries, on="fixture_id", how="left")
    result = result.merge(standings, on="fixture_id", how="left")
    result = result.merge(elo_df, on="fixture_id", how="left")
    result = result.merge(eval_odds, on="fixture_id", how="left")
    result.attrs["elo_final_ratings"] = final_ratings
    return result


def _pivot_side_features(long_features: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Разворачивает таблицу (fixture_id, team_id, is_home, признак__…) в
    широкий формат `home_team_*`/`away_team_*` по одной строке на матч."""
    value_cols = [c for c in long_features.columns if c.startswith(f"{prefix}__")]
    home = long_features[long_features["is_home"]][["fixture_id"] + value_cols].copy()
    home.columns = ["fixture_id"] + [f"home_team_{c[len(prefix) + 2:].replace('__', '_')}" for c in value_cols]
    away = long_features[~long_features["is_home"]][["fixture_id"] + value_cols].copy()
    away.columns = ["fixture_id"] + [f"away_team_{c[len(prefix) + 2:].replace('__', '_')}" for c in value_cols]
    return home.merge(away, on="fixture_id", how="outer")


# ----------------------------------------------------------------------- запись


def write_features(conn: psycopg.Connection, df: pd.DataFrame, incremental: bool) -> int:
    """Пишет в ml_match_features. При --incremental — только новые fixture_id.

    Пишет строго по возрастанию kickoff_at — независимо от того, в каком
    порядке `build()` собрал строки внутри (compute_league_lines
    пересортировывает по лиге/сезону). Без этого обрыв сети на середине
    записи (найдено на проде 30.09.2026: упавший SSH-туннель) оставлял бы
    в базе не «всё до какой-то даты», а случайную вперемешку по лигам
    подмножество — и последующий --incremental, ориентируясь на
    max(kickoff_at) уже записанного, не понял бы, что часть более ранних
    матчей из других лиг ещё не попала в базу, и молча их пропустил бы.
    """
    df = df.sort_values(["kickoff_at", "fixture_id"])
    columns = ["fixture_id"] + [name for name, _ in all_columns()]
    if incremental:
        with conn.cursor() as cur:
            cur.execute("SELECT max(kickoff_at) FROM ml_match_features")
            row = cur.fetchone()
            last = row[0] if row else None
        if last is not None:
            df = df[pd.to_datetime(df["kickoff_at"], utc=True) > last]

    if df.empty:
        return 0

    payload = df[columns].copy()
    payload = payload.astype(object).where(payload.notna(), None)
    rows = [tuple(r) for r in payload.itertuples(index=False)]

    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "fixture_id")
    sql = (
        f"INSERT INTO ml_match_features ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT (fixture_id) DO UPDATE SET {updates}, computed_at = now()"
    )
    BATCH = 2_000
    with conn.cursor() as cur:
        for start in range(0, len(rows), BATCH):
            with conn.transaction():
                cur.executemany(sql, rows[start : start + BATCH])
    return len(rows)


def write_features_resilient(
    url: str, df: pd.DataFrame, incremental: bool, batch_size: int = 2_000, max_attempts: int = 5
) -> int:
    """Как `write_features`, но открывает новое соединение на каждый пакет.

    Нужно для записи через нестабильный канал (SSH-туннель через
    нестабильную сеть): на проде 30.09.2026 три подряд полных прогона
    обрывались посередине записи одним держащимся открытым соединением
    ("server closed the connection unexpectedly") — похоже на разрыв
    долгоживущего TCP где-то на сетевом пути, не связанный ни с Postgres,
    ни с самим кодом (сервер и контейнер оставались здоровы все три раза).
    Короткое соединение на пакет из ~2000 строк живёт секунды, а не
    десятки минут, поэтому куда менее уязвимо к такому разрыву; при сбое
    повторяет именно тот же пакет с новым соединением, а не всё сначала.
    """
    df = df.sort_values(["kickoff_at", "fixture_id"])
    columns = ["fixture_id"] + [name for name, _ in all_columns()]
    if incremental:
        with connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT max(kickoff_at) FROM ml_match_features")
            row = cur.fetchone()
            last = row[0] if row else None
        if last is not None:
            df = df[pd.to_datetime(df["kickoff_at"], utc=True) > last]

    if df.empty:
        return 0

    payload = df[columns].copy()
    payload = payload.astype(object).where(payload.notna(), None)
    rows = [tuple(r) for r in payload.itertuples(index=False)]

    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "fixture_id")
    sql = (
        f"INSERT INTO ml_match_features ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT (fixture_id) DO UPDATE SET {updates}, computed_at = now()"
    )

    written = 0
    total_batches = -(-len(rows) // batch_size)
    for i, start in enumerate(range(0, len(rows), batch_size), 1):
        chunk = rows[start : start + batch_size]
        for attempt in range(1, max_attempts + 1):
            try:
                with connect(url) as conn, conn.cursor() as cur:
                    with conn.transaction():
                        cur.executemany(sql, chunk)
                break
            except psycopg.OperationalError as error:
                if attempt == max_attempts:
                    raise
                print(f"  пакет {i}/{total_batches}: {error}; повтор {attempt}/{max_attempts}")
                time.sleep(min(30, 2**attempt))
        written += len(chunk)
        if i % 20 == 0 or i == total_batches:
            print(f"  записано пакетов: {i}/{total_batches} ({written} строк)")
    return written


def write_elo_state(conn: psycopg.Connection, fx: pd.DataFrame, ratings: dict[tuple[int, int], float]) -> None:
    """Сохраняет итоговый рейтинг Эло и матч, на котором он посчитан последним."""
    with conn.cursor() as cur:
        for team_id, league_id in list(ratings):
            cur.execute(
                """SELECT max(fixture_id) FROM (
                       SELECT fixture_id FROM fixtures WHERE league_id=%s AND home_team_id=%s
                       UNION ALL
                       SELECT fixture_id FROM fixtures WHERE league_id=%s AND away_team_id=%s
                   ) t""",
                (league_id, team_id, league_id, team_id),
            )
            fixture_id = cur.fetchone()[0]
            if fixture_id is None:
                continue
            cur.execute(
                """INSERT INTO ml_team_elo_state (team_id, league_id, rating, as_of_fixture_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (team_id, league_id) DO UPDATE
                   SET rating = EXCLUDED.rating, as_of_fixture_id = EXCLUDED.as_of_fixture_id, updated_at = now()""",
                (team_id, league_id, ratings[(team_id, league_id)], fixture_id),
            )


def write_elo_state_resilient(url: str, fx: pd.DataFrame, ratings: dict[tuple[int, int], float], batch_size: int = 2_000) -> int:
    """Как `write_elo_state`, но пакетно и с переподключением на пакет.

    Первая версия делала SELECT+INSERT отдельным соединением на каждую пару
    (team, league) — при ~26 тысячах пар и накладных расходах на установку
    соединения через SSH-туннель это заняло бы часы (по факту на проде
    30.09.2026 — около 1000 пар за 10 минут, то есть ~4 часа на всё).
    Настоящая причина медленности была не в "зависании" (та версия и
    появилась как раз из-за того, что до неё ОДНО долгоживущее соединение
    на все пары зависло намертво без ошибки) — а в самой стратегии
    "запрос на пару". Правильное решение — то же, что уже применено в
    write_features_resilient: посчитать "последний fixture_id команды
    в лиге" ОДНИМ запросом на все пары сразу, затем писать пакетами.
    """
    if not ratings:
        return 0

    finished = fx[fx["reg_home"].notna()] if "reg_home" in fx.columns else fx
    long_ids = pd.concat(
        [
            finished[["league_id", "home_team_id", "fixture_id"]].rename(columns={"home_team_id": "team_id"}),
            finished[["league_id", "away_team_id", "fixture_id"]].rename(columns={"away_team_id": "team_id"}),
        ],
        ignore_index=True,
    )
    last_fixture = long_ids.groupby(["team_id", "league_id"])["fixture_id"].max()

    rows = []
    for (team_id, league_id), rating in ratings.items():
        fixture_id = last_fixture.get((team_id, league_id))
        if fixture_id is None or pd.isna(fixture_id):
            continue
        rows.append((team_id, league_id, rating, int(fixture_id)))

    sql = (
        "INSERT INTO ml_team_elo_state (team_id, league_id, rating, as_of_fixture_id) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (team_id, league_id) DO UPDATE "
        "SET rating = EXCLUDED.rating, as_of_fixture_id = EXCLUDED.as_of_fixture_id, updated_at = now()"
    )
    written = 0
    total_batches = -(-len(rows) // batch_size)
    for i, start in enumerate(range(0, len(rows), batch_size), 1):
        chunk = rows[start : start + batch_size]
        for attempt in range(1, 4):
            try:
                with connect(url) as conn, conn.cursor() as cur:
                    with conn.transaction():
                        cur.executemany(sql, chunk)
                break
            except psycopg.OperationalError as error:
                if attempt == 3:
                    raise
                print(f"  пакет Эло {i}/{total_batches}: {error}; повтор {attempt}/3")
                time.sleep(2 * attempt)
        written += len(chunk)
        print(f"  Эло записано: {i}/{total_batches} ({written} пар)")
    return written


# ------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Построение таблицы признаков для модели")
    parser.add_argument("--incremental", action="store_true", help="писать только новые матчи")
    parser.add_argument(
        "--upcoming",
        action="store_true",
        help="признаки для ещё не сыгранных матчей (инференс), не трогает историю и Эло",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=DEFAULT_UPCOMING_HORIZON_DAYS,
        help=f"горизонт для --upcoming, дней вперёд от текущего момента (по умолчанию {DEFAULT_UPCOMING_HORIZON_DAYS})",
    )
    args = parser.parse_args(argv)

    if args.upcoming and args.incremental:
        parser.error("--upcoming и --incremental нельзя использовать вместе")

    url = database_url()
    with connect(url) as conn:
        df = build(conn, include_upcoming=args.upcoming, horizon_days=args.horizon_days if args.upcoming else None)

    # Запись — отдельными короткими соединениями (write_features_resilient,
    # write_elo_state_resilient), не одной долгоживущей `conn`: см. их
    # докстринги про обрывы и зависания на проде.
    if args.upcoming:
        # Пишем только сами несыгранные строки — история и так уже в базе
        # (не изменилась), переписывать её заново незачем. reg_home NULL —
        # надёжный признак «матч ещё не сыгран»: у финализированных строк
        # он всегда заполнен (compute_targets либо считает его, либо
        # выбрасывает завершённый матч без счёта как дефект данных).
        upcoming_df = df[df["reg_home"].isna()]
        written = write_features_resilient(url, upcoming_df, incremental=False)
        print(f"признаки будущих матчей записаны: {written} (горизонт {args.horizon_days} дн.)")
        return 0

    written = write_features_resilient(url, df, incremental=args.incremental)
    write_elo_state_resilient(url, df, df.attrs.get("elo_final_ratings", {}))
    print(f"строк записано: {written} (всего вычислено: {len(df)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
