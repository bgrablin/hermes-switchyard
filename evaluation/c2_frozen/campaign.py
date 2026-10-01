"""One preregistered interleaved reuse run. Refuses overwrite or failed preflight."""
import hashlib,json,pathlib,os,queue,re,statistics,subprocess,threading,time
from read_workload import CASES
from provenance import validate_receipt
ROOT=pathlib.Path(__file__).resolve().parent
PY='/home/brian/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3'
HERMES=pathlib.Path('/home/brian/.hermes/hermes-agent')
BOOT="import sys,runpy; sys.path.insert(0,'/home/brian/.hermes/hermes-agent'); import hermes_bootstrap; sys.argv=[sys.argv[1],*sys.argv[2:]]; runpy.run_path(sys.argv[0],run_name='__main__')"
ARMS=['off','release','candidate']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dump(p,x):p.write_text(json.dumps(x,sort_keys=True,indent=2)+'\n')
def manifest(root):
    return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in {'.py','.yaml'} and not any(x in p.relative_to(root).parts for x in ['__pycache__','.venv','.git','tests','evaluation'])}
def normalize(text):
    return re.sub(r'\n\nswitchyard: effort [^\n]+$','',text).strip()
def score(row):
    try:return row['completed'] is True and json.loads(normalize(row['final']))==row['reads']['expected']
    except (TypeError,ValueError):return False
def receive(proc,prefix,timeout,log):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        line=proc.q.get(timeout=max(.1,until-time.monotonic()))
        if not line:raise RuntimeError('worker exited before '+prefix)
        log.write(line);log.flush()
        if line.startswith('EVAL_ERROR'):raise RuntimeError(line.strip())
        if line.startswith(prefix):return json.loads(line[len(prefix):])
    raise TimeoutError(prefix)
def main():
    assert not (ROOT/'freeze.json').exists(), 'one frozen run only'
    prereq=json.loads((ROOT/'prerequisites.json').read_text())
    assert prereq['go'] and prereq['passed']==prereq['total']==7
    assert sha(ROOT/'sources/candidate/hermes_switchyard/local_duplicate_gate.py')==prereq['gate_sha256']
    assert normalize('{"codes":[1]}\n\nswitchyard: effort high → low (adapted)')=='{"codes":[1]}'
    assert normalize('{"note":"switchyard: effort is data"}')=='{"note":"switchyard: effort is data"}'
    order=[]
    for repeat in range(2):
        for i,case in enumerate(CASES):
            arms=ARMS[i%3:]+ARMS[:i%3]
            if repeat:arms=list(reversed(arms))
            order.extend({'arm':arm,'id':case,'repeat':repeat,'job_id':f'r{repeat}-{case}','cap':'high'} for arm in arms)
    sources={a:manifest(ROOT/'sources'/a) for a in ARMS if a!='off'}
    runtime=manifest(HERMES)
    freeze={'schema':'c2-reuse-frozen/1','candidate_revision':prereq['revision'],
            'current_main':'b9f68132c158920dbea6659bd2c4741872e23588','release_revision':'552940b8fbb89d453c6356bd29559484cdfc8a9b',
            'prerequisites_sha256':sha(ROOT/'prerequisites.json'),'source_manifests':sources,'runtime_manifest':runtime,
            'runner_hashes':{f:sha(ROOT/f) for f in ['worker.py','read_workload.py','campaign.py','provenance.py']},
            'cases':CASES,'order':order,'model':'gpt-6-sol','provider':'openai-codex','initial_effort':'high',
            'jev_model':'typesafe/jev-1.13-20260917','jev_provider':'openrouter',
            'gate':{'answer_correctness':'20/20 and >= both baselines','invalid_reuse':0,
                    'latency':'strictly lower p50 AND total vs both baselines',
                    'cost':'all physical attempts and all billed/quota usage accounted; strictly lower total economic cost vs both baselines; unknown does not pass'},
            'normalization':'remove only one final newline-newline switchyard: effort receipt line; same regex in score(), frozen before calls; preserve raw outputs',
            'scope':'Real native plugin loader and tool middleware; harness supplies four reads then native Hermes generates answer from read results. Trusted local-file revalidation adapter is experimental; stock host does not supply it. No model-planned tool loop claim.',
            'timing':'source workflow plus run_conversation; fixture/process setup excluded; source verification included; no artificial delays; provider cache state uncontrolled',
            'accounting':'Jev billed USD + all native physical HTTP attempts + ledger token deltas; subscription quota not valued as zero dollars',
            'limits':{'turns':60,'main_physical':120,'jev_physical':120,'jev_usd':0.02,'wall_seconds':1800}}
    dump(ROOT/'freeze.json',freeze);freeze_digest=sha(ROOT/'freeze.json');print('FROZEN '+freeze_digest,flush=True)
    env={**os.environ,'HERMES_HOME':'/home/brian/.hermes-eval/switchyard-abc','HERMES_ENABLE_PROJECT_PLUGINS':'0','HERMES_DISABLE_LAZY_INSTALLS':'1','PYTHONDONTWRITEBYTECODE':'1'}
    children={};logs={};rows=[];start=time.monotonic()
    try:
        for arm in ARMS:
            p=subprocess.Popen([PY,'-I','-c',BOOT,str(ROOT/'worker.py'),'--arm',arm,'--name','frozen-'+arm],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,env=env,cwd=ROOT)
            p.q=queue.Queue()
            def feed(proc=p):
                for line in proc.stdout:proc.q.put(line)
                proc.q.put('')
            threading.Thread(target=feed,daemon=True).start();children[arm]=p
            logs[arm]=(ROOT/(arm+'.log')).open('w')
            print('READY '+json.dumps(receive(p,'EVAL_READY ',60,logs[arm])),flush=True)
        for i,job in enumerate(order):
            assert time.monotonic()-start<1800
            p=children[job['arm']];p.stdin.write(json.dumps(job)+'\n');p.stdin.flush()
            row=receive(p,'EVAL_ROW ',120,logs[job['arm']]);validate_receipt(row,freeze,freeze_digest);row['order_index']=i;row['success']=score(row)
            rows.append(row);dump(ROOT/'rows.json',rows)
            print('ROW '+json.dumps({'index':i,'arm':row['arm'],'case':row['id'],'correct':row['success'],'ms':round(row['wall_ms'],2),'reuse':sum(e['reused'] for e in row['reads']['events']),'invalid':sum(e['invalid_reuse'] for e in row['reads']['events']),'http':len(row['http_attempts'])}),flush=True)
            if any(e['invalid_reuse'] for e in row['reads']['events']):raise RuntimeError('invalid reuse: halt')
            assert sum(len(r['http_attempts']) for r in rows)<=120
            assert sum(r['jev_physical'] for r in rows)<=120
            assert sum((e.get('usage') or {}).get('cost',0) or 0 for r in rows for e in r['jev_events'])<=0.02
        assert sources=={a:manifest(ROOT/'sources'/a) for a in ARMS if a!='off'}
        assert runtime==manifest(HERMES)
        dump(ROOT/'completed.json',{'rows':len(rows),'freeze_sha256':sha(ROOT/'freeze.json'),'unchanged_sources':True,'unchanged_runtime':True})
        print('COMPLETED',flush=True)
    finally:
        for p in children.values():
            if p.poll() is None:
                try:p.stdin.write('{"stop":true}\n');p.stdin.flush();p.stdin.close()
                except BrokenPipeError:pass
        for p in children.values():
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.terminate()
        for log in logs.values():log.close()
if __name__=='__main__':main()
