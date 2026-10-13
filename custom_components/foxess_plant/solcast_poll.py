"""Solcast polling schedule, quota, and coordinator helpers."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import (
    SOLCAST_AUTO_UPDATE_ALL_DAY,
    SOLCAST_AUTO_UPDATE_DAYLIGHT,
    SOLCAST_MIN_POLL_INTERVAL,
    SOLCAST_POLL_END_BEFORE_SUNSET,
)
from .models import PlantConfig, SolcastConfig
from .solcast_api import SolcastApiClient, SolcastApiError
from .solcast_pv import (
    build_rooftop_pv_requests,
    forecast_hours_until_local_midnight,
    parse_detailed_forecast,
    rooftop_requests_summary,
)
from .solcast_weather import resolve_coordinates

_LOGGER = logging.getLogger(__name__)


def _today_utc() -> str:
    return dt_util.utcnow().date().isoformat()


def reset_api_quota_if_needed(solcast: SolcastConfig) -> None:
    today = _today_utc()
    if solcast.api_used_date != today:
        solcast.api_used_date = today
        solcast.api_used_today = 0


def api_remaining(solcast: SolcastConfig) -> int:
    reset_api_quota_if_needed(solcast)
    return max(0, solcast.api_limit - solcast.api_used_today)


def can_consume_api(solcast: SolcastConfig, count: int = 1) -> bool:
    reset_api_quota_if_needed(solcast)
    return solcast.api_used_today + count <= solcast.api_limit


def record_api_use(solcast: SolcastConfig, count: int = 1) -> None:
    reset_api_quota_if_needed(solcast)
    solcast.api_used_today += count


def is_daylight(hass: HomeAssistant) -> bool:
    sun = hass.states.get("sun.sun")
    if sun is None:
        return True
    return sun.state == "above_horizon"


def get_today_poll_window(hass: HomeAssistant) -> tuple[datetime, datetime] | None:
    """Today's sunrise and last scheduled poll (sunset minus buffer), in local time."""
    try:
        now = dt_util.now()
        sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, now)
        sunset = get_astral_event_date(hass, SUN_EVENT_SUNSET, now)
    except (TypeError, ValueError, KeyError):
        return None
    if sunrise is None or sunset is None:
        return None
    sunrise_local = dt_util.as_local(sunrise)
    sunset_local = dt_util.as_local(sunset)
    poll_end = sunset_local - SOLCAST_POLL_END_BEFORE_SUNSET
    if poll_end <= sunrise_local:
        poll_end = sunset_local
    return sunrise_local, poll_end


def effective_poll_window_hours(hass: HomeAssistant, solcast: SolcastConfig) -> float:
    """Hours between sunrise and (sunset − buffer), or 24h for all-day mode."""
    if solcast.auto_update == SOLCAST_AUTO_UPDATE_ALL_DAY:
        return 24.0
    window = get_today_poll_window(hass)
    if not window:
        return 12.0
    sunrise, poll_end = window
    hours = (poll_end - sunrise).total_seconds() / 3600.0
    return max(1.0, hours)


def is_in_poll_window(hass: HomeAssistant, solcast: SolcastConfig) -> bool:
    """True when automatic PV polling is allowed (daylight window or 24h mode)."""
    if solcast.auto_update == SOLCAST_AUTO_UPDATE_ALL_DAY:
        return True
    window = get_today_poll_window(hass)
    if not window:
        return is_daylight(hass)
    sunrise, poll_end = window
    now = dt_util.now()
    return sunrise <= now < poll_end


def should_poll_now(hass: HomeAssistant, solcast: SolcastConfig) -> bool:
    if not solcast.enabled or not solcast.api_key_configured():
        return False
    return is_in_poll_window(hass, solcast)


