"""Перенос накопленных данных из parquet в PostgreSQL.

Спецификация: specs/перенос-из-parquet.md

Порядок загрузки продиктован внешними ключами:
    лиги -> сезоны лиг -> команды -> игроки -> матчи -> события -> статистика
и в конце — отметки в fixture_fetch_state.

Запись идёт пакетами с ON CONFLICT DO UPDATE, поэтому прерванный перенос
продолжается повторным запуском и не создаёт дублей (ПР-8).

Использование:
    python3 -m src.jobs.import_parquet --source ПУТЬ [--only НАБОР] [--limit N] [--dry-run]
    python3 -m src.jobs.import_parquet --source ПУТЬ --verify
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from src.common.convert import (
    is_missing as _is_missing,
    to_date,
    to_decimal,
    to_int,
    to_percent,
    to_text,
    to_utc,
)
from src.common.db import BATCH_SIZE as _SHARED_BATCH_SIZE, write_batch
from src.common.keys import event_key
from src.db.connection import connect

BATCH_SIZE = _SHARED_BATCH_SIZE

DATASETS = ("leagues", "fixtures", "events", "statistics")

# Числовые колонки статистики, переносимые без преобразования (ПР-6).
STAT_PLAIN_COLUMNS = (
    "shots_on_goal",
    "shots_off_goal",
    "total_shots",
    "blocked_shots",
    "shots_insidebox",
    "shots_outsidebox",
    "fouls",
    "corner_kicks",
    "offsides",
    "yellow_cards",
    "red_cards",
    "goalkeeper_saves",
    "total_passes",
    "passes_accurate",
    "substitutions",
    "free_kicks",
    "assists",
    "counter_attacks",
    "cross_attacks",
    "goals",
    "goal_attempts",
    "throwins",
    "medical_treatment",
)


def num(value: int) -> str:
    """Число с пробелом между разрядами: 1 092 857."""
    return f"{value:,}".replace(",", "\u00a0")


# --------------------------------------------------------------------- отчёт


@dataclass
class Report:
    """Что прочитано, что записано и почему пропущено (ПР-9)."""

    name: str
    read: int = 0
    written: int = 0
    skipped: Counter = field(default_factory=Counter)

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped[reason] += count

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def as_text(self) -> str:
        lines = [
            f"{self.name}: прочитано {num(self.read)}, записано {num(self.written)}, "
            f"пропущено {num(self.skipped_total)}"
        ]
        for reason, count in self.skipped.most_common():
            lines.append(f"    {reason}: {num(count)}")
        return "\n".join(lines)


# ------------------------------------------------------- преобразование типов


# ----------------------------------------------------------- чтение и запись


def read_batches(path: Path, columns: list[str] | None = None) -> Iterator[list[dict]]:
    """Читает parquet частями: файл целиком в память не поднимается."""
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    available = set(parquet.schema_arrow.names)
    selected = [c for c in columns if c in available] if columns else None
    for batch in parquet.iter_batches(batch_size=BATCH_SIZE, columns=selected):
        yield batch.to_pylist()


# write_batch — теперь в src/common/db.py (используется и сбором).


# --------------------------------------------------------------- сопоставление


def load_league_mapping(source: Path) -> dict[str, dict]:
    """Код лиги прежнего проекта -> сведения об API-лиге."""
    path = source / "apifootball_leagues.json"
    if not path.exists():
        raise FileNotFoundError(f"Нет файла сопоставления лиг: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- наборы


def import_leagues(conn, source: Path, limit: int | None, dry_run: bool) -> list[Report]:
    """Лиги (ПР-1) и сезоны лиг с покрытием (ПР-2)."""
    mapping = load_league_mapping(source)
    legacy_by_id = {int(info["id"]): code for code, info in mapping.items()}

    catalog_report = Report("лиги")
    rows: list[tuple] = []
    path = source / "leagues_catalog.parquet"
    if not path.exists():
        catalog_report.skip("файл leagues_catalog.parquet отсутствует")
        return [catalog_report]

    for batch in read_batches(path):
        for row in batch:
            catalog_report.read += 1
            league_id = to_int(row.get("id"))
            if league_id is None:
                catalog_report.skip("нет league_id")
                continue
            rows.append(
                (
                    league_id,
                    to_text(row.get("name")) or f"Лига {league_id}",
                    to_text(row.get("type")),
                    to_text(row.get("country")),
                    to_text(row.get("cc")),
                    legacy_by_id.get(league_id),
                    league_id in legacy_by_id,  # is_tracked: наши 782 лиги (ФТ-9)
                )
            )
            if limit and len(rows) >= limit:
                break
        if limit and len(rows) >= limit:
            break

    if not dry_run:
        for start in range(0, len(rows), BATCH_SIZE):
            catalog_report.written += write_batch(
                conn,
                "leagues",
                ["league_id", "name", "type", "country", "country_code", "legacy_code", "is_tracked"],
                rows[start : start + BATCH_SIZE],
                "league_id",
            )
    else:
        catalog_report.written = 0

    season_report = Report("сезоны лиг")
    season_rows: list[tuple] = []
    known_ids = {r[0] for r in rows}
    for code, info in mapping.items():
        league_id = to_int(info.get("id"))
        if league_id is None or league_id not in known_ids:
            season_report.skip("лига отсутствует в каталоге")
            continue
        coverage = info.get("coverage") or {}
        for season in info.get("seasons") or []:
            season_report.read += 1
            season_rows.append(
                (
                    league_id,
                    int(season),
                    coverage.get("events"),
                    coverage.get("statistics_fixtures"),
                    coverage.get("lineups"),
                    coverage.get("injuries"),
                )
            )

    if not dry_run:
        for start in range(0, len(season_rows), BATCH_SIZE):
            season_report.written += write_batch(
                conn,
                "league_seasons",
                [
                    "league_id",
                    "season",
                    "has_events",
                    "has_statistics",
                    "has_lineups",
                    "has_injuries",
                ],
                season_rows[start : start + BATCH_SIZE],
                "league_id, season",
            )

    return [catalog_report, season_report]


def import_fixtures(conn, source: Path, limit: int | None, dry_run: bool) -> list[Report]:
    """Команды (ПР-3) и матчи (ПР-4)."""
    mapping = load_league_mapping(source)
    league_by_code = {code: int(info["id"]) for code, info in mapping.items()}

    teams_report = Report("команды")
    fixtures_report = Report("матчи")
    path = source / "fixtures.parquet"
    if not path.exists():
        fixtures_report.skip("файл fixtures.parquet отсутствует")
        return [fixtures_report]

    teams: dict[int, str] = {}
    fixture_rows: list[tuple] = []
    processed = 0

    def flush_fixtures() -> None:
        if dry_run or not fixture_rows:
            return
        fixtures_report.written += write_batch(
            conn,
            "fixtures",
            [
                "fixture_id",
                "league_id",
                "season",
                "kickoff_at",
                "match_date",
                "status_short",
                "home_team_id",
                "away_team_id",
                "goals_home",
                "goals_away",
            ],
            list(fixture_rows),
            "fixture_id",
        )
        fixture_rows.clear()

    for batch in read_batches(path):
        for row in batch:
            fixtures_report.read += 1
            fixture_id = to_int(row.get("fixture_id"))
            home_id = to_int(row.get("home_id"))
            away_id = to_int(row.get("away_id"))
            kickoff = to_utc(row.get("kickoff"))
            league_id = league_by_code.get(to_text(row.get("LeagueCode")))

            if fixture_id is None:
                fixtures_report.skip("нет fixture_id")
                continue
            if league_id is None:
                fixtures_report.skip("лига без сопоставления")
                continue
            if home_id is None or away_id is None:
                fixtures_report.skip("нет идентификатора команды")
                continue
            if kickoff is None:
                fixtures_report.skip("нет времени начала")
                continue

            teams.setdefault(home_id, to_text(row.get("HomeTeam")) or f"Команда {home_id}")
            teams[home_id] = to_text(row.get("HomeTeam")) or teams[home_id]
            teams[away_id] = to_text(row.get("AwayTeam")) or teams.get(
                away_id, f"Команда {away_id}"
            )

            fixture_rows.append(
                (
                    fixture_id,
                    league_id,
                    to_int(row.get("Season")) or kickoff.year,
                    kickoff,
                    to_date(row.get("Date")) or kickoff.date(),
                    to_text(row.get("status")),
                    home_id,
                    away_id,
                    to_int(row.get("FTHG")),
                    to_int(row.get("FTAG")),
                )
            )
            processed += 1
            if limit and processed >= limit:
                break
        if limit and processed >= limit:
            break
        if len(fixture_rows) >= BATCH_SIZE and teams:
            # Команды пишутся раньше матчей: на них ссылается внешний ключ.
            _write_teams(conn, teams, teams_report, dry_run)
            teams = {}
            flush_fixtures()

    if teams:
        _write_teams(conn, teams, teams_report, dry_run)
    flush_fixtures()
    return [teams_report, fixtures_report]


def _write_teams(conn, teams: dict[int, str], report: Report, dry_run: bool) -> None:
    report.read += len(teams)
    if dry_run:
        return
    rows = [(team_id, name) for team_id, name in teams.items()]
    for start in range(0, len(rows), BATCH_SIZE):
        report.written += write_batch(
            conn, "teams", ["team_id", "name"], rows[start : start + BATCH_SIZE], "team_id"
        )


def import_events(conn, source: Path, limit: int | None, dry_run: bool) -> list[Report]:
    """Игроки (ПР-3) и события (ПР-5)."""
    players_report = Report("игроки")
    events_report = Report("события")
    path = source / "events.parquet"
    if not path.exists():
        events_report.skip("файл events.parquet отсутствует")
        return [events_report]

    known_fixtures = _existing_ids(conn, "fixtures", "fixture_id") if not dry_run else set()
    teams_report = Report("команды из событий")
    teams: dict[int, str] = {}
    players: dict[int, str] = {}
    event_rows: list[tuple] = []
    seen_keys: set[str] = set()
    processed = 0

    def flush() -> None:
        if dry_run:
            return
        # Команды и игроки пишутся раньше событий: на них ссылаются внешние
        # ключи. В fixtures.parquet есть не все команды, встречающиеся
        # в событиях, поэтому справочник пополняется и отсюда.
        if teams:
            _write_teams(conn, teams, teams_report, dry_run)
            teams.clear()
        if players:
            _write_players(conn, players, players_report)
            players.clear()
        if event_rows:
            events_report.written += write_batch(
                conn,
                "fixture_events",
                [
                    "fixture_id",
                    "team_id",
                    "minute",
                    "minute_extra",
                    "type",
                    "detail",
                    "player_id",
                    "comments",
                    "event_key",
                ],
                list(event_rows),
                "event_key",
            )
            event_rows.clear()

    for batch in read_batches(path):
        for row in batch:
            events_report.read += 1
            fixture_id = to_int(row.get("fixture_id"))
            if fixture_id is None:
                events_report.skip("нет fixture_id")
                continue
            if known_fixtures and fixture_id not in known_fixtures:
                events_report.skip("матч отсутствует в базе")
                continue

            team_id = to_int(row.get("team_id"))
            if team_id is not None:
                teams[team_id] = to_text(row.get("team")) or f"Команда {team_id}"

            player_id = to_int(row.get("player_id"))
            if player_id is not None:
                players[player_id] = to_text(row.get("player")) or f"Игрок {player_id}"

            key = event_key(
                fixture_id,
                row.get("minute"),
                row.get("minute_extra"),
                row.get("team_id"),
                row.get("type"),
                row.get("detail"),
                row.get("player_id"),
            )
            if key in seen_keys:
                events_report.skip("повтор event_key в файле")
                continue
            seen_keys.add(key)

            event_rows.append(
                (
                    fixture_id,
                    team_id,
                    to_int(row.get("minute")),
                    to_int(row.get("minute_extra")),
                    to_text(row.get("type")) or "unknown",
                    to_text(row.get("detail")),
                    player_id,
                    to_text(row.get("comments")),
                    key,
                )
            )
            processed += 1
            if limit and processed >= limit:
                break
        flush()
        if limit and processed >= limit:
            break

    flush()
    return [teams_report, players_report, events_report]


def _write_players(conn, players: dict[int, str], report: Report) -> None:
    rows = [(player_id, name) for player_id, name in players.items()]
    report.read += len(rows)
    for start in range(0, len(rows), BATCH_SIZE):
        report.written += write_batch(
            conn,
            "players",
            ["player_id", "name"],
            rows[start : start + BATCH_SIZE],
            "player_id",
        )


def import_statistics(conn, source: Path, limit: int | None, dry_run: bool) -> list[Report]:
    """Статистика команд в матчах (ПР-6)."""
    report = Report("статистика")
    path = source / "statistics.parquet"
    if not path.exists():
        report.skip("файл statistics.parquet отсутствует")
        return [report]

    known_fixtures = _existing_ids(conn, "fixtures", "fixture_id") if not dry_run else set()
    teams_report = Report("команды из статистики")
    teams: dict[int, str] = {}

    columns = [
        "fixture_id",
        "team_id",
        "ball_possession",
        "passes_pct",
        "expected_goals",
        "goals_prevented",
        *STAT_PLAIN_COLUMNS,
    ]
    rows: list[tuple] = []
    seen: set[tuple[int, int]] = set()
    processed = 0

    def flush() -> None:
        if dry_run:
            return
        if teams:
            _write_teams(conn, teams, teams_report, dry_run)
            teams.clear()
        if rows:
            report.written += write_batch(
                conn, "fixture_statistics", columns, list(rows), "fixture_id, team_id"
            )
            rows.clear()

    for batch in read_batches(path):
        for row in batch:
            report.read += 1
            fixture_id = to_int(row.get("fixture_id"))
            team_id = to_int(row.get("team_id"))
            if fixture_id is None or team_id is None:
                report.skip("нет fixture_id или team_id")
                continue
            if known_fixtures and fixture_id not in known_fixtures:
                report.skip("матч отсутствует в базе")
                continue
            teams[team_id] = to_text(row.get("team")) or f"Команда {team_id}"
            if (fixture_id, team_id) in seen:
                report.skip("повтор пары матч-команда в файле")
                continue
            seen.add((fixture_id, team_id))

            possession = to_percent(row.get("ball_possession"))
            passes_pct = to_percent(row.get("passes_pct"))
            if possession is None and not _is_missing(row.get("ball_possession")):
                report.skip("нечисловое владение мячом")
            if passes_pct is None and not _is_missing(row.get("passes_pct")):
                report.skip("нечисловая точность передач")

            rows.append(
                (
                    fixture_id,
                    team_id,
                    possession,
                    passes_pct,
                    to_decimal(row.get("expected_goals")),
                    to_decimal(row.get("goals_prevented")),
                    *[to_int(row.get(c)) for c in STAT_PLAIN_COLUMNS],
                )
            )
            processed += 1
            if limit and processed >= limit:
                break
        flush()
        if limit and processed >= limit:
            break

    flush()
    return [teams_report, report]


def fill_fetch_state(conn, dry_run: bool) -> Report:
    """Отметки о собранных наборах (ПР-7).

    Без этого дозагрузка (ФТ-11) пойдёт собирать заново миллион матчей,
    по которым события и статистика уже есть.
    """
    report = Report("состояние сбора")
    if dry_run:
        return report
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fixture_fetch_state (
                    fixture_id, events_fetched_at, statistics_fetched_at
                )
                SELECT f.fixture_id,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM fixture_events e WHERE e.fixture_id = f.fixture_id
                       ) THEN now() END,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM fixture_statistics s WHERE s.fixture_id = f.fixture_id
                       ) THEN now() END
                FROM fixtures f
                ON CONFLICT (fixture_id) DO UPDATE SET
                    events_fetched_at = COALESCE(
                        fixture_fetch_state.events_fetched_at,
                        EXCLUDED.events_fetched_at
                    ),
                    statistics_fetched_at = COALESCE(
                        fixture_fetch_state.statistics_fetched_at,
                        EXCLUDED.statistics_fetched_at
                    )
                """
            )
            report.written = cur.rowcount
            report.read = cur.rowcount
    return report


