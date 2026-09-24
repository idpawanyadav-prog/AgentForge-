"""Per-project LLM cost budgets (AGENT_DEV_SPEEDUP 3.3).

projects.budget_usd is the ceiling (0 = unlimited, the default, so existing
projects are unaffected). Spent is the sum of usage_records.cost_estimate
across the project's runs, PLUS in-flight cost: usage_records only land when
a run finishes, so without the in-flight ledger a task burning through
self-fix retries + review gates could double its budget inside one run
before any check saw the spend. Enforcement therefore happens both at
task/sprint start and before every LLM call in codegen.
"""
import threading
import time

from .db import query_one

# In-flight call costs by project: [(monotonic_ts, cost)]. Entries older than
# the TTL are dropped so a crashed run can never strand the budget forever.
_inflight: dict[str, list] = {}
_inflight_lock = threading.Lock()
_INFLIGHT_TTL_S = 3600


def add_inflight(project_id: str, cost: float) -> None:
    if cost <= 0:
        return
    with _inflight_lock:
        _inflight.setdefault(project_id, []).append((time.monotonic(), float(cost)))


def clear_inflight(project_id: str) -> None:
    """Called when a run records its usage row — the DB sum takes over."""
    with _inflight_lock:
        _inflight.pop(project_id, None)


def inflight_usd(project_id: str) -> float:
    cutoff = time.monotonic() - _INFLIGHT_TTL_S
    with _inflight_lock:
        entries = _inflight.get(project_id) or []
        entries = [e for e in entries if e[0] >= cutoff]
        _inflight[project_id] = entries
        return round(sum(c for _, c in entries), 6)


def spent_usd(project_id: str) -> float:
    row = query_one(
        "SELECT COALESCE(SUM(ur.cost_estimate), 0) AS c FROM usage_records ur "
        "JOIN workflow_runs wr ON wr.id = ur.workflow_run_id "
        "WHERE wr.project_id = ?", (project_id,))
    return float((row or {}).get("c") or 0) + inflight_usd(project_id)


def budget_status(project_id: str) -> dict:
    p = query_one("SELECT budget_usd FROM projects WHERE id = ?", (project_id,))
    budget = float((p or {}).get("budget_usd") or 0)
    spent = spent_usd(project_id)
    return {"budget_usd": budget, "spent_usd": round(spent, 4),
            "inflight_usd": round(inflight_usd(project_id), 4),
            "unlimited": budget <= 0,
            "over": budget > 0 and spent >= budget}


def headroom_usd(project_id: str) -> float | None:
    """Remaining spendable amount; None when the project has no cap."""
    st = budget_status(project_id)
    return None if st["unlimited"] else st["budget_usd"] - st["spent_usd"]


def may_spend(project_id: str) -> tuple[bool, str]:
    st = budget_status(project_id)
    if st["over"]:
        return False, (f"LLM budget exhausted — spent ${st['spent_usd']:.2f} "
                       f"of ${st['budget_usd']:.2f} for this project")
    return True, ""
