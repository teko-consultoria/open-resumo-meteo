"""Bounded retry policy shared by the public object-store clients."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

DEFAULT_MAX_ATTEMPTS = 2
MAX_CONFIGURABLE_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 10.0


def _retryable_status(status_code: object) -> bool:
    return isinstance(status_code, int) and (
        status_code == 429 or 500 <= status_code <= 599
    )


def _retry_after_seconds(
    response: Any,
    *,
    fallback_seconds: float,
    max_delay_seconds: float,
    now_fn: Callable[[], datetime],
) -> float:
    headers = getattr(response, "headers", None) or {}
    raw_value = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    delay: float | None = None
    if raw_value:
        try:
            delay = float(raw_value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw_value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                now = now_fn()
                if now.tzinfo is None:
                    now = now.replace(tzinfo=UTC)
                delay = (retry_at - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = None
    if delay is not None and not math.isfinite(delay):
        delay = None
    effective = fallback_seconds if delay is None else delay
    return min(max(float(effective), 0.0), float(max_delay_seconds))


def request_with_status_retry(
    request_fn: Callable[[], Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Any:
    attempts = int(max_attempts)
    if not 1 <= attempts <= MAX_CONFIGURABLE_ATTEMPTS:
        raise ValueError(f"max_attempts deve estar entre 1 e {MAX_CONFIGURABLE_ATTEMPTS}")
    base_delay = float(backoff_seconds)
    maximum_delay = float(max_delay_seconds)
    if (
        not math.isfinite(base_delay)
        or not math.isfinite(maximum_delay)
        or base_delay < 0
        or maximum_delay < 0
    ):
        raise ValueError("delays de retry devem ser finitos e nao negativos")

    for attempt_index in range(attempts):
        response = request_fn()
        if (
            not _retryable_status(getattr(response, "status_code", None))
            or attempt_index + 1 >= attempts
        ):
            return response
        fallback = min(base_delay * (2**attempt_index), maximum_delay)
        delay = _retry_after_seconds(
            response,
            fallback_seconds=fallback,
            max_delay_seconds=maximum_delay,
            now_fn=now_fn,
        )
        close = getattr(response, "close", None)
        if callable(close):
            close()
        sleep_fn(delay)
    raise AssertionError("loop de retry terminou sem resposta")


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY_SECONDS",
    "request_with_status_retry",
]
