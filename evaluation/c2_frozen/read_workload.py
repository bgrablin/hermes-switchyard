"""Frozen bounded source-read workload; real local file I/O, no artificial delay."""
import hashlib,json,pathlib,time
CASES=['repeat','task_change','session_change','source_change','freshness_change','missing_scope','missing_freshness','incomplete','mutable_page','mutation']
def workload(home, job, gate):
    from hermes_cli.middleware import run_tool_execution_middleware
    from hermes_cli.lifecycle import invoke_hook
    case=job['id']; root=home/'workspace'/job['job_id'];root.mkdir()
    a=root/'sourceA.json';b=root/'sourceB.json'
    a.write_text(json.dumps({'code':17,'revision':'v1','payload':'public synthetic fixture'}))
    b.write_text(json.dumps({'code':41,'revision':'v1','payload':'public synthetic alternate source'}))
    state={'path':a};events=[];calls=[];proof_calls=[];outputs=[]
    if gate:gate.reset_store_for_tests()
    scope={'session_id':'workload-session','task_id':job['job_id']}
    args={'snapshot_id':'v1'} if case!='mutable_page' else {'page_id':'mutable'}
    def proof(name,args):
        proof_calls.append(1)
        if case in {'missing_freshness','mutable_page'}:return None
        raw=state['path'].read_bytes(); parsed=json.loads(raw)
        digest=hashlib.sha256(raw).hexdigest()
        return gate.ReadEvidence('file://'+str(state['path'])+'#account=fixture&workspace='+job['job_id'],
                                 parsed['revision'],digest,case!='incomplete',True)
    started=time.perf_counter()
    for i in range(4):
        name='read_file'
        if i==2:
            if case=='task_change':scope['task_id']=job['job_id']+'-sibling'
            if case=='session_change':scope['session_id']='other-session'
            if case=='source_change':state['path']=b
            if case=='freshness_change':a.write_text(json.dumps({'code':29,'revision':'v2','payload':'changed source'}))
            if case=='mutation':
                run_tool_execution_middleware('write_file',{'path':str(a)},lambda args:'ok',**scope)
                invoke_hook('post_tool_call',tool_name='write_file',args={'path':str(a)},result='ok',status='ok',**scope)
        args={**args,'path':str(state['path'])}
        active_scope={} if case=='missing_scope' else dict(scope)
        expected=state['path'].read_text()  # independent oracle excluded from event latency
        before=len(calls)
        def dispatch(args):calls.append(1);return state['path'].read_text()
        tick=time.perf_counter()
        result=run_tool_execution_middleware(name,args,dispatch,**active_scope,
                                            **({'reuse_evidence_provider':proof} if gate else {}))
        invoke_hook('post_tool_call',tool_name=name,args=args,result=result,status='ok',**active_scope)
        elapsed=(time.perf_counter()-tick)*1000
        reused=len(calls)==before
        allowed=i in {1,3} or (i==2 and case=='repeat')
        if case in {'missing_scope','missing_freshness','incomplete','mutable_page'}:allowed=False
        invalid=reused and (not allowed or result!=expected)
        events.append({'index':i,'reused':reused,'allowed_reuse':allowed,'invalid_reuse':invalid,
                       'exact_result':result==expected,'tool_ms':elapsed,'result_sha256':hashlib.sha256(result.encode()).hexdigest(),
                       'expected_sha256':hashlib.sha256(expected.encode()).hexdigest(),'source':state['path'].name,'scope':active_scope})
        outputs.append(json.loads(result))
    # Scored latency includes all source verification and tool dispatch. Fixture setup excluded.
    measured_tool_ms=sum(e['tool_ms'] for e in events)
    return {'events':events,'outputs':outputs,'tool_ms':measured_tool_ms,'read_dispatches':len(calls),
            'verification_reads':len(proof_calls),'orchestration_ms':(time.perf_counter()-started)*1000,
            'expected':{'codes':[17,17,41,41] if case=='source_change' else [17,17,29,29] if case=='freshness_change' else [17]*4}}
