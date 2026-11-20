# Существующие интерфейсы

## Статус документа

Baseline 18.11.2026 описывает интерфейсы локального кода, не новый API-дизайн.
Собственный HTTP-сервер/OpenAPI не найден. Публичные интерфейсы здесь — CLI,
файлы обмена, SQL и Telegram. Команды ниже перечислены для идентификации;
они не выполнялись при документировании. Многие из них пишут данные.
Секреты задаются окружением, их значения не приводятся.

## Внешние системы

| Система | Используемый контракт | Реализация |
|---|---|---|
| API-Football | GET к `https://v3.football.api-sports.io`; ключ в x-apisports-key; JSON response/errors/paging | [ApiClient](../src/api/client.py) |
| API-Basketball | Тот же клиент с другим base_url; `/games`, статистика команд/игроков, лиги и odds | [basketball.py](../src/jobs/basketball.py) |
| Telegram | getUpdates, sendMessage, answerCallbackQuery; curl, опциональный SOCKS-прокси | [telegram_api.py](../src/bot/telegram_api.py) |
| Fonbet live | Отображаемые страницы `fon.bet/live/football/...`, DOM статистики второго тайма | [collect.cjs](../tools/fonbet_live/collect.cjs), [dom.cjs](../tools/fonbet_live/dom.cjs) |
| Fonbet history | Отображаемая история `/account/history/bets`, авторизованный persistent browser | [export.cjs](../tools/fonbet_sync/export.cjs) |

API-Football endpoints в сборе: `/leagues`, `/fixtures`, `/fixtures/events`,
`/fixtures/statistics` (включая half=true), `/fixtures/lineups`, `/injuries`,
`/odds`, `/odds/live`. Параметры включают league/season/date/fixture/ids/live/page
в зависимости от вызова. Это описание потребляемого подмножества, не полная
спецификация провайдера и не гарантия его текущего поведения.

`ApiClient.get` возвращает dict либо None; непустой errors может вернуться в dict.
HTTP 429 и rateLimit в теле имеют повторы; суточный лимит останавливает сессию.
`paged` возвращает накопленную часть при недоступной следующей странице, без
отдельного признака полноты. `run_live_odds` использует собственный строгий цикл
страниц и ошибку при неполном ответе. Общий бюджет не разделяется между процессами.

## Python CLI

Обычная форма запуска — `python -m <модуль>`. Соединение обычно использует
DATABASE_URL через [connection.py](../src/db/connection.py), который при вызове
database_url может читать `.env`. Порядок загрузки окружения зависит от entrypoint;
штатные wrappers заранее передают окружение. Диспетчер получает его непосредственно
из переменных и сам `.env` не открывает.

| Модуль | Основные аргументы | Результат и эффекты |
|---|---|---|
| src.jobs.collect | `--job`, `--max-requests`, `--fresh-days`, `--backfill-since`, `--odds-horizon-days`, `--no-backfill`, `--build-upcoming`, `--cache-dir` | API, кэш, БД, журнал; режимы ниже |
| src.jobs.import_parquet | `--source`, `--only`, `--limit`, `--dry-run`, `--verify` | Импорт или сверка; отчёт, verify возвращает 2 при расхождении |
| src.jobs.build_features | `--upcoming`, `--horizon-days`, `--incremental` | Запись витрины; без флагов полный расчёт; upcoming+incremental запрещены |
| src.model.eda | без специальных параметров CLI | Чтение витрины и текстовые отчёты |
| src.model.train | `--models-dir`, `--full-stats-only` | joblib-модели и metrics.json; каталог по умолчанию models |
| src.model.tune | `--n-trials`, `--models-dir` | Поиск параметров 1X2, модель и отчёты |
| src.model.predict | `--models-dir`, `--out-dir` | По умолчанию читает models/full_stats, пишет models/bets |
| src.jobs.build_late_goal_features | `--cutoff`, `--group`, `--out` | Исследовательская файловая выгрузка |
| src.jobs.scan_goal_after_cutoff | `--cutoff`, `--phase`, `--no-write` | Анализ и обычно запись своей таблицы; no-write отключает эту запись |
| src.model.late_goal_portrait | `--group` | Исследовательский отчёт |
| src.jobs.live_alerts | `--dry-run`, `--collect-only`, `--max-requests`, `--cache-dir` | Live API, архив/срезы; прямая отправка зависит от флага диспетчера |
| src.jobs.alert_dispatch | `--dry-run` | Оценка БД и отправка; dry-run использует rollback-only |
| src.bot.registry_bot | окружение | Постоянный long polling и запись диалога/ставок |
| src.jobs.settle_bets | `--max-requests`, `--cache-dir` | Обновление ставок и исходов старых оповещений |
| src.jobs.fonbet_watchdog | `--check-only` | Обычно уведомления/восстановление; check-only описан ниже |
| src.jobs.basketball | `--job`, `--max-requests`, `--since-year`, `--all-seasons`, `--cache-dir` | Схема basketball; report читает БД |
| src.db.migrate | `up`, `down VERSION`, `status` | Собственный реестр SQL; даже status может создать schema_migrations |

