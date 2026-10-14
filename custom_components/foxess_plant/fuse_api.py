"""Fuse Energy client: signs in to the Fuse web app and reads the current tariffs.

Fuse has no public API, and its mobile API (api.fuseenergy.com) has no tariff call we could find. The web app
(www.fuseenergy.com/app) shows the tariff, so Fox Plant signs in the way the website does:

1. ``phoneSignIn`` {phone} - Fuse texts a six-digit code (and sets ``session_id`` / ``otp`` cookies).
2. ``verifyOtp`` {code} - sets the ``app-auth`` cookie (Fuse gives it a one-year life; the website renews the tokens
   inside it).
3. With that cookie: ``customerPremises`` and ``suppliesCurrentContracts`` (tRPC JSON), then each supply's page
   payload (``RSC: 1``), which holds the tariff (fuse_parse.py).

Cookies are kept by Fox Plant itself (a cookie-less session, so nothing leaks into Home Assistant's shared one) and
handed to ``on_cookies`` to persist whenever Fuse changes them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from .fuse_parse import (
    SUPPLY_TYPES,
    FuseParseError,
    apply_contract,
    contracted_supplies,
    electricity_supplies,
    rsc_value,
    tariff_from_bill,
    tariff_from_cards,
    trpc_data,
    trpc_error,
)
from .octopus_api import OctopusApiError

_LOGGER = logging.getLogger(__name__)

FUSE_WEB = "https://www.fuseenergy.com"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_KEEP_COOKIES = ("app-auth", "session_id", "otp")


class FuseApiError(OctopusApiError):
    """Fuse request failed (subclass so shared tariff error handling catches it)."""


class FuseAuthRequired(FuseApiError):
    """Not signed in, or Fuse ended the session: the user must sign in again with a new code."""


def parse_set_cookie(header: str) -> tuple[str, str | None] | None:
    """(name, value) from a Set-Cookie header; value None when the cookie is being deleted.

    Parsed by hand: http.cookies rejects values with '/', '=' and the like, which long encoded cookies contain.
    """
    first, _, attrs = str(header).partition(";")
    name, sep, value = first.partition("=")
    name = name.strip()
    if not sep or not name:
        return None
    value = value.strip().strip('"')
    for attr in attrs.split(";"):
        key, _, val = attr.strip().partition("=")
        key = key.strip().lower()
        if key == "max-age" and val.strip().lstrip("-").isdigit() and int(val) <= 0:
            return name, None
        if key == "expires":
            try:
                if parsedate_to_datetime(val.strip()) <= datetime.now(UTC):
                    return name, None
            except (TypeError, ValueError):
                pass
    return name, (value or None)


class FuseWebClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        cookies: dict[str, str] | None = None,
        on_cookies: Callable[[dict[str, str]], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self.cookies: dict[str, str] = dict(cookies or {})
        self._on_cookies = on_cookies

    @property
    def has_session(self) -> bool:
        return bool(self.cookies.get("app-auth"))

    async def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, str]:
        sent = {"Accept": "application/json", "Timezone": "Europe/London", **(headers or {})}
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items() if k in _KEEP_COOKIES)
        if cookie:
            sent["Cookie"] = cookie
        try:
            async with self._session.request(
                method, FUSE_WEB + path, json=body, headers=sent, timeout=_TIMEOUT, allow_redirects=False
            ) as resp:
                text = await resp.text()
                await self._take_cookies(resp.headers.getall("Set-Cookie", []))
                waf = resp.headers.get("x-amzn-waf-action")
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FuseApiError(f"Couldn't reach Fuse: {err}") from err
        if waf:
            raise FuseApiError(f"Fuse's website asked for a browser check ({waf}) - try again later")
        if status in (301, 302, 303, 307, 308, 401, 403):
            raise FuseAuthRequired("Fuse signed Fox Plant out - sign in again with a new code")
        if status == 429:
            raise FuseApiError("Fuse is limiting requests (HTTP 429) - try again later")
        if status >= 500:
            raise FuseApiError(f"Fuse's website returned HTTP {status}")
        return status, text

    async def _take_cookies(self, headers: list[str]) -> None:
        changed = False
        for header in headers:
            parsed = parse_set_cookie(header)
            if parsed is None or parsed[0] not in _KEEP_COOKIES:
                continue
            name, value = parsed
            if value is None:
                changed |= self.cookies.pop(name, None) is not None
            elif self.cookies.get(name) != value:
                self.cookies[name] = value
                changed = True
        if changed and self._on_cookies is not None:
            await self._on_cookies(dict(self.cookies))

    async def _trpc_get(self, procedure: str, params: dict[str, Any] | None = None) -> Any:
        path = f"/api/trpc/{procedure}"
        if params is not None:
            path += "?input=" + quote(json.dumps(params, separators=(",", ":")))
        _status, text = await self._request("GET", path)
        return self._json(text, procedure)

    async def _trpc_post(self, procedure: str, body: dict[str, Any]) -> Any:
        _status, text = await self._request("POST", f"/api/trpc/{procedure}", body=body)
        return self._json(text, procedure)

    @staticmethod
    def _json(text: str, procedure: str) -> Any:
        try:
            return json.loads(text)
        except ValueError as err:
            raise FuseApiError(f"Fuse sent an unreadable answer to {procedure}") from err

    # --- Sign-in ------------------------------------------------------------------------------------------------

    async def send_code(self, phone: str) -> None:
        """Ask Fuse to text a sign-in code (starts a fresh sign-in)."""
        self.cookies = {k: v for k, v in self.cookies.items() if k != "otp"}
        payload = await self._trpc_post("phoneSignIn", {"phone": phone})
        if (reason := trpc_error(payload)) is not None:
            raise FuseApiError(f"Fuse didn't send a code: {reason}")
        challenge = ((payload.get("result") or {}).get("data") or {}).get("challenge_type")
        if challenge != "PHONE_OTP":
            raise FuseApiError(f"Fuse didn't send a code (it asked for {challenge or 'something unexpected'})")

    async def verify_code(self, code: str) -> None:
        """Swap the texted code for a signed-in session."""
        if not self.cookies.get("otp"):
            raise FuseApiError("Press Send Code first - the sign-in has expired")
        payload = await self._trpc_post("verifyOtp", {"code": code.strip()})
        if (reason := trpc_error(payload)) is not None:
            raise FuseApiError(f"Fuse didn't accept the code: {reason}")
        challenge = ((payload.get("result") or {}).get("data") or {}).get("challenge_type")
        if challenge == "ADDITIONAL_INFO":
            raise FuseApiError(
                "Fuse wants extra identity details before signing in, which Fox Plant can't answer yet - "
                "sign in once in the Fuse app, then try again"
            )
        if challenge != "AUTHORIZED" or not self.cookies.get("app-auth"):
            raise FuseApiError(f"Fuse didn't sign in (it answered {challenge or 'unexpectedly'})")

    async def signed_in(self) -> bool:
        payload = await self._trpc_get("signedIn")
        try:
            return bool((trpc_data(payload) or {}).get("signedIn"))
        except FuseParseError:
            return False

    # --- Data ---------------------------------------------------------------------------------------------------

    async def customer_premises(self) -> Any:
        return self._data(await self._trpc_get("customerPremises"), "your properties")

    async def current_contracts(self, supply_fids: list[str]) -> Any:
        return self._data(await self._trpc_get("suppliesCurrentContracts", {"supplyFids": supply_fids}), "contracts")

    async def supply_page(self, premises_id: str, supply_fid: str, *, year: int, month: int) -> str:
        _status, text = await self._request(
            "GET",
            f"/app/premises/{premises_id}/supply/{supply_fid}?month={month}&year={year}",
            headers={"RSC": "1", "Accept": "text/x-component"},
        )
        return text

    async def hh_readings_page(self, premises_id: str, supply_fid: str, *, year: int, month: int) -> str:
        """The meter's half-hourly readings for a month (fuse_meter.py)."""
        _status, text = await self._request(
            "GET",
            f"/app/premises/{premises_id}/supply/{supply_fid}/meter/hh-readings?month={month}&year={year}",
            headers={"RSC": "1", "Accept": "text/x-component"},
        )
        return text

    async def tariff_page(self, premises_id: str, supply_fid: str) -> str:
        _status, text = await self._request(
            "GET",
            f"/app/premises/{premises_id}/supply/{supply_fid}/tariff/change",
            headers={"RSC": "1", "Accept": "text/x-component"},
        )
        return text

    @staticmethod
    def _data(payload: Any, what: str) -> Any:
        try:
            return trpc_data(payload)
        except FuseParseError as err:
            if "UNAUTHORIZED" in str(err).upper():
                raise FuseAuthRequired("Fuse signed Fox Plant out - sign in again with a new code") from err
            raise FuseApiError(f"Fuse couldn't load {what}: {err}") from err


