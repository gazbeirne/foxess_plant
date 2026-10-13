"""House load is the inverter's load counter, not load + charge + import − discharge (13 Oct 2026)."""

from custom_components.foxess_plant.analytics import compute_analytics, house_load_kwh, load_from_pv_battery_kwh


def _states(**kw: float) -> dict[str, str]:
    return {k: str(v) for k, v in kw.items()}


def test_load_is_the_inverter_counter() -> None:
    # 13 Oct 2026: inverter load 3.4, charge 8.0, discharge 1.9, import 7.1 → old formula said 16.6
    a = compute_analytics(_states(solar_energy_today=13.7, feed_in_energy_today=10.5, load_energy_today=3.4,
                                  battery_charge_today=8.0, battery_discharge_today=1.9,
                                  grid_consumption_energy_today=7.1))
    assert a["load_consumption_kwh_today"] == 3.4


def test_energy_balance_when_the_inverter_has_no_load_counter() -> None:
    load = house_load_kwh(inverter_load=0.0, pv=13.7, from_grid=7.1, to_grid=10.5, charge=8.0, discharge=1.9)
    assert abs(load - 4.2) < 1e-9


def test_overnight_grid_charge_is_not_house_load() -> None:
    # 7.1 kWh imported, but 8 kWh went into the battery and only 3.2 kWh of solar stayed local:
    # at least 4.8 kWh of the import charged the battery, so at most 2.3 kWh of import fed the house
    assert abs(load_from_pv_battery_kwh(load=3.4, from_grid=7.1, pv=13.7, to_grid=10.5, charge=8.0) - 1.1) < 1e-9
