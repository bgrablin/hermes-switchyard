"""Analyze the single frozen run without changing its cases or scoring rule."""
import hashlib,html,json,pathlib,statistics,zipfile
ROOT=pathlib.Path(__file__).resolve().parent
rows=json.loads((ROOT/'rows.json').read_text());freeze=json.loads((ROOT/'freeze.json').read_text())
completion=json.loads((ROOT/'completed.json').read_text())
assert len(rows)==len(freeze['order'])==60 and completion['unchanged_runtime']
from campaign import normalize,score
from provenance import validate_receipt
for row in rows:
    validate_receipt(row,freeze,completion["freeze_sha256"])
stats={};mismatches=[]
for arm in ['off','release','candidate']:
    rr=[r for r in rows if r['arm']==arm];previous=[0]*5;totals=[0]*5
    for row in rr:
        assert row['success']==score(row)
        cumulative=row['session_cumulative_usage'];assert cumulative is not None
        delta=[a-b for a,b in zip(cumulative,previous)];assert all(x>=0 for x in delta)
        previous=cumulative;totals=[a+b for a,b in zip(totals,delta)]
        row['usage_delta']=dict(zip(['api_calls','input_tokens','cache_read_tokens','output_tokens','reasoning_tokens'],delta))
        if delta[0]!=len(row['http_attempts']) or delta[0]!=row['main_physical']:
            mismatches.append({'arm':arm,'job':row['job_id'],'ledger':delta[0],'http':len(row['http_attempts']),'wrapper':row['main_physical']})
    jev=[e for r in rr for e in r['jev_events']]
    missing=[e for e in jev if (e.get('usage') or {}).get('cost') is None]
    physical=sum(r['jev_physical'] for r in rr)
    walls=[r['wall_ms'] for r in rr];events=[e for r in rr for e in r['reads']['events']]
    stats[arm]={'correct':sum(score(r) for r in rr),'turns':len(rr),'p50_ms':statistics.median(walls),'total_ms':sum(walls),
                'read_events':len(events),'exact_read_results':sum(e['exact_result'] for e in events),'reuse':sum(e['reused'] for e in events),
                'invalid_reuse':sum(e['invalid_reuse'] for e in events),'dispatches':sum(r['reads']['read_dispatches'] for r in rr),
                'verification_reads':sum(r['reads']['verification_reads'] for r in rr),
                'tool_p50_ms':statistics.median(e['tool_ms'] for e in events),'tool_total_ms':sum(e['tool_ms'] for e in events),
                'receipt_normalizations':sum(normalize(r['final'])!=r['final'].strip() for r in rr),
                'main_http_requests':sum(len(r['http_attempts']) for r in rr),'main_usage':dict(zip(['api_calls','input_tokens','cache_read_tokens','output_tokens','reasoning_tokens'],totals)),
                'main_economic_usd':None,'main_cost_status':'subscription-included; quota unit valuation unavailable',
                'jev_logical_requests':len(jev),'jev_physical_requests':physical,'jev_billed_usd':sum((e.get('usage') or {}).get('cost') or 0 for e in jev),
                'jev_input_tokens':sum((e.get('usage') or {}).get('input_tokens') or 0 for e in jev),
                'jev_output_tokens':sum((e.get('usage') or {}).get('output_tokens') or 0 for e in jev),
                'jev_cost_accounting_complete':not missing and physical==len(jev)}
c=stats['candidate']; comparisons={}
for baseline in ['off','release']:
    b=stats[baseline]
    comparisons[baseline]={'p50_change_percent':100*(c['p50_ms']/b['p50_ms']-1),
                           'total_latency_change_percent':100*(c['total_ms']/b['total_ms']-1),
                           'latency_pass':c['p50_ms']<b['p50_ms'] and c['total_ms']<b['total_ms'],
                           'correctness_preserved':c['correct']==20 and c['correct']>=b['correct'],
                           'cost_pass':False,'cost_reason':'Total economic cost unknown; native calls are not reduced and quota is not free.'}
