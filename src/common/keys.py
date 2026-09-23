"""Ключи, общие для переноса и ежедневного сбора.

У событий матча нет собственного идентификатора в API (docs/03), поэтому
ключ собирается из содержимого события. Формула обязана быть одна и та же
в обоих путях записи: разойдись они — и каждое событие задвоится при первом
же пересечении данных. Ради этого функция вынесена в отдельный модуль
и покрыта тестом на одинаковых данных из обоих источников.
"""

from __future__ import annotations

SEPARATOR = "|"


def _part(value: object) -> str:
    """Приводит значение к части ключа.

    Пустое значение (None, NaN, пустая строка) даёт пустую часть, число
    с нулевой дробью — целое: в parquet идентификаторы хранятся как float
    (`867.0`), а в ответах API — как int (`867`). Без этого приведения
    ключи из двух источников не совпали бы.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            return str(int(value))
        return str(value)
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "<na>"} else text


def event_key(
    fixture_id: object,
    minute: object,
    minute_extra: object,
    team_id: object,
    event_type: object,
    detail: object,
    player_id: object,
) -> str:
    """Собирает ключ события для защиты от дублей (ФТ-3)."""
    return SEPARATOR.join(
        _part(v)
        for v in (
            fixture_id,
            minute,
            minute_extra,
            team_id,
            event_type,
            detail,
            player_id,
        )
    )
