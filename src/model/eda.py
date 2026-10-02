"""Разведочный анализ данных для модели предсказаний (ДП-подготовка).

Спецификация: specs/модель-предсказаний.md, «EDA (разведочный анализ)».

Только чтение `ml_match_features` — к API не обращается, ничего не пишет.
Печатает отчёт по train/val/test (specs/модель-предсказаний.md, «Временной
сплит»): объём и диапазон дат, баланс целей, покрытие признаков по группам,
покрытие статистики по месяцам — проверка того, что тонкое покрытие (ДП-4)
не специфично для test (иначе оценка качества была бы искажена не
качеством модели, а сдвигом полноты данных между выборками).

Использование:
    python3 -m src.model.eda
"""

from __future__ import annotations

import pandas as pd
import psycopg

from src.common.feature_columns import (
    elo_columns,
    form_columns,
    h2h_columns,
    injury_columns,
    league_context_columns,
    stat_columns,
)
from src.db.connection import connect, database_url
from src.model.features import TARGET_COLUMNS
from src.model.splits import time_split

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "форма": tuple(name for name, _ in form_columns()),
    "статистика": tuple(name for name, _ in stat_columns()),
    "личные встречи": tuple(name for name, _ in h2h_columns()),
    "травмы": tuple(name for name, _ in injury_columns()),
    "контекст лиги": tuple(name for name, _ in league_context_columns()),
    "Эло": tuple(name for name, _ in elo_columns()),
}


def load_training_rows(conn: psycopg.Connection) -> pd.DataFrame:
    """Все матчи с известным результатом (цель есть — значит, матч сыгран)."""
    columns = ["match_date"] + list(TARGET_COLUMNS.values())
    for group_columns in FEATURE_GROUPS.values():
        columns.extend(group_columns)
    sql = f"SELECT {', '.join(columns)} FROM ml_match_features WHERE reg_home IS NOT NULL"
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def report_split_sizes(splits: dict[str, pd.DataFrame]) -> None:
    print("--- объём и диапазон дат ---")
    for name, part in splits.items():
        if part.empty:
            print(f"{name}: 0 строк")
            continue
        print(f"{name}: {len(part)} строк, {part['match_date'].min()} .. {part['match_date'].max()}")


def report_target_balance(splits: dict[str, pd.DataFrame]) -> None:
    print("\n--- баланс целей ---")
    for target_name, column in TARGET_COLUMNS.items():
        print(f"\n{target_name} ({column}):")
        for name, part in splits.items():
            if part.empty:
                continue
            counts = part[column].value_counts(dropna=False, normalize=True).round(3)
            print(f"  {name}: {counts.to_dict()}")


def report_feature_coverage(splits: dict[str, pd.DataFrame]) -> None:
    """Доля NULL по группе признаков в каждой выборке — показывает, не
    специфичны ли пропуски для train/val/test (иначе оценка искажена)."""
    print("\n--- покрытие признаков по группам (доля НЕ-NULL) ---")
    for group_name, columns in FEATURE_GROUPS.items():
        row = {}
        for name, part in splits.items():
            if part.empty:
                row[name] = None
                continue
            row[name] = round(part[list(columns)].notna().mean().mean(), 3)
        print(f"  {group_name}: {row}")


def report_statistics_coverage_by_month(conn: psycopg.Connection) -> None:
    """ДП-4: подтверждение, что тонкое покрытие статистики — свойство
    данных, не только старой истории (specs/модель-предсказаний.md,
    «Ограничение данных»)."""
    sql = """
        SELECT date_trunc('month', match_date)::date AS m,
               round(avg(home_team_overall_short_stats_coverage)::numeric, 3) AS coverage
        FROM ml_match_features
        WHERE reg_home IS NOT NULL
        GROUP BY 1 ORDER BY 1 DESC LIMIT 12
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    print("\n--- покрытие статистики по месяцам (последние 12) ---")
    for month, coverage in rows:
        print(f"  {month}: {coverage}")


def main() -> int:
    url = database_url()
    with connect(url) as conn:
        df = load_training_rows(conn)
    splits = time_split(df)
    report_split_sizes(splits)
    report_target_balance(splits)
    report_feature_coverage(splits)
    # Отдельное, свежее соединение — после тяжёлой выгрузки (~980 тыс. строк)
    # долгоживущее соединение через SSH-туннель уже может быть разорвано сетью
    # (та же причина, что в build_features.py: "server closed the connection
    # unexpectedly"), а для этого легкого агрегатного запроса это не нужно.
    with connect(url) as conn:
        report_statistics_coverage_by_month(conn)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