def _existing_ids(conn, table: str, column: str) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {column} FROM {table}")
        return {row[0] for row in cur.fetchall()}


# -------------------------------------------------------------------- сверка


def verify(conn, source: Path) -> int:
    """Сравнение базы и файлов (ПР-10). Только чтение."""
    import pyarrow.parquet as pq

    checks = [
        ("лиги", "leagues", "leagues_catalog.parquet", None),
        ("матчи", "fixtures", "fixtures.parquet", None),
        ("события", "fixture_events", "events.parquet", None),
        ("статистика", "fixture_statistics", "statistics.parquet", None),
    ]
    print("Сверка базы и parquet\n")
    print(f"{'набор':<14}{'в файле':>12}{'в базе':>12}{'разница':>12}")
    mismatches = 0
    for title, table, filename, _ in checks:
        path = source / filename
        in_file = pq.ParquetFile(path).metadata.num_rows if path.exists() else 0
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            in_db = cur.fetchone()[0]
        diff = in_db - in_file
        if diff != 0:
            mismatches += 1
        print(f"{title:<14}{num(in_file):>12}{num(in_db):>12}{num(diff):>12}")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM fixture_fetch_state")
        state_rows = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM fixtures")
        fixtures_rows = cur.fetchone()[0]
    print(
        f"\nfixture_fetch_state: {num(state_rows)} строк "
        f"при {num(fixtures_rows)} матчах"
    )
    if state_rows != fixtures_rows:
        mismatches += 1

    print(
        "\nРасхождения ожидаемы там, где строки пропущены осознанно "
        "(см. отчёт переноса)."
        if mismatches
        else "\nРасхождений нет."
    )
    return mismatches