Источники CLI: [collect](../src/jobs/collect.py), [import](../src/jobs/import_parquet.py),
[features](../src/jobs/build_features.py), [train](../src/model/train.py),
[tune](../src/model/tune.py), [predict](../src/model/predict.py),
[scan](../src/jobs/scan_goal_after_cutoff.py), [dispatcher](../src/jobs/alert_dispatch.py),
[watchdog](../src/jobs/fonbet_watchdog.py), [migrate](../src/db/migrate.py).

`collect --job` поддерживает catalog, fixtures, injuries, odds, events, statistics,
lineups, backfill, daily, ft90-backfill, international-fixtures,
international-backfill, live-odds, cleanup, backfill-report, completeness-report.
`cleanup` удаляет кэш; два report читают БД. `--build-upcoming` допустим только
для daily. Историческая очередь `run_historical_fixtures` — внутренняя функция;
отдельный CLI job с таким именем не объявлен.

## Семантика режимов проверки

Название режима не является общим обещанием отсутствия всех побочных эффектов.

| Режим | Что отключено | Что остаётся |
|---|---|---|
| import_parquet --dry-run | Запись импортируемых данных | Чтение исходных файлов/БД, вывод отчёта |
| live_alerts --dry-run | Прямая отправка Telegram | API-запросы, кэш, журнал и live-данные в БД |
| live_alerts --collect-only | Прямая отправка Telegram | Полный сбор/сохранение |
| alert_dispatch --dry-run | Сеть Telegram, сохранённые строки оценки | Чтение БД, блокировки, rollback-only транзакция; PostgreSQL sequences не транзакционны |
| fonbet_live wrapper --dry-run | Импорт срезов в БД | Браузерное чтение, файловый экспорт, lock-файлы |
| reconcile.py без --apply | Применение найденных связей | Чтение БД через контейнер |
| import_history.py без --apply | Импорт купонов | Чтение входного JSON и отчёт |
| watchdog --check-only | Уведомления, рестарты, запуск заданий | Проверки systemd/браузера/памяти; каталог и monitor.lock могут создаваться |
| migrate status | Применение предметных миграций | CREATE TABLE IF NOT EXISTS реестра |

Для строгой диагностики чтением эти команды нельзя считать взаимозаменяемыми.
Ни одна из них не запускалась для создания baseline.

## Fonbet: файловый обмен и wrappers

Штатные точки входа: [run_fonbet_live.sh](../deploy/run_fonbet_live.sh)
с `--dry-run|--apply` и [run_fonbet_sync.sh](../deploy/run_fonbet_sync.sh)
с датой сверки. Обёртки сериализуют браузерные задания и вызывают импорт.
Прямой запуск collect.cjs обходит часть штатных блокировок.

Live JSON из [collect.cjs](../tools/fonbet_live/collect.cjs): run_id, started_at,
finished_at, status, summary, observations; также contexts, context_complete,
context_scan_at. Наблюдение содержит UUID, Fonbet ID/URL, команды/турнир,
минуту/период, счёт и ht, времена, подписи периода/метрики, raw-удары и качество.
[store.py](../tools/fonbet_live/store.py) валидирует типы, окно, происхождение,
время и whitelist полей; вход не может назначить API fixture_id.
`--input` обязателен, `--apply` пишет в БД, `--sql` печатает подготовленный SQL.
Версионированная JSON Schema в репозитории не найдена.

History JSON из [export.cjs](../tools/fonbet_sync/export.cjs): complete, dates
в формате DD.MM, rows. Каждая строка содержит id, date, type, legs и bar с
отображёнными данными купона. [import_history.py](../tools/fonbet_sync/import_history.py)
принимает `--input`, `--day YYYY-MM-DD`, `--apply`, `--dates`.
`--dates` читает даты открытых ставок из БД и добавляет запрошенный день;
обычный импорт требует полный обход. Отчёт содержит status, skipped_count,
found, applied, freebet_count, results и skipped; partial даёт код 2.

