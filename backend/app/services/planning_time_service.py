"""FIFO-safe depart-at and arrive-by time contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from backend.app.schemas.benchmarks import PlanningTimeResult


TravelTimeFunction = Callable[[datetime], float]


def plan_depart_at(
    departure_time: datetime, travel_time_at: TravelTimeFunction
) -> PlanningTimeResult:
    if departure_time.tzinfo is None:
        raise ValueError("departure_time must include a timezone")
    duration = float(travel_time_at(departure_time))
    if duration <= 0:
        raise ValueError("travel time must be positive")
    return PlanningTimeResult(
        planning_mode="depart_at",
        departure_time=departure_time,
        arrival_time=departure_time + timedelta(seconds=duration),
        travel_time_seconds=duration,
        search_iterations=0,
    )


def plan_arrive_by(
    arrival_deadline: datetime,
    travel_time_at: TravelTimeFunction,
    *,
    search_window: timedelta = timedelta(hours=8),
    precision_seconds: float = 1.0,
) -> PlanningTimeResult:
    """Find the latest feasible departure under a FIFO time-dependent model."""

    if arrival_deadline.tzinfo is None:
        raise ValueError("arrival_deadline must include a timezone")
    lower = arrival_deadline - search_window
    upper = arrival_deadline
    if lower + timedelta(seconds=float(travel_time_at(lower))) > arrival_deadline:
        raise ValueError("No feasible departure exists inside the search window")
    iterations = 0
    while (upper - lower).total_seconds() > precision_seconds:
        midpoint = lower + (upper - lower) / 2
        duration = float(travel_time_at(midpoint))
        if duration <= 0:
            raise ValueError("travel time must be positive")
        if midpoint + timedelta(seconds=duration) <= arrival_deadline:
            lower = midpoint
        else:
            upper = midpoint
        iterations += 1
    duration = float(travel_time_at(lower))
    return PlanningTimeResult(
        planning_mode="arrive_by",
        departure_time=lower,
        arrival_time=lower + timedelta(seconds=duration),
        travel_time_seconds=duration,
        search_iterations=iterations,
    )
