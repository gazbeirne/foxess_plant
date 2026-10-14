"""Open-Meteo PV forecast: several weather models, scored against this plant's real PV and blended.

Open-Meteo (open-meteo.com) is free for non-commercial use and needs no key. It gives the irradiance on a
tilted plane (``global_tilted_irradiance``, W/m², mean over the hour ending at each timestamp). Fox Plant turns
that into kW with the PV Configuration (panels × watts × efficiency) and a performance factor learned from
the plant's actual output.

Scores come from Open-Meteo's previous-runs API: what each model forecast about 24 h before each past day
(``global_tilted_irradiance_previous_day1``). That is the forecast SmartCharge works from overnight, and it is
available for the last few weeks straight away, so nothing has to be collected first.

Rows use the same shape as Fox Plant's merged Solcast rows (``period_start == period_end`` = the END of each
30-minute period, ``pv_estimate`` in kW), so the planner treats both sources alike.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from statistics import mean
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Weather models Open-Meteo serves with tilted irradiance and day-ahead history. The "seamless" ones fall back
# to the provider's global model outside its high-resolution area, so every model works anywhere.
MODEL_LABELS: dict[str, str] = {
    "ecmwf_ifs025": "ECMWF IFS",
    "ecmwf_aifs025_single": "ECMWF AI (AIFS)",
    "gfs_seamless": "GFS (NOAA)",
    "icon_seamless": "ICON (DWD)",
    "ukmo_seamless": "UK Met Office",
    "meteofrance_seamless": "Météo-France",
    "knmi_seamless": "KNMI",
    "dmi_seamless": "DMI",
}
DEFAULT_MODELS: tuple[str, ...] = tuple(MODEL_LABELS)

# Before a model has enough scored days: typical share of nameplate × irradiance that reaches the inverter
DEFAULT_PERFORMANCE_FACTOR = 0.85
SCORE_DAYS = 30
MIN_SCORED_DAYS = 7
# A model's error is floored here when weighting, so one lucky week can't take the whole blend
MIN_WEIGHT_MAE_KWH = 0.5
FORECAST_REFRESH = timedelta(hours=1)
SCORE_REFRESH = timedelta(hours=12)
# Solcast rows older than this are treated as failed: Open-Meteo goes first
SOLCAST_FRESH = timedelta(hours=24)
SOURCE = "open_meteo"
HALF_HOUR = timedelta(minutes=30)


class OpenMeteoError(Exception):
    """Open-Meteo request failed."""


@dataclass(frozen=True)
class PvArray:
    """One tilt/azimuth group. ``azimuth`` uses Open-Meteo's convention: 0 = south, -90 = east, 90 = west."""

    tilt: int
    azimuth: int
    kwp: float


def open_meteo_azimuth(compass_deg: float) -> int:
    """Compass bearing the panels face (180 = south) → Open-Meteo azimuth (0 = south, -90 = east)."""
    value = (float(compass_deg) - 180.0 + 180.0) % 360.0 - 180.0
    return int(round(value))


def plant_arrays(pv_config: Any) -> list[PvArray]:
    """Enabled PV strings grouped by tilt/azimuth, sized from PV Configuration (efficiency included)."""
    groups: dict[tuple[int, int], float] = {}
    for key in ("pv1", "pv2"):
        string = getattr(pv_config, key, None)
        if string is None or not string.enabled or string.panel_count <= 0:
            continue
        tilt = max(0, min(90, int(round(float(string.tilt)))))
        azimuth = open_meteo_azimuth(string.azimuth)
        groups[(tilt, azimuth)] = groups.get((tilt, azimuth), 0.0) + string.effective_dc_w / 1000.0
    return [PvArray(t, a, round(k, 3)) for (t, a), k in sorted(groups.items()) if k > 0]