Коды и форматы ошибок различаются между инструментами. Нельзя считать любой
ненулевой код безопасным основанием повторить сетевую отправку Telegram.

## Файлы ML и процесс новостей

[train.py](../src/model/train.py) пишет result_1x2_lightgbm.joblib,
league_total_lightgbm.joblib, baseline-модели и metrics.json.
[predict.py](../src/model/predict.py) создаёт predictions_YYYY-MM-DD.parquet
и bets_YYYY-MM-DD.parquet; повтор в тот же день заменяет соответствующий файл.

Прогнозы содержат fixture_id, дату, команды, kickoff_at, status_short, линию
тотала и вероятности p_home/p_draw/p_away/p_over/p_under. Кандидаты содержат
market, outcome, model_prob, odds, bookmaker, ev и идентификацию/время матча.
Фильтр odds — [1.5, 2.5], для тотала требуется точное совпадение линии.
Список сортируется по EV, но не ограничивается только положительным EV.

[Процесс новостей](../.claude/skills/proverka-stavok/SKILL.md) читает
`models/bets_all_data`, фильтрует ещё не начавшиеся матчи ближайших 24 часов,
проверяет источники и дописывает `models/verdicts/verdicts_YYYY-MM-DD.csv`.
Это инструкция ассистенту, не серверный REST/cron-контракт. Фактическое наличие
свежих входных файлов, выполнение процесса и качество вердиктов не проверены.

## Telegram и SQL

Бот принимает `/start`, `/menu`, `/cancel`, текстовые действия «Внести событие»,
«Показать результат», «Проходимость», callback `rep:day|month`, `mkt:*`,
`bet:<alert_id>:<TB|TM>:<line>`, `cancel`. Меню сейчас показывает отчёт; ручной
ввод сохранён как запасной путь. Новые предлагаемые рынки ограничены TB,
старые TM-callback продолжают приниматься. Ввод числа допускает запятую;
линия кратна 0.25 и в диапазоне 0.25–20, коэффициент больше 1 и не больше 1000.
Чужой chat_id игнорируется, last_update_id защищает обработанное обновление.
Источник: [registry_bot.py](../src/bot/registry_bot.py).

SQL-интерфейсы: [модель данных](data-model.md), `alert_hit_rate`,
`fonbet_live_statistics_75_normalized`, `live_match_statistics_75_combined`,
функции `live_alert_identity`/`live_alert_link`. Последние меняют данные и
захватывают блокировки; это не read-only функции.

Секретные имена переменных, необходимые интеграциям: DATABASE_URL, APIFOOTBALL_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY. Их значения, браузерный профиль
и SSH-ключи не являются частью интерфейсной документации. Внешние интерфейсы
проверяются существующими тестами с подменой транспорта/DOM; живые контракты
и весь путь «источник → БД → Telegram» в этом baseline не проверялись.

## Голы и напоминания после первого сигнала

Рабочая очередь голов использует Fonbet для матчей, чей исходный сигнал пришёл из Fonbet. Сигнал API-Football использует только штатные данные для повтора `half_pressure`. Адаптер API в `signal_goals.py` остаётся доступным для нормализации уже полученных ответов и тестов. Очередь голов не создаёт HTTP-запросы API-Football.

`load_fonbet_timeline()` читает «Ход матча» из спортивной карточки с проверенным ID. Wrapper использует общие host/browser locks и одну временную вкладку с лимитами времени и памяти. `contact_attempted=false` или код wrapper 75 означает внутренний перенос. Сетевая ошибка после начала обращения расходует фактическую попытку послематчевой проверки.

Явная фаза карточки или совпавшая по ID строка в выбранном фильтре «Завершенные» подтверждает конец. Счёт для сверки берётся из явно размеченных первого и второго таймов с совпавшим порядком сторон. Общий итог дополнительных таймов и серии пенальти для сверки не используется. Если подтверждённый счёт расходится с последним доступным таймлайном, сохраняются события и причина `regulation_score_mismatch`. Коррекция отмены и восстановление гола требуют более свежего полного снимка. Незачтённый автогол при неопределимой идентичности хранится в pending.

Диспетчер фиксирует первое определимое решение по архиву штатных опросов исходного источника. Перед отправкой требуется свежий положительный триггер в окне 65–75. Связь с исходным сообщением задаёт `reply_parameters.message_id`. Параметр `allow_sending_without_reply` позволяет доставить напоминание после удаления исходного сообщения. Текст содержит ID первого сигнала. [Объект ReplyParameters](https://core.telegram.org/bots/api#replyparameters)
