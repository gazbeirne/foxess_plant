"""Solcast polling after a failure: no retry storm, 429 waits for the daily reset, stale forecast detection.

13 Oct 2026: Solcast answered HTTP 429 from 11 Oct onwards. A failed poll didn't move the last-poll time, so the
5-minute timer found a poll "due now" on every tick (~120 refused requests a day) and SmartCharge planned two
nights on an estimated forecast without anyone being told.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.foxess_plant import solcast_poll
from custom_components.foxess_plant.models import PvSystemConfig, SolcastConfig
from custom_components.foxess_plant.solcast_api import SolcastApiClient, SolcastApiError
from custom_components.foxess_plant.solcast_poll import async_refresh_solcast, forecast_is_stale


def _plant(last_fetch: datetime) -> SimpleNamespace:
    solcast = SolcastConfig(
        enabled=True,
        api_key="test-key",
        rooftop_site_bindings={"pv1": "abcd-1234"},
        last_fetch_at=last_fetch.isoformat(),
    )
    return SimpleNamespace(solcast=solcast, pv_config=PvSystemConfig())


@pytest.fixture
def clock(monkeypatch):
    """Controllable dt_util.utcnow; starts at 10:00 UTC on 13 Oct 2026."""
    state = {"now": datetime(2026, 10, 13, 10, 0, tzinfo=dt_util.UTC)}
    monkeypatch.setattr(dt_util, "utcnow", lambda: state["now"])
    monkeypatch.setattr(solcast_poll, "should_poll_now", lambda hass, solcast: True)
    return state


def _fail_with(monkeypatch, status: int | None, body: str = "") -> list[str]:
    calls: list[str] = []

    async def fake_forecasts(self, resource_id, *, hours, period="PT30M"):
        calls.append(resource_id)
        raise SolcastApiError(f"Solcast HTTP {status}: {body}", status=status)

    monkeypatch.setattr(SolcastApiClient, "hobbyist_site_forecasts", fake_forecasts)
    return calls


@pytest.mark.asyncio
async def test_limit_spent_429_stops_polling_until_the_daily_reset(hass, monkeypatch, clock) -> None:
    calls = _fail_with(monkeypatch, 429, '{"response_status":{"error_code":"TooManyRequests"}}')
    plant = _plant(clock["now"] - timedelta(days=2))

    await async_refresh_solcast(hass, plant, {})
    assert len(calls) == 1
    assert plant.solcast.api_used_today == plant.solcast.api_limit
    assert "429" in plant.solcast.last_error and "00:00 UTC" in plant.solcast.last_error

    # Every timer tick for the rest of the UTC day: no more requests
    for _ in range(20):
        clock["now"] += timedelta(minutes=30)
        await async_refresh_solcast(hass, plant, {})
    assert clock["now"].date() == datetime(2026, 10, 13).date()
    assert len(calls) == 1

    # After the reset, polling resumes
    clock["now"] = datetime(2026, 10, 14, 9, 0, tzinfo=dt_util.UTC)
    await async_refresh_solcast(hass, plant, {})
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_busy_429_retries_at_the_poll_spacing_and_never_exceeds_the_limit(hass, monkeypatch, clock) -> None:
    calls = _fail_with(monkeypatch, 429)  # empty body, as Solcast sent from 11 Oct 2026
    plant = _plant(clock["now"] - timedelta(days=2))

    # Every 5-minute timer tick from 10:00 to 23:55 UTC (the poll window is patched open all day)
    for _ in range(14 * 12):
        await async_refresh_solcast(hass, plant, {})
        clock["now"] += timedelta(minutes=5)
    assert 2 <= len(calls) <= plant.solcast.api_limit
    assert plant.solcast.api_used_today == len(calls)
    assert "00:00 UTC" not in plant.solcast.last_error


@pytest.mark.asyncio
async def test_other_failures_wait_a_full_interval_and_count_against_the_limit(hass, monkeypatch, clock) -> None:
    calls = _fail_with(monkeypatch, 500)
    plant = _plant(clock["now"] - timedelta(days=2))
    interval = solcast_poll.min_poll_interval(hass, plant.solcast, pv_calls=1)

    await async_refresh_solcast(hass, plant, {})
    assert len(calls) == 1
    assert plant.solcast.api_used_today == 1

    clock["now"] += timedelta(minutes=5)  # next timer tick
    await async_refresh_solcast(hass, plant, {})
    assert len(calls) == 1

    clock["now"] += interval
    await async_refresh_solcast(hass, plant, {})
    assert len(calls) == 2
    assert plant.solcast.api_used_today == 2


@pytest.mark.asyncio
async def test_no_response_still_spaces_retries_but_is_not_counted(hass, monkeypatch, clock) -> None:
    calls = _fail_with(monkeypatch, None)  # network error: Solcast never saw it
    plant = _plant(clock["now"] - timedelta(days=2))

    await async_refresh_solcast(hass, plant, {})
    clock["now"] += timedelta(minutes=5)
    await async_refresh_solcast(hass, plant, {})
    assert len(calls) == 1
    assert plant.solcast.api_used_today == 0


def test_error_text_shows_reason_and_rate_limit_headers_when_the_body_is_empty() -> None:
    from custom_components.foxess_plant.solcast_api import describe_error

    text = describe_error(429, "Too Many Requests", "", {"Retry-After": "3600", "X-RateLimit-Remaining": "0",
                                                         "Content-Type": "text/plain"})
    assert text == ("Solcast HTTP 429 Too Many Requests · no message in the response · Retry-After: 3600"
                    " · X-Ratelimit-Remaining: 0")
    body = '{"response_status": {"error_code": "TooManyRequests", "message": "You have exceeded your free daily limit."}}'
    assert "TooManyRequests You have exceeded" in describe_error(429, "Too Many Requests", body, {})


def test_forecast_is_stale_only_when_tomorrow_afternoon_is_missing() -> None:
    tz = dt_util.get_default_time_zone()
    day = datetime(2026, 10, 12, tzinfo=tz)
    # A poll on 11 Oct covers until the end of 12 Oct
    covered_until = datetime(2026, 10, 13, 0, 0, tzinfo=tz)
    assert not forecast_is_stale(covered_until, day.replace(hour=2), ever_fetched=True)
    assert not forecast_is_stale(covered_until, day.replace(hour=14, minute=59), ever_fetched=True)
    # From 15:00 on 12 Oct tonight's plan needs 13 Oct, and nothing was fetched today
    assert forecast_is_stale(covered_until, day.replace(hour=15), ever_fetched=True)
    assert forecast_is_stale(covered_until, day.replace(hour=23), ever_fetched=True)
    # No forecast at all: only a problem once one has been fetched before
    assert forecast_is_stale(None, day, ever_fetched=True)
    assert not forecast_is_stale(None, day, ever_fetched=False)