def _series(payload: dict[str, Any], variable: str, models: list[str]) -> dict[str, dict[int, float]]:
    """{model: {unix end-of-hour: W/m²}} from one response. A single-model reply has no suffix on the key."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    out: dict[str, dict[int, float]] = {}
    for model in models:
        values = hourly.get(f"{variable}_{model}")
        if values is None and len(models) == 1:
            values = hourly.get(variable)
        if not isinstance(values, list):
            continue
        series = {int(t): float(v) for t, v in zip(times, values) if v is not None}
        if series:
            out[model] = series
    return out


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            body = await resp.text()
            if resp.status != 200:
                reason = body[:200]
                try:
                    reason = json.loads(body).get("reason") or reason
                except (ValueError, AttributeError):
                    pass
                raise OpenMeteoError(f"Open-Meteo HTTP {resp.status}: {reason}")
            data = json.loads(body)
    except aiohttp.ClientError as err:
        raise OpenMeteoError(f"Open-Meteo unreachable: {err}") from err
    except TimeoutError as err:
        raise OpenMeteoError("Open-Meteo timed out") from err
    if not isinstance(data, dict):
        raise OpenMeteoError("Unexpected Open-Meteo response")
    return data


def _base_params(lat: float, lon: float, array: PvArray, models: list[str]) -> dict[str, Any]:
    return {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "tilt": array.tilt,
        "azimuth": array.azimuth,
        "models": ",".join(models),
        "timeformat": "unixtime",
        "timezone": "GMT",
    }


async def async_fetch_forecast(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    arrays: list[PvArray],
    models: list[str],
) -> dict[str, dict[int, float]]:
    """{model: {unix end-of-hour: nameplate kW}} for yesterday → +2 days (one request per array)."""
    totals: dict[str, dict[int, float]] = {}
    for array in arrays:
        params = {
            **_base_params(lat, lon, array, models),
            "hourly": "global_tilted_irradiance",
            "past_days": 1,
            "forecast_days": 3,
        }
        payload = await _get_json(session, FORECAST_URL, params)
        for model, series in _series(payload, "global_tilted_irradiance", models).items():
            bucket = totals.setdefault(model, {})
            for ts, wm2 in series.items():
                bucket[ts] = bucket.get(ts, 0.0) + wm2 / 1000.0 * array.kwp
    return totals


async def async_fetch_day_ahead_daily(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    arrays: list[PvArray],
    models: list[str],
    *,
    start: date,
    end: date,
    tz: tzinfo,
) -> dict[str, dict[str, float]]:
    """{model: {local day ISO: nameplate kWh}} as each model forecast it about 24 h ahead."""
    variable = "global_tilted_irradiance_previous_day1"
    hourly: dict[str, dict[int, float]] = {}
    for array in arrays:
        params = {
            **_base_params(lat, lon, array, models),
            "hourly": variable,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        payload = await _get_json(session, PREVIOUS_RUNS_URL, params)
        for model, series in _series(payload, variable, models).items():
            bucket = hourly.setdefault(model, {})
            for ts, wm2 in series.items():
                bucket[ts] = bucket.get(ts, 0.0) + wm2 / 1000.0 * array.kwp
    return {model: _daily_kwh(series, tz) for model, series in hourly.items()}


def _daily_kwh(series: dict[int, float], tz: tzinfo) -> dict[str, float]:
    """Hourly mean kW (stamped at hour end) → kWh per local day; days with gaps are left out."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for ts, kw in series.items():
        mid = datetime.fromtimestamp(ts, timezone.utc) - HALF_HOUR
        day = mid.astimezone(tz).date().isoformat()
        totals[day] = totals.get(day, 0.0) + kw
        counts[day] = counts.get(day, 0) + 1
    return {day: round(total, 3) for day, total in totals.items() if counts[day] >= 23}


