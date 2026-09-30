"""HTTP middleware for request context and outcome logging."""

import logging
from time import perf_counter
from typing import Awaitable, Callable
from uuid import uuid4

from fastapi import Request
from starlette.responses import Response

logger = logging.getLogger("energy_optimizer.api")


def _request_id(request: Request) -> str:
    """Return a safe request ID for logs and the response header."""
    candidate = request.headers.get("X-Request-ID", "")
    if (
        candidate
        and len(candidate) <= 64
        and all(character.isalnum() or character in "-_." for character in candidate)
    ):
        return candidate
    return uuid4().hex


async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log request outcomes without recording bodies or authorization headers."""
    request_id = _request_id(request)
    request.state.request_id = request_id
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "event=request_failed component=api operation=request method=%s "
            "path=%s request_id=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            request_id,
            (perf_counter() - started) * 1000,
        )
        raise

    duration_ms = (perf_counter() - started) * 1000
    status_code = response.status_code
    health_check = request.method == "GET" and request.url.path == "/health"
    if status_code >= 500:
        level = logging.ERROR
    elif status_code >= 400:
        level = logging.WARNING
    elif health_check:
        level = logging.DEBUG
    else:
        level = logging.INFO
    message = "event=%s component=api operation=request method=%s path=%s"
    details: list[object] = [
        "health_check_request" if health_check else "request_completed",
        request.method,
        request.url.path,
    ]
    if request.url.path == "/api/v1/dashboard/data":
        scenario_kind = request.query_params.get("scenario_kind", "actual")
        if scenario_kind not in {"actual", "forecast", "plan"}:
            scenario_kind = "invalid"
        message += " scenario_kind=%s"
        details.append(scenario_kind)
    logger.log(
        level,
        message + " status=%s request_id=%s duration_ms=%.2f",
        *details,
        status_code,
        request_id,
        duration_ms,
    )
    response.headers["X-Request-ID"] = request_id
    return response