def build_fuse_cache(
    import_tariff: dict[str, Any], export_tariff: dict[str, Any] | None, *, now: datetime
) -> dict[str, Any]:
    """The supplier rate cache (Octopus snapshot shape) from saved Fuse tariffs - no network.

    Rows are rebuilt from the tariff definition on every tick, so they never run out between fetches.
    """
    from .fuse_parse import FuseTariff, window_rows
    from .octopus_tariff import (
        TARIFF_TYPE_ECONOMY7,
        TARIFF_TYPE_FLAT,
        OctopusMeterSummary,
        OctopusTariffSnapshot,
        build_schedule_from_rates,
        rate_at,
    )

    imp = FuseTariff.from_dict(import_tariff)
    exp = FuseTariff.from_dict(export_tariff) if export_tariff else None
    import_rows = window_rows(imp, now)
    export_rows = window_rows(exp, now) if exp else []

    def meter(t: FuseTariff, is_export: bool) -> OctopusMeterSummary:
        return OctopusMeterSummary(
            mpan=t.identifier or t.supply_fid,
            serial=None,
            is_export=is_export,
            tariff_code=t.name,
            product_code=t.tariff_id,
            display_name=f"Fuse {'Export' if is_export else 'Import'}",
        )

    snapshot = OctopusTariffSnapshot(
        tariff_type=TARIFF_TYPE_ECONOMY7 if imp.multi_rate else TARIFF_TYPE_FLAT,
        import_meter=meter(imp, False),
        export_meter=meter(exp, True) if exp else None,
        import_rates=import_rows,
        export_rates=export_rows,
        import_standing_p_per_day=imp.standing_p_per_day,
        current_import_p_per_kwh=rate_at(now, import_rows),
        current_export_p_per_kwh=rate_at(now, export_rows) if export_rows else None,
        schedule=build_schedule_from_rates(import_rows, export_rows or None),
    )
    cache = snapshot.to_cache_dict()
    cache["import_rates"] = import_rows
    cache["export_rates"] = export_rows
    cache["agreement_valid_to"] = imp.end_date
    return cache


