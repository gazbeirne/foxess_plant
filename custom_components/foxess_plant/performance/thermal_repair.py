"""Panel temperatures in ledger days written before the thermal lag existed (0.9.510).

Those days took their min / max / daylight average from the instantaneous model, which jumped 24 -> 58 °C with every
cloud (13 Oct 2026 showed "panel temp 16-72°C" while its smoothed chart peaked at 58). The raw 5-minute samples are
still stored, so each such day is re-smoothed exactly like the chart does (performance_chart._smooth_unlagged_points)
and its temperature fields, temperature-adjusted index and insight line rebuilt. Samples taken after the lag started
are already lagged and stay as they are, so re-running gives the same result: no "done" flag needed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)
LOOKBACK_DAYS = 8  # intraday samples are purged after 8 days (tick.py); older days keep their figures


def _day_samples(store: Any, day: str) -> list[dict[str, Any]]:
    return store.list_intraday_samples(f"{day}T00:00", f"{day}T99")


def smoothed_day_temps(
    samples: list[dict[str, Any]], *, lagged_since_ms: float | None
) -> tuple[float | None, float | None, float | None]:
    """(min, max, daylight average) of a day's panel temperature, smoothed like the chart."""
    from ..performance_chart import _smooth_unlagged_points

    points, pv_at = [], {}
    for s in samples:
        temp = s.get("virtual_panel_temp_c")
        try:
            t = datetime.fromisoformat(str(s["ts"])).timestamp() * 1000
        except (KeyError, TypeError, ValueError):
            continue
        if temp is None:
            continue
        points.append({"t": t, "v": float(temp)})
        pv_at[t] = s.get("pv_power_kw")
    smoothed = _smooth_unlagged_points(points, lagged_since_ms)
    if not smoothed:
        return None, None, None
    values = [p["v"] for p in smoothed]
    daylight = [p["v"] for p in smoothed if (pv_at.get(p["t"]) or 0.0) > 0.1]  # same rule as the accumulator
    avg = sum(daylight) / len(daylight) if daylight else None
    return round(min(values), 1), round(max(values), 1), avg


def repair_row(row: dict[str, Any], samples: list[dict[str, Any]], *, lagged_since_ms: float | None) -> dict[str, Any] | None:
    """The ledger row with its panel-temperature fields rebuilt, or None when nothing changes."""
    from .solar_analysis import build_daily_insight, compute_temp_adjusted_index_pct

    t_min, t_max, avg = smoothed_day_temps(samples, lagged_since_ms=lagged_since_ms)
    if t_max is None:
        return None
    index = compute_temp_adjusted_index_pct(forecast_accuracy_pct=row.get("forecast_accuracy_pct"), avg_virtual_temp_c=avg)
    insight = build_daily_insight(
        forecast_accuracy_pct=row.get("forecast_accuracy_pct"),
        peak_vs_rated_pct=row.get("peak_vs_rated_pct"),
        solar_day_class=str(row.get("solar_day_class") or ""),
        virtual_temp_min_c=t_min,
        virtual_temp_max_c=t_max,
        clipping_loss_kwh=row.get("clipping_loss_kwh"),
        temp_adjusted_index_pct=index,
        soiling_recovery_note=row.get("soiling_recovery_note"),
        visibility_avg_km=row.get("visibility_avg_km"),
    )
    new = {
        **row,
        "virtual_temp_min_c": t_min,
        "virtual_temp_max_c": t_max,
        "temp_adjusted_index_pct": index,
        "insight_note": insight,
        "wind_correlation_note": insight,
    }
    keys = ("virtual_temp_min_c", "virtual_temp_max_c", "temp_adjusted_index_pct", "insight_note")
    return new if any(new[k] != row.get(k) for k in keys) else None


def repair_ledger_panel_temps(store: Any, *, lag_since: str | None, today: date) -> list[str]:
    """Rebuild the panel temperatures of ledger days up to the day the thermal lag started. Returns repaired days."""
    from homeassistant.util import dt as dt_util

    if not lag_since:
        return []
    since = dt_util.parse_datetime(lag_since)
    if since is None:
        return []
    since_ms = since.timestamp() * 1000
    last_day = min(dt_util.as_local(since).date(), today - timedelta(days=1))
    first_day = last_day - timedelta(days=LOOKBACK_DAYS)
    repaired = []
    for row in store.list_ledger_between(first_day.isoformat(), last_day.isoformat()):
        if row.get("virtual_temp_max_c") is None:
            continue
        samples = _day_samples(store, row["date"])
        if not samples:
            continue
        new = repair_row(row, samples, lagged_since_ms=since_ms)
        if new is not None:
            store.upsert_daily_ledger(new)
            repaired.append(row["date"])
    if repaired:
        _LOGGER.info("Panel temperatures rebuilt with the thermal lag for %s", ", ".join(repaired))
    return repaired
