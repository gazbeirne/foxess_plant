"""Export priced apart from import (tariff_export.py): migration, provider rules, rate overlay, fetch spacing."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.foxess_plant.coordinator import FoxessPlantCoordinator
from custom_components.foxess_plant.models import TariffConfig
from custom_components.foxess_plant.tariff_export import export_mode, flat_rows


def _cfg(**raw) -> TariffConfig:
    return TariffConfig.from_dict(raw)


EON = {"enabled": True, "provider": "eon_next", "source": "native", "refresh_token": "tok"}


def test_migrates_export_from_manual_to_manual_rates() -> None:
    cfg = _cfg(dynamic={**EON, "export_from_api": False})
    assert cfg.export_provider == "manual"
    assert cfg.dynamic.export_from_api is False
    assert export_mode(cfg) == "manual"


def test_migrates_supplier_export_to_same() -> None:
    cfg = _cfg(dynamic=EON)
    assert cfg.export_provider == "same" and cfg.dynamic.export_from_api is True
    assert export_mode(cfg) == "import"


def test_migrates_without_a_supplier() -> None:
    assert _cfg(export_source="entity", export_entity="sensor.seg").export_provider == "entity"
    assert _cfg().export_provider == "manual"


def test_same_supplier_for_export_is_one_login() -> None:
    cfg = _cfg(dynamic=EON, export_provider="eon_next")
    assert cfg.export_provider == "same"
    assert cfg.export_supplier.enabled is False


def test_different_export_supplier() -> None:
    cfg = _cfg(
        dynamic=EON,
        export_provider="octopus",
        export_supplier={"api_key": "sk_x", "account_number": "a-1"},
    )
    assert cfg.export_provider == "octopus" and export_mode(cfg) == "supplier"
    assert cfg.dynamic.export_from_api is False  # E.ON doesn't fetch export
    sup = cfg.export_supplier
    assert sup.enabled and sup.provider == "octopus" and sup.native_credentials_configured()
    again = TariffConfig.from_dict(cfg.to_dict(include_secrets=True))
    assert again.export_supplier.api_key == "sk_x" and again.export_provider == "octopus"
    assert "api_key" not in cfg.to_dict()["export_supplier"]  # never sent to the panel


def test_same_without_an_import_supplier_falls_back() -> None:
    assert _cfg(export_provider="same").export_provider == "manual"
    assert _cfg(export_provider="same", export_source="entity").export_provider == "entity"


def test_entity_and_manual_set_the_band_source() -> None:
    assert _cfg(export_provider="entity", export_entity="sensor.x").export_source == "entity"
    assert _cfg(export_provider="manual", export_source="entity").export_source == "schedule"


def test_flat_rows_cover_two_days_from_this_half_hour() -> None:
    now = dt_util.utcnow().replace(minute=40, second=5)
    rows = flat_rows(15.0, now=now)
    assert len(rows) == 96 and rows[0]["valid_from"].startswith(now.strftime("%Y-%m-%dT%H:30"))
    assert {r["value_inc_vat"] for r in rows} == {15.0}


class _Fake:
    def __init__(self, tariff: TariffConfig) -> None:
        self.plant = SimpleNamespace(tariff=tariff)
        self._export_supplier_cache: dict = {}
        self._octopus_cache: dict = {}
        self.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: None))

    def _export_override_rows(self):
        return FoxessPlantCoordinator._export_override_rows(self)

    def _export_entity_rows(self):
        return FoxessPlantCoordinator._export_entity_rows(self)

    def _smart_charge_import_based_rate_rows(self):
        return [{"valid_from": "a", "value_inc_vat": 30.0}], [{"valid_from": "a", "value_inc_vat": 0.0}]


def test_planner_uses_the_export_supplier_whatever_import_is() -> None:
    fake = _Fake(_cfg(export_provider="octopus", export_supplier={"api_key": "k", "account_number": "a"}))
    outgoing = flat_rows(21.0)
    fake._export_supplier_cache = {"export_rates": outgoing}
    imp, exp = FoxessPlantCoordinator._smart_charge_rate_rows(fake)
    assert imp[0]["value_inc_vat"] == 30.0 and exp == outgoing


def test_planner_keeps_import_export_rows_for_same_and_manual() -> None:
    fake = _Fake(_cfg(dynamic=EON))
    assert FoxessPlantCoordinator._smart_charge_rate_rows(fake)[1][0]["value_inc_vat"] == 0.0


def test_export_supplier_without_rates_yet_falls_back() -> None:
    fake = _Fake(_cfg(export_provider="octopus", export_supplier={"api_key": "k", "account_number": "a"}))
    assert FoxessPlantCoordinator._smart_charge_rate_rows(fake)[1][0]["value_inc_vat"] == 0.0


class _RefreshFake(_Fake):
    def __init__(self, tariff):
        super().__init__(tariff)
        self.calls = 0
        self.saved = 0

    def _octopus_parse_iso_age(self, iso):
        return FoxessPlantCoordinator._octopus_parse_iso_age(self, iso)

    async def _async_fetch_export_supplier(self, cfg):
        from custom_components.foxess_plant.octopus_api import OctopusApiError

        self.calls += 1
        raise OctopusApiError("HTTP 503")

    def _save_export_supplier(self):
        self.saved += 1

    def _apply_manual_export_to_cache(self):
        pass

    def _sync_octopus_current_rates_from_cache(self):
        pass

    async def async_request_refresh(self):
        pass


@pytest.mark.asyncio
async def test_failed_export_fetch_waits_and_is_saved() -> None:
    fake = _RefreshFake(_cfg(export_provider="octopus", export_supplier={"api_key": "k", "account_number": "a"}))
    fake._export_supplier_cache = {"provider": "octopus"}
    await FoxessPlantCoordinator._async_refresh_export_supplier(fake)
    await FoxessPlantCoordinator._async_refresh_export_supplier(fake)  # next 10-minute tick
    assert fake.calls == 1 and fake.saved == 1  # the attempt time is saved, so a restart waits too
    assert fake._export_supplier_cache["last_error"] == "HTTP 503"
    await FoxessPlantCoordinator._async_refresh_export_supplier(fake, force=True)
    assert fake.calls == 2
