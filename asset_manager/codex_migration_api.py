from fastapi import APIRouter
from . import codex_migration as migration

router = APIRouter()


@router.get('/api/codex-migrations')
def listing():
    return {'items': migration.listing(), 'policies': migration.resource_policies()}


@router.post('/api/codex-migrations/plan')
def plan(body: dict):
    return migration.make_plan(body)


@router.get('/api/codex-migrations/{ident}')
def detail(ident: str):
    return migration.read(ident)


@router.post('/api/codex-migrations/{ident}/start')
def start(ident: str, body: dict):
    if body.get('reviewed_plan_id') != ident:
        raise ValueError('请先核对目录方案，再开始复制迁移')
    return migration.start(ident)


@router.post('/api/codex-migrations/{ident}/pause')
def pause(ident: str):
    return migration.stop(ident)


@router.get('/api/codex-migrations/{ident}/verification')
def verification(ident: str):
    return migration.reports(ident)


@router.post('/api/codex-migrations/{ident}/prepare-profile')
def prepare_profile(ident: str):
    return migration.start_activation(ident)


@router.post('/api/codex-migrations/{ident}/activate')
def activate(ident: str, body: dict):
    if body.get('reviewed_plan_id') != ident or not body.get('allow_app_restart'):
        raise ValueError('请核对接续报告并允许目标应用正常重启')
    return migration.start_activation(ident, True)


@router.post('/api/codex-migrations/{ident}/verify-native')
def verify_native(ident: str):
    return migration.verify_native(ident)


@router.post('/api/codex-migrations/{ident}/deploy-atlas')
def deploy_atlas(ident: str, body: dict):
    return migration.deploy_atlas(ident, int(body.get('port', 8765)))


@router.post('/api/codex-migrations/{ident}/verify-workspaces')
def verify_workspaces(ident: str):
    return migration.verify_workspaces(ident)


@router.post('/api/codex-migrations/{ident}/repair-runtimes')
def repair_runtimes(ident: str):
    return migration.repair_runtimes(ident)


@router.post('/api/codex-migrations/{ident}/complete')
def complete(ident: str):
    return migration.complete(ident)


@router.post('/api/codex-migrations/{ident}/recover-worktree')
def recover_worktree(ident: str, body: dict):
    return migration.recover_worktree(ident, body['thread_id'], body['repository'])


@router.post('/api/codex-migrations/{ident}/prepare-history')
def prepare_history(ident: str):
    return migration.maintain_history(ident,'prepare_history')


@router.post('/api/codex-migrations/{ident}/verify-history')
def verify_history(ident: str):
    return migration.maintain_history(ident,'verify_history')


@router.post('/api/codex-migrations/{ident}/install-history')
def install_history(ident: str, body: dict):
    if body.get('reviewed_plan_id') != ident or not body.get('allow_app_restart'):
        raise ValueError('请核对历史修复报告并允许目标正常重启')
    return migration.maintain_history(ident,'install_history')


@router.post('/api/codex-migrations/{ident}/capture-finished')
def capture_finished(ident: str):
    return migration.capture_finished_history(ident)


@router.post('/api/codex-migrations/{ident}/apply-finished')
def apply_finished(ident: str):
    return migration.apply_finished_history(ident)
