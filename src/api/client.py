"""Клиент API-Football.

Стадия 1 потока данных (ADR-2): только скачивание сырых ответов в кэш на
диск, без разбора. Разбор в строки таблиц делают шаги сбора (`src/jobs`),
читая из кэша — так повторный разбор не тратит лимит API.

Логика лимитов и повторов во многом повторяет проверенный
`scripts/legacy/download_apifootball.py`: два разных вида отказа со стороны
API нужно различать.

- **Лимит в минуту** приходит как HTTP 200 с
  `errors={"rateLimit": "Too many requests..."}` — это временно, стоит
  подождать и повторить тот же запрос.
- **Суточный лимит** приходит как `errors={"requests": "...limit for the
  day..."}` — это не пройдёт до сброса в 00:00 UTC по счётчику API, поэтому
  вся сессия клиента останавливается, а не только один запрос (ФТ-5).

Дневной бюджет (`max_requests`) — наш собственный резерв, меньше лимита
тарифа: он даёт остановиться, не исчерпав API-шный счётчик полностью, и
оставляет запас на повторные попытки в тот же день.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BASE_URL = "https://v3.football.api-sports.io"

# Ключ заголовка, где API сообщает остаток суточного лимита.
REMAINING_HEADER = "x-ratelimit-requests-remaining"


@dataclass
class HttpResponse:
    """Минимальный ответ HTTP, независимый от urllib — для тестов."""

    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, dict[str, str], float], HttpResponse]


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
    """Транспорт по умолчанию: обычный HTTP-запрос через стандартную библиотеку."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(
            status=error.code,
            headers=dict(error.headers.items()) if error.headers else {},
            body=error.read() if error.fp else b"",
        )


def _is_rate_limited(body: dict) -> bool:
    """Лимит в минуту: HTTP 200 с errors={'rateLimit': '...'}."""
    errors = body.get("errors")
    if isinstance(errors, dict):
        for key, value in errors.items():
            if "ratelimit" in str(key).lower() or "too many requests" in str(value).lower():
                return True
    return False


def _is_daily_limit_hit(body: dict) -> bool:
    """Суточный лимит: не сбросится до 00:00 UTC, останавливаем всю сессию."""
    errors = body.get("errors")
    if isinstance(errors, dict):
        return any("limit for the day" in str(v).lower() for v in errors.values())
    return False


