"""One "PV Forecast" line for the Solar Output chart: the forecast SmartCharge was using at each moment.

SmartCharge uses Solcast while its last good fetch is under 24 h old, otherwise the Open-Meteo blend
(open_meteo.combine_forecasts). The chart applies the same rule per 5-minute slot, using the fetch times of
the saved Solcast polls, so a day where Solcast stopped updating switches to Open-Meteo where it did.
Open-Meteo is hourly (one point mid-hour); slots in between are interpolated so the line is smooth.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Any

SOLCAST = "Solcast"
OPEN_METEO = "Open-Meteo"
FRESH_MS = 24 * 3_600_000
STEP_MS = 5 * 60_000


def _sorted(points: list[dict[str, float]] | None) -> list[dict[str, float]]:
    return sorted((p for p in points or [] if p.get("v") is not None), key=lambda p: p["t"])


def _interpolate(points: list[dict[str, float]], times: list[float], t: float) -> float | None:
    """Linear between neighbours; None outside the points' span."""
    if not points or t < times[0] or t > times[-1]:
        return None
    i = bisect_left(times, t)
    if times[i] == t:
        return float(points[i]["v"])
    a, b = points[i - 1], points[i]
    span = b["t"] - a["t"]
    return float(a["v"]) + (float(b["v"]) - float(a["v"])) * (t - a["t"]) / span if span > 0 else float(a["v"])


def _nearest(points: list[dict[str, float]], times: list[float], t: float, within_ms: float) -> float | None:
    """The point at this slot (Solcast slots no poll covered are missing: don't bridge them)."""
    if not points:
        return None
    i = bisect_left(times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(times) and abs(times[j] - t) <= within_ms and (best is None or abs(times[j] - t) < abs(times[best] - t)):
            best = j
    return float(points[best]["v"]) if best is not None else None


def solcast_fresh_at(fetches_ms: list[float], t_ms: float, now_ms: float) -> bool:
    """Was Solcast's latest poll at that moment (or now, for the future) under 24 h old?"""
    at = min(t_ms, now_ms)
    i = bisect_right(fetches_ms, at)
    return i > 0 and at - fetches_ms[i - 1] < FRESH_MS


def pv_forecast_line(
    *,
    solcast_points: list[dict[str, float]] | None,
    solcast_fetches_ms: list[float],
    open_meteo_points: list[dict[str, float]] | None,
    start_ms: float,
    end_ms: float,
    now_ms: float,
) -> tuple[list[dict[str, float]], list[str]]:
    """(5-minute kW points, the providers used in order of first use); ([], []) when there's nothing to draw."""
    sc = _sorted(solcast_points)
    sc_t = [p["t"] for p in sc]
    om = _sorted(open_meteo_points)
    om_t = [p["t"] for p in om]
    fetches = sorted(solcast_fetches_ms)
    out: list[dict[str, float]] = []
    used: list[str] = []
    t = start_ms
    while t <= end_ms:
        sc_v = _nearest(sc, sc_t, t, STEP_MS / 2)
        om_v = _interpolate(om, om_t, t)
        if sc_v is not None and (om_v is None or solcast_fresh_at(fetches, t, now_ms)):
            value, source = sc_v, SOLCAST
        elif om_v is not None:
            value, source = om_v, OPEN_METEO
        else:
            value, source = None, None
        if value is not None:
            out.append({"t": t, "v": round(max(0.0, value), 3)})
            if value > 0 and source not in used:  # night zeros don't name a provider
                used.append(source)
        t += STEP_MS
    if not any(p["v"] > 0 for p in out):
        return [], []
    return out, used


def interpolate_now(points: list[dict[str, Any]], t_ms: float) -> float | None:
    """kW at ``t_ms`` from hourly mid-hour points (for the live sample)."""
    pts = _sorted(points)
    return _interpolate(pts, [p["t"] for p in pts], t_ms)
