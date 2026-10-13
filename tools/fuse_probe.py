"""Find the Fuse Energy API call that returns tariff unit rates.

Fuse has no public API. The web app (www.fuseenergy.com/app) shows the rate inside a
server-rendered bill object (``per_billing_span[].tariff_contract_details.unit_rates``),
so the same object must come from api.fuseenergy.com, but the path is unknown. This
signs in like the mobile app (phone + SMS code) and tries likely paths.

Three steps, so it also works without an interactive terminal:

    python3 fuse_probe.py send +447700900123   # Fuse texts a code
    python3 fuse_probe.py verify 123456         # swaps the code for tokens
    python3 fuse_probe.py probe                 # tries the candidate paths

Everything (tokens, responses) is written to OUT_DIR, outside the repo. Responses hold
personal data (address, MPAN, meter serial) - never commit them.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import date

API = "https://api.fuseenergy.com"
WEB = "https://www.fuseenergy.com"
# The website sends its build as x-fuse-app-version; this one was live on 13 Oct 2026.
WEB_APP_VERSION = "git-4b2ee97b3cf67e4ed2d13e13f9e4fdd5157fd8c1"
OUT_DIR = os.path.expanduser(os.environ.get("FUSE_PROBE_OUT", "~/fuse_probe"))
STATE = os.path.join(OUT_DIR, "state.json")
GAP_S = 0.5  # spacing between probe requests - be gentle, this is someone's account
RATE_MARKERS = ("price_per_kWh", "unit_rates", "per_tpr_charges", "price_per_unit", "unit_rate", "charge_time_ranges_uk", "estimated_monthly")


def _load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _save(state: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)


def _request(method: str, url: str, *, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def cmd_send(phone: str) -> None:
    device_id = str(uuid.uuid4())
    status, text = _request(
        "POST",
        f"{API}/api/v3/auth",
        body={"challenge_type": "INITIAL", "data": {"method": "PHONE", "data": {"phone_number": phone, "prelude_dispatch_id": None}}},
        headers={"Device-Id": device_id},
    )
    payload = _json(text) or {}
    flow = payload.get("auth_flow_token")
    print("INITIAL:", status, payload.get("challenge_type") or text[:200])
    if not flow:
        sys.exit("No auth_flow_token - stopping.")
    # The mobile endpoint only mints the flow token; the website call sends the SMS.
    status, text = _request("POST", f"{WEB}/api/trpc/phoneSignIn", body={"phone": phone}, headers={"x-fuse-app-version": WEB_APP_VERSION})
    print("phoneSignIn:", status, text[:300])
    _save({"device_id": device_id, "flow_token": flow})
    print("Now run: fuse_probe.py verify <code>")


def _advance(state: dict, body: dict) -> None:
    status, text = _request("POST", f"{API}/api/v3/auth", body=body, headers={"Device-Id": state["device_id"], "Authorization": f"Bearer {state['flow_token']}"})
    payload = _json(text) or {}
    if payload.get("auth_flow_token"):
        state["flow_token"] = payload["auth_flow_token"]
    challenge = payload.get("challenge_type")
    print("auth:", status, challenge or text[:300])
    data = payload.get("data") or {}
    if challenge == "AUTHORIZED":
        state["access_token"], state["refresh_token"] = data["access_token"], data["refresh_token"]
        state.pop("flow_token", None)
        _save(state)
        print("Signed in. Now run: fuse_probe.py probe")
    elif challenge == "ADDITIONAL_INFO":
        _save(state)
        for q in data.get("questions") or []:
            print(f"  question key={q.get('key')} type={q.get('type')}: {q.get('title')}")
        print('Answer with: fuse_probe.py answer key=value [key=value ...]  (dates as YYYY-MM-DD)')
    else:
        _save(state)


def cmd_verify(code: str) -> None:
    _advance(_load(), {"challenge_type": "PHONE_OTP", "data": {"code": code}})


def cmd_answer(pairs: list[str]) -> None:
    answers = dict(p.split("=", 1) for p in pairs)
    _advance(_load(), {"challenge_type": "ADDITIONAL_INFO", "data": {"responses": answers}})


def _get(state: dict, path: str):
    """Authenticated GET with one token refresh on 401 (tokens rotate - saved at once)."""
    for retry in (False, True):
        status, text = _request("GET", f"{API}{path}", headers={"Device-Id": state["device_id"], "Authorization": f"Bearer {state['access_token']}"})
        if status != 401 or retry:
            return status, text
        rs, rt = _request(
            "POST",
            f"{API}/api/v1/auth/refresh",
            body={"refresh_token": state["refresh_token"], "original_request_path": None},
            headers={"Device-Id": state["device_id"], "Authorization": f"Bearer {state['access_token']}"},
        )
        fresh = _json(rt) or {}
        if rs >= 300 or "access_token" not in fresh:
            sys.exit(f"Token refresh failed ({rs}) - run send/verify again.")
        state["access_token"], state["refresh_token"] = fresh["access_token"], fresh["refresh_token"]
        _save(state)
    return status, text


def _candidates(premises: str, supplies: list[str], pairs: list[tuple[str, str]]) -> list[str]:
    today = date.today()
    ym = f"year={today.year}&month={today.month}"
    ymd = f"{ym}&day={today.day}"
    paths = [f"/api/v1/premises/{premises}/chart?{ymd}"]  # known good - control
    # Contracts first: the web app's suppliesCurrentContracts gives the current tariff_id.
    for v in (1, 2, 3):
        paths += [f"/api/v{v}/premises/{premises}/contracts", f"/api/v{v}/premises/{premises}/current-contracts"]
        if supplies:
            paths.append(f"/api/v{v}/supplies/current-contracts?supply_fids={','.join(supplies)}")
    # Only supplies Fuse has a contract on, once the contracts are known.
    contracted = sorted({s for s, _ in pairs}) or supplies
    for supply in contracted:
        for v in (1, 2, 3):
            paths.append(f"/api/v{v}/supplies/{supply}/contracts")
        for v in (1, 2):
            paths += [f"/api/v{v}/supplies/{supply}/tariffs", f"/api/v{v}/supplies/{supply}/available-tariffs"]
    # Tariff details (web tRPC tariffDetails({supplyFid, tariffIds}) → tariff_id_to_details).
    for supply, tariff in pairs:
        for v in (1, 2, 3):
            paths += [
                f"/api/v{v}/supplies/{supply}/tariffs?tariff_ids={tariff}",
                f"/api/v{v}/supplies/{supply}/tariffs/{tariff}",
                f"/api/v{v}/supplies/{supply}/tariff-details?tariff_ids={tariff}",
                f"/api/v{v}/tariffs/{tariff}",
            ]
    # Tariff-change list (web page renders cards {tariff, availability, estimated_monthly, is_cheapest}).
    for supply in contracted:
        for v in (1, 2):
            paths += [f"/api/v{v}/supplies/{supply}/tariff-change", f"/api/v{v}/supplies/{supply}/tariff-options"]
    # The current billing span carries tariff_contract_details {unit_rates, standing_charge_per_day,
    # contract dates} and per_rate_charges[].charge_time_ranges_uk - the same tariff data, proven on the web.
    for v in (4, 5, 6):
        paths.append(f"/api/v{v}/properties/{premises}/billing?{ymd}")
    for v in (1, 2):
        paths += [f"/api/v{v}/premises/{premises}/bill?{ym}", f"/api/v{v}/premises/{premises}/billing?{ym}"]
    return paths


def _find_ids(obj, out: dict) -> None:
    """Collect supply fids, and (supply fid, tariff id) pairs from contract objects."""
    if isinstance(obj, dict):
        supply = obj.get("supply_fid")
        if isinstance(supply, str):
            out["supplies"].add(supply)
            tariff = obj.get("tariff")
            if isinstance(tariff, dict) and isinstance(tariff.get("tariff_id"), str):
                out["pairs"].add((supply, tariff["tariff_id"]))
        for value in obj.values():
            _find_ids(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _find_ids(value, out)


def cmd_probe() -> None:
    state = _load()
    if not state.get("access_token"):
        sys.exit("Not signed in - run send/verify first.")
    status, text = _get(state, "/api/v2/customer/premises")
    premises_list = _json(text)
    print("premises:", status)
    if not isinstance(premises_list, list) or not premises_list:
        sys.exit(text[:300])
    with open(os.path.join(OUT_DIR, "premises.json"), "w", encoding="utf-8") as fh:
        fh.write(text)
    premises = premises_list[0]["premises"]["id"]
    ids: dict = {"supplies": set(), "pairs": set()}
    _find_ids(premises_list, ids)
    print(f"supplies found: {len(ids['supplies'])}, contracted tariffs: {len(ids['pairs'])}")
    results, done = [], set()
    # Two passes: tariff ids usually only turn up in a contracts response from pass one.
    for _pass in range(2):
        paths = _candidates(premises, sorted(ids["supplies"]), sorted(ids["pairs"]))
        for path in paths:
            if path in done:
                continue
            done.add(path)
            time.sleep(GAP_S)
            status, text = _get(state, path)
            body = _json(text)
            markers = [m for m in RATE_MARKERS if m in text]
            if status < 300:
                _find_ids(body, ids)
                name = path.split("?")[0].replace(premises, "P").strip("/").replace("/", "_")
                with open(os.path.join(OUT_DIR, f"{name}.json"), "w", encoding="utf-8") as fh:
                    fh.write(text)
            keys = list(body)[:8] if isinstance(body, dict) else type(body).__name__
            line = f"{status} {path.replace(premises, 'P')[:90]}  keys={keys}  RATES={markers or '-'}"
            print(line)
            results.append(line)
    with open(os.path.join(OUT_DIR, "probe_results.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(results))
    print(f"Saved to {OUT_DIR}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["send"] and len(args) == 2:
        cmd_send(args[1])
    elif args[:1] == ["verify"] and len(args) == 2:
        cmd_verify(args[1])
    elif args[:1] == ["answer"] and len(args) > 1:
        cmd_answer(args[1:])
    elif args[:1] == ["probe"]:
        cmd_probe()
    else:
        sys.exit(__doc__)
