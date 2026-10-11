"""House load profile from recorder history — pure stdlib (no Home Assistant import).

Input is the hourly cumulative ``sum`` statistic of the inverter's load energy
counter. Output is 48 half-hour kWh values per day type, used by the planner.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any

MAX_HOURLY_KWH = 25.0  # drop counter resets / spikes
# A day's weight halves every this many days, so the profile follows seasonal change
# (heating evenings in autumn) instead of averaging it away over the whole history.
HALF_LIFE_DAYS = 5.0


def hourly_consumption(points: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """Diff cumulative hourly sums into (hour_start_utc, kWh) pairs.

    ``points`` rows look like ``{"start": epoch_seconds, "mean": cumulative_sum}``
    (the shape returned by ``websocket_api._fetch_statistics_points``).
    """
    ordered = sorted(
        (p for p in points if p.get("start") is not None and p.get("mean") is not None),
        key=lambda p: float(p["start"]),
    )
    out: list[tuple[datetime, float]] = []
    for prev, cur in zip(ordered, ordered[1:]):
        if float(cur["start"]) - float(prev["start"]) != 3600:
            continue
        kwh = float(cur["mean"]) - float(prev["mean"])
        if kwh < 0 or kwh > MAX_HOURLY_KWH:
            continue
        # A statistics row's sum is the counter at the END of its hour.
        out.append((datetime.fromtimestamp(float(cur["start"]), tz=timezone.utc), kwh))
    return out


def build_load_profile(
    hourly: list[tuple[datetime, float]],
    tz: tzinfo,
    *,
    min_days_per_type: int = 4,
) -> dict[str, Any] | None:
    """Recency-weighted mean kWh per local half-hour; split weekday/weekend when there's enough history.

    A mean, not a median: the load counter steps in 0.1 kWh, so hourly values are mostly 0.3 or 0.4
    and the median sat on the lower one, and it ignored loads that come most but not all nights.
    """
    buckets: dict[str, list[list[tuple[float, float]]]] = {
        "weekday": [[] for _ in range(24)],
        "weekend": [[] for _ in range(24)],
        "all": [[] for _ in range(24)],
    }
    days: dict[str, set] = {"weekday": set(), "weekend": set(), "all": set()}
    newest = max((start for start, _ in hourly), default=None)
    for start, kwh in hourly:
        local = start.astimezone(tz)
        kind = "weekend" if local.weekday() >= 5 else "weekday"
        age_days = (newest - start).total_seconds() / 86400.0
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        for key in (kind, "all"):
            buckets[key][local.hour].append((kwh, weight))
            days[key].add(local.date())
    if not days["all"]:
        return None

    def halves(hours: list[list[tuple[float, float]]]) -> list[float] | None:
        if any(not h for h in hours):
            return None
        out: list[float] = []
        for values in hours:
            mean = sum(k * w for k, w in values) / sum(w for _, w in values)
            half = round(mean / 2.0, 4)
            out.extend([half, half])
        return out

    profile: dict[str, Any] = {"days": len(days["all"])}
    all_profile = halves(buckets["all"])
    if all_profile is None:
        return None
    profile["all"] = all_profile
    for key in ("weekday", "weekend"):
        if len(days[key]) >= min_days_per_type:
            values = halves(buckets[key])
            if values is not None:
                profile[key] = values
    profile["daily_kwh"] = round(sum(all_profile), 2)
    return profile


def average_load_kw(profile: dict[str, Any] | None, fallback_kw: float) -> float:
    if not profile or not isinstance(profile.get("all"), list):
        return max(0.0, fallback_kw)
    return max(0.0, sum(float(v) for v in profile["all"]) / 24.0)