async def fetch_fuse_tariffs(client: FuseWebClient, *, today: date) -> dict[str, dict[str, Any]]:
    """Every electricity supply Fuse currently supplies: {"ELEC_IMPORT": tariff dict, "ELEC_EXPORT": ...}."""
    if not client.has_session:
        raise FuseAuthRequired("Sign in to Fuse first")
    if not await client.signed_in():
        raise FuseAuthRequired("Fuse signed Fox Plant out - sign in again with a new code")
    supplies = electricity_supplies(await client.customer_premises())
    if not supplies:
        raise FuseApiError("No electricity supply on this Fuse account")
    contracts = contracted_supplies(await client.current_contracts([s["supply_fid"] for s in supplies]))
    out: dict[str, dict[str, Any]] = {}
    for sup in supplies:
        contract = contracts.get(sup["supply_fid"])
        if contract is None or sup["supply_type"] in out:
            continue
        try:
            page = await client.supply_page(sup["premises_id"], sup["supply_fid"], year=today.year, month=today.month)
            tariff = tariff_from_bill(rsc_value(page, "initialBill"), sup["supply_fid"], today=today)
            if tariff is None:  # no billing period yet (new supply, first of the month): the tariff card
                cards = rsc_value(await client.tariff_page(sup["premises_id"], sup["supply_fid"]), "groups")
                tariff = tariff_from_cards(cards, sup["supply_fid"], sup["supply_type"])
        except FuseParseError as err:
            raise FuseApiError(f"Fuse {sup['supply_type'].split('_')[-1].title()} tariff: {err}") from err
        if tariff is None:
            raise FuseApiError(
                f"Couldn't find the {sup['supply_type'].split('_')[-1].lower()} tariff on Fuse's website"
            )
        tariff.supply_type = sup["supply_type"]
        tariff.identifier = sup.get("identifier")
        tariff.premises_id = sup["premises_id"]
        out[sup["supply_type"]] = apply_contract(tariff, contract).to_dict()
    if not out:
        raise FuseApiError("Fuse isn't supplying electricity on this account yet")
    _LOGGER.debug("Fuse tariffs: %s", {k: (v["name"], v["rates"]) for k, v in out.items()})
    return {k: out[k] for k in SUPPLY_TYPES if k in out}
