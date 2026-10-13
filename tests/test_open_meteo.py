"""Open-Meteo PV forecast: parsing, scoring, blending and how it combines with Solcast."""

from datetime import datetime, timedelta, timezone

from custom_components.foxess_plant import open_meteo as om
from custom_components.foxess_plant.models import OpenMeteoConfig, PvStringConfig, PvSystemConfig

UTC = timezone.utc


def test_azimuth_compass_to_open_meteo() -> None:
    assert om.open_meteo_azimuth(180) == 0  # south
    assert om.open_meteo_azimuth(164) == -16  # a little east of south
    assert om.open_meteo_azimuth(90) == -90  # east
    assert om.open_meteo_azimuth(270) == 90  # west
    assert om.open_meteo_azimuth(0) == -180  # north


def test_arrays_use_pv_configuration_and_group_same_orientation() -> None:
    pv = PvSystemConfig(
        pv1=PvStringConfig(enabled=True, panel_count=10, watts_per_panel=450, efficiency_factor=99.0, tilt=45, azimuth=164),
        pv2=PvStringConfig(enabled=True, panel_count=4, watts_per_panel=400, efficiency_factor=100.0, tilt=45, azimuth=164),
    )
    arrays = om.plant_arrays(pv)
    assert len(arrays) == 1
    assert arrays[0].tilt == 45 and arrays[0].azimuth == -16
    assert abs(arrays[0].kwp - (4.455 + 1.6)) < 0.01


def test_series_reads_model_suffixed_keys_and_single_model_plain_key() -> None:
    payload = {"hourly": {"time": [100, 200], "global_tilted_irradiance_gfs_seamless": [50.0, None],
                          "global_tilted_irradiance_icon_seamless": [10.0, 20.0]}}
    out = om._series(payload, "global_tilted_irradiance", ["gfs_seamless", "icon_seamless", "dmi_seamless"])
    assert out == {"gfs_seamless": {100: 50.0}, "icon_seamless": {100: 10.0, 200: 20.0}}
    single = {"hourly": {"time": [100], "global_tilted_irradiance": [5.0]}}
    assert om._series(single, "global_tilted_irradiance", ["gfs_seamless"]) == {"gfs_seamless": {100: 5.0}}


def test_scoring_is_leave_one_out_and_weights_favour_the_accurate_model() -> None:
    days = [f"2026-10-{d:02d}" for d in range(1, 11)]
    actual = {d: 10.0 + i for i, d in enumerate(days)}
    good = {d: actual[d] / 0.9 for d in days}  # perfect shape, needs a 0.9 factor
    noisy = {d: actual[d] * (1.4 if i % 2 else 0.6) for i, d in enumerate(days)}
    scores = om.score_models({"good": good, "noisy": noisy}, actual)
    assert scores["good"]["factor"] == 0.9
    assert scores["good"]["mae_kwh"] < 0.01
    assert scores["noisy"]["mae_kwh"] > 3
    weights = om.model_weights(scores, ["good", "noisy"])
    assert weights["good"] > 0.9 and abs(sum(weights.values()) - 1) < 1e-9


def test_equal_weights_and_default_factor_until_enough_days() -> None:
    actual = {"2026-10-01": 10.0, "2026-10-02": 12.0, "2026-10-03": 8.0}
    scores = om.score_models({"a": actual, "b": actual}, actual)
    assert scores["a"]["days"] == 3 < om.MIN_SCORED_DAYS
    assert om.model_weights(scores, ["a", "b"]) == {"a": 0.5, "b": 0.5}
    assert om.model_factor(scores, "a") == om.DEFAULT_PERFORMANCE_FACTOR


def test_blend_rows_are_half_hourly_end_stamped_and_calibrated() -> None:
    end = datetime(2026, 10, 14, 12, tzinfo=UTC)
    forecast = {"a": {int(end.timestamp()): 4.0}, "b": {int(end.timestamp()): 2.0}}
    rows = om.blend_rows(forecast, {}, ["a", "b"], now=end - timedelta(hours=12))
    assert [r["period_end"] for r in rows] == [(end - timedelta(minutes=30)).isoformat(), end.isoformat()]
    assert all(r["period_start"] == r["period_end"] for r in rows)
    assert rows[0]["pv_estimate"] == round(3.0 * om.DEFAULT_PERFORMANCE_FACTOR, 4)
    assert rows[0]["source"] == om.SOURCE


