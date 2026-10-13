"""Panel thermal lag: the glass warms and cools over minutes, not instantly.

panel_temp.py gives the steady-state temperature for the sun and wind right now. A real module has heat
capacity, so after a cloud edge it moves towards that figure exponentially:

    C dT/dt = a G - U (T - T_air)   →   T(t + Δt) = T_ss + (T - T_ss) e^(-Δt/τ),   τ = C / U

  C  heat capacity: ~11 kJ/m²K for a glass-backsheet module (~11 kg/m² of mostly glass at ~1 kJ/kgK)
  U  true heat loss = a × the Faiman/NOCT factor panel_temp.py uses (that factor is "sun per °C rise", so it
     already contains the share of sunlight that becomes heat: a = 0.9 absorbed × (1 - ~0.2 made into
     electricity) ≈ 0.72)

That gives τ ≈ 15 min in still air on a roof, ~10 min in a light breeze and ~6 min in a fresh wind, in line
with the 5–10 min measured for open-rack modules (roof-mounted ones are slower). The state is stepped every
30 s and saved with the day's running totals, so a restart carries on instead of jumping.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .panel_temp import ArrayThermal, heat_loss_w_per_m2k

HEAT_CAPACITY_J_M2K = 11_000.0
ABSORBED_FRACTION = 0.72
MIN_TAU_S = 180.0
MAX_TAU_S = 1_200.0
STEP_INTERVAL = timedelta(seconds=30)
# Longer than this without a step (HA down, restart) and the saved temperature means nothing: start afresh
RESUME_GAP = timedelta(minutes=30)


def time_constant_s(arrays: list[ArrayThermal], wind_ms: float | None) -> float:
    """Thermal time constant (s) for the plant, weighted by array size."""
    weighted = 0.0
    total = 0.0
    for array in arrays:
        u_true = ABSORBED_FRACTION * heat_loss_w_per_m2k(array, wind_ms)
        tau = HEAT_CAPACITY_J_M2K / max(u_true, 1.0)
        weight = max(array.stc_kw, 0.01)
        weighted += tau * weight
        total += weight
    tau = weighted / total if total else HEAT_CAPACITY_J_M2K / (ABSORBED_FRACTION * 20.0)
    return min(MAX_TAU_S, max(MIN_TAU_S, tau))


def relax(current_c: float, target_c: float, dt_s: float, tau_s: float) -> float:
    """One exact step of the first-order lag towards ``target_c`` over ``dt_s`` seconds."""
    if dt_s <= 0:
        return current_c
    return target_c + (current_c - target_c) * math.exp(-dt_s / max(tau_s, 1.0))


class PanelThermal:
    """Lagged panel temperature (and the same in still air), stepped towards the steady-state model."""

    def __init__(self) -> None:
        self.temp_c: float | None = None
        self.still_air_c: float | None = None
        self.ts: datetime | None = None
        # When lagged values started being stored: older chart samples are the instant model
        self.since: str | None = None

    def update(
        self,
        *,
        now: datetime,
        target_c: float | None,
        still_air_target_c: float | None,
        air_c: float | None,
        arrays: list[ArrayThermal],
        wind_ms: float | None,
    ) -> tuple[float | None, float | None]:
        """Step to ``now``; returns (panel °C, still-air °C), or (None, None) when there's no sun to report.

        With no sun (``target_c`` None) the panels cool towards the air, so dawn starts from air temperature.
        """
        if self.since is None:
            self.since = now.isoformat()
        goal = target_c if target_c is not None else air_c
        still_goal = still_air_target_c if still_air_target_c is not None else air_c
        gap = (now - self.ts).total_seconds() if self.ts is not None else None
        if goal is None:
            return None, None
        if self.temp_c is None or gap is None or gap < 0 or gap > RESUME_GAP.total_seconds():
            self.temp_c = goal
            self.still_air_c = still_goal
        else:
            self.temp_c = relax(self.temp_c, goal, gap, time_constant_s(arrays, wind_ms))
            if still_goal is not None:
                start = self.still_air_c if self.still_air_c is not None else still_goal
                self.still_air_c = relax(start, still_goal, gap, time_constant_s(arrays, 0.0))
        self.ts = now
        if target_c is None:
            return None, None
        return round(self.temp_c, 1), (round(self.still_air_c, 1) if self.still_air_c is not None else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "temp_c": self.temp_c,
            "still_air_c": self.still_air_c,
            "ts": self.ts.isoformat() if self.ts else None,
            "since": self.since,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PanelThermal:
        state = cls()
        if not isinstance(data, dict):
            return state
        try:
            state.temp_c = float(data["temp_c"]) if data.get("temp_c") is not None else None
            state.still_air_c = float(data["still_air_c"]) if data.get("still_air_c") is not None else None
            state.ts = datetime.fromisoformat(data["ts"]) if data.get("ts") else None
        except (TypeError, ValueError):
            return cls()
        state.since = str(data["since"]) if data.get("since") else None
        return state
