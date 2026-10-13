"""Daily energy analytics derived from mapped inverter entities."""

from __future__ import annotations

from typing import Any


def _float_state(states: dict[str, str | None], key: str) -> float:
    raw = states.get(key)
    if raw in (None, "unknown", "unavailable", ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def house_load_kwh(*, inverter_load: float, pv: float, from_grid: float, to_grid: float, charge: float,
                   discharge: float) -> float:
    """The house's consumption today.

    The inverter's own "load energy" counter already IS the house load. (Fox Plant used to add battery charge
    and grid import to it and subtract discharge, double-counting: 16.8 kWh for a 3.4 kWh day on 13 Oct 2026,
    when the energy balance below gave 4.2.) Without that counter, use the balance: what came in (PV, grid,
    battery) minus what went out (export, battery); it includes inverter losses, so reads a little high.
    """
    if inverter_load > 0:
        return inverter_load
    return max(0.0, pv + from_grid + discharge - to_grid - charge)


def load_from_pv_battery_kwh(*, load: float, from_grid: float, pv: float, to_grid: float, charge: float) -> float:
    """House load met by solar or the battery: the load minus the grid import that went to the house.

    Grid import also charges the battery (SmartCharge overnight). Whatever battery charging the solar kept
    locally couldn't cover must have come from the grid, so that part of the import isn't house load.
    """
    grid_to_battery = max(0.0, charge - max(0.0, pv - to_grid))
    grid_to_house = max(0.0, from_grid - grid_to_battery)
    return max(0.0, load - grid_to_house)


def compute_analytics(entity_states: dict[str, str | None]) -> dict[str, Any]:
    """Daily solar / grid / battery / house figures from the inverter's 'today' counters."""
    pv = _float_state(entity_states, "solar_energy_today")
    to_grid = _float_state(entity_states, "feed_in_energy_today")
    # Export (feed-in) cannot exceed PV generation for the same period.
    if pv > 0:
        to_grid = min(to_grid, pv)
    to_load_battery = max(0.0, pv - to_grid)

    battery_discharge = _float_state(entity_states, "battery_discharge_today")
    battery_charge = _float_state(entity_states, "battery_charge_today")
    from_grid = _float_state(entity_states, "grid_consumption_energy_today")
    load_consumption = house_load_kwh(
        inverter_load=_float_state(entity_states, "load_energy_today"),
        pv=pv,
        from_grid=from_grid,
        to_grid=to_grid,
        charge=battery_charge,
        discharge=battery_discharge,
    )
    from_pv_battery = load_from_pv_battery_kwh(
        load=load_consumption, from_grid=from_grid, pv=pv, to_grid=to_grid, charge=battery_charge
    )

    self_consumption = min(100.0, max(0.0, (to_load_battery / pv) * 100)) if pv > 0 else 0.0
    self_sufficiency = (
        min(100.0, max(0.0, (from_pv_battery / load_consumption) * 100))
        if load_consumption > 0
        else 0.0
    )

    return {
        "pv_production_kwh_today": round(pv, 2),
        "pv_to_load_battery_kwh_today": round(to_load_battery, 2),
        "pv_to_grid_kwh_today": round(to_grid, 2),
        "load_consumption_kwh_today": round(load_consumption, 2),
        "load_from_pv_battery_kwh_today": round(from_pv_battery, 2),
        "load_from_grid_kwh_today": round(from_grid, 2),
        "self_consumption_percent_today": round(self_consumption, 1),
        "self_sufficiency_percent_today": round(self_sufficiency, 1),
        # Panel expects these balance keys (aliases for balance card).
        "battery_charge_kwh_today": round(battery_charge, 2),
        "battery_discharge_kwh_today": round(battery_discharge, 2),
    }
