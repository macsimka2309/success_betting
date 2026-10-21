"""Тесты признаков группы А/Б для анализа поздних голов (без БД).

Источник формул: сообщение чата «Анализ данных» 17.10.2026, см.
src/jobs/build_late_goal_features.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.jobs.build_late_goal_features import (
    DEFAULT_CUTOFF_MINUTE,
    build_group_a_features,
    build_group_b_features,
    compute_dominant_side,
    load_dominant_base,
)


def make_base_row(
    fixture_id: int,
    home_possession: int,
    away_possession: int,
    home_team_id: int = 1,
    away_team_id: int = 2,
    ht_home: float | None = 1,
    ht_away: float | None = 0,
    league_id: int = 10,
    season: int = 2025,
) -> dict:
    return {
        "fixture_id": fixture_id,
        "league_id": league_id,
        "season": season,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "ht_home": ht_home,
        "ht_away": ht_away,
        "home_possession": home_possession,
        "away_possession": away_possession,
    }


# --------------------------------------------------------------- доминирующая сторона


def test_home_dominant_when_possession_greater_or_equal():
    base = pd.DataFrame([make_base_row(1, 60, 40), make_base_row(2, 50, 50), make_base_row(3, 30, 70)])
    out = compute_dominant_side(base)
    assert out.loc[out["fixture_id"] == 1, "home_dominant"].item() is np.True_ or out.loc[out["fixture_id"] == 1, "home_dominant"].item()
    assert bool(out.loc[out["fixture_id"] == 2, "home_dominant"].item()) is True  # 50/50 -> home (>=)
    assert bool(out.loc[out["fixture_id"] == 3, "home_dominant"].item()) is False


# --------------------------------------------------------------- группа А


def test_group_a_attributes_home_dominant_values():
    base = pd.DataFrame([make_base_row(1, 60, 40, ht_home=1, ht_away=0)])
    halves = pd.DataFrame(
        [
            {
                "fixture_id": 1,
                "team_id": 1,  # home
                "total_shots": 8,
                "shots_on_goal": 4,
                "shots_off_goal": 3,
                "blocked_shots": 1,
                "shots_insidebox": 5,
                "shots_outsidebox": 3,
                "ball_possession": 60,
                "corner_kicks": 3,
                "offsides": 1,
                "passes_pct": 70,
                "goalkeeper_saves": 2,
                "yellow_cards": 1,
                "expected_goals": 0.8,
            },
            {
                "fixture_id": 1,
                "team_id": 2,  # away
                "total_shots": 3,
                "shots_on_goal": 1,
                "shots_off_goal": 1,
                "blocked_shots": 1,
                "shots_insidebox": 2,
                "shots_outsidebox": 1,
                "ball_possession": 40,
                "corner_kicks": 1,
                "offsides": 0,
                "passes_pct": 50,
                "goalkeeper_saves": 3,
                "yellow_cards": 0,
                "expected_goals": 0.2,
            },
        ]
    )
    out = build_group_a_features(base, halves)
    row = out.iloc[0]

    assert row["h1_total_shots_dominant"] == 8
    assert row["h1_total_shots_other"] == 3
    assert row["h1_total_shots_diff"] == 5
    assert row["h1_possession_dominant"] == 60
    assert row["h1_possession_other"] == 40
    assert row["h1_xg_dominant"] == 0.8
    assert row["h1_xg_other"] == 0.2
    # xg_overperformance: ht_home(1) - h1_xg_home(0.8) = 0.2; ht_away(0) - h1_xg_away(0.2) = -0.2
    assert row["h1_xg_overperformance_dominant"] == pytest.approx(0.2)
    assert row["h1_xg_overperformance_other"] == pytest.approx(-0.2)
    assert row["h1_xg_overperformance_diff"] == pytest.approx(0.4)


def test_group_a_keeps_xg_null_when_missing_not_fillna_zero():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    halves = pd.DataFrame(
        [
            {
                "fixture_id": 1,
                "team_id": 1,
                "total_shots": 5,
                "shots_on_goal": 2,
                "shots_off_goal": 2,
                "blocked_shots": 1,
                "shots_insidebox": 3,
                "shots_outsidebox": 2,
                "ball_possession": 60,
                "corner_kicks": 2,
                "offsides": 0,
                "passes_pct": 65,
                "goalkeeper_saves": 1,
                "yellow_cards": 0,
                "expected_goals": None,
            },
            {
                "fixture_id": 1,
                "team_id": 2,
                "total_shots": 4,
                "shots_on_goal": 1,
                "shots_off_goal": 2,
                "blocked_shots": 1,
                "shots_insidebox": 2,
                "shots_outsidebox": 2,
                "ball_possession": 40,
                "corner_kicks": 1,
                "offsides": 1,
                "passes_pct": 55,
                "goalkeeper_saves": 2,
                "yellow_cards": 1,
                "expected_goals": 0.3,
            },
        ]
    )
    out = build_group_a_features(base, halves)
    row = out.iloc[0]
    assert pd.isna(row["h1_xg_dominant"])  # отсутствующий xg остаётся NaN, не 0
    assert row["h1_xg_other"] == 0.3
    assert pd.isna(row["h1_xg_diff"])  # NaN распространяется


def test_group_a_excludes_fixtures_without_half1_for_both_teams():
    base = pd.DataFrame([make_base_row(1, 60, 40), make_base_row(2, 55, 45)])
    halves = pd.DataFrame(
        [
            {
                "fixture_id": 1,
                "team_id": 1,
                "total_shots": 5,
                "shots_on_goal": 2,
                "shots_off_goal": 2,
                "blocked_shots": 1,
                "shots_insidebox": 3,
                "shots_outsidebox": 2,
                "ball_possession": 60,
                "corner_kicks": 2,
                "offsides": 0,
                "passes_pct": 65,
                "goalkeeper_saves": 1,
                "yellow_cards": 0,
                "expected_goals": 0.5,
            }
            # fixture 1, team 2 (away) отсутствует -> матч 1 не должен попасть в out
            # fixture 2 вообще без half=1 -> тоже не должен попасть
        ]
    )
    out = build_group_a_features(base, halves)
    assert out.empty


# --------------------------------------------------------------- группа Б


def test_group_b_goals_46_75_counted_and_attributed():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 1, "type": "Goal", "detail": None, "minute": 50},  # dominant (home)
            {"fixture_id": 1, "team_id": 1, "type": "Goal", "detail": "Missed Penalty", "minute": 55},  # не считается
            {"fixture_id": 1, "team_id": 2, "type": "Goal", "detail": None, "minute": 80},  # за окном
            {"fixture_id": 1, "team_id": 2, "type": "Goal", "detail": None, "minute": 60},  # other
        ]
    )
    out = build_group_b_features(base, events, DEFAULT_CUTOFF_MINUTE)
    row = out.iloc[0]
    assert row["goals_46_75_dominant"] == 1
    assert row["goals_46_75_other"] == 1


def test_group_b_red_card_includes_second_yellow():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 2, "type": "Card", "detail": "Second Yellow Card", "minute": 70},
        ]
    )
    out = build_group_b_features(base, events, DEFAULT_CUTOFF_MINUTE)
    row = out.iloc[0]
    assert row["red_card_before_75_dominant"] is False or row["red_card_before_75_dominant"] == False
    assert row["red_card_before_75_other"] == True


def test_group_b_red_card_false_when_after_cutoff():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 1, "type": "Card", "detail": "Red Card", "minute": 80},
        ]
    )
    out = build_group_b_features(base, events, DEFAULT_CUTOFF_MINUTE)
    row = out.iloc[0]
    assert row["red_card_before_75_dominant"] == False


def test_group_b_subst_count_not_boolean():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 1, "type": "subst", "detail": None, "minute": 60},
            {"fixture_id": 1, "team_id": 1, "type": "subst", "detail": None, "minute": 65},
            {"fixture_id": 1, "team_id": 1, "type": "subst", "detail": None, "minute": 80},  # после отсечки
        ]
    )
    out = build_group_b_features(base, events, DEFAULT_CUTOFF_MINUTE)
    row = out.iloc[0]
    assert row["subst_count_before_75_dominant"] == 2
    assert row["subst_count_before_75_other"] == 0


def test_group_b_yellow_cards_window_60_75():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 2, "type": "Card", "detail": "Yellow Card", "minute": 59},  # до окна
            {"fixture_id": 1, "team_id": 2, "type": "Card", "detail": "Yellow Card", "minute": 61},
            {"fixture_id": 1, "team_id": 2, "type": "Card", "detail": "Yellow Card", "minute": 75},
            {"fixture_id": 1, "team_id": 2, "type": "Card", "detail": "Yellow Card", "minute": 76},  # после окна
        ]
    )
    out = build_group_b_features(base, events, DEFAULT_CUTOFF_MINUTE)
    row = out.iloc[0]
    assert row["yellow_cards_60_75_other"] == 2
    assert row["yellow_cards_60_75_dominant"] == 0


def test_group_b_no_events_gives_zero_counts_and_false_flags():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(columns=["fixture_id", "team_id", "type", "detail", "minute"])
    out = build_group_b_features(base, events, DEFAULT_CUTOFF_MINUTE)
    row = out.iloc[0]
    assert row["goals_46_75_dominant"] == 0
    assert row["goals_46_75_other"] == 0
    assert row["red_card_before_75_dominant"] == False
    assert row["red_card_before_75_other"] == False
    assert row["subst_count_before_75_dominant"] == 0
    assert row["yellow_cards_60_75_other"] == 0


def test_group_b_respects_custom_cutoff():
    base = pd.DataFrame([make_base_row(1, 60, 40)])
    events = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 1, "type": "Goal", "detail": None, "minute": 68},
        ]
    )
    out75 = build_group_b_features(base, events, 75)
    out65 = build_group_b_features(base, events, 65)
    assert out75.iloc[0]["goals_46_75_dominant"] == 1
    assert out65.iloc[0]["goals_46_65_dominant"] == 0


def test_load_dominant_base_is_a_function_with_connection_arg():
    # Чистая сигнатура-проверка — не обращаемся к БД, только к интерфейсу функции.
    import inspect

    sig = inspect.signature(load_dominant_base)
    assert list(sig.parameters) == ["conn"]
