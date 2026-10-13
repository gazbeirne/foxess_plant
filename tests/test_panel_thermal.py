"""Panel thermal lag (13 Oct 2026: the instant model jumped 21 → 64 °C between two 5-minute samples)."""

from datetime import datetime, timedelta, timezone

from custom_components.foxess_plant.performance import panel_thermal as pt
from custom_components.foxess_plant.performance.panel_temp import ArrayThermal, plant_panel_temp_c
from custom_components.foxess_plant.performance_chart import _convert_weather_points, _smooth_unlagged_points

ROOF = [ArrayThermal(stc_kw=4.455, noct_c=45.0, power_temp_coeff_pct=-0.3, mounting="roof_gap")]
T0 = datetime(2026, 10, 13, 11, 40, tzinfo=timezone.utc)


def _run(thermal: pt.PanelThermal, steps: list[tuple[float, float]], *, air=16.5, wind=1.4, start=T0):
    """Step every 30 s through [(seconds, target °C)] segments; returns the last reported value."""
    now = start
    out = None
    for seconds, target in steps:
        for _ in range(int(seconds // 30)):
            out = thermal.update(
                now=now, target_c=target, still_air_target_c=target, air_c=air, arrays=ROOF, wind_ms=wind
            )
            now += timedelta(seconds=30)
    return out, now


def test_time_constant_is_physical_and_faster_in_wind() -> None:
    calm = pt.time_constant_s(ROOF, 0.0)
    breeze = pt.time_constant_s(ROOF, 1.4)
    windy = pt.time_constant_s(ROOF, 5.0)
    assert 12 * 60 < calm < 16 * 60
    assert 9 * 60 < breeze < 12 * 60
    assert 5 * 60 < windy < 7 * 60


def test_cloud_gap_does_not_jump_to_steady_state() -> None:
    # 13 Oct 12:40 → 12:50: output 0.64 → 3.86 kW, the instant model went 21 → 64 °C
    thermal = pt.PanelThermal()
    (temp, _), _ = _run(thermal, [(1800, 21.0), (300, 64.0)])
    assert 33 < temp < 42  # ~40 % of the way after 5 min with τ ≈ 10 min
    (temp, _), _ = _run(thermal, [(3600, 64.0)], start=T0 + timedelta(minutes=35))
    assert temp > 63  # and it does get there in steady sun


def test_a_30_second_flicker_barely_moves_it() -> None:
    thermal = pt.PanelThermal()
    (before, _), now = _run(thermal, [(1800, 25.0)])
    (after, _), _ = _run(thermal, [(30, 60.0)], start=now)
    assert after - before < 2.0


def test_no_sun_reports_nothing_but_keeps_cooling_towards_air() -> None:
    thermal = pt.PanelThermal()
    _, now = _run(thermal, [(600, 50.0)])
    out = thermal.update(now=now + timedelta(minutes=10), target_c=None, still_air_target_c=None, air_c=15.0,
                         arrays=ROOF, wind_ms=1.4)
    assert out == (None, None)
    assert 15.0 < thermal.temp_c < 35.0


def test_restart_resumes_within_30_minutes_and_resets_after() -> None:
    thermal = pt.PanelThermal()
    _, now = _run(thermal, [(1800, 50.0)])
    saved = thermal.to_dict()
    resumed = pt.PanelThermal.from_dict(saved)
    temp, _ = resumed.update(now=now + timedelta(minutes=5), target_c=20.0, still_air_target_c=20.0, air_c=16.0,
                             arrays=ROOF, wind_ms=1.4)
    assert temp > 35  # carried on from ~50, not reset to 20
    fresh = pt.PanelThermal.from_dict(saved)
    temp, _ = fresh.update(now=now + timedelta(hours=2), target_c=20.0, still_air_target_c=20.0, air_c=16.0,
                           arrays=ROOF, wind_ms=1.4)
    assert temp == 20.0
    assert fresh.since == saved["since"]


def test_bad_saved_state_starts_afresh() -> None:
    assert pt.PanelThermal.from_dict({"temp_c": "x", "ts": "nope"}).temp_c is None
    assert pt.PanelThermal.from_dict(None).ts is None


def test_steady_state_peak_matches_the_audit() -> None:
    # 13 Oct 13:20: 3.86 kW, 16.9 °C, ~1.4 m/s → ~57 °C (Sandia close-roof ~62, open rack ~41)
    temp = plant_panel_temp_c([(ROOF[0], 3.86)], air_c=16.9, wind_ms=1.4)
    assert 54 < temp < 60


def test_chart_wind_from_the_recorder_is_converted_from_km_h() -> None:
    pts = _convert_weather_points([{"t": 1.0, "v": 19.8}], kind="wind_speed_ms", unit="km/h")
    assert abs(pts[0]["v"] - 5.5) < 1e-6


def test_chart_smooths_only_samples_from_before_the_lag() -> None:
    pts = [{"t": 0.0, "v": 20.0}, {"t": 300_000.0, "v": 60.0}, {"t": 600_000.0, "v": 20.0}, {"t": 900_000.0, "v": 60.0}]
    out = _smooth_unlagged_points(pts, 600_000.0)
    assert out[1]["v"] < 60.0  # old instant-model sample smoothed
    assert out[2:] == pts[2:]  # lagged samples untouched
