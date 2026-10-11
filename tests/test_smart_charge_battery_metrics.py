"""Unit tests for SmartCharge battery metric resolution."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "foxess_plant" / "smart_charge"


def _load(name: str, rel: str):
    path = PKG / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load("sc_battery_metrics_test", "battery_metrics.py")


class BatteryMetricsTests(unittest.TestCase):
    def test_evo_nominal_and_soc_derive_remaining(self) -> None:
        values = {
            "battery_soc": 80.0,
            "bms_kwh_remaining_1": 10.24,
        }

        def read_float(key: str):
            return values.get(key)

        soc, capacity, remaining = bm.resolve_battery_metrics(
            read_float=read_float,
            read_unit=lambda _key: "kWh",
        )
        self.assertEqual(soc, 80.0)
        self.assertAlmostEqual(capacity, 10.24, places=2)
        self.assertAlmostEqual(remaining, 8.192, places=2)

    def test_derive_capacity_from_remaining_and_soc(self) -> None:
        values = {
            "battery_soc": 80.0,
            "battery_kwh_remaining": 8.0,
        }

        def read_float(key: str):
            return values.get(key)

        _soc, capacity, remaining = bm.resolve_battery_metrics(
            read_float=read_float,
            read_unit=lambda _key: "",
        )
        self.assertAlmostEqual(capacity, 10.0, places=2)
        self.assertAlmostEqual(remaining, 8.0, places=2)

    def test_parse_percent_state(self) -> None:
        self.assertEqual(bm.parse_state_float("87%"), 87.0)

    def test_parse_state_with_trailing_unit(self) -> None:
        self.assertEqual(bm.parse_state_float("3.2 km/h"), 3.2)
        self.assertEqual(bm.parse_state_float("1.5m/s"), 1.5)
        self.assertEqual(bm.parse_state_float("12,5"), 12.5)


def _nights(nights: int, kwh_per_hour: float, kwh_per_pct: float, *, hours: int = 12, start_soc: float = 95.0):
    """Hourly SOC means / discharge / charge for nights of steady discharge (daytime charging between)."""
    soc, dis, chg = {}, {}, {}
    for n in range(nights):
        for h in range(hours):
            t = n * 86400.0 + h * 3600.0
            soc[t] = start_soc - h * kwh_per_hour / kwh_per_pct
            dis[t] = kwh_per_hour
            chg[t] = 0.0
        day = n * 86400.0 + hours * 3600.0
        soc[day], dis[day], chg[day] = start_soc, 0.0, 2.0  # recharge hour breaks the run
    return soc, dis, chg


class MeasuredCapacityTests(unittest.TestCase):
    def test_real_world_ratio(self) -> None:
        # 9-10 Oct 2026: ~8.0 kWh per 100 % on the discharge counter, measured RTE 0.8.
        soc, dis, chg = _nights(4, 0.5, 0.08)
        cap = bm.measured_usable_capacity_kwh(soc, dis, chg, round_trip_efficiency=0.8, nominal_kwh=10.24)
        self.assertAlmostEqual(cap, 8.0 / 0.8**0.5, places=1)

    def test_charging_hours_are_skipped(self) -> None:
        soc, dis, chg = _nights(4, 0.5, 0.08)
        # A charging hour with a bogus SOC jump must not count.
        soc[6 * 3600.0] -= 20
        chg[6 * 3600.0] = 1.0
        cap = bm.measured_usable_capacity_kwh(soc, dis, chg, round_trip_efficiency=0.8, nominal_kwh=10.24)
        self.assertAlmostEqual(cap, 8.0 / 0.8**0.5, places=1)

    def test_too_little_history(self) -> None:
        soc, dis, chg = _nights(1, 0.5, 0.08)
        self.assertIsNone(bm.measured_usable_capacity_kwh(soc, dis, chg, round_trip_efficiency=0.8, nominal_kwh=10.24))

    def test_implausible_against_nominal(self) -> None:
        soc, dis, chg = _nights(4, 0.5, 0.03)  # 3 kWh per 100 % on a 10 kWh pack: counters wrong
        self.assertIsNone(bm.measured_usable_capacity_kwh(soc, dis, chg, round_trip_efficiency=0.8, nominal_kwh=10.24))


if __name__ == "__main__":
    unittest.main()