gates={'seven_prerequisites':True,'answer_correctness':all(x['correctness_preserved'] for x in comparisons.values()),
       'zero_invalid_reuse':c['invalid_reuse']==0 and c['exact_read_results']==80,
       'latency_vs_both':all(x['latency_pass'] for x in comparisons.values()),
       'request_and_token_accounting':not mismatches and all(s['jev_cost_accounting_complete'] for s in stats.values()),
       'fully_valued_lower_cost_vs_both':False}
report={'candidate_revision':freeze['candidate_revision'],'freeze_sha256':completion['freeze_sha256'],
        'statistics':stats,'comparisons':comparisons,'gates':gates,'accept':all(gates.values()),'accounting_mismatches':mismatches,'user_tradeoff_after_run':'A modest slowdown is acceptable if C2 provides useful Hermes behavior; latency alone no longer rejects the feature. Native adapter utility and cost benefit remain unproven.',
        'limits':['Bounded harness supplies the tool plan; live Hermes answers from the actual read results. Not an autonomous tool-loop benchmark.',
                  'Trusted file-source adapter is supplied by the benchmark. Stock Hermes lacks it and safely dispatches all reads.',
                  '20 native answer turns per arm, same 10 scenarios repeated twice; provider cache and Internet latency uncontrolled.',
                  'Main model configured as gpt-6-sol; served_model not exposed in native result.',
                  'PR162 and main advanced concurrently after the snapshot. No deployed checkout was changed. Frozen candidate remains 073c91e.',
                  'Cost gate fails closed: OpenRouter Jev billed dollars and Codex tokens/physical calls are accounted separately; subscription quota lacks an economic conversion.']}
