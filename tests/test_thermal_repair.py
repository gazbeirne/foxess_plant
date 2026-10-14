"""Ledger panel temperatures from before the thermal lag (performance/thermal_repair.py)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.foxess_plant.performance.thermal_repair import repair_ledger_panel_temps, smoothed_day_temps

UK = ZoneInfo("Europe/London")


def _samples(day: date, temps: list[tuple[str, float, float]]) -> list[dict]:
    """[(HH:MM, panel temp, pv kW)] as stored (local ISO with offset)."""
    out = []
    for hhmm, temp, pv in temps:
        h, m = map(int, hhmm.split(":"))
        out.append({"ts": datetime(day.year, day.month, day.day, h, m, tzinfo=UK).isoformat(), "virtual_panel_temp_c": temp, "pv_power_kw": pv})
    return out


def _cloudy_spikes(day: date) -> list[dict]:
    """A raw unlagged day: 25 °C with one 5-minute spike to 72 °C (a cloud edge), like 13 Oct."""
    rows = []
    t = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UK)
    for i in range(36):
        temp = 72.0 if i == 18 else 25.0
        rows.append((t.strftime("%H:%M"), temp, 2.0))
        t += timedelta(minutes=5)
    return _samples(day, rows)


class _Store:
    def __init__(self, ledger: dict[str, dict], samples: dict[str, list[dict]]):
        self.ledger = ledger
        self.samples = samples
        self.upserts = 0

    def list_ledger_between(self, start: str, end: str):
        return [dict(r) for d, r in sorted(self.ledger.items()) if start <= d <= end]

    def list_intraday_samples(self, start_ts: str, end_ts: str):
        return [s for d, rows in self.samples.items() for s in rows if start_ts <= s["ts"] <= end_ts]

    def upsert_daily_ledger(self, row):
        self.ledger[row["date"]] = dict(row)
        self.upserts += 1


def _row(day: str, t_max=72.1) -> dict:
    return {
        "date": day,
        "forecast_accuracy_pct": 99.3,
        "peak_vs_rated_pct": 105.4,
        "solar_day_class": "under_forecast",
        "clipping_loss_kwh": 0.0,
        "virtual_temp_min_c": 15.7,
        "virtual_temp_max_c": t_max,
        "temp_adjusted_index_pct": 104.2,
        "insight_note": "old",
        "soiling_recovery_note": None,
        "visibility_avg_km": 15.2,
    }


def test_spikes_are_smoothed_like_the_chart() -> None:
    t_min, t_max, avg = smoothed_day_temps(_cloudy_spikes(date(2026, 10, 13)), lagged_since_ms=None)
    assert t_min == 25.0 and 35 < t_max < 45  # one 5-min spike only gets ~40 % of the way with tau 10 min
    assert 25 < avg < 27


def test_days_before_the_lag_are_rebuilt_once_and_later_days_left_alone() -> None:
    day = date(2026, 10, 13)
    since = datetime(2026, 10, 14, 7, 10, tzinfo=UK).isoformat()
    store = _Store(
        {"2026-10-13": _row("2026-10-13"), "2026-10-15": _row("2026-10-15", t_max=48.0)},
        {"2026-10-13": _cloudy_spikes(day), "2026-10-15": _cloudy_spikes(date(2026, 10, 15))},
    )
    assert repair_ledger_panel_temps(store, lag_since=since, today=date(2026, 10, 16)) == ["2026-10-13"]
    fixed = store.ledger["2026-10-13"]
    assert fixed["virtual_temp_max_c"] < 45 and "panel temp 25–" in fixed["insight_note"] and "72" not in fixed["insight_note"]
    assert fixed["temp_adjusted_index_pct"] != 104.2
    assert store.ledger["2026-10-15"]["virtual_temp_max_c"] == 48.0  # after the lag: already right
    # Running again changes nothing (the raw samples are smoothed the same way)
    assert repair_ledger_panel_temps(store, lag_since=since, today=date(2026, 10, 16)) == []
    assert store.upserts == 1


def test_install_day_only_smooths_the_part_before_the_lag() -> None:
    day = date(2026, 10, 14)
    raw_morning = _samples(day, [("07:00", 10.0, 0.5), ("07:05", 40.0, 0.5)])  # raw spike before the lag
    lagged = _samples(day, [("12:00", 30.0, 2.0), ("12:05", 31.0, 2.0)])  # already lagged
    since_ms = datetime(2026, 10, 14, 7, 10, tzinfo=UK).timestamp() * 1000
    t_min, t_max, _avg = smoothed_day_temps(raw_morning + lagged, lagged_since_ms=since_ms)
    assert t_max == 31.0 and t_min == 10.0  # morning spike damped below 31; lagged values kept


def test_no_lag_yet_or_no_samples_does_nothing() -> None:
    store = _Store({"2026-10-13": _row("2026-10-13")}, {})
    assert repair_ledger_panel_temps(store, lag_since=None, today=date(2026, 10, 16)) == []
    assert repair_ledger_panel_temps(store, lag_since="2026-10-14T07:10:00+01:00", today=date(2026, 10, 16)) == []
