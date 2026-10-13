"""Repair daily savings that HA restarts inflated, using the inverter's own lifetime energy counters.

Until 0.9.507 a daily counter that read ~0 right after a restart (Glow MQTT not yet reporting) was taken as a
reset, so its return to the real value was counted as new energy: each restart re-added the day's import,
export and load so far. 13 Oct 2026 priced ~157 kWh of import for a 7.3 kWh day (£36 "saved").

The raw per-tick energy isn't kept, so a damaged day is re-priced from its true kWh at the average rates its
own (inflated, but consistently priced) totals imply. That keeps each day's import/export price mix, close
enough for savings; only days whose counted kWh are clearly above the counters are touched, so it is safe to
run at every start.
"""

from __future__ import annotations

from typing import Any

TOLERANCE = 0.25  # counted kWh may exceed the counters by 25 % + 0.5 kWh (meter vs inverter CT)
SLACK_KWH = 0.5


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _over(counted: float, actual: float) -> bool:
    return counted > actual * (1 + TOLERANCE) + SLACK_KWH


def repaired_financials(
    counted: dict[str, Any],
    actual: dict[str, float],
    *,
    fallback_import_p: float | None,
    fallback_export_p: float | None,
) -> dict[str, float] | None:
    """Corrected money lines for one day, or None when its counted kWh look right.

    ``counted``: a ledger row or today's accumulator (import_kwh, export_kwh, import_spend_gbp,
    export_earnings_gbp, avoided_grid_cost_gbp). ``actual``: kWh from the counters (import, export, load).
    """
    imp_c, exp_c = _f(counted.get("import_kwh")), _f(counted.get("export_kwh"))
    spend, earn, avoided = (
        _f(counted.get("import_spend_gbp")),
        _f(counted.get("export_earnings_gbp")),
        _f(counted.get("avoided_grid_cost_gbp")),
    )
    a_imp, a_exp, a_load = actual.get("import"), actual.get("export"), actual.get("load")
    if a_imp is None or a_exp is None or a_load is None:
        return None
    imp_p = spend * 100 / imp_c if imp_c > 0.1 else fallback_import_p
    exp_p = earn * 100 / exp_c if exp_c > 0.1 else fallback_export_p
    if imp_p is None or exp_p is None:
        return None
    # Load isn't stored; avoided = (load − import) × import price gives the load that was priced
    load_c = (avoided * 100 / imp_p + imp_c) if imp_p > 0 else 0.0
    if not (_over(imp_c, a_imp) or _over(exp_c, a_exp) or _over(load_c, a_load)):
        return None
    export_earnings = a_exp * exp_p / 100
    avoided_cost = (a_load - a_imp) * imp_p / 100
    return {
        "import_kwh": round(a_imp, 3),
        "export_kwh": round(a_exp, 3),
        "import_spend_gbp": round(a_imp * imp_p / 100, 2),
        "export_earnings_gbp": round(export_earnings, 2),
        "avoided_grid_cost_gbp": round(avoided_cost, 2),
        "net_daily_savings_gbp": round(avoided_cost + export_earnings, 2),
    }
