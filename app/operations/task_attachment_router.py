import hashlib
from urllib.parse import quote
from fastapi import APIRouter,Depends,File,Form,HTTPException,Request,UploadFile
from fastapi.responses import RedirectResponse,Response
from fastapi.routing import APIRoute
from app.db import get_db
from app.models import TaskAttachment
from app.operations.parity_router import same_origin
from app.operations.router import require_admin
from app.operations.task_attachments import MAX_FILE,save_attachment,archive_attachment
from app.operations.tasks import TaskError

class BoundedUploadRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def bounded(request):
            if request.method!='POST':return await handler(request)
            received=0
            async def receive():
                nonlocal received
                message=await request.receive()
                received+=len(message.get('body',b''))
                if received>MAX_FILE+65536:raise HTTPException(413,'Upload request exceeds the 5 MiB file limit')
                return message
            return await handler(Request(request.scope,receive))
        return bounded

router=APIRouter(route_class=BoundedUploadRoute)


@router.post('/operations/tasks/{task_id}/attachments')
def upload(task_id:int,request:Request,token:str=Form(...),note:str=Form(...),file:UploadFile=File(...),
           db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:
        data=file.file.read(MAX_FILE+1)
        save_attachment(db,actor=admin,task_id=task_id,token=token,filename=file.filename or '',data=data,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    finally:file.file.close()
    return RedirectResponse('/operations/tasks/'+str(task_id),status_code=303)


@router.post('/operations/tasks/{task_id}/attachments/{entry_id}/archive')
def archive(task_id:int,entry_id:int,request:Request,token:str=Form(...),action:str=Form(...),note:str=Form(...),
            db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    if action not in ('archive','restore'):raise HTTPException(422,'Choose archive or restore')
    try:archive_attachment(db,actor=admin,task_id=task_id,entry_id=entry_id,token=token,archived=action=='archive',note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/tasks/'+str(task_id),status_code=303)


@router.get('/operations/tasks/{task_id}/attachments/{entry_id}/download')
def download(task_id:int,entry_id:int,db=Depends(get_db),admin=Depends(require_admin)):
    row=db.get(TaskAttachment,entry_id)
    if not row or row.work_order_id!=task_id or row.archived:raise HTTPException(404,'Attachment unavailable')
    if len(row.data)!=row.size or hashlib.sha256(row.data).hexdigest()!=row.sha256:raise HTTPException(409,'Attachment integrity needs review')
    return Response(row.data,media_type=row.media_type,headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(row.filename,safe=''),
        'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':'sandbox', 'Referrer-Policy':'same-origin'})
