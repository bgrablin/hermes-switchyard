"""Memory-only replay of successful public navigation decisions.

Plans never contain typing, buttons, completion decisions, or retained page text.
A fresh observation and the normal browser gates are still required at every step.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(encoded) > 256_000:
        raise ValueError("plan state too large")
    return hashlib.sha256(encoded.encode()).hexdigest()


def page_evidence(page):
    # A document generation is session-specific. Cache content, not that handle;
    # the action loop separately requires the fresh generation to remain stable.
    evidence = {k: v for k, v in page.items() if k != "document_id"}
    # These plans contain links only. Offscreen document height can change as
    # images load; it cannot identify an offered link. Offset and viewport stay.
    if isinstance(evidence.get("scroll"), dict):
        evidence["scroll"] = {
            k: v for k, v in evidence["scroll"].items() if k != "document_height"
        }
    return evidence


class BrowserPlanCache:
    def __init__(self, *, ttl_seconds=300, max_plans=32):
        self.ttl = ttl_seconds
        self.limit = max_plans
        self.plans = OrderedDict()
        self.lock = threading.Lock()

    def begin(self, scope, goal, page, condition, min_actions):
        key = fingerprint([scope, goal, page_evidence(page), condition, min_actions])
        with self.lock:
            now = time.monotonic()
            for stale in [
                k for k, (at, _) in self.plans.items() if now - at > self.ttl
            ]:
                self.plans.pop(stale)
            prior = copy.deepcopy(self.plans.get(key, (None, {}))[1])
        return Replay(self, key, prior)

    def save(self, key, steps):
        with self.lock:
            self.plans[key] = (time.monotonic(), copy.deepcopy(steps))
            self.plans.move_to_end(key)
            while len(self.plans) > self.limit:
                self.plans.popitem(last=False)


class Replay:
    def __init__(self, cache, key, prior):
        self.cache, self.key, self.prior = cache, key, prior
        self.steps = {}
        self.eligible = True

    def lookup(self, page, state, questions):
        key = fingerprint([page_evidence(page), state, questions])
        found = self.prior.get(key)
        if found is None:
            # Divergence invalidates the rest of this plan, not just this step.
            self.prior = {}
        return copy.deepcopy(found)

    def record(self, page, state, questions, decision):
        answers = decision.get("answers", {})
        operation = answers.get("operation", {}).get("choice")
        if operation == "DONE":
            return  # Completion always needs fresh evidence.
        target = answers.get("click_target", {}).get("choice")
        element = next(
            (
                e
                for e in page.get("elements", [])
                if isinstance(e, dict) and e.get("id") == target
            ),
            {},
        )
        href = urlsplit(str(element.get("href") or ""))
        current = urlsplit(str(page.get("url") or ""))
        eligible = (
            operation == "CLICK"
            and element.get("role") == "link"
            and href.scheme == "https"
            and href.netloc == current.netloc
            and not href.query
            and not href.username
            and not href.password
        )
        if not eligible or len(self.steps) >= 20:
            self.eligible = False
            return
        # Only validated Choice answers survive; no text or provider usage is cached.
        self.steps[fingerprint([page_evidence(page), state, questions])] = {
            "answers": copy.deepcopy(answers),
            "source": "plan_cache",
            "usage": {},
            "latency_ms": 0,
        }

    def finish(self, *, status, completion, actions):
        if (
            self.eligible
            and self.steps
            and status == "completion_candidate"
            and isinstance(completion, dict)
            and completion.get("satisfied") is True
            and actions
            and all(
                a.get("operation") == "CLICK" and a.get("effect_observed") is True
                for a in actions
            )
        ):
            self.cache.save(self.key, self.steps)
