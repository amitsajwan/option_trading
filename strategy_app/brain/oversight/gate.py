"""Engine-side gate: apply the oversight risk state to a proposed entry.

Pure + cheap (no LLM, no I/O) — the engine reads the precomputed risk state and
calls this per candidate. **Risk-reducing only:** it can VETO an entry (stand
down, or don't take the side the brain leans against) but can NEVER create one.
"""

from __future__ import annotations

from typing import Any


def oversight_entry_veto(direction: str, risk_state: dict[str, Any]) -> tuple[bool, str]:
    """Return (vetoed, reason) for a proposed entry given the oversight risk state.

    - ``risk_flag == stand_down`` → veto every new entry.
    - ``veto_side`` matches the entry's side → veto (don't take the side the brain
      leans against; e.g. a confident PE lean vetoes CE entries).
    Otherwise allow. Never forces a trade.
    """
    if not isinstance(risk_state, dict):
        return False, ""
    flag = str(risk_state.get("oversight_risk_flag", "normal") or "normal")
    veto_side = str(risk_state.get("oversight_veto_side", "") or "").upper()
    d = str(direction or "").upper()
    if flag == "stand_down":
        return True, "oversight:stand_down"
    if veto_side and d == veto_side:
        return True, f"oversight:veto_{veto_side.lower()}"
    return False, ""


__all__ = ["oversight_entry_veto"]