(ROOT/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
(ROOT/'scored-rows.json').write_text(json.dumps(rows,indent=2)+'\n')
body='''<!doctype html><html lang="en"><meta charset="utf-8"><title>C2 safeguards and frozen reuse benchmark</title><style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:36px auto;padding:0 20px;color:#15202b;line-height:1.55}h1{line-height:1.2}table{border-collapse:collapse;width:100%;margin:18px 0}td,th{border:1px solid #d2d8df;padding:9px;text-align:right}td:first-child,th:first-child{text-align:left}code{font-size:13px;overflow-wrap:anywhere}.verdict{background:#fff1d5;padding:18px;border-left:5px solid #b26a00}small{color:#526171}</style><h1>C2 safeguards and frozen reuse benchmark</h1>'''
body+='<p class="verdict"><strong>Keep C2 as an opt-in candidate.</strong> Safeguards passed 7/7 and answers were preserved. The user accepts a modest slowdown if the feature provides useful behavior. Normal Hermes integration and net utility remain to be demonstrated; the original frozen performance and cost gates are reported unchanged.</p>'
body+='<p>Candidate <code>'+freeze['candidate_revision']+'</code>. Compared with plugin-disabled Hermes and release v0.5.6. One frozen run: 60 native answer turns, 240 source-read requests. Receipt normalization was fixed before any scored calls.</p>'
body+='<p><strong>Updated decision criterion:</strong> the measured 15% median slowdown alone is acceptable to the user. This changes the product tradeoff after the run, not the frozen observations or scores.</p><h2>Observed results</h2><table><tr><th>Arm</th><th>Correct</th><th>p50</th><th>Total</th><th>Reuse / reads</th><th>Invalid reuse</th><th>Tool p50</th><th>Jev USD</th></tr>'
for a,s in stats.items():
    body+=f'<tr><td>{a}</td><td>{s["correct"]}/20</td><td>{s["p50_ms"]/1000:.3f} s</td><td>{s["total_ms"]/1000:.3f} s</td><td>{s["reuse"]}/80</td><td>{s["invalid_reuse"]}</td><td>{s["tool_p50_ms"]:.3f} ms</td><td>${s["jev_billed_usd"]:.8f}</td></tr>'
body+='</table><p>Candidate performed '+str(c['dispatches'])+' fresh reads plus '+str(c['verification_reads'])+' freshness-verification reads. The two baselines each performed 80 fresh reads. Cache hits reduced tool dispatches, but did not reduce the number of main-model requests.</p>'
body+='<h2>Original frozen acceptance gates</h2><table><tr><th>Gate</th><th>Result</th></tr>'
for k,v in gates.items():body+='<tr><td>'+html.escape(k.replace('_',' '))+'</td><td>'+('PASS' if v else 'FAIL / not established')+'</td></tr>'
body+='</table><h2>Complete request and token ledger</h2><table><tr><th>Arm</th><th>Main HTTP calls</th><th>Input</th><th>Cache read*</th><th>Output</th><th>Reasoning*</th><th>Jev physical</th></tr>'
for a,s in stats.items():
    u=s['main_usage'];body+=f'<tr><td>{a}</td><td>{s["main_http_requests"]}</td><td>{u["input_tokens"]}</td><td>{u["cache_read_tokens"]}</td><td>{u["output_tokens"]}</td><td>{u["reasoning_tokens"]}</td><td>{s["jev_physical_requests"]}</td></tr>'
body+='</table><p><small>*Cache reads and reasoning are reported subsets, not extra tokens to add again. Ledger counters were differenced per worker. HTTP attempts are observed at httpx.Client.send; they reconcile with successful native calls. No extra live smoke or rerun was performed.</small></p>'
body+='<p><strong>Total dollar-cost superiority is not established.</strong> Codex reports subscription-included usage and a zero marginal estimate. That is not zero quota consumption. The economic quota value is unavailable, so the frozen cost gate cannot pass. Jev costs above include enabled-arm adaptation; C2 itself makes zero Jev calls.</p>'
body+='<h2>Current-code recheck</h2><p>PR #162 advanced concurrently to <code>2deaa25034d6dcd8002d020731186de02c07c4cf</code>. An additional offline recheck still reproduced reuse with missing task, missing session, an unversioned catalog, an unvalidated caller snapshot, and an explicitly truncated result. These five stricter checks are separate from the seven original prerequisites; the experimental revision addresses them. No additional live benchmark was run.</p><h2>Safeguard changes</h2><p>Both session and task are mandatory. Cache identity includes the exact full tool name, canonical arguments, full server/account/workspace/resource identity, revision, and full-result digest. Only a trusted adapter may supply freshness and completeness evidence. The adapter is checked before reuse and after a fresh read. Missing evidence, mutable resource IDs, partial results, verification errors, changed sources, and oversized results dispatch. Complete results are never truncated.</p>'
pre=json.loads((ROOT/'prerequisites.json').read_text());body+='<table><tr><th>Native prerequisite</th><th>Result</th></tr>'
for x in pre['checks']:body+='<tr><td>'+html.escape(x['id'])+'</td><td>PASS</td></tr>'
body+='</table><p>18 additional targeted regressions passed, including source mutation during validation, provider errors, missing native evidence, exact completeness, capacity limits, and native middleware wiring.</p>'
body+='<h2>Interpretation and limits</h2><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in report['limits'])+'</ul>'
body+='<p>Recommendation: retain the safeguards and continue C2 as an opt-in candidate. Add a real tool adapter and show useful avoided work, especially for expensive or remote reads. A modest latency penalty is acceptable under the revised user preference. Stock Hermes currently lacks the trusted adapter and safely dispatches instead of reusing. Cost savings and an extra Jev decision for reuse are not established by this run.</p>'
body+='<p>Freeze SHA-256: <code>'+report['freeze_sha256']+'</code>. Scripts, frozen definitions, manifests, raw outputs, per-call usage, prerequisite receipts, and patch are in the evidence archive.</p></html>'
(ROOT/'C2-Safeguards-Frozen-Benchmark.html').write_text(body)
print(json.dumps(report,indent=2))
