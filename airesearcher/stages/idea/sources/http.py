"""受权限和速率约束的单次 HTTP 请求；等待由引擎调度。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx


class SourceWait(Exception):
    def __init__(self, seconds: float, reason: str):
        self.seconds = max(0.05, seconds)
        self.reason = reason
        super().__init__(reason)


class SourceFailure(ConnectionError):
    def __init__(self, message: str, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__(message)


def retry_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError):
            return 0


class GuardedHTTP:
    _locks: dict[str, threading.Lock] = {}
    _next: dict[str, float] = {}

    def __init__(self, guard: Callable, state: dict | None = None, client: httpx.Client | None = None,
                 clock: Callable = time.time):
        self.guard, self.state, self.client, self.clock = guard, state if state is not None else {}, client, clock

    def get(self, url: str, params: dict, *, interval: float, headers: dict | None = None,
            max_bytes: int | None = None) -> httpx.Response:
        host = urlsplit(url).hostname
        self.guard("network", host)
        lock = self._locks.setdefault(host, threading.Lock())
        if not lock.acquire(blocking=False):
            raise SourceWait(0.25, f"等待 {host} 的上一个请求结束")
        try:
            deadline = max(self.state.get(host, 0), self._next.get(host, 0))
            if self.clock() < deadline:
                raise SourceWait(deadline - self.clock(), f"{host} 请求节流")
            self.state[host] = self._next[host] = self.clock() + interval
            try:
                if self.client is not None:
                    if max_bytes is not None:
                        return self._bounded_get(self.client, url, params, headers, max_bytes)
                    return self.client.get(url, params=params, headers=headers, follow_redirects=False)
                with httpx.Client(timeout=25, follow_redirects=False,
                                  headers={"User-Agent": "ai-researcher-agent/0.1"}) as client:
                    if max_bytes is not None:
                        return self._bounded_get(client, url, params, headers, max_bytes)
                    return client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                # Never stringify request headers or credentials in logs/errors.
                raise SourceFailure(f"{host}: {type(exc).__name__}") from exc
        finally:
            lock.release()

    @staticmethod
    def _bounded_get(client, url, params, headers, maximum):
        with client.stream("GET", url, params=params, headers=headers, follow_redirects=False) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > maximum:
                    raise SourceFailure("响应文件超过允许大小")
                chunks.append(chunk)
            # iter_bytes already decoded Content-Encoding; avoid decoding the reconstructed body twice.
            kept = {k: v for k, v in response.headers.items() if k not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status_code, headers=kept, content=b"".join(chunks),
                                  request=response.request)


def require_success(response: httpx.Response) -> None:
    if not response.is_success:
        raise SourceFailure(f"{response.request.url.host}: HTTP {response.status_code}",
                            retry_seconds(response.headers.get("retry-after")))
