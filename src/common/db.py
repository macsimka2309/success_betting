"""Запись в базу с защитой от дублей.

Общая для переноса из parquet и ежедневного сбора: один и тот же приём
`INSERT ... ON CONFLICT DO UPDATE` (ФТ-3), чтобы правило не разошлось
в двух местах.
"""

from __future__ import annotations

from typing import Iterable

import psycopg

BATCH_SIZE = 5_000


def write_batch(
    conn: psycopg.Connection,
    table: str,
    columns: Iterable[str],
    rows: list[tuple],
    conflict_key: str,
    update_columns: Iterable[str] | None = None,
) -> int:
    """Пишет пакет одной транзакцией с ON CONFLICT DO UPDATE.

    `conflict_key` — имена колонок конфликта через запятую, например
    "fixture_id, team_id". Колонки из конфликта автоматически исключаются
    из SET, обновлять остальные.
    """
    if not rows:
        return 0
    cols = list(columns)
    placeholders = ", ".join(["%s"] * len(cols))
    key_cols = {c.strip() for c in conflict_key.split(",")}
    updates = [c for c in (update_columns if update_columns is not None else cols) if c not in key_cols]
    action = (
        "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
        if updates
        else "DO NOTHING"
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_key}) {action}"
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
    return len(rows)


def write_row(
    conn: psycopg.Connection,
    table: str,
    columns: Iterable[str],
    row: tuple,
    conflict_key: str,
    update_columns: Iterable[str] | None = None,
) -> None:
    """Пишет одну строку — та же логика, что write_batch, для точечной записи."""
    write_batch(conn, table, columns, [row], conflict_key, update_columns)