def last_poll_timestamp(
    solcast: SolcastConfig,
    cache: dict[str, Any],
) -> datetime | None:
    """Latest successful PV poll time (in-memory cache or persisted config entry)."""
    raw_times: list[str] = []
    for key in ("updated_at", "pv_forecast_fetched_at"):
        value = cache.get(key)
        if value:
            raw_times.append(str(value))
    if solcast.last_fetch_at:
        raw_times.append(str(solcast.last_fetch_at))
    latest: datetime | None = None
    for raw in raw_times:
        try:
            parsed = dt_util.parse_datetime(raw)
        except (TypeError, ValueError):
            continue
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        else:
            parsed = dt_util.as_utc(parsed)
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def last_try_timestamp(
    solcast: SolcastConfig,
    cache: dict[str, Any],
) -> datetime | None:
    """Latest request sent to Solcast, successful or not.

    Polls are spaced from this, so a failed request waits a full interval before the next try
    instead of being retried on every 5-minute timer tick.
    """
    last = last_poll_timestamp(solcast, cache)
    attempt = dt_util.parse_datetime(solcast.last_attempt_at) if solcast.last_attempt_at else None
    if attempt is not None:
        attempt = dt_util.as_utc(attempt) if attempt.tzinfo else attempt.replace(tzinfo=dt_util.UTC)
        if last is None or attempt > last:
            return attempt
    return last


def is_quota_exhausted(err: SolcastApiError) -> bool:
    """A 429 whose body says the daily limit is spent ("TooManyRequests" / "exceeded").

    Any other 429 (e.g. an empty body) means Solcast is busy: retry at the normal poll spacing.
    """
    text = str(err).lower()
    return err.status == 429 and ("toomanyrequests" in text or "exceeded" in text)


REFUSAL_PAUSE_AFTER = timedelta(hours=24)
REFUSAL_PAUSE_FOR = timedelta(hours=24)
REFUSAL_PAUSE_MIN_COUNT = 3


def _parse_utc(raw: str | None) -> datetime | None:
    parsed = dt_util.parse_datetime(raw) if raw else None
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


def paused_until(solcast: SolcastConfig) -> datetime | None:
    """When an automatic refusal pause ends, or None when Solcast isn't paused."""
    until = _parse_utc(solcast.paused_until)
    return until if until and until > dt_util.utcnow() else None


def record_refusal(solcast: SolcastConfig) -> bool:
    """Count an HTTP 429; pause Solcast for a day once every request for a whole day has been refused.

    Returns True when this refusal started a pause. Hammering a service that keeps refusing gets keys and
    IPs blocked (Oct 2026: a 5-minute retry loop sent ~150–200 requests in a day and a half), so after a
    day of nothing but refusals Fox Plant goes quiet for a day, then tries once.
    """
    now = dt_util.utcnow()
    solcast.refused_count += 1
    since = _parse_utc(solcast.refused_since)
    if since is None:
        solcast.refused_since = now.isoformat()
        return False
    if now - since >= REFUSAL_PAUSE_AFTER and solcast.refused_count >= REFUSAL_PAUSE_MIN_COUNT:
        solcast.paused_until = (now + REFUSAL_PAUSE_FOR).isoformat()
        return True
    return False


def clear_refusals(solcast: SolcastConfig) -> None:
    solcast.refused_count = 0
    solcast.refused_since = None
    solcast.paused_until = None


def mark_daily_limit_reached(solcast: SolcastConfig) -> None:
    """Solcast says the daily limit is spent: treat today's quota as used up until the UTC reset."""
    reset_api_quota_if_needed(solcast)
    solcast.api_used_today = max(solcast.api_used_today, solcast.api_limit)


STALE_CHECK_HOUR = 15


def forecast_is_stale(forecast_end: datetime | None, now: datetime, *, ever_fetched: bool) -> bool:
    """True when the PV forecast doesn't reach the next 15:00 local.

    Every successful poll covers until the end of tomorrow, so this only trips when a whole
    day's polls failed — e.g. by 15:00 nothing was fetched today, and tonight's SmartCharge plan
    would have to guess tomorrow's PV. A plant that has never fetched isn't flagged.
    """
    if forecast_end is None:
        return ever_fetched
    local = dt_util.as_local(now)
    need = local.replace(hour=STALE_CHECK_HOUR, minute=0, second=0, microsecond=0)
    if need <= local:
        need += timedelta(days=1)
    return forecast_end < need


def poll_interval_elapsed(
    hass: HomeAssistant,
    solcast: SolcastConfig,
    cache: dict[str, Any],
    *,
    pv_calls: int = 1,
) -> bool:
    """True when the configured spacing since the last poll (or failed try) has passed."""
    last = last_try_timestamp(solcast, cache)
    if last is None:
        return True
    return dt_util.utcnow() - last >= min_poll_interval(hass, solcast, pv_calls=pv_calls)


