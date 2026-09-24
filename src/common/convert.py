"""Преобразование значений источника в типы базы.

Используется и переносом из parquet (`src/jobs/import_parquet.py`), и
ежедневным сбором (`src/jobs/collect.py`) — одна функция на одно правило,
чтобы поведение не разошлось в двух местах (спецификации требуют этого
явно: ПР-6 и «Шаг 6» в specs/ежедневный-сбор.md).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    text = str(value).strip().lower()
    return text in {"", "nan", "none", "<na>", "nat"}


def to_int(value: Any) -> int | None:
    if is_missing(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_percent(value: Any) -> int | None:
    """`"58%"` -> 58. Нечисловое значение даёт пустое поле (ПР-6)."""
    if is_missing(value):
        return None
    try:
        return int(float(str(value).strip().rstrip("%")))
    except (TypeError, ValueError):
        return None


def to_decimal(value: Any) -> float | None:
    if is_missing(value):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_utc(value: Any) -> datetime | None:
    """ISO-строка со смещением -> время в UTC (ФТ-2)."""
    if is_missing(value):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def to_date(value: Any) -> date | None:
    if is_missing(value):
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def to_text(value: Any) -> str | None:
    return None if is_missing(value) else str(value).strip()
