"""Тесты перебора признаков для голов после отсечки (без БД).

specs/разведочный-анализ-поздних-голов.md, раздел «Тестовые сценарии»:
синтетический набор матчей с известными владением/ударами/заменами/
карточками/голами по минутам. Ни одного обращения к БД — проверяется
только чистая логика (src/jobs/scan_goal_after_cutoff.py).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.jobs.scan_goal_after_cutoff import (
    Combo,
    ComboResult,
    GRID_SIZE,
    SITUATIONS,
    apply_gate,
    assign_split,
    build_feature_table,
    combo_mask,
    evaluate_combo,
    filter_reliable,
    generate_grid,
    situation_mask,
)


# --------------------------------------------------------------- решётка и сезоны


def test_grid_size_is_14520_with_wider_shots_range():
    grid = generate_grid()
    assert len(grid) == 14_520 == GRID_SIZE


def test_grid_combos_are_unique():
    grid = generate_grid()
    assert len(set(grid)) == len(grid)


@pytest.mark.parametrize(
    "season,expected",
    [(2017, None), (2018, "train"), (2020, "train"), (2022, "train"), (2023, "holdout"),
     (2024, "holdout"), (2025, "holdout"), (2026, "monitor"), (2027, "monitor"), (2028, None)],
)
def test_assign_split(season, expected):
    assert assign_split(season) == expected


def test_situation_mask_variants():
    diff = pd.Series([-3, -2, -1, 0, 1, 2, 3])
    assert situation_mask(diff, "проигрывает_2+").tolist() == [True, True, False, False, False, False, False]
    assert situation_mask(diff, "проигрывает_1").tolist() == [False, False, True, False, False, False, False]
    assert situation_mask(diff, "ничья").tolist() == [False, False, False, True, False, False, False]
    assert situation_mask(diff, "выигрывает_1").tolist() == [False, False, False, False, True, False, False]
    assert situation_mask(diff, "выигрывает_2+").tolist() == [False, False, False, False, False, True, True]


def test_situation_categories_are_a_clean_partition():
    # пять категорий не пересекаются и покрывают весь диапазон без пропусков
    diff = pd.Series(range(-5, 6))
    masks = [situation_mask(diff, s) for s in SITUATIONS]
    assert all(sum(m[i] for m in masks) == 1 for i in range(len(diff)))


def test_situation_mask_rejects_unknown_label():
    with pytest.raises(ValueError):
        situation_mask(pd.Series([0]), "???")


# --------------------------------------------------------------- надёжность (РА-2)


def _base_row(fixture_id, goals_home, goals_away, events_fetched, **overrides):
    row = {
        "fixture_id": fixture_id,
        "league_id": 1,
        "season": 2020,
        "home_team_id": 100,
        "away_team_id": 200,
        "goals_home": goals_home,
        "goals_away": goals_away,
        "ft90_home": None,
        "ft90_away": None,
        "home_possession": 65,
        "away_possession": 35,
        "home_total_shots": 10,
        "home_shots_on_goal": 5,
        "away_total_shots": 6,
        "away_shots_on_goal": 2,
        "events_fetched": events_fetched,
    }
    row.update(overrides)
    return row


def test_filter_reliable_drops_nonzero_score_without_events():
    df = pd.DataFrame([_base_row(1, 1, 0, events_fetched=False)])
    assert filter_reliable(df).empty


def test_filter_reliable_keeps_scoreless_draw_without_events():
    df = pd.DataFrame([_base_row(1, 0, 0, events_fetched=False)])
    assert len(filter_reliable(df)) == 1


def test_filter_reliable_keeps_nonzero_score_with_events():
    df = pd.DataFrame([_base_row(1, 1, 0, events_fetched=True)])
    assert len(filter_reliable(df)) == 1


# --------------------------------------------------------------- построение признаков


def test_build_feature_table_full_example_at_cutoff_75():
    base = pd.DataFrame([_base_row(1, 2, 1, events_fetched=True)])
    goals = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 100, "minute": 50},  # home, до отсечки
            {"fixture_id": 1, "team_id": 200, "minute": 60},  # away, до отсечки
            {"fixture_id": 1, "team_id": 100, "minute": 85},  # home, после отсечки
        ]
    )
    subst = pd.DataFrame(
        [
            {"fixture_id": 1, "team_id": 100, "minute": 65},  # home (доминирующая) — в окне [60,75]
            {"fixture_id": 1, "team_id": 200, "minute": 50},  # away (другая) — до окна, не считается
        ]
    )

    out = build_feature_table(base, goals, subst, cutoff_minute=75).iloc[0]

    assert out["dominant_side"] == "H"
    assert out["dominant_possession"] == 65
    assert out["home_goals_cutoff"] == 1
    assert out["away_goals_cutoff"] == 1
    assert out["situation_diff"] == 0  # ничья на 75-й, несмотря на итоговые 2:1
    assert bool(out["any_after"])  # home забил на 85-й
    assert bool(out["dominant_after"])  # это гол именно доминирующей команды
    assert bool(out["dominant_subst_window"])  # замена home (доминирующей) на 65-й, внутри [60,75]
    assert not bool(out["other_subst_window"])  # замена away была на 50-й, до окна
    assert out["split"] == "train"


def test_build_feature_table_substitution_at_window_boundaries():
    """Окно замкнуто с обоих концов: [60, cutoff_minute] включительно,
    минута ровно 59 (вне) и ровно cutoff+1 (вне) не считаются."""
    base = pd.DataFrame([_base_row(10, 0, 0, events_fetched=True)])  # home dominant
    subst = pd.DataFrame(
        [
            {"fixture_id": 10, "team_id": 100, "minute": 60},  # ровно нижняя граница — считается
            {"fixture_id": 10, "team_id": 200, "minute": 76},  # на 1 минуту после отсечки — не считается
        ]
    )
    out = build_feature_table(base, pd.DataFrame(columns=["fixture_id", "team_id", "minute"]),
                               subst, cutoff_minute=75).iloc[0]
    assert bool(out["dominant_subst_window"])  # минута 60 входит в [60,75]
    assert not bool(out["other_subst_window"])  # минута 76 вне [60,75]


def test_build_feature_table_away_dominant_and_no_goal_after_cutoff():
    base = pd.DataFrame([_base_row(2, 0, 1, events_fetched=True, home_possession=30, away_possession=70)])
    goals = pd.DataFrame([{"fixture_id": 2, "team_id": 200, "minute": 40}])  # away, до отсечки

    out = build_feature_table(base, goals, pd.DataFrame(columns=["fixture_id", "team_id", "minute"]),
                               cutoff_minute=75).iloc[0]

    assert out["dominant_side"] == "A"
    assert not bool(out["any_after"])
    assert not bool(out["dominant_after"])
    assert not bool(out["dominant_subst_window"])
    assert not bool(out["other_subst_window"])


def test_build_feature_table_substitution_in_window_for_other_team():
    base = pd.DataFrame([_base_row(4, 1, 0, events_fetched=True)])  # home dominant
    subst = pd.DataFrame([{"fixture_id": 4, "team_id": 200, "minute": 68}])  # away (другая), внутри [60,75]

    out = build_feature_table(
        base, pd.DataFrame(columns=["fixture_id", "team_id", "minute"]), subst, cutoff_minute=75
    ).iloc[0]

    assert not bool(out["dominant_subst_window"])
    assert bool(out["other_subst_window"])


def test_build_feature_table_empty_goals_df_does_not_crash():
    base = pd.DataFrame([_base_row(3, 0, 0, events_fetched=True)])
    empty_goals = pd.DataFrame(columns=["fixture_id", "team_id", "minute"])
    empty_subst = pd.DataFrame(columns=["fixture_id", "team_id", "minute"])
    out = build_feature_table(base, empty_goals, empty_subst, cutoff_minute=75).iloc[0]
    assert out["home_goals_cutoff"] == 0
    assert not bool(out["any_after"])


# --------------------------------------------------------------- маска комбинации и подсчёт


def _feature_df(n_matching, n_total, target_col="any_after"):
    rows = []
    for i in range(n_total):
        matches = i < n_matching
        rows.append(
            {
                "fixture_id": i,
                "split": "train",
                "dominant_possession": 80 if matches else 40,
                "dominant_total_shots": 15 if matches else 1,
                "dominant_sog": 10 if matches else 0,
                "situation_diff": 0,
                "dominant_subst_window": True,
                "other_subst_window": False,
                target_col: True,
                ("dominant_after" if target_col == "any_after" else "any_after"): True,
            }
        )
    return pd.DataFrame(rows)


def test_combo_mask_selects_only_matching_rows():
    df = _feature_df(n_matching=3, n_total=10)
    combo = Combo(poss_threshold=55, shots_threshold=7, sog_threshold=2, situation="ничья",
                  dominant_subst_window=True, other_subst_window=False)
    assert combo_mask(df, combo).sum() == 3


def test_evaluate_combo_zero_matches_returns_none_metrics():
    df = _feature_df(n_matching=0, n_total=10)
    combo = Combo(poss_threshold=55, shots_threshold=7, sog_threshold=2, situation="ничья",
                  dominant_subst_window=True, other_subst_window=False)
    result = evaluate_combo(df, combo, "any_team", "train")
    assert result.n == 0
    assert result.share is None
    assert result.ev_yes is None


def test_evaluate_combo_computes_ev_only_for_any_team_target():
    df = _feature_df(n_matching=10, n_total=10)
    combo = Combo(poss_threshold=55, shots_threshold=7, sog_threshold=2, situation="ничья",
                  dominant_subst_window=True, other_subst_window=False)
    any_result = evaluate_combo(df, combo, "any_team", "train")
    dominant_result = evaluate_combo(df, combo, "dominant_team", "train")
    assert any_result.ev_yes is not None and any_result.ev_no is not None
    assert dominant_result.ev_yes is None and dominant_result.ev_no is None
    assert any_result.share == 1.0


# --------------------------------------------------------------- гейтинг (РА-12)


def _combo():
    return Combo(60, 9, 4, "ничья", True, False)


def test_gate_passes_train_and_confirms_on_holdout():
    train = ComboResult(_combo(), "any_team", "train", n=300, successes=220,
                         share=220 / 300, ci_low=0.68, ci_high=0.78, ev_yes=0.1, ev_no=0.1, p_value=0.001)
    holdout = ComboResult(_combo(), "any_team", "holdout", n=200, successes=130,
                           share=0.65, ci_low=0.58, ci_high=0.71, ev_yes=0.1, ev_no=0.1, p_value=0.01)
    gated = apply_gate(train, holdout)
    assert gated.meets_threshold_train is True
    assert gated.meets_threshold_holdout is True
    assert gated.holdout_status == "подтверждено"


def test_gate_fails_train_when_ci_low_below_threshold():
    train = ComboResult(_combo(), "any_team", "train", n=300, successes=205,
                         share=205 / 300, ci_low=0.62, ci_high=0.73, ev_yes=0.0, ev_no=0.0, p_value=0.2)
    gated = apply_gate(train, None)
    assert gated.meets_threshold_train is False
    assert gated.holdout_status == "не применимо"


def test_gate_fails_train_when_n_too_small_even_if_share_high():
    train = ComboResult(_combo(), "any_team", "train", n=50, successes=40,
                         share=0.8, ci_low=0.68, ci_high=0.89, ev_yes=0.3, ev_no=0.3, p_value=0.001)
    gated = apply_gate(train, None)
    assert gated.meets_threshold_train is False  # n < MIN_N_TARGET=300, несмотря на высокую долю


def test_gate_marks_not_tested_when_holdout_sample_too_small():
    train = ComboResult(_combo(), "any_team", "train", n=300, successes=220,
                         share=220 / 300, ci_low=0.68, ci_high=0.78, ev_yes=0.1, ev_no=0.1, p_value=0.001)
    holdout = ComboResult(_combo(), "any_team", "holdout", n=50, successes=35,
                           share=0.7, ci_low=0.56, ci_high=0.81, ev_yes=0.1, ev_no=0.1, p_value=0.01)
    gated = apply_gate(train, holdout)
    assert gated.meets_threshold_train is True
    assert gated.holdout_status == "не проверено"
    assert gated.meets_threshold_holdout is False


def test_gate_marks_not_confirmed_when_holdout_share_drops():
    train = ComboResult(_combo(), "any_team", "train", n=300, successes=220,
                         share=220 / 300, ci_low=0.68, ci_high=0.78, ev_yes=0.1, ev_no=0.1, p_value=0.001)
    holdout = ComboResult(_combo(), "any_team", "holdout", n=200, successes=100,
                           share=0.5, ci_low=0.43, ci_high=0.57, ev_yes=-0.1, ev_no=-0.1, p_value=0.5)
    gated = apply_gate(train, holdout)
    assert gated.holdout_status == "не подтверждено"
    assert gated.meets_threshold_holdout is False


def test_gate_strong_candidate_requires_point_estimate_at_least_070():
    weak = ComboResult(_combo(), "any_team", "train", n=300, successes=205,
                        share=205 / 300, ci_low=0.67, ci_high=0.735, ev_yes=0.1, ev_no=0.1, p_value=0.01)
    gated = apply_gate(weak, None)
    assert gated.meets_threshold_train is True
    assert gated.is_strong_train is False  # share ~0.683 < 0.70
