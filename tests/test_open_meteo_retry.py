"""Open-Meteo scoring must not retry every 10-minute tick after a failure (the Solcast lesson)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.foxess_plant import open_meteo as om
from custom_components.foxess_plant.coordinator import FoxessPlantCoordinator
from custom_components.foxess_plant.models import OpenMeteoConfig, PvSystemConfig


class _Fake:
    def __init__(self) -> None:
        self.plant = SimpleNamespace(open_meteo=OpenMeteoConfig(enabled=True), pv_config=PvSystemConfig())
        self._open_meteo_cache = {"fetched_at": "2099-01-01T00:00:00+00:00"}  # forecast not due
        self._open_meteo_lock = asyncio.Lock()
        self.hass = SimpleNamespace()
        self.saves = 0
        self.calls = 0

    def _open_meteo_location(self):
        return (52.0, -1.0)

    def _open_meteo_due(self, key, every):
        return FoxessPlantCoordinator._open_meteo_due(self, key, every)

    def _save_open_meteo(self):
        self.saves += 1

    async def async_request_refresh(self):
        pass


@pytest.mark.asyncio
async def test_failed_scoring_waits_an_hour() -> None:
    fake = _Fake()

    async def _fail(*args, **kwargs):
        fake.calls += 1
        raise om.OpenMeteoError("HTTP 502")

    with patch.object(om, "async_fetch_day_ahead_daily", _fail), patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession", lambda hass: None
    ):
        await FoxessPlantCoordinator._async_refresh_open_meteo(fake)
        await FoxessPlantCoordinator._async_refresh_open_meteo(fake)  # the next 10-minute tick
        assert fake.calls == 1
        assert fake._open_meteo_cache["score_error"] == "HTTP 502"
        assert fake.saves == 2  # the attempt time is persisted, so a restart doesn't retry at once
        await FoxessPlantCoordinator._async_refresh_open_meteo(fake, rescore=True)
        assert fake.calls == 2
