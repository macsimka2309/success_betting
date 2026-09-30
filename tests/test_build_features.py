"""Тесты препроцессинга для модели (specs/препроцессинг-для-модели.md).

Часть тестов — на чистых pandas-данных (формулы, отсечение по времени,
границы сезона), часть — end-to-end на временной базе (TEST_DATABASE_URL),
без обращения к API (НФТ-8).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

psycopg = pytest.importorskip("psycopg")

from src.db import migrate
from src.jobs import build_features as bf

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="не задана TEST_DATABASE_URL — тестовая база недоступна"
)


def ts(day: int, month: int = 1, year: int = 2026, hour: int = 15) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def make_fixtures_df(rows: list[dict]) -> pd.DataFrame:
    """rows: fixture_id, league_id, season, home_team_id, away_team_id,
    kickoff_at, goals_home, goals_away, (ft90_home/away, status_short опционально)."""
    df = pd.DataFrame(rows)
    for col in ("ft90_home", "ft90_away"):
        if col not in df.columns:
            df[col] = np.nan
    df["match_date"] = df["kickoff_at"].dt.date
    if "status_short" not in df.columns:
        df["status_short"] = "FT"
    return df


def make_upcoming_row(fixture_id: int, league_id: int, season: int, home: int, away: int, kickoff_at) -> dict:
    """Строка ещё не сыгранного матча (--upcoming): без счёта, статус NS."""
    return {
        "fixture_id": fixture_id, "league_id": league_id, "season": season,
        "home_team_id": home, "away_team_id": away, "kickoff_at": kickoff_at,
        "goals_home": None, "goals_away": None, "status_short": "NS",
    }


# -------------------------------------------------------------------- half_line


def test_half_line_always_ends_in_half():
    values = bf.half_line(pd.Series([0.0, 0.9, 1.0, 1.4, 1.5, 2.99]))
    assert list(values) == [0.5, 0.5, 1.5, 1.5, 1.5, 2.5]


# ------------------------------------------------------------------ targets


def test_targets_aet_uses_ft90_not_goals():
    df = make_fixtures_df(
        [
            {
                "fixture_id": 1, "league_id": 1, "season": 2026,
                "home_team_id": 10, "away_team_id": 20, "kickoff_at": ts(1),
                "goals_home": 2, "goals_away": 2, "ft90_home": 1, "ft90_away": 2,
            }
        ]
    )
    out = bf.compute_targets(df)
    row = out.iloc[0]
    assert (row["reg_home"], row["reg_away"]) == (1, 2)
    assert row["result_1x2"] == "A"  # не 'D', как дал бы goals_home/away
    assert row["btts"]  # 1:2 по основному времени — забили обе команды


def test_targets_ft_falls_back_to_goals_and_btts():
    df = make_fixtures_df(
        [
            {
                "fixture_id": 2, "league_id": 1, "season": 2026,
                "home_team_id": 10, "away_team_id": 20, "kickoff_at": ts(1),
                "goals_home": 2, "goals_away": 1,
            }
        ]
    )
    out = bf.compute_targets(df)
    row = out.iloc[0]
    assert (row["reg_home"], row["reg_away"]) == (2, 1)
    assert row["result_1x2"] == "H"
    assert row["btts"]
    assert row["dc_1x"] and not row["dc_x2"] and row["dc_12"]


def test_finished_match_without_score_is_dropped_not_crashed(capsys):
    """Найдено на продовых данных 29.09.2026: FT/AET/PEN без счёта — дефект
    данных, а не что-то, что np.select должен молча проглотить или упасть на."""
    df = make_fixtures_df(
        [
            {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
             "away_team_id": 20, "kickoff_at": ts(1), "goals_home": None, "goals_away": None},
            {"fixture_id": 2, "league_id": 1, "season": 2026, "home_team_id": 10,
             "away_team_id": 20, "kickoff_at": ts(2), "goals_home": 1, "goals_away": 0},
        ]
    )
    out = bf.compute_targets(df)
    assert list(out["fixture_id"]) == [2]
    assert "1" in capsys.readouterr().out  # fixture_id пропущенного упомянут в выводе


def test_draw_gives_both_double_chance_but_not_no_draw():
    df = make_fixtures_df(
        [{"fixture_id": 3, "league_id": 1, "season": 2026, "home_team_id": 10,
          "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 1, "goals_away": 1}]
    )
    out = bf.compute_targets(df)
    row = out.iloc[0]
    assert row["result_1x2"] == "D"
    assert row["dc_1x"] and row["dc_x2"] and not row["dc_12"]


# ------------------------------------------------------------- лиги/команды


def _league_series(n: int, goals_per_match: list[tuple[int, int]], season=2026, league_id=1):
    rows = []
    for i, (h, a) in enumerate(goals_per_match):
        rows.append(
            {
                "fixture_id": 100 + i, "league_id": league_id, "season": season,
                "home_team_id": 10 + (i % 4), "away_team_id": 20 + (i % 4),
                "kickoff_at": ts(1 + i), "goals_home": h, "goals_away": a,
            }
        )
    return make_fixtures_df(rows)


def test_league_line_null_below_minimum():
    df = bf.compute_targets(_league_series(5, [(1, 1)] * 5))
    out = bf.compute_league_lines(df)
    assert out["league_total_line"].isna().all()  # 5 матчей < MIN_LEAGUE_MATCHES=20


def test_league_line_no_leakage_uses_only_prior_matches():
    """Матч №21 не должен учитывать собственный тотал в своей линии."""
    goals = [(1, 1)] * 20 + [(9, 9)]  # резкий выброс в 21-м матче
    df = bf.compute_targets(_league_series(21, goals))
    out = bf.compute_league_lines(df)
    last_line = out.iloc[20]["league_total_line"]
    assert last_line == 2.5  # floor(2.0)+0.5 по первым 20 матчам, не искажён выбросом


def test_league_season_boundary_resets_average():
    prev_season = _league_series(20, [(0, 0)] * 20, season=2025)
    this_season = _league_series(1, [(1, 1)], season=2026)
    this_season["fixture_id"] += 1000
    df = bf.compute_targets(pd.concat([prev_season, this_season], ignore_index=True))
    out = bf.compute_league_lines(df)
    new_season_row = out[out["season"] == 2026].iloc[0]
    assert pd.isna(new_season_row["league_total_line"])  # прошлый сезон не считается


def test_team_line_gated_by_min_team_matches():
    rows = [
        {"fixture_id": 200 + i, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 900 + i, "kickoff_at": ts(1 + i), "goals_home": 2, "goals_away": 0}
        for i in range(3)
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    long_df = bf._team_long_format(df)
    out = bf.compute_team_lines(df, long_df)
    # у первого домашнего матча команды 10 в сезоне история отсутствует
    assert pd.isna(out.iloc[0]["home_team_line"])
    # у третьего накопилось MIN_TEAM_MATCHES=3-1=2 предыдущих -> ещё мало (нужно >=3)


def test_handicap_favorite_and_no_push():
    df = pd.DataFrame(
        {
            "fixture_id": [1], "home_team_line": [1.8], "away_team_line": [0.9],
            "reg_home": [3], "reg_away": [1],
        }
    )
    both_known = df["home_team_line"].notna() & df["away_team_line"].notna()
    raw = df["home_team_line"] - df["away_team_line"]
    favorite = np.where(raw >= 0, "home", "away")
    handicap_line = bf.half_line(raw.abs())
    assert favorite[0] == "home"
    assert handicap_line[0] == 0.5  # floor(0.9)+0.5, не 1.0 (см. коммит ad09f03)
    margin = df["reg_home"] - df["reg_away"]
    assert bool((margin > handicap_line).iloc[0]) is True


# ------------------------------------------------------------------- форма


def test_form_short_window_excludes_previous_season():
    prev = [{"fixture_id": 1, "league_id": 1, "season": 2025, "home_team_id": 10,
              "away_team_id": 20, "kickoff_at": ts(1, 5, 2025), "goals_home": 5, "goals_away": 0}]
    this = [{"fixture_id": 2, "league_id": 1, "season": 2026, "home_team_id": 10,
              "away_team_id": 30, "kickoff_at": ts(1, 8, 2026), "goals_home": 1, "goals_away": 0}]
    df = bf.compute_targets(make_fixtures_df(prev + this))
    long_df = bf._team_long_format(df)
    form = bf.compute_form(long_df)
    team10_second_match = form[(form["fixture_id"] == 2) & (form["team_id"] == 10)].iloc[0]
    assert team10_second_match["form__overall__short__matches_played"] == 0
    assert pd.isna(team10_second_match["form__overall__short__goals_scored_avg"])


def test_form_no_min_window_gate_uses_available_matches():
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 2, "goals_away": 0},
        {"fixture_id": 2, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 30, "kickoff_at": ts(8), "goals_home": 4, "goals_away": 0},
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    long_df = bf._team_long_format(df)
    form = bf.compute_form(long_df)
    second = form[(form["fixture_id"] == 2) & (form["team_id"] == 10)].iloc[0]
    assert second["form__overall__short__matches_played"] == 1  # только 1, не NULL
    assert second["form__overall__short__goals_scored_avg"] == 2.0


# --------------------------------------------------------------------- h2h


def test_h2h_null_with_fewer_than_two_matches():
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2025, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1, 1, 2025), "goals_home": 1, "goals_away": 0},
        {"fixture_id": 2, "league_id": 1, "season": 2026, "home_team_id": 20,
         "away_team_id": 10, "kickoff_at": ts(1, 1, 2026), "goals_home": 0, "goals_away": 0},
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    h2h = bf.compute_h2h(df)
    assert pd.isna(h2h[h2h["fixture_id"] == 2].iloc[0]["h2h_avg_total_goals"])


def test_h2h_computed_from_two_matches_perspective_of_current_home_team():
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2025, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1, 1, 2025), "goals_home": 2, "goals_away": 0},  # 10 победил
        {"fixture_id": 2, "league_id": 1, "season": 2025, "home_team_id": 20,
         "away_team_id": 10, "kickoff_at": ts(1, 6, 2025), "goals_home": 1, "goals_away": 1},  # ничья
        {"fixture_id": 3, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1, 1, 2026), "goals_home": 0, "goals_away": 0},
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    h2h = bf.compute_h2h(df)
    row = h2h[h2h["fixture_id"] == 3].iloc[0]
    assert row["h2h_matches_played"] == 2
    assert row["h2h_avg_total_goals"] == pytest.approx((2 + 2) / 2)
    assert row["h2h_home_team_win_rate"] == pytest.approx(0.5)  # команда 10 выиграла 1 из 2
    assert row["h2h_draw_rate"] == pytest.approx(0.5)
    assert row["h2h_away_team_win_rate"] == pytest.approx(0.0)


# ------------------------------------------------------------- upcoming (ДП-9)


def test_targets_upcoming_match_has_null_targets_not_dropped():
    """NS-матч (--upcoming) без счёта — не дефект данных (в отличие от
    FT без счёта), строка не выбрасывается, целевые колонки — NULL."""
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 2, "goals_away": 1},
        make_upcoming_row(2, 1, 2026, 10, 30, ts(8)),
    ]
    out = bf.compute_targets(make_fixtures_df(rows))
    assert list(out["fixture_id"]) == [1, 2]  # upcoming не выброшен
    upcoming = out[out["fixture_id"] == 2].iloc[0]
    assert pd.isna(upcoming["reg_home"]) and pd.isna(upcoming["reg_away"])
    assert upcoming["result_1x2"] is None
    assert upcoming["btts"] is None
    assert upcoming["dc_1x"] is None and upcoming["dc_x2"] is None and upcoming["dc_12"] is None
    finished = out[out["fixture_id"] == 1].iloc[0]
    assert finished["result_1x2"] == "H"  # завершённый матч считается как обычно


def test_league_line_valid_but_league_total_over_null_for_upcoming():
    """league_total_line — предсказательный признак, валиден и для будущего
    матча; league_total_over — по факту счёта, у будущего матча остаётся NULL."""
    finished = [
        {"fixture_id": 100 + i, "league_id": 1, "season": 2026,
         "home_team_id": 10 + (i % 4), "away_team_id": 20 + (i % 4),
         "kickoff_at": ts(1 + i), "goals_home": 1, "goals_away": 1}
        for i in range(20)
    ]
    rows = finished + [make_upcoming_row(200, 1, 2026, 10, 21, ts(1, 2))]
    df = bf.compute_targets(make_fixtures_df(rows))
    df = bf.compute_league_lines(df)
    upcoming = df[df["fixture_id"] == 200].iloc[0]
    assert pd.notna(upcoming["league_total_line"])
    assert pd.isna(upcoming["league_total_over"])


def test_form_two_upcoming_matches_in_horizon_do_not_pollute_each_other():
    """Команда играет дважды в горизонте --upcoming (например, две игры за
    неделю): первый ещё не сыгранный матч не должен войти в форму второго."""
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 2, "goals_away": 0},
        {"fixture_id": 2, "league_id": 1, "season": 2026, "home_team_id": 30,
         "away_team_id": 10, "kickoff_at": ts(8), "goals_home": 1, "goals_away": 1},
        make_upcoming_row(3, 1, 2026, 10, 40, ts(15)),   # upcoming #1
        make_upcoming_row(4, 1, 2026, 50, 10, ts(18)),   # upcoming #2, позже
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    long_df = bf._team_long_format(df)
    form = bf.compute_form(long_df)
    before_first_upcoming = form[(form["fixture_id"] == 3) & (form["team_id"] == 10)].iloc[0]
    before_second_upcoming = form[(form["fixture_id"] == 4) & (form["team_id"] == 10)].iloc[0]
    assert before_first_upcoming["form__overall__short__matches_played"] == 2
    assert before_second_upcoming["form__overall__short__matches_played"] == 2  # не 3
    assert before_first_upcoming["form__overall__short__goals_scored_avg"] == before_second_upcoming[
        "form__overall__short__goals_scored_avg"
    ]


def test_elo_does_not_update_from_upcoming_match():
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 2, "goals_away": 0},
        make_upcoming_row(2, 1, 2026, 10, 30, ts(8)),
        make_upcoming_row(3, 1, 2026, 40, 10, ts(15)),
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    elo_df, ratings = bf.compute_elo(df)
    elo_before_first_upcoming = elo_df[elo_df["fixture_id"] == 2].iloc[0]["home_team_elo"]
    elo_before_second_upcoming = elo_df[elo_df["fixture_id"] == 3].iloc[0]["away_team_elo"]
    assert elo_before_first_upcoming == pytest.approx(elo_before_second_upcoming)  # не сдвинулся


def test_standings_upcoming_match_does_not_advance_table():
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 3, "goals_away": 0},
        make_upcoming_row(2, 1, 2026, 10, 30, ts(8)),
        make_upcoming_row(3, 1, 2026, 40, 10, ts(15)),
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    long_df = bf._team_long_format(df)
    standings = bf.compute_standings(df, long_df)
    points_before_first_upcoming = standings[standings["fixture_id"] == 2].iloc[0]["home_team_points"]
    points_before_second_upcoming = standings[standings["fixture_id"] == 3].iloc[0]["away_team_points"]
    assert points_before_first_upcoming == 3  # только матч 1 (победа)
    assert points_before_second_upcoming == 3  # матч 2 (upcoming) не добавил очков


# ------------------------------------------------------------------- Эло


def test_elo_sequential_matches_manual_calculation():
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 20, "kickoff_at": ts(1), "goals_home": 2, "goals_away": 0},
        {"fixture_id": 2, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 30, "kickoff_at": ts(8), "goals_home": 0, "goals_away": 1},
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    elo_df, ratings = bf.compute_elo(df)

    r10 = r20 = r30 = bf.ELO_DEFAULT
    expected1 = 1.0 / (1.0 + 10 ** (-(r10 + bf.ELO_HOME_ADVANTAGE - r20) / 400.0))
    delta1 = bf.ELO_K * (1.0 - expected1)
    r10_after = r10 + delta1

    row1 = elo_df[elo_df["fixture_id"] == 1].iloc[0]
    row2 = elo_df[elo_df["fixture_id"] == 2].iloc[0]
    assert row1["home_team_elo"] == pytest.approx(bf.ELO_DEFAULT)
    assert row2["home_team_elo"] == pytest.approx(r10_after)  # рейтинг перенесён из матча 1


def test_elo_new_team_starts_at_current_league_average():
    """Новая команда стартует со среднего пула, а не с фиксированной константы.

    Один уже рейтингованный участник (1700) — пул смещён от 1500, поэтому
    случай различим: если бы старт был жёстко 1500, тест бы это поймал.
    (Замечание: при паре уже рейтингованных команд средний рейтинг лиги
    математически инвариантен — обмен Эло между двумя участниками
    сохраняет их сумму, — поэтому проверить это на паре с нуля нельзя;
    нужен пул нечётного состава или заданное `initial_state`.)
    """
    rows = [
        {"fixture_id": 1, "league_id": 1, "season": 2026, "home_team_id": 10,
         "away_team_id": 99, "kickoff_at": ts(1), "goals_home": 0, "goals_away": 0},
    ]
    df = bf.compute_targets(make_fixtures_df(rows))
    elo_df, _ = bf.compute_elo(df, initial_state={(10, 1): 1700.0})
    row = elo_df.iloc[0]
    assert row["home_team_elo"] == pytest.approx(1700.0)  # уже известный рейтинг
    assert row["away_team_elo"] == pytest.approx(1700.0)  # новая команда: старт со среднего пула (=1700)


# ------------------------------------------------------------- интеграция (DB)


@pytest.fixture()
def conn():
    with psycopg.connect(TEST_URL, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        migrate.run_up(connection)
        yield connection


def _seed_season(conn, league_id=39, season=2026, n_matches=25, fixture_offset=0):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO leagues (league_id, name, is_tracked) VALUES (%s, 'PL', true)", (league_id,))
        cur.execute("INSERT INTO league_seasons (league_id, season) VALUES (%s, %s)", (league_id, season))
        team_ids = list(range(1, 9))
        for tid in team_ids:
            cur.execute(
                "INSERT INTO teams (team_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (tid, f"Team {tid}")
            )
        for i in range(n_matches):
            home, away = team_ids[i % 8], team_ids[(i + 1) % 8]
            cur.execute(
                """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
                   status_short, home_team_id, away_team_id, goals_home, goals_away)
                   VALUES (%s, %s, %s, %s, %s, 'FT', %s, %s, %s, %s)""",
                (1000 + fixture_offset + i, league_id, season, ts(1 + i), ts(1 + i).date(), home, away, i % 3, i % 2),
            )


def test_build_end_to_end_writes_expected_columns(conn):
    _seed_season(conn)
    df = bf.build(conn)
    written = bf.write_features(conn, df, incremental=False)
    assert written == 25
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_match_features")
        assert cur.fetchone()[0] == 25
        cur.execute("SELECT result_1x2, home_team_elo, away_team_elo FROM ml_match_features WHERE fixture_id = 1000")
        result, home_elo, away_elo = cur.fetchone()
        assert result in ("H", "D", "A")
        assert float(home_elo) == pytest.approx(bf.ELO_DEFAULT)
        assert float(away_elo) == pytest.approx(bf.ELO_DEFAULT)


def test_build_full_rebuild_is_idempotent(conn):
    _seed_season(conn)
    df1 = bf.build(conn)
    bf.write_features(conn, df1, incremental=False)
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id, result_1x2, home_team_elo FROM ml_match_features ORDER BY fixture_id")
        before = cur.fetchall()

    df2 = bf.build(conn)
    bf.write_features(conn, df2, incremental=False)
    with conn.cursor() as cur:
        cur.execute("SELECT fixture_id, result_1x2, home_team_elo FROM ml_match_features ORDER BY fixture_id")
        after = cur.fetchall()
    assert before == after


def test_write_features_writes_in_kickoff_order_regardless_of_input_order(conn):
    """Обрыв соединения на середине записи не должен оставлять «дырки» по
    датам из-за внутренней пересортировки build() по лиге/сезону (найдено
    на проде 30.09.2026: упавший SSH-туннель посреди записи)."""
    _seed_season(conn, n_matches=5)
    df = bf.build(conn)
    shuffled = df.sample(frac=1, random_state=42).reset_index(drop=True)  # имитация "не по датам"

    written_order = []
    real_executemany = None
    import psycopg as _psycopg

    original = _psycopg.Cursor.executemany

    def spy(self, sql, params_seq):
        params_seq = list(params_seq)
        written_order.extend(row[0] for row in params_seq)  # fixture_id — первая колонка
        return original(self, sql, params_seq)

    _psycopg.Cursor.executemany = spy
    try:
        bf.write_features(conn, shuffled, incremental=False)
    finally:
        _psycopg.Cursor.executemany = original

    kickoff_by_fixture = dict(zip(df["fixture_id"], df["kickoff_at"]))
    dates_written = [kickoff_by_fixture[fid] for fid in written_order]
    assert dates_written == sorted(dates_written)


def test_write_features_resilient_matches_write_features(conn):
    """Тот же результат, что и обычная запись — разница только в том, как
    держится соединение (одно на всё vs новое на пакет), не в данных."""
    _seed_season(conn, n_matches=10)
    df = bf.build(conn)

    written = bf.write_features_resilient(TEST_URL, df, incremental=False, batch_size=3)

    assert written == 10
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_match_features")
        assert cur.fetchone()[0] == 10


def test_write_features_resilient_retries_failed_batch(conn, monkeypatch):
    """Обрыв соединения на одном пакете повторяется с новым соединением,
    а не роняет всю запись (найдено на проде 30.09.2026)."""
    _seed_season(conn, n_matches=6)
    df = bf.build(conn)

    real_connect = bf.connect
    attempts = {"n": 0}

    def flaky_connect(url):
        attempts["n"] += 1
        if attempts["n"] == 2:  # второй пакет — первая попытка обрывается
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return real_connect(url)

    monkeypatch.setattr(bf, "connect", flaky_connect)
    written = bf.write_features_resilient(TEST_URL, df, incremental=False, batch_size=2, max_attempts=3)

    assert written == 6
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_match_features")
        assert cur.fetchone()[0] == 6


def test_incremental_writes_only_new_matches(conn):
    _seed_season(conn, n_matches=20)
    df = bf.build(conn)
    bf.write_features(conn, df, incremental=False)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id, goals_home, goals_away)
               VALUES (9999, 39, 2026, %s, %s, 'FT', 1, 2, 1, 0)""",
            (ts(1, month=6), ts(1, month=6).date()),
        )
    df2 = bf.build(conn)
    written = bf.write_features(conn, df2, incremental=True)
    assert written == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_match_features")
        assert cur.fetchone()[0] == 21


