"""Supervised background loops: failing cycles, dead loops, health and shutdown."""

import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from energy_optimizer.api.app import app
from energy_optimizer.background import (
    BackgroundLoops,
    LoopStatus,
    SupervisedLoop,
)
from energy_optimizer.control.ev_controller import ControlOutcome, EvController
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)
from fake_world import FakeWorld, running_fake_world
from test_ev_controller import NOON, configuration, save_schedule

FAST_SECONDS = 0.01


class LoopEnded(BaseException):
    """Escapes the supervision on purpose, as an internal defect would."""


class FailingOnce:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("the schedule file is locked")


async def _run_until(loops: BackgroundLoops, condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            break
        await asyncio.sleep(FAST_SECONDS)
    await loops.stop()


def test_a_failing_cycle_is_logged_and_retried_and_a_later_cycle_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cycle = FailingOnce()
    loop = SupervisedLoop("ev_control", cycle, FAST_SECONDS, retry_seconds=FAST_SECONDS)

    async def scenario() -> None:
        loops = BackgroundLoops()
        loops.start(loop)
        await _run_until(loops, lambda: cycle.calls >= 3)

    with caplog.at_level(logging.ERROR, logger="energy_optimizer.background"):
        asyncio.run(scenario())

    assert cycle.calls >= 3
    assert "event=background_cycle_failed" in caplog.text
    assert "loop=ev_control" in caplog.text
    assert loop.health.consecutive_failures == 0
    assert loop.health.last_error == "RuntimeError: the schedule file is locked"
    assert loop.health.last_success_at is not None


def test_the_wait_after_a_failure_is_bounded_by_the_retry_delay() -> None:
    loop = SupervisedLoop("orchestration", lambda: None, 3600, retry_seconds=60)

    assert loop.wait_after_failure_seconds() == 60
    assert SupervisedLoop("x", lambda: None, 5).wait_after_failure_seconds() == 5


def test_a_loop_that_ends_is_reported_and_shutdown_still_waits_for_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    healthy_calls: list[int] = []

    def defect() -> None:
        raise LoopEnded

    async def scenario() -> tuple[list[str], dict[str, LoopStatus]]:
        loops = BackgroundLoops()
        loops.start(SupervisedLoop("plug_in_reminder", defect, FAST_SECONDS))
        loops.start(
            SupervisedLoop(
                "orchestration", lambda: healthy_calls.append(1), FAST_SECONDS
            )
        )
        for _ in range(500):
            if loops.stopped_unexpectedly():
                break
            await asyncio.sleep(FAST_SECONDS)
        stopped = loops.stopped_unexpectedly()
        statuses = loops.statuses()
        await loops.stop()
        assert all(running.task.done() for running in loops.running.values())
        assert loops.stopped_unexpectedly() == []
        return stopped, statuses

    with caplog.at_level(logging.ERROR, logger="energy_optimizer.background"):
        stopped, statuses = asyncio.run(scenario())

    assert stopped == ["plug_in_reminder"]
    assert statuses["plug_in_reminder"].running is False
    assert statuses["orchestration"].running is True
    assert healthy_calls
    assert "event=background_loop_failed" in caplog.text


def test_shutdown_interrupts_a_long_wait_between_cycles() -> None:
    calls: list[int] = []

    async def scenario() -> float:
        loops = BackgroundLoops()
        loops.start(SupervisedLoop("orchestration", lambda: calls.append(1), 3600))
        while not calls:
            await asyncio.sleep(FAST_SECONDS)
        started = asyncio.get_running_loop().time()
        await loops.stop()
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 5


class LockedOnceStore(ProviderDataStore):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.failures_left = 1

    def load[ModelT](
        self, key: ProviderDataKey, adapter: TypeAdapter[ModelT]
    ) -> ModelT | None:
        if self.failures_left:
            self.failures_left -= 1
            raise ProviderDataStoreError("the EV schedule file is locked")
        return super().load(key, adapter)


@pytest.fixture
def world() -> Iterator[tuple[str, FakeWorld]]:
    with running_fake_world() as (url, fake):
        yield url, fake


def test_charger_control_recovers_after_the_schedule_cannot_be_read(
    world: tuple[str, FakeWorld], tmp_path: Path
) -> None:
    url, fake = world
    store = LockedOnceStore(tmp_path)
    save_schedule(store, (4.0,))
    controller = EvController(
        configuration(url), store, clock=lambda: NOON + timedelta(minutes=5)
    )
    outcomes: list[ControlOutcome] = []
    loop = SupervisedLoop(
        "ev_control",
        lambda: outcomes.append(controller.apply_due()),
        FAST_SECONDS,
        retry_seconds=FAST_SECONDS,
    )

    async def scenario() -> None:
        loops = BackgroundLoops()
        loops.start(loop)
        await _run_until(loops, lambda: bool(outcomes))

    asyncio.run(scenario())

    assert loop.health.last_error == (
        "ProviderDataStoreError: the EV schedule file is locked"
    )
    assert outcomes[0] is ControlOutcome.APPLIED
    assert fake.service_calls


class StoppedLoops:
    def stopped_unexpectedly(self) -> list[str]:
        return ["ev_control"]

    def statuses(self) -> dict[str, LoopStatus]:
        return {
            "ev_control": LoopStatus(
                running=False,
                stopped_unexpectedly=True,
                consecutive_failures=2,
                last_success_at=datetime(2026, 3, 10, 12, tzinfo=UTC),
                last_error="RuntimeError: boom",
            )
        }


@pytest.mark.usefixtures("minimal_configuration")
def test_health_reports_a_stopped_loop_as_degraded() -> None:
    with TestClient(app) as client:
        healthy = client.get("/health")
        started_loops = app.state.background_loops
        app.state.background_loops = StoppedLoops()
        degraded = client.get("/health")
        app.state.background_loops = started_loops

    assert healthy.status_code == 200
    assert healthy.json()["status"] == "ok"
    assert degraded.status_code == 503
    body = degraded.json()
    assert body["status"] == "degraded"
    assert body["background_loops"]["ev_control"] == {
        "running": False,
        "stopped_unexpectedly": True,
        "consecutive_failures": 2,
        "last_success_at": "2026-03-10T12:00:00Z",
        "last_error": "RuntimeError: boom",
    }
