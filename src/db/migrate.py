"""Применение миграций (ADR-7).

Миграции — файлы `migrations/NNN_описание.sql`, применяются по возрастанию
номера. Применённые отмечаются в таблице `schema_migrations`, повторно
не выполняются. Каждый файл идёт в отдельной транзакции: либо применяется
целиком, либо не применяется вовсе.

Откат: `migrations/NNN_описание.down.sql`, выполняется по явной команде.

Использование:
    python3 -m src.db.migrate status     показать состояние
    python3 -m src.db.migrate up         применить неприменённые
    python3 -m src.db.migrate down NNN   откатить одну миграцию
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from src.db.connection import PROJECT_ROOT, connect

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
VERSION_PATTERN = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

CREATE_REGISTRY = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT        PRIMARY KEY,
    name       TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path

    @property
    def down_path(self) -> Path:
        return self.path.with_name(f"{self.version}_{self.name}.down.sql")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Находит миграции и проверяет, что номера не повторяются."""
    found: dict[str, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        match = VERSION_PATTERN.match(path.name)
        if not match:
            raise ValueError(
                f"Файл {path.name} не соответствует шаблону NNN_описание.sql"
            )
        version, name = match.groups()
        if version in found:
            raise ValueError(
                f"Номер миграции {version} занят файлом {found[version].path.name}"
            )
        found[version] = Migration(version=version, name=name, path=path)
    return [found[v] for v in sorted(found)]


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(CREATE_REGISTRY)
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply(conn: psycopg.Connection, migration: Migration) -> None:
    """Применяет одну миграцию в собственной транзакции."""
    sql = migration.path.read_text(encoding="utf-8")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )


def revert(conn: psycopg.Connection, migration: Migration) -> None:
    """Откатывает одну миграцию обратным файлом."""
    if not migration.down_path.exists():
        raise FileNotFoundError(
            f"Нет файла отката {migration.down_path.name}; откат невозможен"
        )
    sql = migration.down_path.read_text(encoding="utf-8")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "DELETE FROM schema_migrations WHERE version = %s",
                (migration.version,),
            )


def run_up(conn: psycopg.Connection) -> list[Migration]:
    done = applied_versions(conn)
    pending = [m for m in discover() if m.version not in done]
    for migration in pending:
        print(f"применяю {migration.version}_{migration.name}")
        apply(conn, migration)
    if not pending:
        print("новых миграций нет")
    return pending


def run_down(conn: psycopg.Connection, version: str) -> None:
    done = applied_versions(conn)
    if version not in done:
        print(f"миграция {version} не применена, откатывать нечего")
        return
    target = next((m for m in discover() if m.version == version), None)
    if target is None:
        raise FileNotFoundError(f"Миграция {version} не найдена в {MIGRATIONS_DIR}")
    print(f"откатываю {target.version}_{target.name}")
    revert(conn, target)


def run_status(conn: psycopg.Connection) -> None:
    done = applied_versions(conn)
    migrations = discover()
    if not migrations:
        print("миграций не найдено")
        return
    for migration in migrations:
        mark = "применена" if migration.version in done else "ожидает"
        print(f"{migration.version}  {migration.name:<28} {mark}")
    unknown = done - {m.version for m in migrations}
    for version in sorted(unknown):
        print(f"{version}  {'(файла нет)':<28} применена в базе")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Применение миграций")
    parser.add_argument("command", choices=["up", "down", "status"])
    parser.add_argument("version", nargs="?", help="номер миграции для отката")
    args = parser.parse_args(argv)

    if args.command == "down" and not args.version:
        parser.error("для отката укажите номер миграции, например: down 001")

    with connect() as conn:
        if args.command == "up":
            run_up(conn)
        elif args.command == "down":
            run_down(conn, args.version)
        else:
            run_status(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
