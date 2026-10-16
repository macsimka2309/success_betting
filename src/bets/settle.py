"""Расчёт результата ставки на тотал «больше» (specs/реестр-ставок.md, РС-3…РС-5)."""

from __future__ import annotations

from decimal import Decimal

MARKETS = ("TB", "ITB1", "ITB2")
QUARTER = Decimal("0.25")
CENT = Decimal("0.001")


def compared_goals(market: str, goals_home: int, goals_away: int) -> int:
    """ТБ — голы обеих команд, ИТБ1 — хозяев, ИТБ2 — гостей."""
    if market == "TB":
        return goals_home + goals_away
    if market == "ITB1":
        return goals_home
    if market == "ITB2":
        return goals_away
    raise ValueError(f"неизвестный рынок: {market}")


def _half_result(value: int, line: Decimal) -> int:
    """1 — выигрыш, 0 — возврат, -1 — проигрыш для целой или половинной линии."""
    if value > line:
        return 1
    if value == line:
        return 0
    return -1


def settle(market: str, line: Decimal, goals_home: int, goals_away: int) -> str:
    """Статус: won, half_won, push, half_lost, lost."""
    if line % QUARTER != 0:
        raise ValueError(f"линия должна быть кратна 0.25: {line}")
    value = compared_goals(market, goals_home, goals_away)
    if line % Decimal("0.5") == 0:
        return {1: "won", 0: "push", -1: "lost"}[_half_result(value, line)]
    total = _half_result(value, line - QUARTER) + _half_result(value, line + QUARTER)
    return {2: "won", 1: "half_won", -1: "half_lost", -2: "lost"}[total]


def profit(status: str, stake: Decimal, odds: Decimal) -> Decimal:
    """Прибыль в тех же единицах, что сумма ставки (РС-5)."""
    gain = stake * (odds - 1)
    result = {
        "won": gain,
        "half_won": gain / 2,
        "push": Decimal(0),
        "void": Decimal(0),
        "half_lost": -stake / 2,
        "lost": -stake,
    }[status]
    return result.quantize(CENT)
