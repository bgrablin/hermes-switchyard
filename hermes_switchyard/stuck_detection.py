"""Bounded cross-tool obstacle advice, supplementing native exact-loop guards."""

from __future__ import annotations

import math
import json
import threading
import time
from collections import OrderedDict
from typing import Any

from .client import request_budget_scope
from .egress_redaction import redact_for_jev

NOTICE = "\n[Switchyard: several different tools hit the same unresolved obstacle. Check the prerequisite or change approach before retrying.]"


def _probability(answer: Any) -> float | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get("noul")
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        return None
    return float(value)


class StuckDetector:
    """At most one hosted check per turn, only after three cross-tool failures."""

    def __init__(self, client_factory, *, deadline_seconds=0.8):
        self.client_factory = client_factory
        self.deadline_seconds = deadline_seconds
        self.lock = threading.Lock()
        self.turns: OrderedDict[tuple, dict] = OrderedDict()

    def __call__(
        self,
        *,
        tool_name="",
        result=None,
        status="",
        session_id="",
        task_id="",
        turn_id="",
        tool_call_id="",
        **_,
    ):
        ids = (session_id, task_id, turn_id, tool_call_id)
        if not all(isinstance(i, str) and 0 < len(i) <= 256 for i in ids):
            return None
        key = ids[:3]
        with self.lock:
            now = time.monotonic()
            for stale in [k for k, v in self.turns.items() if now - v["at"] > 600]:
                self.turns.pop(stale, None)
            while len(self.turns) >= 128 and key not in self.turns:
                self.turns.popitem(last=False)
            state = self.turns.setdefault(
                key, {"at": now, "events": [], "seen": set(), "checked": False}
            )
            if tool_call_id in state["seen"] or state["checked"]:
                return None
            state["seen"].add(tool_call_id)
            if len(state["seen"]) > 64:
                state["checked"] = True
                return None
            if status != "error" or not isinstance(result, str) or len(result) > 16_000:
                state["events"].clear()
                return None
            safe, reason = redact_for_jev(result)
            if reason or safe is None:
                state["events"].clear()
                return None
            # Keep only tool categories; custom/MCP tool names never leave the host.
            kind = (
                "terminal"
                if tool_name == "terminal"
                else "file"
                if tool_name in {"read_file", "search_files", "write_file", "patch"}
                else "other"
            )
            state["events"].append((tool_name, {"kind": kind, "failure": safe[:500]}))
            state["events"] = state["events"][-3:]
            events = list(state["events"])
            if len(events) < 3 or len({e[0] for e in events}) < 2:
                return None
            state["checked"] = True
        client = None
        try:
            client = self.client_factory()
            with request_budget_scope(
                client, max_requests=1, deadline_seconds=self.deadline_seconds
            ):
                response = client.decide(
                    {"failures": [e[1] for e in events]},
                    {
                        "same_obstacle": {
                            "type": "noul",
                            "instructions": "Do all three failures show the same unresolved prerequisite, such that repeating these attempts without changing it will fail again? Different independent errors or useful progress mean no.",
                            "criteria": {
                                "true": "Same unresolved prerequisite, no progress",
                                "false": "Independent errors, useful progress, or insufficient evidence",
                            },
                        },
                    },
                    public_or_sanitized_data_ack=True,
                )
            probability = _probability(
                (response.get("answers") or {}).get("same_obstacle")
            )
            if probability is not None and probability >= 0.9:
                try:
                    data = json.loads(result)
                except ValueError:
                    return result + NOTICE
                if isinstance(data, dict) and "switchyard_advice" not in data:
                    return json.dumps(
                        {**data, "switchyard_advice": NOTICE.strip()},
                        ensure_ascii=False,
                    )
                return None
        except Exception:
            return None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass  # Advisory cleanup cannot replace the original result.
        return None
