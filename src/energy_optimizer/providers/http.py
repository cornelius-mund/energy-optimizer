"""Shared HTTP helpers for provider integrations."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

ErrorFactory = Callable[[str], Exception]
StatusErrorFactory = Callable[[int], Exception | None]


class JsonHttpClient:
    """Perform bounded JSON requests while leaving provider errors configurable."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        error_factory: ErrorFactory,
        timeout_message: str,
        transport_message: str,
        malformed_message: str,
        status_error: StatusErrorFactory | None,
        log_event: str,
        component: str,
        operation: str,
        log_context: str = "",
        success_log_level: int = logging.INFO,
        error_log_level: int = logging.WARNING,
    ) -> Any:
        """Fetch and decode JSON, translating transport failures for a provider."""
        started_at = perf_counter()
        status: int | str = "not_sent"
        context = f"{log_context} " if log_context else ""
        try:
            try:
                if self._client is not None:
                    response = self._client.get(
                        url,
                        headers=headers,
                        timeout=timeout_seconds,
                    )
                else:
                    with httpx.Client(timeout=timeout_seconds) as client:
                        response = client.get(url, headers=headers)
            except httpx.TimeoutException as error:
                status = "timeout"
                raise error_factory(timeout_message) from error
            except httpx.RequestError as error:
                status = "transport_error"
                raise error_factory(transport_message) from error

            status = response.status_code
            if status_error is not None:
                status_exception = status_error(response.status_code)
                if status_exception is not None:
                    raise status_exception
            if response.is_error:
                raise error_factory(
                    f"HTTP {response.status_code} response from {component}"
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise error_factory(malformed_message) from error
        except Exception as error:
            logger.log(
                error_log_level,
                "event=%s component=%s operation=%s %sstatus=%s duration_ms=%.1f "
                "error_type=%s",
                log_event,
                component,
                operation,
                context,
                status,
                (perf_counter() - started_at) * 1000,
                error.__class__.__name__,
            )
            raise
        logger.log(
            success_log_level,
            "event=%s component=%s operation=%s %sstatus=%s duration_ms=%.1f",
            log_event,
            component,
            operation,
            context,
            status,
            (perf_counter() - started_at) * 1000,
        )
        return payload
