"""Administrator-only source intake for the normal-module trial, not a demo UI."""
import os
from fastapi import APIRouter,Depends,Form,HTTPException,Request,UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from app.db import get_db
from app.operations.router import require_admin
from app.operations.tasks import form_token,verify_token,TaskError
from app import internal_module_trial as trial
from app.operations.internal_trial import TrialImportError

router=APIRouter(prefix='/operations/module-trial',dependencies=[Depends(require_admin)])
templates=Jinja2Templates(directory='app/templates')


def active_only():
    if not trial.ACTIVE:raise HTTPException(404,'Not available in this environment')


@router.get('/setup')
def setup(request:Request,actor=Depends(require_admin),db=Depends(get_db)):
    active_only()
    from app.models import Property,Booking
    return templates.TemplateResponse(request=request,name='internal_module_trial_setup.html',context={
        'property_count':db.query(Property).count(),'booking_count':db.query(Booking).count(),
        'token':form_token(actor,purpose='module-trial-import')},headers={'Cache-Control':'private, no-store','X-Robots-Tag':'noindex, nofollow'})


@router.post('/sources')
async def sources(request:Request,file:UploadFile,token:str=Form(...),actor=Depends(require_admin),db=Depends(get_db)):
    active_only()
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'Submit from this site')
    try:
        verify_token(token,actor,0,purpose='module-trial-import')
        content=await file.read(512*1024+1)
        if len(content)>512*1024:raise ValueError('Use at most 512 KiB')
        trial.import_source_properties(db.get_bind(),content)
    except (ValueError,TaskError,TrialImportError) as exc:
        raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/properties',status_code=303)


@router.get('/enter')
def enter(actor=Depends(require_admin)):
    if trial.ACTIVE:return RedirectResponse('/properties',status_code=303)
    # No SSO/cookie sharing or user supplied redirect targets.
    target=os.environ.get('OPS_MODULE_TRIAL_URL','')
    if target!='https://omi-holiday-ops-internal-test.onrender.com':raise HTTPException(404,'Trial entry not configured')
    return RedirectResponse(target+'/properties',status_code=303)
