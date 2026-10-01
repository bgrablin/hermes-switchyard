"""Seven native prerequisites. No network. Real plugin loader and middleware."""
import hashlib, importlib, json, os, pathlib, shutil, sys, subprocess
ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parents[1]
HOME = ROOT / 'runs/prerequisites'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}: raise RuntimeError('offline prerequisites')
sys.addaudithook(audit)
assert not HOME.exists()
for part in ['plugins', 'skills', 'empty-bundled', 'workspace']: (HOME/part).mkdir(parents=True, exist_ok=True)
dest=HOME/'plugins/hermes-switchyard';dest.mkdir()
for part in ['__init__.py', 'plugin.yaml']: shutil.copy2(REPO/part,dest/part)
shutil.copytree(REPO/'hermes_switchyard',dest/'hermes_switchyard',ignore=shutil.ignore_patterns('__pycache__'))
(HOME/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['hermes-switchyard'],'entries':{'hermes-switchyard':{'settings':{'automatic_skill_recommendation':False,'adaptive_reasoning_effort':False,'local_duplicate_tool_gate':True}}}}}))
os.environ['HERMES_HOME']=str(HOME);os.environ['HERMES_BUNDLED_PLUGINS']=str(HOME/'empty-bundled')
from hermes_constants import set_hermes_home_override
set_hermes_home_override(str(HOME))
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load(force=True)
cb=next(c for c in manager._middleware['tool_execution'] if 'local_duplicate_gate' in c.__module__)
g=importlib.import_module(cb.__module__)
from hermes_cli.middleware import run_tool_execution_middleware
from hermes_cli.lifecycle import invoke_hook
checks=[]
for name in ['same_scope_exact','missing_observation_identity','sibling_task_same_session','missing_scope_identity','oversized_result','cross_mcp_server','mutable_page_id']:
    g.reset_store_for_tests(); calls=[]
    body='complete original';args={'snapshot_id':'immutable-v1'}
    scope={'session_id':'session-1','task_id':'task-1'};scope2=dict(scope)
    tool='browser_snapshot';tool2=tool
    if name=='sibling_task_same_session': scope2['task_id']='task-2'
    if name=='missing_scope_identity':scope={};scope2={}
    if name=='oversized_result':body='x'*262145
    if name=='cross_mcp_server':tool='mcp__sourceA__browser_snapshot';tool2='mcp__sourceB__browser_snapshot'
    if name=='mutable_page_id':args={'page_id':'mutable-page'}
    if name=='missing_observation_identity':args={}
    def proof(n,a):
        if name in {'missing_observation_identity','mutable_page_id'}:return None
        return g.ReadEvidence('fixture://server/account/workspace/source', 'immutable-v1', hashlib.sha256(body.encode()).hexdigest(),True,True)
    def first(a):calls.append('first');return body
    def second(a):calls.append('second');return 'fresh'
    one=run_tool_execution_middleware(tool,args,first,**scope,reuse_evidence_provider=proof)
    invoke_hook('post_tool_call',tool_name=tool,args=args,result=one,status='ok',**scope)
    two=run_tool_execution_middleware(tool2,args,second,**scope2,reuse_evidence_provider=proof)
    dispatched='second' in calls
    ok=(not dispatched and two==body) if name=='same_scope_exact' else dispatched
    checks.append({'id':name,'pass':ok,'second_dispatched':dispatched,'complete_result':two==body,'counters':g.gate_counters()})
report={'revision':subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip(),
        'gate_sha256':sha(REPO/'hermes_switchyard/local_duplicate_gate.py'),'script_sha256':sha(pathlib.Path(__file__)),
        'checks':checks,'passed':sum(c['pass'] for c in checks),'total':7,'provider_calls':0,
        'trusted_adapter_required':True,'native_stock_missing_evidence':'dispatch',
        'go':all(c['pass'] for c in checks)}
(ROOT/'prerequisites.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2));manager.unload()
assert report['go']
