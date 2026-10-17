"""Тесты расчёта ставок на тотал (specs/реестр-ставок.md, РС-3…РС-5). База не нужна."""

from decimal import Decimal as D

import pytest

from src.bets.settle import compared_goals, profit, settle


@pytest.mark.parametrize(
    "market,line,home,away,expected",
    [
        ("TB", "2.5", 2, 1, "won"), ("TB", "2.5", 1, 1, "lost"),
        ("TB", "2.0", 1, 1, "push"), ("TB", "2.0", 2, 1, "won"), ("TB", "2.0", 0, 1, "lost"),
        ("TB", "2.25", 2, 1, "won"), ("TB", "2.25", 1, 1, "half_lost"), ("TB", "2.25", 1, 0, "lost"),
        ("TB", "2.75", 3, 1, "won"), ("TB", "2.75", 2, 1, "half_won"), ("TB", "2.75", 1, 1, "lost"),
        ("ITB1", "1.5", 2, 0, "won"), ("ITB1", "1.5", 1, 5, "lost"),
        ("ITB2", "0.5", 2, 0, "lost"), ("ITB2", "0.5", 0, 1, "won"),
        ("ITB2", "0.75", 0, 1, "half_won"), ("ITB1", "0.25", 0, 3, "half_lost"),
        ("ITB1", "0.25", 1, 0, "won"),
        ("TB", "20", 0, 0, "lost"),
        ("TM", "2.5", 1, 1, "won"), ("TM", "2.5", 2, 1, "lost"),
        ("TM", "2.0", 1, 1, "push"), ("TM", "2.0", 0, 1, "won"), ("TM", "2.0", 2, 1, "lost"),
        ("TM", "2.25", 1, 0, "won"), ("TM", "2.25", 1, 1, "half_won"), ("TM", "2.25", 2, 1, "lost"),
        ("TM", "2.75", 1, 1, "won"), ("TM", "2.75", 2, 1, "half_lost"), ("TM", "2.75", 2, 2, "lost"),
        ("TM", "0.5", 0, 0, "won"), ("TM", "0.5", 1, 0, "lost"),
    ],
)
def test_settle_table(market, line, home, away, expected):
    assert settle(market, D(line), home, away) == expected


def test_compared_goals():
    assert compared_goals("TB", 2, 3) == 5
    assert compared_goals("TM", 2, 3) == 5
    assert compared_goals("ITB1", 2, 3) == 2
    assert compared_goals("ITB2", 2, 3) == 3


@pytest.mark.parametrize("line", ["2.3", "2.1", "2.6"])
def test_line_must_be_multiple_of_quarter(line):
    with pytest.raises(ValueError):
        settle("TB", D(line), 1, 1)


def test_unknown_market_rejected():
    with pytest.raises(ValueError):
        settle("BTTS", D("2.5"), 1, 1)


@pytest.mark.parametrize(
    "status,expected",
    [("won", "90.000"), ("half_won", "45.000"), ("push", "0.000"), ("void", "0.000"),
     ("half_lost", "-50.000"), ("lost", "-100.000")],
)
def test_profit(status, expected):
    assert profit(status, D("100"), D("1.9")) == D(expected)


def test_profit_rounds_to_thousandths():
    assert profit("half_won", D("10"), D("1.333")) == D("1.665")


@pytest.mark.parametrize("line", ["0.5", "1.0", "1.5", "2.0", "2.25", "2.5", "2.75", "3.5"])
@pytest.mark.parametrize("total", range(0, 7))
def test_tm_is_mirror_of_tb(line, total):
    """На любом счёте ТБ и ТМ дают зеркальный результат, возврат остаётся возвратом."""
    mirror = {"won": "lost", "lost": "won", "half_won": "half_lost", "half_lost": "half_won", "push": "push"}
    assert settle("TM", D(line), total, 0) == mirror[settle("TB", D(line), total, 0)]
