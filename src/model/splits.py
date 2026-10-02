"""Временной train/val/test сплит для модели предсказаний.

Спецификация: specs/модель-предсказаний.md, «Временной сплит».

Матчи упорядочены во времени — случайное перемешивание (k-fold) дало бы
оптимистичную оценку качества, даже при отсутствии утечки в самих признаках
(ДП-1 уже гарантирует это на уровне построения `ml_match_features`):
модель оценивалась бы на матчах вперемешку с теми, что видела при обучении
по времени, а не как при реальном применении — только вперёд.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

# specs/модель-предсказаний.md, «Принятые решения»: train <2025, val 2025,
# test 2026 (текущий, неполный год — соответствует реальному сценарию
# применения: модель предсказывает вперёд, не ретроспективно).
DEFAULT_VAL_START = date(2025, 1, 1)
DEFAULT_TEST_START = date(2026, 1, 1)


def time_split(
    df: pd.DataFrame,
    val_start: date = DEFAULT_VAL_START,
    test_start: date = DEFAULT_TEST_START,
    date_column: str = "match_date",
) -> dict[str, pd.DataFrame]:
    """Делит по `date_column` на train/val/test без пересечений по времени.

    `train`: date < val_start. `val`: val_start <= date < test_start.
    `test`: date >= test_start. Строки с `date_column` вне этих трёх
    диапазонов не существует по построению — границы покрывают всё.
    """
    if val_start >= test_start:
        raise ValueError(f"val_start ({val_start}) должен быть раньше test_start ({test_start})")
    dates = pd.to_datetime(df[date_column]).dt.date
    return {
        "train": df[dates < val_start],
        "val": df[(dates >= val_start) & (dates < test_start)],
        "test": df[dates >= test_start],
    }
