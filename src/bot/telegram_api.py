"""Минимальный клиент Telegram Bot API для бота реестра ставок.

Запросы идут через curl (он умеет SOCKS-прокси, нужный с нашего сервера); токен
передаётся конфигом через stdin, а не в аргументах. Тексты ошибок не содержат адреса.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from src.jobs.live_alerts import _curl_quote

API_HOST = "https://api.telegram.org"


class ApiError(RuntimeError):
    """Ошибка вызова Telegram. Токена в тексте нет."""


Runner = Callable[[list[str], str, int], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], stdin: str, timeout: int) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)


class TelegramApi:
    def __init__(self, token: str, proxy: str | None = None, runner: Runner = _run) -> None:
        self._token = token
        self._proxy = proxy
        self._runner = runner

    def request(self, method: str, params: dict[str, str], timeout: int) -> tuple[list[str], str]:
        argv = ["curl", "-sS", "-m", str(timeout), "-K", "-"]
        if self._proxy:
            argv += ["--proxy", self._proxy]
        lines = [f'url = "{API_HOST}/bot{_curl_quote(self._token)}/{method}"']
        lines += [f'data-urlencode = "{_curl_quote(k)}={_curl_quote(v)}"' for k, v in params.items()]
        return argv, "\n".join(lines) + "\n"

    def call(self, method: str, params: dict[str, str] | None = None, timeout: int = 15):
        argv, stdin = self.request(method, params or {}, timeout)
        try:
            proc = self._runner(argv, stdin, timeout + 10)
        except subprocess.TimeoutExpired:
            raise ApiError("таймаут") from None
        if proc.returncode != 0:
            raise ApiError(f"curl завершился с кодом {proc.returncode}")
        try:
            body = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise ApiError("ответ не похож на JSON") from None
        if not body.get("ok"):
            raise ApiError(f"{body.get('error_code')}: {body.get('description', '')}")
        return body.get("result")

    def get_updates(self, offset: int, poll_seconds: int = 30) -> list[dict]:
        return self.call(
            "getUpdates",
            {"offset": str(offset), "timeout": str(poll_seconds), "allowed_updates": '["message","callback_query"]'},
            timeout=poll_seconds + 15,
        ) or []

    def send(self, chat_id: int, text: str, reply_markup: dict | None = None, html: bool = False) -> None:
        params = {"chat_id": str(chat_id), "text": text}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if html:
            params["parse_mode"] = "HTML"
        self.call("sendMessage", params)

    def answer_callback(self, callback_id: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id})
