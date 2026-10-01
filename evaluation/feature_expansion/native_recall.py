"""Small native-provider recall check; not a long-session compaction benchmark."""

import importlib
import json
import time


def run(root, template):
    from hermes_constants import set_hermes_home_override
    from agent.auxiliary_client import call_llm

    set_hermes_home_override(str(template))
    output = importlib.import_module(root.__name__ + ".output_pruning")
    gold = {"deployment_id": "DEP-7382", "rollback_sha": "a9e28c1", "port": 4827}
    original = (
        "ordinary build progress\n" * 200
        + "deployment_id=DEP-7382\n"
        + "ordinary build progress\n" * 200
        + "rollback_sha=a9e28c1\nport=4827\n"
    )
    compressed, _ = output.compact_repeated_lines(original)
    rows = []
    for repeat in range(2):
        for arm, text in (
            (("original", original), ("duplicate_counts", compressed))
            if repeat == 0
            else (("duplicate_counts", compressed), ("original", original))
        ):
            route = {}
            started = time.perf_counter()
            response = call_llm(
                provider="openai-codex",
                model="gpt-6.1-sol",
                timeout=30,
                max_tokens=512,
                route_info=route,
                messages=[
                    {
                        "role": "system",
                        "content": "Extract facts from the supplied tool output. Return only a JSON object with deployment_id, rollback_sha, and integer port. Do not infer absent facts.",
                    },
                    {"role": "user", "content": text},
                ],
            )
            answer = (response.choices[0].message.content or "").strip()
            try:
                parsed = json.loads(answer)
            except ValueError:
                parsed = None
            rows.append(
                {
                    "repeat": repeat,
                    "arm": arm,
                    "input_chars": len(text),
                    "correct": parsed == gold,
                    "answer": answer,
                    "route": route,
                    "wall_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
    return {
        "scope": "public synthetic extraction only; no transcript compression or long-session claim",
        "rows": rows,
    }
