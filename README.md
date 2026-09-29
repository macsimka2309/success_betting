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

## Ежедневный сбор

```bash
export APIFOOTBALL_KEY=...
python3 -m src.jobs.collect --job catalog     # раз в неделю
python3 -m src.jobs.collect --job odds        # дважды в сутки, 00:00 и 12:00 UTC
python3 -m src.jobs.collect --job daily       # матчи, травмы, коэффициенты, события, статистика, составы
```

Отдельные шаги (`fixtures`, `injuries`, `events`, `statistics`, `lineups`) —
для ручного запуска и разбора проблем. `--max-requests` ограничивает бюджет
разового запуска, `--cache-dir` задаёт каталог сырых ответов API.

## Препроцессинг для модели

```bash
python3 -m src.jobs.build_features                 # полный пересчёт
python3 -m src.jobs.build_features --incremental    # только новые матчи
```

Строит `ml_match_features` — одна строка на завершённый матч, цели и
признаки по семи рынкам (specs/препроцессинг-для-модели.md). К API не
обращается, только читает сырые таблицы и пишет свои. На 40 000 матчей
занимает ~75 секунд; на полной базе (~1,1 млн) — по порядку 20–30 минут.

## Тесты

Тесты клиента API-Football и сбора обращений к сети не делают (подменённый
транспорт). Тесты миграций и переноса нужна отдельная база, production не
затрагивается:

```bash
createdb football_test
TEST_DATABASE_URL="postgresql:///football_test" .venv/bin/python -m pytest tests -q
```

Без `TEST_DATABASE_URL` тесты, зависящие от базы, пропускаются, а не падают.

## Доступ к серверной базе

База слушает только localhost сервера. С ноутбука — через SSH-туннель:

```bash
ssh -f -N -L 5433:127.0.0.1:5433 vac-ru
```

Пароли и ключи хранятся в `.env`, в Git не попадают.
