"""Read-only Git/log inference for concise task metadata updates."""
import json
import os
import re
import subprocess
from pathlib import Path
from .collaboration_registry import resolve_project,list_tasks,update_task,_load_ledger,default_data_root


def infer_git(worktree):
    def git(*args):
        p=subprocess.run(['git','-C',str(worktree),*args],capture_output=True,text=True,timeout=3)
        return p.stdout.strip() if p.returncode==0 else None
    return {'commit':git('rev-parse','HEAD'),'branch':git('branch','--show-current')}


def ctest_record(files,commit,actor):
    checks=[]
    for file in files:
        with Path(file).open('rb') as stream: raw=stream.read(2097153)
        if len(raw)>2097152: raise ValueError('ctest log exceeds 2 MiB; provide a bounded result log')
        text=raw.decode('utf-8','replace')
        matches=list(re.finditer(r'(\d+)% tests passed,\s*(\d+) tests failed out of (\d+)',text))
        if not matches or int(matches[-1][3])==0: raise ValueError('ctest log has no completed nonempty test summary')
        match=matches[-1]
        checks.append({'name':Path(file).name,'command':'ctest (operator-supplied log)','result':match[0],'status':'pass' if int(match[2])==0 else 'fail','required':True,'log_sha256':__import__('hashlib').sha256(raw).hexdigest()})
    return {'commit':commit,'actor':actor,'checks':checks,'unverified':[],'binding_basis':'operator-supplied logs for observed checkout; Continuity did not run these tests'}


def command(args):
    project=resolve_project(args.root,args.data_root)
    project_id=args.project_id or (project or {}).get('project_id')
    if not project_id: raise ValueError('registered project root or project-id required')
    task=next((t for t in list_tasks(args.data_root,project_id=project_id) if t['task_id']==args.task_id),None)
    if not task: raise ValueError('task_id does not exist')
    directory=args.worktree or task.get('worktree') or args.root
    observed=infer_git(directory)
    commit=args.commit or observed['commit'] or task.get('commit')
    branch=args.branch or observed['branch'] or task.get('branch')
    data=json.loads(args.verification_json) if args.verification_json else None
    actor=os.environ.get('CONTINUITY_ASSIGNEE') or 'operator'
    if args.ctest_log:
        data=ctest_record(args.ctest_log,commit,actor)
    if args.batch:
        ledger=_load_ledger(Path(args.data_root or default_data_root()),project_id)
        batch=ledger.get('workflow_records',{}).get('batch',{}).get(args.batch)
        if not batch or args.task_id not in batch['value']['task_ids']:
            raise ValueError('batch does not include this task')
        commit=batch['value']['result_commit']; data=batch['value']['verification']
    result=update_task(args.data_root,project_id=project_id,task_id=args.task_id,status='merged',commit=commit,branch=branch,verification_data=data,landed_commit=task.get('landed_commit') or task.get('commit'),released_in=commit,force=args.force,force_reason=args.reason)
    from .delivery_records import detail_view
    print(json.dumps({**result,'evidence_gaps':detail_view(args.data_root,project_id,args.task_id).get('evidence_gaps',[]),'batch':args.batch},ensure_ascii=False))
    return 0


def register_cli(commands):
    parser=commands.add_parser('task',help='Concise collaboration task operations; no Git mutation')
    sub=parser.add_subparsers(dest='task_action',required=True)
    merged=sub.add_parser('merged',help='Record a merge using observed Git metadata, optional batch and ctest logs')
    merged.set_defaults(handler=command)
    merged.add_argument('task_id'); merged.add_argument('--root',default='.'); merged.add_argument('--project-id'); merged.add_argument('--data-root')
    for field in ('worktree','branch','commit','batch','verification-json','reason'): merged.add_argument('--'+field)
    merged.add_argument('--ctest-log',action='append'); merged.add_argument('--force',action='store_true')
