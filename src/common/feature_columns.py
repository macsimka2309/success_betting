"""Единственный источник правды для колонок `ml_match_features`.

Список используется дважды: генератором миграции (`scripts/`) и самим
`src/jobs/build_features.py`. Раздельные списки в двух местах — это ровно
тот сорт дублирования, который уже давал молчаливые ошибки в проекте
(ср. индексную путаницу в `collect.py`), поэтому колонки объявлены один
раз здесь.
"""

from __future__ import annotations

SIDES = ("home_team", "away_team")
SCOPES = ("overall", "home", "away")
WINDOWS = ("short", "long")

FORM_METRICS = (
    "goals_scored_avg",
    "goals_conceded_avg",
    "points_avg",
    "win_rate",
    "draw_rate",
    "loss_rate",
    "clean_sheet_rate",
    "failed_to_score_rate",
    "matches_played",
)

STAT_METRICS = (
    "shots_on_goal",
    "total_shots",
    "corner_kicks",
    "ball_possession",
    "expected_goals",
)

# Рынки для eval_*-колонок (ДП-10) и букмекеры, для которых они считаются.
EVAL_BOOKMAKERS = ("1xbet", "pinnacle")


def form_columns() -> list[tuple[str, str]]:
    """(имя колонки, тип SQL) для признаков формы (ДП-3)."""
    out = []
    for side in SIDES:
        for scope in SCOPES:
            for window in WINDOWS:
                for metric in FORM_METRICS:
                    sql_type = "SMALLINT" if metric == "matches_played" else "NUMERIC(6,3)"
                    out.append((f"{side}_{scope}_{window}_{metric}", sql_type))
    return out


def stat_columns() -> list[tuple[str, str]]:
    """(имя колонки, тип SQL) для признаков статистики (ДП-4), с coverage."""
    out = []
    for side in SIDES:
        for scope in SCOPES:
            for window in WINDOWS:
                out.append((f"{side}_{scope}_{window}_stats_coverage", "NUMERIC(4,3)"))
                for metric in STAT_METRICS:
                    for direction in ("for", "against"):
                        out.append((f"{side}_{scope}_{window}_{metric}_{direction}_avg", "NUMERIC(7,3)"))
    return out


def target_columns() -> list[tuple[str, str]]:
    """Цели и линии (ДП-0, ДП-2, ДП-13, ДП-14)."""
    return [
        ("reg_home", "SMALLINT"),
        ("reg_away", "SMALLINT"),
        ("result_1x2", "TEXT"),
        ("league_total_line", "NUMERIC(5,2)"),
        ("league_total_over", "BOOLEAN"),
        ("btts", "BOOLEAN"),
        ("home_team_line", "NUMERIC(5,2)"),
        ("home_total_over", "BOOLEAN"),
        ("away_team_line", "NUMERIC(5,2)"),
        ("away_total_over", "BOOLEAN"),
        ("handicap_favorite", "TEXT"),
        ("handicap_line", "NUMERIC(5,2)"),
        ("handicap_favorite_covers", "BOOLEAN"),
        ("dc_1x", "BOOLEAN"),
        ("dc_x2", "BOOLEAN"),
        ("dc_12", "BOOLEAN"),
    ]


def h2h_columns() -> list[tuple[str, str]]:
    """Личные встречи (ДП-5)."""
    return [
        ("h2h_matches_played", "SMALLINT"),
        ("h2h_avg_total_goals", "NUMERIC(5,2)"),
        ("h2h_home_team_win_rate", "NUMERIC(4,3)"),
        ("h2h_draw_rate", "NUMERIC(4,3)"),
        ("h2h_away_team_win_rate", "NUMERIC(4,3)"),
    ]


def injury_columns() -> list[tuple[str, str]]:
    """Травмы к матчу (ДП-6)."""
    return [
        ("injuries_home_count", "SMALLINT"),
        ("injuries_away_count", "SMALLINT"),
    ]


def league_context_columns() -> list[tuple[str, str]]:
    """Контекст лиги и турнирное положение (ДП-7)."""
    return [
        ("league_home_win_rate", "NUMERIC(4,3)"),
        ("league_draw_rate", "NUMERIC(4,3)"),
        ("league_away_win_rate", "NUMERIC(4,3)"),
        ("home_team_rank", "SMALLINT"),
        ("away_team_rank", "SMALLINT"),
        ("home_team_points", "SMALLINT"),
        ("away_team_points", "SMALLINT"),
        ("points_gap", "SMALLINT"),
    ]


def elo_columns() -> list[tuple[str, str]]:
    """Рейтинг силы команд (ДП-8)."""
    return [
        ("home_team_elo", "NUMERIC(7,2)"),
        ("away_team_elo", "NUMERIC(7,2)"),
        ("elo_diff", "NUMERIC(7,2)"),
    ]


def eval_columns() -> list[tuple[str, str]]:
    """Коэффициенты для сверки, не для модели (ДП-10)."""
    out = []
    for bm in EVAL_BOOKMAKERS:
        out += [
            (f"eval_{bm}_odds_home", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_draw", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_away", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_btts_yes", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_btts_no", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_league_total_over", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_league_total_under", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_home_total_over", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_home_total_under", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_away_total_over", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_away_total_under", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_handicap_favorite", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_dc_1x", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_dc_x2", "NUMERIC(8,3)"),
            (f"eval_{bm}_odds_dc_12", "NUMERIC(8,3)"),
        ]
    return out


def grain_columns() -> list[tuple[str, str]]:
    """Идентификаторы и контекст матча — не признак и не цель."""
    return [
        ("league_id", "INTEGER"),
        ("season", "INTEGER"),
        ("match_date", "DATE"),
        ("kickoff_at", "TIMESTAMPTZ"),
        ("home_team_id", "INTEGER"),
        ("away_team_id", "INTEGER"),
    ]


def all_columns() -> list[tuple[str, str]]:
    """Полный список признаковых колонок (без fixture_id и computed_at —
    они заданы в самой миграции 003 как структура таблицы)."""
    return (
        grain_columns()
        + target_columns()
        + form_columns()
        + stat_columns()
        + h2h_columns()
        + injury_columns()
        + league_context_columns()
        + elo_columns()
        + eval_columns()
    )
