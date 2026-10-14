"""Sign in to the Fuse web app (www.fuseenergy.com/app) and read the current tariff.

The mobile API (api.fuseenergy.com) has no tariff call we could find, but the web app
renders the current tariff period - tariff_contract_details {unit_rates, standing charge,
contract dates} and per_rate_charges[].charge_time_ranges_uk - into the supply page's
React Server Components payload. The web app keeps its session in cookies.

    python3 fuse_web_probe.py send +447700900123
    python3 fuse_web_probe.py verify 123456
    python3 fuse_web_probe.py read

Cookies and responses go to OUT_DIR (outside the repo) - they hold personal data.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

WEB = "https://www.fuseenergy.com"
WEB_APP_VERSION = "git-4b2ee97b3cf67e4ed2d13e13f9e4fdd5157fd8c1"
OUT_DIR = os.path.expanduser(os.environ.get("FUSE_PROBE_OUT", "~/fuse_probe"))
JAR = os.path.join(OUT_DIR, "web_cookies.txt")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def _opener() -> tuple[urllib.request.OpenerDirector, http.cookiejar.LWPCookieJar]:
    os.makedirs(OUT_DIR, exist_ok=True)
    jar = http.cookiejar.LWPCookieJar(JAR)
    if os.path.exists(JAR):
        jar.load(ignore_discard=True, ignore_expires=True)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def _call(method: str, path: str, *, body: dict | None = None, headers: dict | None = None) -> tuple[int, str, dict]:
    opener, jar = _opener()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(WEB + path, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("x-fuse-app-version", WEB_APP_VERSION)
    req.add_header("Timezone", "Europe/London")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=30) as resp:
            status, text, hdrs = resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as err:
        status, text, hdrs = err.code, err.read().decode("utf-8", "replace"), dict(err.headers)
    jar.save(ignore_discard=True, ignore_expires=True)
    return status, text, hdrs


def _trpc_get(proc: str, inp: dict) -> tuple[int, str]:
    q = urllib.parse.quote(json.dumps(inp, separators=(",", ":")))
    status, text, _ = _call("GET", f"/api/trpc/{proc}?input={q}", headers={"Accept": "application/json"})
    return status, text


def cmd_send(phone: str) -> None:
    status, text, _ = _call("POST", "/api/trpc/phoneSignIn", body={"phone": phone})
    print("phoneSignIn:", status, text[:300])


def cmd_verify(code: str) -> None:
    status, text, hdrs = _call("POST", "/api/trpc/verifyOtp", body={"code": code})
    print("verifyOtp:", status, text[:300])
    _, jar = _opener()
    print("cookies held:", sorted(c.name for c in jar))


def cmd_read() -> None:
    status, text, _ = _call("GET", "/api/trpc/signedIn", headers={"Accept": "application/json"})
    print("signedIn:", status, text[:200])
    status, text, _ = _call("GET", "/api/trpc/customerPremises", headers={"Accept": "application/json"})
    print("customerPremises:", status, len(text))
    premises = (json.loads(text).get("result") or {}).get("data") if status == 200 else None
    if not premises:
        sys.exit(text[:300])
    with open(os.path.join(OUT_DIR, "web_customerPremises.json"), "w", encoding="utf-8") as fh:
        fh.write(text)
    entry = premises[0]
    pid = entry["premises"]["id"]
    sids = [s["supply_fid"] for s in entry.get("supplies") or []]
    status, text = _trpc_get("suppliesCurrentContracts", {"supplyFids": sids})
    print("suppliesCurrentContracts:", status, len(text))
    with open(os.path.join(OUT_DIR, "web_contracts.json"), "w", encoding="utf-8") as fh:
        fh.write(text)
    contracts = (((json.loads(text).get("result") or {}).get("data") or {}).get("contracts") or {}).get("supply_fid_to_contracts") or {}
    today = date.today()
    for sid, c in contracts.items():
        if not (c or {}).get("current"):
            continue
        # The supply page's RSC payload carries initialBill → per_billing_span[].tariff_contract_details.
        status, text, hdrs = _call(
            "GET",
            f"/app/premises/{pid}/supply/{sid}?month={today.month}&year={today.year}",
            headers={"RSC": "1", "Accept": "text/x-component"},
        )
        print(f"supply page {sid[:8]}…:", status, len(text), hdrs.get("Content-Type"))
        with open(os.path.join(OUT_DIR, f"web_supply_{sid[:8]}.rsc"), "w", encoding="utf-8") as fh:
            fh.write(text)
        for m in re.finditer(r'"tariff_contract_details":(\{.*?"tariff_disclaimers":\[[^\]]*\]\})', text):
            print("  tariff_contract_details:", m.group(1)[:600])
        for m in re.finditer(r'"per_rate_charges":(\[.*?\])\s*,\s*"standing_charge"', text):
            print("  per_rate_charges:", m.group(1)[:600])


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["send"] and len(a) == 2:
        cmd_send(a[1])
    elif a[:1] == ["verify"] and len(a) == 2:
        cmd_verify(a[1])
    elif a[:1] == ["read"]:
        cmd_read()
    else:
        sys.exit(__doc__)
