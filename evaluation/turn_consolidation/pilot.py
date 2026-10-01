"""Native feasibility adapter for same-turn Jev request consolidation.
Evaluation only. It is not installed or registered by Switchyard.
"""

from __future__ import annotations
import copy
import contextvars
import threading
import time
from .client import DecisionClient
from .reasoning_effort_adapter import _task_scan, choose_reasoning_effort

ACTIVE = None
PRE = contextvars.ContextVar("consolidation_pre", default=None)
WIRE = contextvars.ContextVar("consolidation_wire", default=None)


class Capture:
    def decide(self, state, questions, **kwargs):
        self.parts = copy.deepcopy((state, questions))
        raise RuntimeError("capture only")


class Replay:
    def __init__(self, result):
        self.result = result

    def decide(self, state, questions, **kwargs):
        return {
            **self.result,
            "answers": {k: self.result["answers"][k] for k in questions},
        }


def effort_parts(task, cap):
    c = Capture()
    levels = ["low", "medium", "high"][: ["low", "medium", "high"].index(cap) + 1]
    choose_reasoning_effort(
        task=task,
        recent_tool_outcomes=[],
        client=c,
        requested_effort=cap,
        allowed_efforts=levels,
        deadline_seconds=0.4,
    )
    return c.parts


class Broker:
    def __init__(self):
        self.records = {}
        self.lock = threading.Lock()
        self.receipts = []

    def clear(self, key):
        with self.lock:
            self.records.pop(key, None)

    def store(self, key, record):
        with self.lock:
            if len(self.records) >= 32:
                self.records.pop(next(iter(self.records)))
            self.records[key] = record

    def consume(self, key, capture, requested, candidates, controller):
        if (
            key is None
            or capture is None
            or capture.get("scan_reason")
            or capture.get("metadata")
        ):
            return None
        if not controller.public_or_sanitized_data_ack:
            return None
        expected = {"medium": ["low", "medium"], "high": ["low", "medium", "high"]}.get(
            requested
        )
        if expected is None or list(candidates) != expected:
            return None
        with self.lock:
            record = self.records.pop(key, None)
        if record is None or record["task"] != capture.get("excerpt"):
            return None
        if (
            record["wall_ms"] > controller.deadline_seconds * 1000
            or time.monotonic() - record["at"] > 30
        ):
            return None
        result = copy.deepcopy(record["result"])
        result["answers"]["reasoning_effort"] = result["answers"][
            "reasoning_effort_" + requested
        ]
        choice = choose_reasoning_effort(
            task=record["task"],
            recent_tool_outcomes=[],
            client=Replay(result),
            requested_effort=requested,
            allowed_efforts=candidates,
            public_or_sanitized_data_ack=True,
            deadline_seconds=0.4,
        )
        if choice.get("status") != "selected":
            return None
        self.receipts.append(
            {
                "shared_request_id": result.get("request_id"),
                "cap": requested,
                "effort": choice["effort"],
                "shared_latency_ms": record["wall_ms"],
            }
        )
        # Count the physical request in skill routing only; this is an experiment
        # with user receipt lines disabled, not a shipped receipt schema.
        return {
            **choice,
            "jev_called": False,
            "jev_latency_ms": record["wall_ms"],
            "shared_decision": True,
        }


class JointClient(DecisionClient):
    def decide(self, state, questions, **kwargs):
        scope = PRE.get()
        if scope is None or not isinstance(state, dict) or state.get("stage") == 2:
            return super().decide(state, questions, **kwargs)
        task = state.get("task")
        if task != scope["task"] or not (
            "skill" in questions or "skill_chunk_0" in questions
        ):
            return super().decide(state, questions, **kwargs)
        qs = copy.deepcopy(questions)
        merged_state = copy.deepcopy(state)
        for cap in ["medium", "high"]:
            es, eq = effort_parts(task, cap)
            merged_state.update({k: v for k, v in es.items() if k != "current_request"})
            qs["reasoning_effort_" + cap] = {
                **eq["reasoning_effort"],
                "instructions": eq["reasoning_effort"]["instructions"].replace(
                    "current_request", "task"
                ),
            }
        qs["stakes"] = {
            **eq["stakes"],
            "instructions": eq["stakes"]["instructions"].replace(
                "current_request", "task"
            ),
        }
        started = time.perf_counter()
        result = super().decide(merged_state, qs, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        scope["broker"].store(
            scope["key"],
            {
                "task": task,
                "result": copy.deepcopy(result),
                "at": time.monotonic(),
                "wall_ms": elapsed,
            },
        )
        return {**result, "answers": {k: result["answers"][k] for k in questions}}


class ContextProxy:
    def __init__(self, ctx):
        global ACTIVE
        self.ctx = ctx
        self.broker = Broker()
        ACTIVE = self.broker
        from . import __name__ as package_name
        import sys

        # Only the isolated prototype's factory is changed; the host stays intact.
        sys.modules[package_name].DecisionClient = JointClient

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    def register_hook(self, kind, callback):
        if kind != "pre_llm_call" or not callback.__module__.endswith(".automatic"):
            return self.ctx.register_hook(kind, callback)

        def wrapped(**context):
            key = tuple(context.get(k) for k in ["session_id", "task_id", "turn_id"])
            self.broker.clear(key)
            task = context.get("user_message")
            clean, reason = _task_scan(task)
            eligible = (
                all(isinstance(k, str) and k.strip() for k in key)
                and isinstance(task, str)
                and len(task) <= 1200
                and clean == task
                and not reason
                and context.get("platform") == "cli"
                and not context.get("parent_session_id")
                and context.get("model") == "gpt-6-sol"
            )
            token = PRE.set(
                {"key": key, "task": clean, "broker": self.broker} if eligible else None
            )
            try:
                return callback(**context)
            finally:
                PRE.reset(token)

        return self.ctx.register_hook(kind, wrapped)

    def register_middleware(self, kind, callback):
        controller = getattr(callback, "__self__", None)
        if (
            kind != "llm_request"
            or controller is None
            or not hasattr(controller, "_ask_jev")
        ):
            return self.ctx.register_middleware(kind, callback)
        original = controller._ask_jev

        def ask(state, capture, requested, candidates, **kw):
            shared = None
            if not state.outcomes and kw.get("step_kinds") is None:
                shared = self.broker.consume(
                    WIRE.get(), capture, requested, candidates, controller
                )
            return (
                shared
                if shared is not None
                else original(state, capture, requested, candidates, **kw)
            )

        controller._ask_jev = ask

        def wrapped(request=None, **context):
            key = tuple(context.get(k) for k in ["session_id", "task_id", "turn_id"])
            supported = (
                context.get("provider") == "openai-codex"
                and context.get("api_mode") == "codex_responses"
            )
            token = WIRE.set(key if supported else None)
            try:
                return callback(request=request, **context)
            finally:
                WIRE.reset(token)

        return self.ctx.register_middleware(kind, wrapped)