def min_poll_interval(
    hass: HomeAssistant,
    solcast: SolcastConfig,
    *,
    pv_calls: int = 1,
) -> timedelta:
    """Space refreshes so API quota spans the poll window (daylight or 24h).

    Example: 10 h window, limit 10, 2 API calls per refresh → 120 min between refreshes.
    """
    limit = max(1, solcast.api_limit)
    calls = max(1, pv_calls)
    hours = effective_poll_window_hours(hass, solcast)
    interval_hours = (hours * calls) / limit
    minutes = max(
        int(SOLCAST_MIN_POLL_INTERVAL.total_seconds() // 60),
        int(interval_hours * 60),
    )
    return timedelta(minutes=minutes)


def _next_sunrise_local(hass: HomeAssistant, after: datetime) -> datetime | None:
    """Next sunrise at or after ``after`` (local time)."""
    probe = dt_util.as_local(after)
    for day_offset in range(0, 3):
        when = probe + timedelta(days=day_offset)
        try:
            sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, when)
        except (TypeError, ValueError, KeyError):
            return None
        if sunrise is None:
            continue
        sunrise_local = dt_util.as_local(sunrise)
        if sunrise_local >= probe:
            return sunrise_local
    return None


def _next_poll_after_quota_reset(hass: HomeAssistant, solcast: SolcastConfig) -> datetime:
    """When quota resets at UTC midnight, when the next automatic poll can run."""
    tomorrow = dt_util.utcnow().date() + timedelta(days=1)
    reset_at_utc = datetime.combine(tomorrow, time.min, tzinfo=dt_util.UTC)
    if solcast.auto_update == SOLCAST_AUTO_UPDATE_ALL_DAY:
        return reset_at_utc
    reset_local = dt_util.as_local(reset_at_utc)
    next_sun = _next_sunrise_local(hass, reset_local)
    if next_sun:
        return dt_util.as_utc(next_sun)
    return reset_at_utc


def solcast_next_fetch(
    hass: HomeAssistant,
    solcast: SolcastConfig,
    cache: dict[str, Any],
    *,
    pv_calls: int = 1,
) -> dict[str, Any] | None:
    """Estimate when the next automatic PV poll may run (UTC ISO + status hint)."""
    if not solcast.enabled or not solcast.api_key_configured() or not solcast.fetch_pv_forecast:
        return {"at": None, "status": "disabled"}
    pause_end = paused_until(solcast)
    if pause_end:
        return {"at": pause_end.isoformat(), "status": "paused"}
    if not can_consume_api(solcast, pv_calls):
        tomorrow = dt_util.utcnow().date() + timedelta(days=1)
        reset_at = datetime.combine(tomorrow, time.min, tzinfo=dt_util.UTC)
        poll_at = _next_poll_after_quota_reset(hass, solcast)
        return {
            "at": reset_at.isoformat(),
            "reset_at": reset_at.isoformat(),
            "poll_at": poll_at.isoformat(),
            "status": "quota_exhausted",
        }

    schedule = solcast_poll_schedule(hass, solcast, pv_calls=pv_calls)
    if not schedule.get("in_window", True):
        now_local = dt_util.now()
        window = get_today_poll_window(hass)
        if window and solcast.auto_update != SOLCAST_AUTO_UPDATE_ALL_DAY:
            sunrise, poll_end = window
            if now_local < sunrise:
                at = dt_util.as_utc(sunrise)
                return {"at": at.isoformat(), "status": "before_sunrise"}
            if now_local >= poll_end:
                next_sun = _next_sunrise_local(hass, now_local + timedelta(minutes=1))
                if next_sun:
                    at = dt_util.as_utc(next_sun)
                    return {"at": at.isoformat(), "status": "after_sunset"}
        return {"at": None, "status": "outside_window"}

    interval = min_poll_interval(hass, solcast, pv_calls=pv_calls)
    last = last_try_timestamp(solcast, cache)
    now = dt_util.utcnow()
    if last is None or last + interval <= now:
        return {"at": now.isoformat(), "status": "due_now"}
    due = last + interval
    return {"at": due.isoformat(), "status": "scheduled"}


