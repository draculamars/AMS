from fastapi import APIRouter
from . import core, vault, knowledge
router=APIRouter()

@router.get('/api/vault')
def overview():return vault.overview()
@router.get('/api/vault/profiles/{pid}/plan')
def plan(pid:str):return vault.plan(pid)
@router.post('/api/vault/profiles/{pid}/backup')
def backup(pid:str):
    vault.profile(pid)
    return {'job_id':core.run_job('vault_backup',vault.backup,pid)}
@router.post('/api/vault/snapshots/{sid}/verify')
def verify(sid:str,body:dict):
    vault.profile(vault.snapshot(sid)['profile_id'])
    return {'job_id':core.run_job('vault_verify',vault.verify,sid,bool(body.get('full')))}
@router.post('/api/vault/snapshots/{sid}/drill')
def drill(sid:str):
    vault.profile(vault.snapshot(sid)['profile_id'])
    return {'job_id':core.run_job('vault_drill',vault.drill,sid)}
@router.post('/api/vault/snapshots/{sid}/restore-plan')
def restore_plan(sid:str,body:dict):return vault.restore_plan(sid,body.get('paths'))
@router.post('/api/vault/restores/{rid}/apply')
def restore(rid:str,body:dict):
    if body.get('acknowledgement')!='确认恢复到隔离目录':raise ValueError('请先查看方案并确认隔离恢复')
    return {'job_id':core.run_job('vault_restore',vault.restore,rid)}
@router.post('/api/vault/sync')
def sync():return {'job_id':core.run_job('vault_metadata_sync',vault.sync_metadata)}
@router.get('/api/knowledge')
def items(q:str='',kind:str='',page:int=1):return knowledge.listing(q,kind,page)
@router.get('/api/knowledge/{ident}')
def item(ident:str):return knowledge.detail(ident)
@router.post('/api/knowledge-index')
def index():return {'job_id':core.run_job('knowledge_scan',knowledge.scan)}