def score_models(predicted: dict[str, dict[str, float]], actual: dict[str, float]) -> dict[str, dict[str, Any]]:
    """Per model: days scored, performance factor (actual ÷ nameplate forecast) and day-ahead mean abs error.

    The error is leave-one-out: each day is predicted with the factor learned from the other days, so a model
    isn't rewarded for being calibrated on the day it's judged on.
    """
    out: dict[str, dict[str, Any]] = {}
    for model, days in predicted.items():
        common = sorted(d for d in days if d in actual)
        total_pred = sum(days[d] for d in common)
        if len(common) < 3 or total_pred <= 0:
            out[model] = {"days": len(common)}
            continue
        factor = sum(actual[d] for d in common) / total_pred
        errors = []
        for day in common:
            others_pred = total_pred - days[day]
            k = (sum(actual[d] for d in common if d != day) / others_pred) if others_pred > 0 else factor
            errors.append(abs(days[day] * k - actual[day]))
        out[model] = {"days": len(common), "factor": round(factor, 3), "mae_kwh": round(mean(errors), 2)}
    return out


def model_weights(scores: dict[str, dict[str, Any]], models: list[str]) -> dict[str, float]:
    """Blend weights ∝ 1 / error² over models with enough scored days; equal weights until then."""
    scored = {
        m: scores[m]["mae_kwh"]
        for m in models
        if scores.get(m, {}).get("mae_kwh") is not None and scores[m].get("days", 0) >= MIN_SCORED_DAYS
    }
    if not scored:
        return {m: 1.0 / len(models) for m in models} if models else {}
    raw = {m: 1.0 / max(mae, MIN_WEIGHT_MAE_KWH) ** 2 for m, mae in scored.items()}
    total = sum(raw.values())
    return {m: v / total for m, v in raw.items()}


def model_factor(scores: dict[str, dict[str, Any]], model: str) -> float:
    s = scores.get(model) or {}
    if s.get("factor") and s.get("days", 0) >= MIN_SCORED_DAYS:
        return float(s["factor"])
    return DEFAULT_PERFORMANCE_FACTOR


def blend_mae(predicted: dict[str, dict[str, float]], actual: dict[str, float], scores, models) -> float | None:
    """Mean abs error of the blend over the scored days (same weights and factors as the live blend)."""
    weights = model_weights(scores, models)
    errors = []
    for day, kwh in actual.items():
        num = den = 0.0
        for m, w in weights.items():
            value = predicted.get(m, {}).get(day)
            if value is not None:
                num += w * value * model_factor(scores, m)
                den += w
        if den > 0:
            errors.append(abs(num / den - kwh))
    return round(mean(errors), 2) if len(errors) >= MIN_SCORED_DAYS else None


