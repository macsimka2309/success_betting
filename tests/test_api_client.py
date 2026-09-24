"""Тесты клиента API-Football.

Транспорт, часы и сон подменены: тесты не обращаются к сети и не ждут
реальных секунд (НФТ-8). Каждый тест собирает свой ApiClient на временном
каталоге кэша.
"""

from __future__ import annotations

import json

import pytest

from src.api.client import ApiClient, HttpResponse


class FakeClock:
    """Управляемое время: каждый sleep(n) сразу сдвигает часы на n вперёд."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeTransport:
    """Очередь заранее заданных ответов или исключений, по одному на вызов."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> HttpResponse:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("транспорт вызван больше раз, чем ожидалось")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def json_response(payload: dict, status: int = 200, headers: dict | None = None) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


def make_client(tmp_path, transport, **kwargs) -> ApiClient:
    clock = FakeClock()
    return ApiClient(
        key="test-key",
        cache_dir=tmp_path / "cache",
        transport=transport,
        sleep=clock.sleep,
        now=clock.now,
        verbose=False,
        **kwargs,
    )


# --------------------------------------------------------------------- кэш


def test_successful_response_is_cached(tmp_path):
    transport = FakeTransport([json_response({"response": [1, 2, 3]})])
    client = make_client(tmp_path, transport)

    first = client.get("/fixtures", {"league": 39})
    second = client.get("/fixtures", {"league": 39})

    assert first == {"response": [1, 2, 3]}
    assert second == first
    assert len(transport.calls) == 1  # второй раз — из кэша, без сети
    assert client.requests_used == 1


def test_use_cache_false_bypasses_cache(tmp_path):
    transport = FakeTransport(
        [json_response({"response": [1]}), json_response({"response": [2]})]
    )
    client = make_client(tmp_path, transport)

    client.get("/fixtures", {"league": 39})
    second = client.get("/fixtures", {"league": 39}, use_cache=False)

    assert second == {"response": [2]}
    assert len(transport.calls) == 2


def test_error_response_is_not_cached(tmp_path):
    """Ошибка не по лимиту не должна застревать в кэше навсегда."""
    transport = FakeTransport(
        [
            json_response({"errors": {"token": "неверный ключ"}}),
            json_response({"response": [1]}),
        ]
    )
    client = make_client(tmp_path, transport)

    first = client.get("/fixtures", {"league": 39})
    second = client.get("/fixtures", {"league": 39})

    assert first == {"errors": {"token": "неверный ключ"}}
    assert second == {"response": [1]}
    assert len(transport.calls) == 2  # оба раза реально сходили в API


# ------------------------------------------------------------- бюджет и лимиты


def test_daily_budget_stops_without_spending_more(tmp_path):
    transport = FakeTransport(
        [json_response({"response": [1]}), json_response({"response": [2]})]
    )
    client = make_client(tmp_path, transport, max_requests=1)

    first = client.get("/fixtures", {"a": 1})
    second = client.get("/fixtures", {"a": 2})

    assert first == {"response": [1]}
    assert second is None
    assert client.requests_used == 1
    assert len(transport.calls) == 1


def test_daily_limit_from_api_stops_whole_session(tmp_path):
    transport = FakeTransport(
        [
            json_response({"errors": {"requests": "You have reached the request limit for the day"}}),
            json_response({"response": [1]}),
        ]
    )
    client = make_client(tmp_path, transport, max_requests=100)

    first = client.get("/fixtures", {"a": 1})
    second = client.get("/fixtures", {"a": 2})  # другой запрос — сессия уже остановлена

    assert first is None
    assert client.quota_exhausted is True
    assert second is None
    assert len(transport.calls) == 1  # второй вызов даже не пошёл в сеть


def test_per_minute_rate_limit_retries_same_call(tmp_path):
    transport = FakeTransport(
        [
            json_response({"errors": {"rateLimit": "Too many requests"}}),
            json_response({"response": [42]}),
        ]
    )
    client = make_client(tmp_path, transport)

    result = client.get("/fixtures", {"a": 1})

    assert result == {"response": [42]}
    assert len(transport.calls) == 2
    assert client.requests_used == 1  # это один и тот же запрос, не два


def test_http_429_is_retried(tmp_path):
    transport = FakeTransport(
        [
            HttpResponse(status=429, headers={}, body=b""),
            json_response({"response": [1]}),
        ]
    )
    client = make_client(tmp_path, transport)

    result = client.get("/fixtures", {"a": 1})

    assert result == {"response": [1]}
    assert len(transport.calls) == 2


def test_network_error_retried_then_gives_up(tmp_path):
    transport = FakeTransport([TimeoutError(), TimeoutError(), TimeoutError()])
    client = make_client(tmp_path, transport, max_attempts=3)

    result = client.get("/fixtures", {"a": 1})

    assert result is None
    assert len(transport.calls) == 3


def test_network_error_recovers_within_attempts(tmp_path):
    transport = FakeTransport([TimeoutError(), json_response({"response": [7]})])
    client = make_client(tmp_path, transport, max_attempts=3)

    result = client.get("/fixtures", {"a": 1})

    assert result == {"response": [7]}


def test_http_4xx_is_not_retried(tmp_path):
    transport = FakeTransport(
        [
            HttpResponse(status=404, headers={}, body=b"{}"),
            json_response({"response": [1]}),
        ]
    )
    client = make_client(tmp_path, transport)

    result = client.get("/fixtures", {"a": 1})

    assert result is None
    assert len(transport.calls) == 1  # 4xx не повторяем


def test_daily_remaining_captured_from_header(tmp_path):
    transport = FakeTransport(
        [
            json_response(
                {"response": [1]},
                headers={"x-ratelimit-requests-remaining": "7499"},
            )
        ]
    )
    client = make_client(tmp_path, transport)

    client.get("/fixtures", {"a": 1})

    assert client.daily_remaining == "7499"


# --------------------------------------------------------------- пагинация


def test_paged_collects_all_pages_without_page_param_on_first_call(tmp_path):
    transport = FakeTransport(
        [
            json_response({"response": [1, 2], "paging": {"current": 1, "total": 2}}),
            json_response({"response": [3, 4], "paging": {"current": 2, "total": 2}}),
        ]
    )
    client = make_client(tmp_path, transport)

    items = client.paged("/fixtures", {"date": "2026-09-23"})

    assert items == [1, 2, 3, 4]
    assert "page=" not in transport.calls[0]
    assert "page=2" in transport.calls[1]


def test_paged_stops_on_failed_page(tmp_path):
    transport = FakeTransport(
        [json_response({"response": [1], "paging": {"current": 1, "total": 3}})]
    )
    client = make_client(tmp_path, transport, max_requests=1)

    items = client.paged("/fixtures", {"date": "2026-09-23"})

    assert items == [1]  # вторая страница не пришла — бюджет исчерпан


# --------------------------------------------------------------- очистка кэша


def test_cleanup_removes_only_old_files(tmp_path):
    client = make_client(tmp_path, FakeTransport([]))
    old = client.cache_dir / "old.json"
    fresh = client.cache_dir / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")

    day = 86400
    old_time = client.now() - 31 * day
    import os

    os.utime(old, (old_time, old_time))

    removed = client.cleanup_cache(max_age_days=30)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