# ---------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос данных из parquet в PostgreSQL")
    parser.add_argument("--source", required=True, type=Path, help="каталог с parquet")
    parser.add_argument("--only", choices=DATASETS, help="перенести один набор")
    parser.add_argument("--limit", type=int, help="ограничить число строк")
    parser.add_argument("--dry-run", action="store_true", help="не писать в базу")
    parser.add_argument("--verify", action="store_true", help="только сверка")
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"Каталог не найден: {args.source}", file=sys.stderr)
        return 1

    started = datetime.now(timezone.utc)
    with connect() as conn:
        if args.verify:
            return 0 if verify(conn, args.source) == 0 else 2

        reports: list[Report] = []
        steps = {
            "leagues": import_leagues,
            "fixtures": import_fixtures,
            "events": import_events,
            "statistics": import_statistics,
        }
        for name, step in steps.items():
            if args.only and args.only != name:
                continue
            print(f"--- {name}")
            reports.extend(step(conn, args.source, args.limit, args.dry_run))

        if not args.only and not args.dry_run:
            print("--- состояние сбора")
            reports.append(fill_fetch_state(conn, args.dry_run))

        print("\nОтчёт о переносе")
        for report in reports:
            print(report.as_text())
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\nВремя: {elapsed:.1f} с")
        if args.dry_run:
            print("Режим --dry-run: в базу ничего не записано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
