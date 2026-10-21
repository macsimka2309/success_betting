"""Статистические примитивы для разведочного перебора признаков.

specs/разведочный-анализ-поздних-голов.md: РА-7 (доверительный интервал),
РА-9 (поправка на множественные сравнения). Чистые функции без обращения
к БД — проверяются на синтетических числах, не на матчах.
"""

from __future__ import annotations

import math


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """95%-доверительный интервал Уилсона для доли `successes / n`.

    Устойчивее нормального приближения на малых `n` и долях у краёв (0 или 1):
    никогда не выходит за пределы [0, 1], в отличие от `p ± z*se`.

    `n = 0` не имеет смысла как доля — возвращает (0.0, 1.0), самый
    неинформативный интервал, а не деление на ноль.
    """
    if n <= 0:
        return 0.0, 1.0
    z = _z_score(confidence)
    p = successes / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    low = (centre - margin) / denom
    high = (centre + margin) / denom
    return max(0.0, low), min(1.0, high)


def _z_score(confidence: float) -> float:
    """Квантиль стандартного нормального распределения для двустороннего ДИ.

    Таблица — не аппроксимация: избегает зависимости от scipy ради
    нескольких используемых в проекте уровней доверия (ДП-правило проекта —
    без scipy, см. анализ Эло/Пуассона в переписке чата).
    """
    table = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}
    try:
        return table[round(confidence, 2)]
    except KeyError as exc:
        raise ValueError(f"нет таблицы для уровня доверия {confidence}; добавьте значение") from exc


def two_sided_p_value(successes: int, n: int, p0: float) -> float:
    """Двусторонний p-value для H0: истинная доля = `p0`, через нормальное
    приближение к биномиальному распределению (с поправкой на непрерывность).

    `n = 0` — p-value не определён, возвращается 1.0 (нет оснований отвергать
    H0 при отсутствии данных).
    """
    if n <= 0:
        return 1.0
    if p0 <= 0 or p0 >= 1:
        raise ValueError("p0 должно быть строго между 0 и 1")
    p_hat = successes / n
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0:
        return 1.0
    continuity = 1 / (2 * n)
    diff = abs(p_hat - p0) - continuity
    z = max(diff, 0.0) / se
    return 2 * (1 - _standard_normal_cdf(z))


def _standard_normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> tuple[list[float], list[bool]]:
    """Поправка Беньямини-Хохберга (контроль FDR) — РА-9.

    Возвращает (скорректированные p-value той же длины и порядка, что вход;
    флаги отклонения H0 на уровне `alpha`).

    Скорректированное значение — минимум по убывающей последовательности
    `p[k] * m / rank`, монотонизированный (классическая реализация BH),
    так что скорректированные значения не убывают при увеличении исходного.
    """
    m = len(p_values)
    if m == 0:
        return [], []
    indexed = sorted(range(m), key=lambda i: p_values[i])
    corrected = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        idx = indexed[rank - 1]
        value = p_values[idx] * m / rank
        running_min = min(running_min, value)
        corrected[idx] = min(running_min, 1.0)
    rejected = [corrected[i] <= alpha for i in range(m)]
    return corrected, rejected
