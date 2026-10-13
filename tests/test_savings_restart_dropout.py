"""Savings counters across HA restarts (13 Oct 2026: £36 'saved' from ~21 re-counts of the day's import)."""

from custom_components.foxess_plant.performance import financial
from custom_components.foxess_plant.performance.repair import repaired_financials


def _a(imp: float, exp: float = 0.0, load: float = 0.0) -> dict:
    return {"load_from_grid_kwh_today": imp, "pv_to_grid_kwh_today": exp, "load_consumption_kwh_today": load}


def _run(seq: list[float]) -> float:
    last, total = None, 0.0
    for v in seq:
        deltas, last = financial.counter_deltas(last, _a(v))
        total += deltas["import"]
    return total


def test_restart_dropout_is_not_counted_again() -> None:
    # Counter at 7.0, reads 0 for a tick or two after a restart, comes back at 7.1
    assert abs(_run([7.0, 0.0, 0.0, 7.1, 7.3]) - 0.3) < 1e-9


def test_many_restarts_never_inflate_the_day() -> None:
    seq = [5.0]
    for i in range(20):
        seq += [0.0, 5.0 + 0.1 * (i + 1)]
    assert abs(_run(seq) - 2.0) < 1e-9


def test_real_reset_counts_from_zero() -> None:
    # Counter resets just after our own midnight rollover and climbs from 0
    assert abs(_run([12.0, 0.0, 0.2, 0.5]) - 0.5) < 1e-9


def test_repair_reprices_an_inflated_day_at_its_own_rates() -> None:
    counted = {"import_kwh": 157.0, "export_kwh": 49.3, "import_spend_gbp": 28.36, "export_earnings_gbp": 6.41,
               "avoided_grid_cost_gbp": 29.91}
    fixed = repaired_financials(counted, {"import": 7.3, "export": 11.6, "load": 16.8},
                                fallback_import_p=None, fallback_export_p=None)
    imp_p, exp_p = 28.36 * 100 / 157.0, 6.41 * 100 / 49.3
    assert fixed["import_kwh"] == 7.3 and fixed["export_kwh"] == 11.6
    assert abs(fixed["export_earnings_gbp"] - 11.6 * exp_p / 100) < 0.01
    assert abs(fixed["avoided_grid_cost_gbp"] - (16.8 - 7.3) * imp_p / 100) < 0.01
    assert fixed["net_daily_savings_gbp"] < 5


def test_repair_leaves_a_normal_day_alone() -> None:
    counted = {"import_kwh": 7.6, "export_kwh": 11.2, "import_spend_gbp": 1.4, "export_earnings_gbp": 1.46,
               "avoided_grid_cost_gbp": 1.7}
    assert repaired_financials(counted, {"import": 7.3, "export": 11.6, "load": 16.8},
                               fallback_import_p=18.0, fallback_export_p=13.0) is None


def test_repair_catches_inflated_load_alone() -> None:
    # 7 Oct style: little import counted, but avoided cost implies far more load than the counter saw
    counted = {"import_kwh": 2.5, "export_kwh": 3.0, "import_spend_gbp": 0.45, "export_earnings_gbp": 0.39,
               "avoided_grid_cost_gbp": 24.68}
    fixed = repaired_financials(counted, {"import": 2.5, "export": 3.0, "load": 14.0},
                                fallback_import_p=None, fallback_export_p=None)
    assert fixed is not None and fixed["avoided_grid_cost_gbp"] < 3
