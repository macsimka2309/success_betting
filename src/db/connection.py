"""Подключение к PostgreSQL.

Строка подключения берётся из переменной окружения DATABASE_URL либо
из файла .env в корне проекта. Секреты в коде не хранятся (НФТ-3).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Читает .env и добавляет значения в окружение.

    Уже заданные переменные окружения не перезаписываются: явный экспорт
    важнее файла.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def database_url() -> str:
    """Возвращает строку подключения или объясняет, чего не хватает."""
    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Не задана переменная окружения DATABASE_URL. "
            "Скопируйте .env.example в .env и укажите строку подключения."
        )
    return url


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Открывает соединение и закрывает его при выходе из блока.

    Режим autocommit включён намеренно. Без него первый же читающий запрос
    открывает неявную транзакцию, и все последующие блоки `conn.transaction()`
    становятся вложенными: они не фиксируют данные, а лишь ставят точки
    сохранения. Пакетная запись тогда копится в одной огромной транзакции
    и целиком пропадает при обрыве — перенос перестаёт быть возобновляемым
    (ПР-8), а база держит растущую блокировку.

    С autocommit каждый блок `conn.transaction()` — настоящая транзакция,
    которая фиксируется на выходе из блока. Запросы вне таких блоков
    выполняются сами по себе, что для чтения и нужно.
    """
    with psycopg.connect(url or database_url(), autocommit=True) as conn:
        yield conn
