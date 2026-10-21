"""Тесты статистических примитивов (src/common/stats.py).

Чистые функции, без БД — specs/разведочный-анализ-поздних-голов.md, РА-7/РА-9.
"""

from __future__ import annotations

import pytest

from src.common.stats import benjamini_hochberg, two_sided_p_value, wilson_interval


def test_wilson_interval_half_of_ten_matches_known_reference():
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-3)
    assert high == pytest.approx(0.7634, abs=1e-3)


def test_wilson_interval_all_successes_is_bounded_by_one():
    low, high = wilson_interval(10, 10)
    assert high == pytest.approx(1.0, abs=1e-9)
    assert low == pytest.approx(0.7226, abs=1e-3)
    assert 0.0 <= low <= high <= 1.0


def test_wilson_interval_zero_n_is_maximally_uninformative():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_never_escapes_unit_interval():
    for successes, n in [(0, 5), (5, 5), (1, 1000), (999, 1000)]:
        low, high = wilson_interval(successes, n)
        assert 0.0 <= low <= high <= 1.0


def test_wilson_interval_narrows_with_larger_n_at_same_share():
    low_small, high_small = wilson_interval(5, 10)
    low_big, high_big = wilson_interval(500, 1000)
    assert (high_big - low_big) < (high_small - low_small)


def test_two_sided_p_value_large_deviation_is_significant():
    # 900 успехов из 1000 при H0 p0=0.5 — явно значимо
    assert two_sided_p_value(900, 1000, 0.5) < 0.001


def test_two_sided_p_value_matches_null_is_not_significant():
    assert two_sided_p_value(500, 1000, 0.5) > 0.5


def test_two_sided_p_value_zero_n_returns_one():
    assert two_sided_p_value(0, 0, 0.5) == 1.0


def test_two_sided_p_value_rejects_invalid_p0():
    with pytest.raises(ValueError):
        two_sided_p_value(1, 10, 0.0)
    with pytest.raises(ValueError):
        two_sided_p_value(1, 10, 1.0)


def test_benjamini_hochberg_empty_input():
    assert benjamini_hochberg([]) == ([], [])


def test_benjamini_hochberg_corrected_values_are_monotonic_with_rank():
    # три p-value по возрастанию -> скорректированные не убывают при увеличении исходного
    corrected, _ = benjamini_hochberg([0.001, 0.02, 0.5])
    assert corrected[0] <= corrected[1] <= corrected[2]


def test_benjamini_hochberg_rejects_fewer_or_equal_than_bonferroni():
    # BH менее консервативен, чем Бонферрони: отвергает не меньше гипотез
    p_values = [0.001, 0.004, 0.02, 0.04, 0.9]
    _, bh_rejected = benjamini_hochberg(p_values, alpha=0.05)
    bonferroni_rejected = [p <= 0.05 / len(p_values) for p in p_values]
    assert sum(bh_rejected) >= sum(bonferroni_rejected)


def test_benjamini_hochberg_all_null_rarely_rejects_many():
    # на равномерно распределённом "шуме" BH должен отвергать около alpha*m гипотез, не все
    import random

    random.seed(0)
    p_values = [random.random() for _ in range(200)]
    _, rejected = benjamini_hochberg(p_values, alpha=0.05)
    assert sum(rejected) < 40  # существенно меньше, чем "почти все"
