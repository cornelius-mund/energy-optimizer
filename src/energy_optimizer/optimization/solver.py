"""The boundary between the application and the optimization technology."""

from typing import Protocol

from energy_optimizer.optimization.problem import Plan, PlanningProblem


class PlanSolver(Protocol):
    """Calculates the cheapest schedule for a planning problem.

    The application depends on this port only, so the modelling library behind
    it can be replaced without touching the API or the orchestration.
    """

    def solve(self, problem: PlanningProblem) -> Plan: ...
