"""Solar Output chart PV Forecast line (13 Oct 2026: flat 0 until 16:00 with Solcast stale, then hourly steps)."""

from custom_components.foxess_plant.pv_forecast_line import (
    OPEN_METEO,
    SOLCAST,
    interpolate_now,
    pv_forecast_line,
    solcast_fresh_at,
)

H = 3_600_000
DAY = 1_760_310_000_000.0  # any day start; only relative times matter


def _om(values):
    """Hourly Open-Meteo points at mid-hour."""
    return [{"t": DAY + h * H + H / 2, "v": v} for h, v in enumerate(values)]


def _sc(value, start_h=0, end_h=24):
    return [{"t": DAY + m * 300_000, "v": value} for m in range(start_h * 12, end_h * 12 + 1)]


def test_stale_solcast_uses_open_meteo_smoothly() -> None:
    # Last Solcast poll two days ago: its old values must not be drawn
    pts, used = pv_forecast_line(
        solcast_points=_sc(0.0),
        solcast_fetches_ms=[DAY - 48 * H],
        open_meteo_points=_om([0.0] * 12 + [2.0, 3.0] + [0.0] * 10),
        start_ms=DAY,
        end_ms=DAY + 24 * H - 1,
        now_ms=DAY + 20 * H,
    )
    assert used == [OPEN_METEO]
    at = {p["t"]: p["v"] for p in pts}
    assert at[DAY + 13 * H] == 2.5  # between the 12:30 (2.0) and 13:30 (3.0) points, not a flat step
    assert at[DAY + 12 * H + H / 2] == 2.0


def test_fresh_solcast_wins_and_switches_when_it_goes_stale() -> None:
    pts, used = pv_forecast_line(
        solcast_points=_sc(1.0),
        solcast_fetches_ms=[DAY - 20 * H],  # fresh until 04:00 today
        open_meteo_points=_om([2.0] * 24),
        start_ms=DAY,
        end_ms=DAY + 24 * H - 1,
        now_ms=DAY + 20 * H,
    )
    at = {p["t"]: p["v"] for p in pts}
    assert at[DAY + 2 * H] == 1.0
    assert at[DAY + 10 * H] == 2.0
    assert used == [SOLCAST, OPEN_METEO]


def test_no_open_meteo_keeps_solcast_even_when_stale() -> None:
    pts, used = pv_forecast_line(
        solcast_points=_sc(1.5, 8, 16),
        solcast_fetches_ms=[DAY - 72 * H],
        open_meteo_points=[],
        start_ms=DAY,
        end_ms=DAY + 24 * H - 1,
        now_ms=DAY + 20 * H,
    )
    assert used == [SOLCAST] and len(pts) == 8 * 12 + 1


def test_all_zero_draws_nothing() -> None:
    assert pv_forecast_line(
        solcast_points=_sc(0.0), solcast_fetches_ms=[DAY], open_meteo_points=[], start_ms=DAY,
        end_ms=DAY + H, now_ms=DAY,
    ) == ([], [])


def test_freshness_uses_now_for_the_future() -> None:
    fetches = [DAY]
    assert solcast_fresh_at(fetches, DAY + 30 * H, now_ms=DAY + 2 * H)
    assert not solcast_fresh_at(fetches, DAY + 30 * H, now_ms=DAY + 25 * H)
    assert not solcast_fresh_at(fetches, DAY - H, now_ms=DAY)


def test_interpolate_now() -> None:
    assert interpolate_now(_om([1.0, 3.0]), DAY + H) == 2.0
    assert interpolate_now(_om([1.0, 3.0]), DAY + 5 * H) is None
