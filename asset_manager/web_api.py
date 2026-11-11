from fastapi import APIRouter
from fastapi.responses import FileResponse
from . import web_assets

router=APIRouter()

@router.get('/api/web-assets')
def listing(q:str='',project:str='',kind:str='',page:int=1,limit:int=40):
    return web_assets.listing(q,project,kind,page,limit)

@router.post('/api/web-publications/{pid}/preview')
def ticket(pid:str):return web_assets.preview_ticket(pid)

@router.get('/api/web-preview/{token}/{bid}/{path:path}')
def content(token:str,bid:str,path:str):
    source,mime=web_assets.preview_file(token,bid,path)
    return FileResponse(source,media_type=mime)
