"""Public synthetic live probes for native hooks and decision utility."""

import importlib
import json
import time


def run(root, home):
    from hermes_cli.lifecycle import invoke_hook
    from model_tools import handle_function_call
    from tools.terminal_scope import build_profile_terminal_scope, set_terminal_scope
    from agent.runtime_cwd import set_session_cwd

    workspace = home / "workspace"
    workspace.mkdir()
    set_terminal_scope(build_profile_terminal_scope(home))
    set_session_cwd(str(workspace))
    row = {}
    result = handle_function_call(
        "terminal",
        {
            "command": "printf 'routine progress line\\n%.0s' {1..400}; printf 'EXACT_RESULT=731\\n'",
            "timeout": 10,
        },
        task_id="feature-probe",
        session_id="feature-probe",
        turn_id="prune",
        tool_call_id="print",
        enabled_tools=["terminal"],
    )
    parsed = json.loads(result)
    row["native_pruning"] = {
        "exit_code": parsed.get("exit_code"),
        "collapsed": "399 additional times" in result,
        "unique_preserved": "EXACT_RESULT=731" in result,
        "result_chars": len(result),
    }

    def factory():
        credential = root._secret("openrouter")
        return root.DecisionClient(
            api_key=credential, model="typesafe/jev-1.13-20260917"
        )

    outcomes = []
    for name, failures in [
        (
            "shared",
            [
                ("terminal", "Compiler unavailable: project SDK not installed"),
                (
                    "read_file",
                    "Generated manifest missing because project SDK not installed",
                ),
                ("terminal", "Build unavailable: project SDK not installed"),
            ],
        ),
        (
            "independent",
            [
                ("terminal", "Unit test failed: expected 3, got 2"),
                ("read_file", "Optional example file missing"),
                ("terminal", "Format check failed: whitespace at line 1"),
            ],
        ),
    ]:
        advised = False
        t = time.perf_counter()
        for i, (tool, text) in enumerate(failures):
            results = invoke_hook(
                "transform_tool_result",
                tool_name=tool,
                args={},
                result=json.dumps({"error": text}),
                status="error",
                session_id="feature-probe",
                task_id="feature-probe",
                turn_id=name,
                tool_call_id=str(i),
            )
            advised = advised or any(
                isinstance(r, str) and "[Switchyard:" in r for r in results
            )
        outcomes.append(
            {
                "id": name,
                "advised": advised,
                "wall_ms": round((time.perf_counter() - t) * 1000, 1),
            }
        )
    row["stuck"] = outcomes
    timing = [
        ("new_topic", "We finished the recipe. Now explain Saturn's rings.", True),
        (
            "refers_back",
            "Use the exact retry budget from our earlier investigation.",
            False,
        ),
        (
            "implicit_dependency",
            "Now update the config using those constraints.",
            False,
        ),
        (
            "new_with_dependency",
            "Switch topics to deployment, but keep the database limits we established.",
            False,
        ),
    ]
    client = factory()
    rows = []
    try:
        for name, message, expected in timing:
            response = client.decide(
                {
                    "previous_topic": "Earlier discussion established a recipe and precise retry/database limits.",
                    "incoming_message": message,
                },
                {
                    "new_topic": {
                        "type": "noul",
                        "instructions": "Does the incoming request begin a new topic?",
                        "criteria": {"true": "New topic", "false": "Same topic"},
                    },
                    "refers_back": {
                        "type": "noul",
                        "instructions": "Does the new request depend on any earlier discussion, facts, constraints or decisions? Implicit references count.",
                        "criteria": {
                            "true": "Depends on earlier context",
                            "false": "Independent",
                        },
                    },
                },
            )
            a = response["answers"]
            early = a["new_topic"]["noul"] >= 0.7 and a["refers_back"]["noul"] <= 0.3
            rows.append(
                {
                    "id": name,
                    "expected_early_candidate": expected,
                    "early_candidate": early,
                    "answers": a,
                    "applied": False,
                }
            )
    finally:
        client.close()
    row["compaction_shadow"] = rows
    browser = importlib.import_module(root.__name__ + ".browser_use")
    plans = importlib.import_module(root.__name__ + ".browser_plan")
    cache = plans.BrowserPlanCache()
    snapshots = []
    original_begin = cache.begin

    def observe_begin(scope, goal, page, condition, min_actions):
        snapshots.append(json.loads(json.dumps(plans.page_evidence(page))))
        return original_begin(scope, goal, page, condition, min_actions)

    cache.begin = observe_begin
    browsers = []
    for repeat in range(2):
        client = factory()
        try:
            receipt = browser.run_browser_goal(
                goal="Starting on Cat, open the Felidae article on Wikipedia.",
                start_url="https://en.wikipedia.org/wiki/Cat",
                client=client,
                max_steps=4,
                deadline_seconds=35,
                completion_condition={
                    "url_equals": "https://en.wikipedia.org/wiki/Felidae"
                },
                plan_cache=cache,
                cache_scope="synthetic-public-probe",
            )
            browsers.append(
                {
                    k: receipt.get(k)
                    for k in [
                        "status",
                        "failure_phase",
                        "goal_verified",
                        "completion",
                        "action_dispatched_count",
                        "effect_observed_count",
                        "jev_request_count",
                        "attempted_request_count",
                        "plan_cache_hits",
                        "elapsed_ms",
                    ]
                }
            )
        finally:
            client.close()
    row["public_browser"] = browsers
    row["cache_entries"] = len(cache.plans)
    if len(snapshots) == 2:
        row["changed_page_keys"] = [
            k for k in snapshots[0] if snapshots[0].get(k) != snapshots[1].get(k)
        ]
        row["page_keys"] = sorted(snapshots[0])
    return row
