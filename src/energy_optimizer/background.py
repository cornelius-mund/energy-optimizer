"""Background loops that survive failing cycles and report whether they still run.

Collection, wall box control and the plug-in reminder each repeat one cycle. A
cycle that raises is logged and retried after a bounded delay instead of ending
the loop: a locked file or a bug in one cycle must not silently stop charger
control for good. Shutdown cancels nothing it does not own and waits for every
loop, even when one of them failed.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RETRY_AFTER_FAILURE_SECONDS = 60.0
SHUTDOWN_TIMEOUT_SECONDS = 30.0

type Cycle = Callable[[], object]
type Clock = Callable[[], datetime]


@dataclass
class LoopHealth:
    """What a loop last did; a provider outage inside a cycle is not a failure."""

    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def record_success(self, moment: datetime) -> None:
        self.last_success_at = moment
        self.consecutive_failures = 0

    def record_failure(self, moment: datetime, error: Exception) -> None:
        self.last_failure_at = moment
        self.last_error = f"{error.__class__.__name__}: {error}"
        self.consecutive_failures += 1


@dataclass
class SupervisedLoop:
    """Run ``cycle`` in a worker thread every ``interval_seconds`` until stopped."""

    name: str
    cycle: Cycle
    interval_seconds: float
    retry_seconds: float = RETRY_AFTER_FAILURE_SECONDS
    clock: Clock = field(default=lambda: datetime.now(timezone.utc))
    health: LoopHealth = field(default_factory=LoopHealth)

    def wait_after_failure_seconds(self) -> float:
        return min(self.interval_seconds, self.retry_seconds)

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "event=background_loop_started component=background operation=run "
            "loop=%s interval_seconds=%s",
            self.name,
            self.interval_seconds,
        )
        while not stop_event.is_set():
            wait_seconds = await self._run_one_cycle()
            await _wait_unless_stopped(stop_event, wait_seconds)
        logger.info(
            "event=background_loop_stopped component=background operation=run loop=%s",
            self.name,
        )

    async def _run_one_cycle(self) -> float:
        try:
            await asyncio.to_thread(self.cycle)
        except Exception as error:
            self.health.record_failure(self.clock(), error)
            wait_seconds = self.wait_after_failure_seconds()
            logger.error(
                "event=background_cycle_failed component=background operation=run "
                "loop=%s error_type=%s error=%s consecutive_failures=%s "
                "retry_in_seconds=%s",
                self.name,
                error.__class__.__name__,
                error,
                self.health.consecutive_failures,
                wait_seconds,
                exc_info=True,
            )
            return wait_seconds
        self.health.record_success(self.clock())
        return self.interval_seconds


async def _wait_unless_stopped(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


@dataclass(frozen=True)
class LoopStatus:
    running: bool
    stopped_unexpectedly: bool
    consecutive_failures: int
    last_success_at: datetime | None
    last_error: str | None


@dataclass
class RunningLoop:
    loop: SupervisedLoop
    task: asyncio.Task[None]

    def has_stopped_unexpectedly(self, stopping: bool) -> bool:
        return self.task.done() and not stopping

    def status(self, stopping: bool) -> LoopStatus:
        health = self.loop.health
        return LoopStatus(
            running=not self.task.done(),
            stopped_unexpectedly=self.has_stopped_unexpectedly(stopping),
            consecutive_failures=health.consecutive_failures,
            last_success_at=health.last_success_at,
            last_error=health.last_error,
        )


@dataclass
class BackgroundLoops:
    """The loops the service started, so health can see them and shutdown can
    wait for all of them."""

    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    running: dict[str, RunningLoop] = field(default_factory=dict)

    def start(self, loop: SupervisedLoop) -> None:
        task = asyncio.create_task(loop.run(self.stop_event), name=loop.name)
        self.running[loop.name] = RunningLoop(loop, task)

    def is_stopping(self) -> bool:
        return self.stop_event.is_set()

    def stopped_unexpectedly(self) -> list[str]:
        return [
            name
            for name, running in self.running.items()
            if running.has_stopped_unexpectedly(self.is_stopping())
        ]

    def statuses(self) -> dict[str, LoopStatus]:
        return {
            name: running.status(self.is_stopping())
            for name, running in self.running.items()
        }

    async def stop(self) -> None:
        """Signal every loop, then wait for all; a loop that does not end in
        time is cancelled, and one that failed is logged, not re-raised, so
        the others are still awaited."""
        self.stop_event.set()
        tasks = [running.task for running in self.running.values()]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for name, running in self.running.items():
            if running.task.cancelled():
                continue
            error = running.task.exception()
            if error is not None:
                logger.error(
                    "event=background_loop_failed component=background "
                    "operation=stop loop=%s error_type=%s",
                    name,
                    error.__class__.__name__,
                    exc_info=error,
                )
