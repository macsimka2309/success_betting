"""Тесты инференса и отбора ставок (specs/модель-предсказаний.md,
«Инференс и отбор ставок»). Синтетика, без обращения к БД (НФТ-8)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.model import predict as mp


class FakeMultiClass:
    classes_ = np.array(["A", "D", "H"])

    def predict_proba(self, X):
        return np.tile([0.2, 0.3, 0.5], (len(X), 1))


class FakeBinary:
    def predict_proba(self, X):
        return np.tile([0.4, 0.6], (len(X), 1))


def _features(line):
    row = {c: 0.0 for c in mp.FEATURE_COLUMNS}
    row.update({"fixture_id": 1, "match_date": date(2026, 10, 4), "league_total_line": line,
                "home_team": "Alpha", "away_team": "Beta",
                "kickoff_at": datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc), "status_short": "NS"})
    return pd.DataFrame([row])


def _predictions(line=2.5):
    return mp.predict_predictions(_features(line), FakeMultiClass(), FakeBinary())


def _odds(rows):
    return pd.DataFrame(rows, columns=["fixture_id", "bet_type", "value", "odd"])


def test_predict_maps_classes_and_totals_to_outcome_columns():
    p = _predictions()
    assert p.loc[0, "p_away"] == pytest.approx(0.2)
    assert p.loc[0, "p_draw"] == pytest.approx(0.3)
    assert p.loc[0, "p_home"] == pytest.approx(0.5)
    assert p.loc[0, "p_over"] == pytest.approx(0.6)
    assert p.loc[0, "p_under"] == pytest.approx(0.4)


def test_parse_line_extracts_side_and_value():
    assert mp._parse_line("Over 2.5") == ("Over", 2.5)
    assert mp._parse_line("Under 3") == ("Under", 3.0)
    assert mp._parse_line("Home") is None


def test_odds_range_bounds_are_inclusive():
    odds = _odds([
        (1, "Match Winner", "Home", 1.5),
        (1, "Match Winner", "Draw", 2.5),
        (1, "Match Winner", "Away", 1.49),
        (1, "Match Winner", "Away", 2.51),
    ])
    bets = mp.build_bets(_predictions(), odds)
    assert set(bets["odds"]) == {1.5, 2.5}


def test_ev_is_probability_times_odds_minus_one():
    odds = _odds([(1, "Match Winner", "Home", 2.0)])
    bets = mp.build_bets(_predictions(), odds)
    assert bets.loc[0, "ev"] == pytest.approx(0.5 * 2.0 - 1.0)
    assert bets.loc[0, "market"] == "1X2"
    assert bets.loc[0, "outcome"] == "H"


def test_kickoff_at_and_status_carried_into_predictions_and_bets():
    """ФТ-1а (specs/проверка-ставок-новостями.md): соседний чат читает время
    и статус матча из наших файлов, а не отдельным запросом."""
    p = _predictions()
    assert p.loc[0, "kickoff_at"] == datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
    assert p.loc[0, "status_short"] == "NS"

    odds = _odds([(1, "Match Winner", "Home", 2.0)])
    bets = mp.build_bets(p, odds)
    assert bets.loc[0, "kickoff_at"] == datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
    assert bets.loc[0, "status_short"] == "NS"


def test_total_uses_only_exact_line_match():
    odds = _odds([
        (1, "Goals Over/Under", "Over 2.5", 2.0),
        (1, "Goals Over/Under", "Under 2.25", 2.0),
    ])
    bets = mp.build_bets(_predictions(line=2.5), odds)
    assert list(bets["outcome"]) == ["Over 2.5"]
    assert bets.loc[0, "model_prob"] == pytest.approx(0.6)


def test_total_skipped_when_match_has_no_line():
    odds = _odds([(1, "Goals Over/Under", "Over 2.5", 2.0)])
    bets = mp.build_bets(_predictions(line=None), odds)
    assert bets.empty


def test_bets_sorted_by_ev_descending():
    odds = _odds([
        (1, "Match Winner", "Home", 2.0),  # EV 0.5*2-1 = 0.0
        (1, "Match Winner", "Away", 1.6),  # EV 0.2*1.6-1 = -0.68
        (1, "Goals Over/Under", "Over 2.5", 2.4),  # EV 0.6*2.4-1 = 0.44
    ])
    bets = mp.build_bets(_predictions(), odds)
    assert list(bets["outcome"]) == ["Over 2.5", "H", "A"]
    assert list(bets["ev"]) == sorted(bets["ev"], reverse=True)