def _rows(start: datetime, n: int, kw: float, source: str | None = None) -> list[dict]:
    out = []
    for i in range(n):
        iso = (start + timedelta(minutes=30 * (i + 1))).isoformat()
        row = {"period_start": iso, "period_end": iso, "pv_estimate": kw}
        if source:
            row["source"] = source
        out.append(row)
    return out


def test_fresh_solcast_wins_and_open_meteo_fills_after_it() -> None:
    t0 = datetime(2026, 10, 14, 8, tzinfo=UTC)
    solcast = _rows(t0, 4, 1.0)
    meteo = _rows(t0, 8, 2.0, om.SOURCE)
    combined = om.combine_forecasts(solcast, meteo, primary="solcast", solcast_fresh=True)
    assert [r["pv_estimate"] for r in combined] == [1.0] * 4 + [2.0] * 4


def test_stale_solcast_gives_way_to_open_meteo() -> None:
    t0 = datetime(2026, 10, 14, 8, tzinfo=UTC)
    solcast = _rows(t0 - timedelta(hours=1), 6, 1.0)
    meteo = _rows(t0, 4, 2.0, om.SOURCE)
    combined = om.combine_forecasts(solcast, meteo, primary="solcast", solcast_fresh=False)
    # Old Solcast rows only fill the hour Open-Meteo lacks
    assert [r["pv_estimate"] for r in combined] == [1.0, 1.0, 2.0, 2.0, 2.0, 2.0]
    assert om.source_at(combined, t0 + timedelta(minutes=45)) == om.SOURCE


def test_open_meteo_primary_wins_even_with_fresh_solcast() -> None:
    t0 = datetime(2026, 10, 14, 8, tzinfo=UTC)
    combined = om.combine_forecasts(_rows(t0, 2, 1.0), _rows(t0, 2, 2.0, om.SOURCE), primary="open_meteo", solcast_fresh=True)
    assert [r["pv_estimate"] for r in combined] == [2.0, 2.0]


def test_config_defaults_and_unknown_models_dropped() -> None:
    cfg = OpenMeteoConfig.from_dict(None)
    assert not cfg.enabled and cfg.primary == "solcast" and cfg.models == list(om.DEFAULT_MODELS)
    cfg = OpenMeteoConfig.from_dict({"enabled": True, "primary": "nonsense", "models": ["gfs_seamless", "made_up"]})
    assert cfg.primary == "solcast" and cfg.models == ["gfs_seamless"]


def test_chart_points_one_per_hour_at_mid_hour() -> None:
    end = datetime(2026, 10, 14, 12, tzinfo=UTC)
    rows = om.blend_rows({"a": {int(end.timestamp()): 2.0}}, {}, ["a"], now=end)
    points = om.chart_points(rows)
    assert points == [{"t": round((end - timedelta(minutes=30)).timestamp() * 1000), "v": rows[0]["pv_estimate"]}]


def test_history_keeps_past_days_frozen_and_today_live() -> None:
    from datetime import date

    tz = timezone.utc
    today = date(2026, 10, 14)

    def pts(day: date, kw: float) -> list[dict]:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return [{"t": (start + timedelta(hours=h, minutes=30)).timestamp() * 1000, "v": kw} for h in range(24)]

    history = {"2026-10-12": [[1.0, 9.9]], "2026-09-01": [[1.0, 1.0]]}
    new = om.update_history(history, pts(date(2026, 10, 12), 5.0) + pts(today, 2.0), today=today, tz=tz)
    assert new["2026-10-12"] == [[1.0, 9.9]]  # two days back: frozen
    assert len(new["2026-10-14"]) == 24  # today: from the latest blend
    assert "2026-09-01" not in new  # older than HISTORY_DAYS
    # A past day the live blend no longer covers comes from history
    assert om.points_for_day([], new, date(2026, 10, 12), tz) == [{"t": 1.0, "v": 9.9}]
    assert len(om.points_for_day(pts(today, 3.0), new, today, tz)) == 24


def test_daily_kwh_needs_a_whole_day() -> None:
    tz = timezone.utc
    start = datetime(2026, 10, 14, 1, tzinfo=UTC)
    full = {int((start + timedelta(hours=h)).timestamp()): 1.0 for h in range(24)}
    assert om._daily_kwh(full, tz) == {"2026-10-14": 24.0}
    partial = dict(list(full.items())[:10])
    assert om._daily_kwh(partial, tz) == {}
