# Success Betting

Хранилище и ежедневный сбор футбольных данных из API-Football в PostgreSQL.

Проект ведётся по методологии Spec-Driven Development: сначала требования
и спецификации, затем реализация. Правила — в `CLAUDE.md`.

## Документация

| Документ | Содержание |
|---|---|
| [docs/00-контекст-проекта.md](docs/00-контекст-проекта.md) | цель, стек, ограничения |
| [docs/01-требования.md](docs/01-требования.md) | что система обязана делать |
| [docs/02-архитектура.md](docs/02-архитектура.md) | устройство и решения (ADR) |
| [docs/03-модель-данных.md](docs/03-модель-данных.md) | схема базы |
| [docs/04-инфраструктура.md](docs/04-инфраструктура.md) | сервер и развёртывание |
| [docs/06-план-реализации.md](docs/06-план-реализации.md) | этапы работ |

## Установка

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # и указать DATABASE_URL и APIFOOTBALL_KEY
```

## Миграции

```bash
.venv/bin/python -m src.db.migrate status   # состояние
.venv/bin/python -m src.db.migrate up       # применить неприменённые
.venv/bin/python -m src.db.migrate down 001 # откатить одну
```

Схема меняется только миграциями. Файлы — `migrations/NNN_описание.sql`,
откат — `migrations/NNN_описание.down.sql`.

## Тесты

Нужна отдельная база, production не затрагивается:

```bash
createdb football_test
TEST_DATABASE_URL="postgresql:///football_test" .venv/bin/python -m pytest tests -q
```

## Доступ к серверной базе

База слушает только localhost сервера. С ноутбука — через SSH-туннель:

```bash
ssh -f -N -L 5433:127.0.0.1:5433 vac-ru
```

Пароли и ключи хранятся в `.env`, в Git не попадают.