@dataclass
class ApiClient:
    """Один клиент — один прогон сбора: свой счётчик и свой кэш.

    Все параметры, которые в тестах нужно подменить (транспорт, часы, сон),
    вынесены в поля, а не жёстко зашиты, чтобы тесты не обращались к сети
    и не ждали реальных секунд (НФТ-8).
    """

    key: str
    cache_dir: Path
    max_requests: int = 7_000
    requests_per_minute: int = 300
    timeout: float = 30.0
    max_attempts: int = 5
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    verbose: bool = True

    requests_used: int = field(default=0, init=False)
    daily_remaining: str | None = field(default=None, init=False)
    quota_exhausted: bool = field(default=False, init=False)
    _last_send: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ кэш

    def _cache_path(self, endpoint: str, params: dict) -> Path:
        query = urllib.parse.urlencode(sorted(params.items()))
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{endpoint.strip('/')}__{query}")
        return self.cache_dir / f"{safe}.json"

    def cached(self, endpoint: str, params: dict) -> dict | None:
        """Читает ответ из кэша, если он там есть, без обращения к API."""
        path = self._cache_path(endpoint, params)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None  # повреждённый файл — запросим заново

    def cleanup_cache(self, max_age_days: int = 30) -> int:
        """Удаляет файлы кэша старше срока (ADR-2, 30 дней). Возвращает число удалённых."""
        cutoff = self.now() - max_age_days * 86400
        removed = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------ запросы

    def _pace(self) -> None:
        """Не превышать лимит запросов в минуту (Pro-тариф)."""
        min_interval = 60.0 / max(1, self.requests_per_minute)
        wait = min_interval - (self.now() - self._last_send)
        if wait > 0:
            self.sleep(wait)
        self._last_send = self.now()

    def get(self, endpoint: str, params: dict | None = None, use_cache: bool = True) -> dict | None:
        """Один вызов API. Возвращает разобранное тело ответа или None.

        None означает: суточный бюджет исчерпан, суточный лимит API исчерпан,
        либо запрос не удался после всех попыток. Ни один из этих случаев
        не тратит кэш понапрасну — ошибка не кэшируется (иначе временный сбой
        застрял бы в кэше навсегда).
        """
        params = params or {}
        if use_cache:
            cached = self.cached(endpoint, params)
            if cached is not None:
                return cached

        if self.quota_exhausted:
            return None
        if self.requests_used >= self.max_requests:
            if self.verbose:
                print(
                    f"  [бюджет] достигнут max_requests={self.max_requests}; "
                    "останавливаюсь. Повторный запуск продолжит с этого места."
                )
            return None

        self.requests_used += 1
        body = self._fetch_with_retries(endpoint, params)
        if body is None:
            return None

        if body.get("errors"):
            # Ошибка не по лимиту (авторизация, неверный параметр) — не кэшируем,
            # иначе временная проблема застряла бы в кэше навсегда.
            if self.verbose:
                print(f"  ! ошибка API на {endpoint} {params}: {body['errors']}")
            return body

        self._write_cache(endpoint, params, body)
        return body

    def _write_cache(self, endpoint: str, params: dict, body: dict) -> None:
        try:
            self._cache_path(endpoint, params).write_text(
                json.dumps(body, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as error:
            # Запись в кэш — не гарантия, а оптимизация: не записалось — просто
            # запросим заново в следующий раз, а не потеряем всю сессию сбора.
            if self.verbose:
                print(f"  ~ кэш не записан ({type(error).__name__}): {endpoint}")

    def _fetch_with_retries(self, endpoint: str, params: dict) -> dict | None:
        url = f"{BASE_URL}{endpoint}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"
        headers = {"x-apisports-key": self.key}

        body: dict | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                response = self.transport(url, headers, self.timeout)
            except (OSError, TimeoutError) as error:
                if attempt == self.max_attempts:
                    if self.verbose:
                        print(f"  ! сетевая ошибка на {endpoint} {params}: {error}")
                    return None
                self.sleep(3 * attempt)
                continue

            if response.status == 429:
                self.sleep(2 * attempt)
                continue
            if response.status >= 400:
                if self.verbose:
                    print(f"  ! HTTP {response.status} на {endpoint} {params}")
                return None

            try:
                body = json.loads(response.body)
            except ValueError:
                if attempt == self.max_attempts:
                    if self.verbose:
                        print(f"  ! неразбираемый ответ на {endpoint} {params}")
                    return None
                self.sleep(2 * attempt)
                continue

            self.daily_remaining = response.headers.get(REMAINING_HEADER, self.daily_remaining)

            if _is_rate_limited(body):
                body = None
                self.sleep(2 * attempt)
                continue
            if _is_daily_limit_hit(body):
                self.quota_exhausted = True
                if self.verbose:
                    print(
                        "  [суточный лимит] API сообщил об исчерпании дневной квоты — "
                        "останавливаю сессию. Продолжится после сброса в 00:00 UTC."
                    )
                return None
            break

        if body is None and self.verbose:
            print(f"  ! не удалось получить {endpoint} {params} за {self.max_attempts} попыток")
        return body

    # ------------------------------------------------------------ постраничные

    def paged(self, endpoint: str, params: dict, use_cache: bool = True) -> list[Any]:
        """Отдаёт все элементы `response` по всем страницам.

        Первый вызов идёт без параметра `page`: часть эндпоинтов (например
        `/leagues`) не постраничные и отвергают этот параметр, а страница
        добавляется только со второй, для тех, что реально пагинируют.
        """
        items: list[Any] = []
        page = 1
        while True:
            call_params = dict(params)
            if page > 1:
                call_params["page"] = page
            body = self.get(endpoint, call_params, use_cache=use_cache)
            if body is None:
                break
            items.extend(body.get("response") or [])
            paging = body.get("paging") or {}
            total = paging.get("total") or 1
            current = paging.get("current") or 1
            if current >= total:
                break
            page = current + 1
        return items