def test_elo_state_persisted_after_write(conn):
    _seed_season(conn, n_matches=5)
    df = bf.build(conn)
    bf.write_elo_state(conn, df, df.attrs["elo_final_ratings"])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_team_elo_state")
        assert cur.fetchone()[0] > 0


def test_write_elo_state_resilient_matches_batch_write(conn):
    """Пакетная версия (по факту переписанная после зависания на проде
    30.09.2026: одиночные соединения на пару team/league оказались
    непрактично медленными через SSH-туннель) даёт тот же результат."""
    _seed_season(conn, n_matches=8)
    df = bf.build(conn)
    ratings = df.attrs["elo_final_ratings"]

    written = bf.write_elo_state_resilient(TEST_URL, df, ratings, batch_size=3)

    assert written == len(ratings)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_team_elo_state")
        assert cur.fetchone()[0] == len(ratings)


def _insert_upcoming_fixture(conn, fixture_id, league_id, season, home, away, kickoff_at):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO fixtures (fixture_id, league_id, season, kickoff_at, match_date,
               status_short, home_team_id, away_team_id, goals_home, goals_away)
               VALUES (%s, %s, %s, %s, %s, 'NS', %s, %s, NULL, NULL)""",
            (fixture_id, league_id, season, kickoff_at, kickoff_at.date(), home, away),
        )


def test_load_upcoming_fixtures_respects_horizon(conn):
    _seed_season(conn, n_matches=5)
    now = datetime.now(timezone.utc)
    _insert_upcoming_fixture(conn, 9001, 39, 2026, 1, 2, now + timedelta(days=2))
    _insert_upcoming_fixture(conn, 9002, 39, 2026, 1, 2, now + timedelta(days=30))

    df = bf.load_upcoming_fixtures(conn, horizon_days=7)
    ids = set(df["fixture_id"])
    assert 9001 in ids
    assert 9002 not in ids  # за горизонтом


def test_load_season_history_scoped_to_affected_league_seasons(conn):
    """build_upcoming не должен читать всю базу (981к+ строк на проде) —
    только текущий сезон тех лиг, что реально есть в --upcoming наборе."""
    _seed_season(conn, league_id=39, season=2026, n_matches=5)
    _seed_season(conn, league_id=61, season=2026, n_matches=5, fixture_offset=100)  # другая лига

    df = bf.load_season_history(conn, [(39, 2026)])
    assert set(df["league_id"]) == {39}
    assert len(df) == 5


def test_load_season_history_empty_league_seasons_returns_empty_df():
    df = bf.load_season_history(conn=None, league_seasons=[])
    assert df.empty
    assert "fixture_id" in df.columns


def test_build_upcoming_returns_only_upcoming_rows_with_features_and_no_targets(conn):
    _seed_season(conn, n_matches=25)
    now = datetime.now(timezone.utc)
    _insert_upcoming_fixture(conn, 9001, 39, 2026, 1, 2, now + timedelta(days=2))

    df = bf.build_upcoming(conn, horizon_days=7)
    assert list(df["fixture_id"]) == [9001]  # только сама будущая строка, не история-основа
    upcoming = df.iloc[0]
    assert pd.isna(upcoming["reg_home"])
    assert upcoming["result_1x2"] is None
    assert pd.notna(upcoming["home_team_elo"])  # признак посчитан по сохранённому состоянию Эло


def test_build_upcoming_uses_persisted_elo_state_not_full_recompute(conn):
    """Ключевая оптимизация (ДП-9а): рейтинг берётся из ml_team_elo_state
    как есть, без последовательного прохода по всей истории — иначе
    --upcoming был бы так же тяжёл по памяти, как полный пересчёт."""
    _seed_season(conn, n_matches=5)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ml_team_elo_state (team_id, league_id, rating, as_of_fixture_id) VALUES (1, 39, 1777.0, 1000)"
        )
    now = datetime.now(timezone.utc)
    _insert_upcoming_fixture(conn, 9001, 39, 2026, 1, 2, now + timedelta(days=2))

    df = bf.build_upcoming(conn, horizon_days=7)
    assert float(df.iloc[0]["home_team_elo"]) == pytest.approx(1777.0)


def test_build_upcoming_empty_horizon_returns_empty_df_with_columns(conn):
    _seed_season(conn, n_matches=3)
    df = bf.build_upcoming(conn, horizon_days=2)  # нет NS-матчей в горизонте
    assert df.empty
    assert "fixture_id" in df.columns


def test_upcoming_mode_writes_only_unplayed_rows_without_touching_history(conn):
    _seed_season(conn, n_matches=5)
    now = datetime.now(timezone.utc)
    _insert_upcoming_fixture(conn, 9001, 39, 2026, 1, 2, now + timedelta(days=2))

    df = bf.build_upcoming(conn, horizon_days=7)
    assert list(df["fixture_id"]) == [9001]

    written = bf.write_features(conn, df, incremental=False)
    assert written == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_match_features")
        assert cur.fetchone()[0] == 1  # история отдельно не переписывалась этим прогоном
        cur.execute("SELECT result_1x2, home_team_elo FROM ml_match_features WHERE fixture_id = 9001")
        result, elo = cur.fetchone()
        assert result is None
        assert elo is not None


def test_main_upcoming_flag_writes_upcoming_and_skips_elo_state(conn, monkeypatch):
    _seed_season(conn, n_matches=5)
    now = datetime.now(timezone.utc)
    _insert_upcoming_fixture(conn, 9001, 39, 2026, 1, 2, now + timedelta(days=2))

    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    rc = bf.main(["--upcoming", "--horizon-days", "7"])
    assert rc == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_match_features")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM ml_team_elo_state")
        assert cur.fetchone()[0] == 0  # --upcoming не трогает состояние Эло


def test_main_rejects_upcoming_with_incremental():
    with pytest.raises(SystemExit):
        bf.main(["--upcoming", "--incremental"])


def test_injuries_counted_without_leaking_across_matches(conn):
    _seed_season(conn, n_matches=3)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO players (player_id, name) VALUES (1, 'X')")
        cur.execute(
            "INSERT INTO injuries (fixture_id, player_id, team_id, league_id, season) VALUES (1000, 1, 1, 39, 2026)"
        )
    df = bf.build(conn)
    bf.write_features(conn, df, incremental=False)
    with conn.cursor() as cur:
        cur.execute("SELECT injuries_home_count FROM ml_match_features WHERE fixture_id = 1000")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT injuries_home_count FROM ml_match_features WHERE fixture_id = 1001")
        assert cur.fetchone()[0] == 0