def solcast_poll_schedule(
    hass: HomeAssistant,
    solcast: SolcastConfig,
    *,
    pv_calls: int = 1,
) -> dict[str, Any]:
    """Human/panel-facing schedule derived from HA sun position."""
    window = get_today_poll_window(hass)
    interval = min_poll_interval(hass, solcast, pv_calls=pv_calls)
    now = dt_util.now()
    if solcast.auto_update == SOLCAST_AUTO_UPDATE_ALL_DAY:
        return {
            "mode": SOLCAST_AUTO_UPDATE_ALL_DAY,
            "window_hours": 24.0,
            "interval_minutes": int(interval.total_seconds() // 60),
            "in_window": True,
        }
    if not window:
        return {
            "mode": SOLCAST_AUTO_UPDATE_DAYLIGHT,
            "window_hours": effective_poll_window_hours(hass, solcast),
            "interval_minutes": int(interval.total_seconds() // 60),
            "in_window": is_daylight(hass),
            "sunrise": None,
            "poll_until": None,
        }
    sunrise, poll_end = window
    return {
        "mode": SOLCAST_AUTO_UPDATE_DAYLIGHT,
        "window_hours": round((poll_end - sunrise).total_seconds() / 3600.0, 1),
        "interval_minutes": int(interval.total_seconds() // 60),
        "in_window": sunrise <= now < poll_end,
        "sunrise": sunrise.isoformat(),
        "poll_until": poll_end.isoformat(),
        "sunset_buffer_minutes": int(SOLCAST_POLL_END_BEFORE_SUNSET.total_seconds() // 60),
    }


def solcast_status_dict(
    solcast: SolcastConfig,
    cache: dict[str, Any] | None,
    *,
    plant: PlantConfig | None = None,
    hass: HomeAssistant | None = None,
    forecast_history_snapshots: int = 0,
    forecast_intraday_points: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    reset_api_quota_if_needed(solcast)
    out = solcast.to_dict(include_api_key=False)
    out["api_remaining"] = api_remaining(solcast)
    out["forecast_history_snapshots"] = forecast_history_snapshots
    if forecast_intraday_points:
        out["forecast_intraday_points"] = forecast_intraday_points
    out["forecast_persisted"] = bool(cache and cache.get("pv_forecast_parsed"))
    out["coordinates_configured"] = solcast.coordinates_configured()
    if solcast.latitude is not None and solcast.longitude is not None:
        out["coordinates"] = {
            "latitude": solcast.latitude,
            "longitude": solcast.longitude,
        }
    if plant is not None:
        out["pv_requests"] = rooftop_requests_summary(plant)
        if hass is not None:
            pv_calls = max(1, len(build_rooftop_pv_requests(plant.pv_config)) or 1)
            out["poll_schedule"] = solcast_poll_schedule(hass, solcast, pv_calls=pv_calls)
            next_fetch = solcast_next_fetch(hass, solcast, cache or {}, pv_calls=pv_calls)
            if next_fetch:
                out["next_fetch_at"] = next_fetch.get("at")
                out["next_fetch_status"] = next_fetch.get("status")
                if next_fetch.get("reset_at"):
                    out["next_fetch_reset_at"] = next_fetch.get("reset_at")
                if next_fetch.get("poll_at"):
                    out["next_fetch_poll_at"] = next_fetch.get("poll_at")
    if cache:
        out["cache_updated_at"] = cache.get("updated_at")
        pv_parsed = cache.get("pv_forecast_parsed")
        if isinstance(pv_parsed, dict):
            from .solcast_forecast_metrics import merge_forecast_metrics_into_status

            merge_forecast_metrics_into_status(out, pv_parsed, hass)
    return out


async def _fetch_rooftop_pv_forecasts(
    hass: HomeAssistant,
    plant: PlantConfig,
    client: SolcastApiClient,
) -> tuple[dict[str, Any] | None, list[str], dict[str, dict[str, Any]], int, bool]:
    """Fetch forecasts via hobbyist GET /rooftop_sites/{resource_id}/forecasts.

    Returns (parsed, errors, raw by resource id, requests Solcast answered, daily limit spent).
    Stops at the first 429: the other sites would be refused too.
    """
    from .solcast_hobbyist import async_resolve_rooftop_bindings

    solcast = plant.solcast
    requests = build_rooftop_pv_requests(plant.pv_config)
    if not requests:
        return None, ["No enabled PV strings in PV Configuration"], {}, 0, False

    if not solcast.rooftop_site_bindings:
        await async_resolve_rooftop_bindings(hass, plant, trigger="site lookup before a poll", manual=False)

    bindings = solcast.rooftop_site_bindings
    # Include tomorrow: SmartCharge decides tonight's grid charge from tomorrow's PV.
    hours = forecast_hours_until_local_midnight(hass, days_ahead=1)
    period = plant.solcast.period
    payloads: list[tuple[str, dict[str, Any]]] = []
    errors: list[str] = []
    fetched_by_rid: dict[str, dict[str, Any]] = {}
    answered = 0
    rate_limited = False

    for req in requests:
        resource_id = bindings.get(req.label)
        if not resource_id:
            errors.append(
                f"{req.label}: no matching Solcast Home PV site "
                f"(tilt {req.tilt}° / azimuth {req.azimuth}°)"
            )
            continue
        try:
            if resource_id not in fetched_by_rid:
                fetched_by_rid[resource_id] = await client.hobbyist_site_forecasts(
                    resource_id,
                    hours=hours,
                    period=period,
                )
                answered += 1
            payloads.append((req.label, fetched_by_rid[resource_id]))
        except SolcastApiError as err:
            if err.status is not None:
                answered += 1
            msg = f"{req.label}: {err}"
            errors.append(msg)
            _LOGGER.warning("Solcast hobbyist forecast failed for %s: %s", req.label, err)
            if err.status == 429:
                rate_limited = is_quota_exhausted(err)
                break
    if not payloads:
        return None, errors, fetched_by_rid, answered, rate_limited
    return parse_detailed_forecast(payloads, hass), errors, fetched_by_rid, answered, rate_limited


async def async_refresh_solcast(
    hass: HomeAssistant,
    plant: PlantConfig,
    cache: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch Solcast rooftop PV forecast only (weather/StormSafe use Google Weather)."""
    solcast = plant.solcast
    reset_api_quota_if_needed(solcast)
    if not solcast.enabled or not solcast.api_key_configured():
        return cache
    if not solcast.fetch_pv_forecast:
        return cache
    requests = build_rooftop_pv_requests(plant.pv_config)
    if not requests:
        return cache
    if not force and not should_poll_now(hass, solcast):
        return cache
    # A day of nothing but refusals: stay quiet until the pause ends (a manual save/test still tries)
    if not force and paused_until(solcast):
        return cache

    # Use request count as an upper bound before bindings are resolved (same quota math).
    pv_calls_estimate = max(1, len(requests))
    if not force and not poll_interval_elapsed(
        hass, solcast, cache, pv_calls=pv_calls_estimate
    ):
        _LOGGER.debug(
            "Skipping Solcast PV poll (last fetch %s, interval %s min)",
            last_poll_timestamp(solcast, cache),
            int(
                min_poll_interval(hass, solcast, pv_calls=pv_calls_estimate).total_seconds()
                // 60
            ),
        )
        return cache

    bindings = solcast.rooftop_site_bindings
    if not bindings:
        try:
            from .solcast_hobbyist import async_resolve_rooftop_bindings

            await async_resolve_rooftop_bindings(
                hass, plant, trigger="site lookup before a poll", manual=force
            )
            bindings = solcast.rooftop_site_bindings
        except SolcastApiError as err:
            solcast.last_error = str(err)
            return cache

    unique_sites = {bindings[r.label] for r in requests if r.label in bindings}
    pv_calls = max(1, len(unique_sites))

    if not can_consume_api(solcast, pv_calls):
        solcast.last_error = "Daily API limit reached"
        return cache

    client = SolcastApiClient(
        hass,
        api_key=solcast.api_key or "",
        solcast=solcast,
        trigger="fetch after a settings change" if force else "scheduled poll",
        manual=force,
    )
    # Recorded before the request, so even an unexpected failure waits a full interval to retry
    solcast.last_attempt_at = dt_util.utcnow().isoformat()
    try:
        parsed, fetch_errors, raw_forecasts, answered, rate_limited = (
            await _fetch_rooftop_pv_forecasts(hass, plant, client)
        )
    except SolcastApiError as err:
        solcast.last_error = str(err)
        _LOGGER.warning("Solcast PV forecast fetch failed: %s", err)
        return cache

    # Solcast counts every request it answers against the daily limit, failed or not
    record_api_use(solcast, answered)
    if rate_limited:
        mark_daily_limit_reached(solcast)

    period_count = int(parsed.get("period_count", 0) or 0) if parsed else 0
    if parsed and period_count > 0:
        cache["pv_forecast_parsed"] = parsed
        cache["raw_forecasts"] = raw_forecasts
        cache["pv_forecast_fetched_at"] = dt_util.utcnow().isoformat()
        lat, lon = resolve_coordinates(hass, solcast)
        cache["coordinates"] = {"latitude": lat, "longitude": lon}
        cache["updated_at"] = dt_util.utcnow().isoformat()
        solcast.last_fetch_at = cache["updated_at"]
        solcast.last_error = None
        clear_refusals(solcast)
    elif rate_limited:
        solcast.last_error = (
            f"{fetch_errors[0]} — Solcast says the daily limit is used up; "
            "no more tries until it resets at 00:00 UTC"
        )
    elif fetch_errors:
        solcast.last_error = fetch_errors[0] if len(fetch_errors) == 1 else "; ".join(fetch_errors[:2])
    elif parsed is not None:
        solcast.last_error = (
            "Solcast returned no PV forecast periods — check site coordinates, "
            "capacity, and that PV1 is enabled under PV Configuration"
        )
    if not (parsed and period_count > 0) and any("HTTP 429" in e for e in fetch_errors):
        if record_refusal(solcast):
            until = dt_util.as_local(_parse_utc(solcast.paused_until)).strftime("%d %b %H:%M")
            _LOGGER.warning("Solcast refused every request for a day: pausing Solcast until %s", until)
            solcast.last_error = f"{solcast.last_error} — refused for a whole day, so paused until {until}"
    return cache


async def async_test_solcast_connection(
    hass: HomeAssistant,
    solcast: SolcastConfig,
    *,
    plant: PlantConfig | None = None,
) -> dict[str, Any]:
    """Verify hobbyist API key by listing Home PV sites (no daily quota use)."""
    from .solcast_hobbyist import async_list_rooftop_sites, match_rooftop_site_bindings

    if not solcast.api_key_configured():
        return {"test_ok": False, "error": "Solcast API key is not configured"}

    try:
        sites = await async_list_rooftop_sites(hass, solcast, trigger="connection test", manual=True)
    except SolcastApiError as err:
        solcast.last_error = str(err)
        return {
            "test_ok": False,
            "error": str(err),
            "test_hobbyist": True,
            "quota_charged": False,
        }

    if not sites:
        solcast.last_error = "No Home PV systems on this Solcast account"
        return {
            "test_ok": False,
            "error": solcast.last_error,
            "test_hobbyist": True,
            "quota_charged": False,
        }

    names = [str(s.get("name") or s.get("resource_id") or "site") for s in sites]
    match_note = None
    if solcast.coordinates_configured() and plant is not None:
        coords = resolve_coordinates(hass, solcast)
        requests = build_rooftop_pv_requests(plant.pv_config)
        if coords and requests:
            bindings, _meta = match_rooftop_site_bindings(
                sites, coords[0], coords[1], requests
            )
            if bindings:
                match_note = f"Matched {len(bindings)} PV group(s) to toolkit site(s)"
            else:
                match_note = "Sites found but none match saved coordinates / tilt / azimuth"

    solcast.last_error = None
    return {
        "test_ok": True,
        "test_hobbyist": True,
        "quota_charged": False,
        "test_site_count": len(sites),
        "test_site_names": names[:5],
        "test_match_note": match_note,
        "live_summary": {
            "condition_label": f"{len(sites)} Home PV site(s) on account",
            "site_names": ", ".join(names[:3]),
            "match_note": match_note,
        },
    }