def blend_rows(
    forecast: dict[str, dict[int, float]],
    scores: dict[str, dict[str, Any]],
    models: list[str],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Weighted, calibrated blend as 30-minute rows (end-stamped), from yesterday onwards."""
    available = [m for m in models if forecast.get(m)]
    weights = model_weights(scores, available)
    factors = {m: model_factor(scores, m) for m in weights}
    stamps = sorted({ts for m in weights for ts in forecast[m]})
    cutoff = now - timedelta(days=1)
    rows: list[dict[str, Any]] = []
    for ts in stamps:
        num = den = 0.0
        for m, w in weights.items():
            value = forecast[m].get(ts)
            if value is not None:
                num += w * value * factors[m]
                den += w
        if den <= 0:
            continue
        kw = round(max(0.0, num / den), 4)
        end = datetime.fromtimestamp(ts, timezone.utc)
        if end < cutoff:
            continue
        for stamp in (end - HALF_HOUR, end):
            iso = stamp.isoformat()
            rows.append({"period_start": iso, "period_end": iso, "pv_estimate": kw, "source": SOURCE})
    return rows


HISTORY_DAYS = 14


def chart_points(rows: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Blend rows → chart points {t: ms at the middle of each hour, v: kW}.

    Open-Meteo is hourly; each hour becomes two equal half-hour rows for the planner. Charting both would
    draw flat steps, so the chart gets one point per hour (from the row that ends on the hour).
    """
    out = []
    for row in rows:
        end = _row_end(row)
        if end is None or end.minute != 0:
            continue
        out.append({"t": round((end - HALF_HOUR).timestamp() * 1000), "v": float(row["pv_estimate"])})
    return sorted(out, key=lambda p: p["t"])


def update_history(
    history: dict[str, list[list[float]]], points: list[dict[str, float]], *, today: date, tz: tzinfo
) -> dict[str, list[list[float]]]:
    """Keep each local day's blend for the charts: today and yesterday follow the latest fetch, older days freeze.

    Stored as {day ISO: [[ms, kW], ...]} for the last HISTORY_DAYS days. Merged hour by hour: the blend only reaches
    back 24 hours, so replacing yesterday wholesale wiped one more of its morning hours with every hourly fetch.
    """
    by_day: dict[str, dict[float, float]] = {}
    for p in points:
        day = datetime.fromtimestamp(p["t"] / 1000, timezone.utc).astimezone(tz).date()
        if today - timedelta(days=1) <= day <= today:
            by_day.setdefault(day.isoformat(), {})[p["t"]] = round(p["v"], 3)
    out = dict(history)
    for day, fresh in by_day.items():
        merged = {t: v for t, v in history.get(day) or []}
        merged.update(fresh)
        out[day] = [[t, merged[t]] for t in sorted(merged)]
    keep = {(today - timedelta(days=i)).isoformat() for i in range(HISTORY_DAYS)}
    return {d: v for d, v in out.items() if d in keep}


def points_for_day(
    current: list[dict[str, float]], history: dict[str, list[list[float]]], day: date, tz: tzinfo
) -> list[dict[str, float]]:
    """Chart points for one local day: the stored history with the current blend laid over the hours it covers."""
    start = datetime.combine(day, datetime.min.time(), tz).timestamp() * 1000
    end = start + 86_400_000
    merged = {t: v for t, v in history.get(day.isoformat()) or []}
    merged.update({p["t"]: p["v"] for p in current if start <= p["t"] < end})
    return [{"t": t, "v": merged[t]} for t in sorted(merged)]


def model_day_totals(
    forecast: dict[str, dict[int, float]], scores: dict[str, dict[str, Any]], models: list[str], tz: tzinfo
) -> dict[str, dict[str, float]]:
    """{model: {local day: calibrated kWh}} for the panel's comparison table."""
    return {
        m: {d: round(v * model_factor(scores, m), 1) for d, v in _daily_kwh(forecast[m], tz).items()}
        for m in models
        if forecast.get(m)
    }


def _row_end(row: dict[str, Any]) -> datetime | None:
    raw = row.get("period_end") or row.get("period_start")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def combine_forecasts(
    solcast_rows: list[dict[str, Any]],
    open_meteo_rows: list[dict[str, Any]],
    *,
    primary: str,
    solcast_fresh: bool,
) -> list[dict[str, Any]]:
    """One forecast for SmartCharge: the preferred provider wins each half-hour, the other fills gaps.

    Solcast is preferred unless Open-Meteo is the chosen primary, or Solcast hasn't fetched for a day
    (then its old rows only fill what Open-Meteo lacks).
    """
    if not open_meteo_rows:
        return solcast_rows
    if not solcast_rows:
        return open_meteo_rows
    solcast_first = primary != SOURCE and solcast_fresh
    base, fill = (solcast_rows, open_meteo_rows) if solcast_first else (open_meteo_rows, solcast_rows)
    by_end: dict[datetime, dict[str, Any]] = {}
    for row in fill:
        if (end := _row_end(row)) is not None:
            by_end[end] = row
    for row in base:
        if (end := _row_end(row)) is not None:
            by_end[end] = row
    return [by_end[k] for k in sorted(by_end)]


def source_at(rows: list[dict[str, Any]], when: datetime) -> str | None:
    """Which provider supplies the half-hour ending nearest after ``when`` ("solcast" / "open_meteo")."""
    best: tuple[timedelta, dict[str, Any]] | None = None
    for row in rows:
        end = _row_end(row)
        if end is None or end < when:
            continue
        gap = end - when
        if best is None or gap < best[0]:
            best = (gap, row)
    if best is None or best[0] > timedelta(hours=1):
        return None
    return SOURCE if best[1].get("source") == SOURCE else "solcast"
