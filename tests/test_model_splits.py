"""Тесты временного сплита (specs/модель-предсказаний.md, «Временной сплит»)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.model.splits import time_split


def _df(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"match_date": pd.to_datetime(dates), "value": range(len(dates))})


def test_split_has_no_overlap_across_boundaries():
    df = _df(["2024-06-01", "2024-12-31", "2025-01-01", "2025-12-31", "2026-01-01", "2026-06-01"])
    splits = time_split(df, val_start=date(2025, 1, 1), test_start=date(2026, 1, 1))

    assert splits["train"]["match_date"].max() < pd.Timestamp("2025-01-01")
    assert splits["val"]["match_date"].min() >= pd.Timestamp("2025-01-01")
    assert splits["val"]["match_date"].max() < pd.Timestamp("2026-01-01")
    assert splits["test"]["match_date"].min() >= pd.Timestamp("2026-01-01")


def test_split_covers_every_row_exactly_once():
    df = _df(["2023-01-01", "2024-12-31", "2025-06-15", "2026-03-01", "2027-01-01"])
    splits = time_split(df)

    total = sum(len(part) for part in splits.values())
    assert total == len(df)


def test_boundary_dates_go_to_correct_side():
    """Границы включительно слева: val_start попадает в val, test_start — в test."""
    df = _df(["2024-12-31", "2025-01-01", "2025-12-31", "2026-01-01"])
    splits = time_split(df, val_start=date(2025, 1, 1), test_start=date(2026, 1, 1))

    assert list(splits["train"]["match_date"]) == [pd.Timestamp("2024-12-31")]
    assert list(splits["val"]["match_date"]) == [pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")]
    assert list(splits["test"]["match_date"]) == [pd.Timestamp("2026-01-01")]


def test_val_start_must_be_before_test_start():
    df = _df(["2025-01-01"])
    with pytest.raises(ValueError):
        time_split(df, val_start=date(2026, 1, 1), test_start=date(2025, 1, 1))


def test_empty_dataframe_does_not_crash():
    df = _df([])
    splits = time_split(df)
    assert all(part.empty for part in splits.values())
