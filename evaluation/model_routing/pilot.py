"""Experimental, no-tools first-turn Codex router. Not registered by the product."""

from __future__ import annotations
import hashlib
import threading
import time
from .client import DecisionClient, request_budget_scope
from .reasoning_effort_adapter import _task_scan
from .routing import _noul_score

ORIGIN = "gpt-6-sol"
TARGET = "gpt-6-luna"
JEV = "typesafe/jev-1.13-20260917"


class PilotRouter:
    def __init__(self):
        self.turns = {}
        self.receipts = []
        self.lock = threading.Lock()
        self.client = None

    def capture(
        self,
        *,
        session_id=None,
        task_id=None,
        turn_id=None,
        user_message=None,
        is_first_turn=False,
        parent_session_id=None,
        model=None,
        platform=None,
        **context,
    ):
        key = (session_id, task_id, turn_id)
        if any(not isinstance(v, str) or not v.strip() for v in key):
            return
        if (
            is_first_turn is not True
            or parent_session_id
            or model != ORIGIN
            or platform != "cli"
        ):
            return
        text, reason = _task_scan(user_message)
        if not text or reason or len(user_message) > 1200:
            return
        with self.lock:
            if len(self.turns) >= 32:
                self.turns.pop(next(iter(self.turns)))
            self.turns[key] = {"text": text, "model": None}

    def apply(self, request, **context):
        key = tuple(context.get(k) for k in ("session_id", "task_id", "turn_id"))
        if (
            context.get("provider") != "openai-codex"
            or context.get("api_mode") != "codex_responses"
        ):
            return request
        if (
            context.get("model") != ORIGIN
            or request.get("model") != ORIGIN
            or request.get("tools")
        ):
            return request
        content = request.get("input")
        if not isinstance(content, list) or len(content) != 1:
            return request
        if not isinstance(content[0], dict) or content[0].get("role") != "user":
            return request
        if len(str(content)) > 8000 or request.get("previous_response_id"):
            return request
        with self.lock:
            turn = self.turns.get(key)
            if turn is None:
                return request
            if turn["model"] is not None:
                return {**request, "model": turn["model"]}
            selected = ORIGIN
            row = {
                "scope_sha256": hashlib.sha256(repr(key).encode()).hexdigest(),
                "from": ORIGIN,
                "to": ORIGIN,
                "applied": False,
                "stage": "prototype",
                "reason": "kept",
                "decision": None,
            }
            started = time.perf_counter()
            try:
                if self.client is None:
                    from . import _secret

                    self.client = DecisionClient(
                        api_key=_secret("openrouter"), model=JEV, timeout=0.4
                    )
                questions = {
                    "routine": {
                        "type": "noul",
                        "instructions": "Can a smaller capable language model reliably answer this complete task? "
                        "True only for self-contained lookup, extraction, formatting, short arithmetic "
                        "or simple code tracing. False for complex reasoning, missing context, "
                        "ambiguous instructions, or consequential decisions. Judge task difficulty only.",
                        "criteria": {
                            "true": "Routine and self-contained",
                            "false": "Complex or uncertain",
                        },
                    },
                    "stakes": {
                        "type": "noul",
                        "instructions": "Could a wrong answer cause data loss, a security exposure or an irreversible action? "
                        "When the task is only harmless arithmetic or text extraction answer false.",
                        "criteria": {"true": "Consequential", "false": "Routine"},
                    },
                }
                with request_budget_scope(self.client, 1, deadline_seconds=0.4):
                    answer = self.client.decide(
                        {"task": turn["text"]},
                        questions,
                        public_or_sanitized_data_ack=True,
                    )
                row["decision"] = answer
                routine = _noul_score(answer["answers"]["routine"], "routine")
                stakes = _noul_score(answer["answers"]["stakes"], "stakes")
                elapsed = time.perf_counter() - started
                if elapsed <= 0.4 and routine >= 0.8 and stakes < 0.5:
                    selected = TARGET
                    row["reason"] = "routine_qualified"
                elif elapsed > 0.4:
                    row["reason"] = "late_decision"
                else:
                    row["reason"] = "not_qualified"
            except Exception as exc:
                row["reason"] = "decision_failed"
                row["error_type"] = type(exc).__name__
            row.update(
                to=selected,
                applied=selected != ORIGIN,
                wall_ms=(time.perf_counter() - started) * 1000,
            )
            turn["model"] = selected
            self.receipts.append(row)
            return {**request, "model": selected}

    def close(self):
        if self.client is not None:
            self.client.close()


ACTIVE = None


class ContextProxy:
    def __init__(self, ctx):
        global ACTIVE
        self.ctx = ctx
        self.router = PilotRouter()
        ACTIVE = self.router
        ctx.register_hook("pre_llm_call", self.router.capture)
        ctx.on_unload(self.router.close)

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    def register_middleware(self, kind, callback):
        if kind != "llm_request":
            return self.ctx.register_middleware(kind, callback)

        def composed(request=None, **context):
            updated = self.router.apply(request or {}, **context)
            selected_context = {
                **context,
                "model": updated.get("model", context.get("model")),
            }
            downstream = callback(request=updated, **selected_context)
            if isinstance(downstream, dict) and isinstance(
                downstream.get("request"), dict
            ):
                return downstream
            if updated is not request:
                return {
                    "request": updated,
                    "source": "switchyard-routing-pilot",
                    "reason": "same_provider",
                }
            return None

        return self.ctx.register_middleware(kind, composed)
