import json
from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import CustomFieldDefinition,CustomFieldChange,OpsCommand
from app.operations.custom_fields import SCOPES,KINDS,create_definition,value_token,values_state,save_values,archive_definition
from app.operations.parity_router import same_origin
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError,form_token

router=APIRouter()


@router.get('/operations/custom-fields')
def definition_page(request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    rows=db.query(CustomFieldDefinition).order_by(CustomFieldDefinition.scope,CustomFieldDefinition.id).all()
    return templates.TemplateResponse(request=request,name='custom_fields.html',context={'fields':rows,'scopes':SCOPES,'kinds':KINDS,
        'token':form_token(admin,purpose='custom-field-create'),'tokens':{r.id:form_token(admin,r,purpose='custom-field-archive') for r in rows}})


@router.post('/operations/custom-fields')
def definition_save(request:Request,token:str=Form(...),scope:str=Form(...),key:str=Form(...),label:str=Form(...),kind:str=Form(...),options:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:create_definition(db,actor=admin,token=token,scope=scope,key=key,label=label,kind=kind,options=options,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/custom-fields',status_code=303)


@router.post('/operations/custom-fields/definitions/{field_id}/archive')
def definition_archive(field_id:int,request:Request,token:str=Form(...),action:str=Form(...),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    if action not in ('archive','restore'):raise HTTPException(422,'Choose archive or restore')
    try:archive_definition(db,actor=admin,field_id=field_id,token=token,archived=action=='archive',note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/custom-fields',status_code=303)


@router.get('/operations/custom-field-record')
def open_record(scope:str,entity_id:int,admin=Depends(require_admin)):
    if scope not in SCOPES or entity_id<1:raise HTTPException(422,'Choose a record type and positive ID')
    return RedirectResponse(f'/operations/custom-fields/{scope}/{entity_id}',status_code=303)


@router.get('/operations/custom-fields/{scope}/{entity_id}')
def value_page(scope:str,entity_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    try:defs,values,_=values_state(db,scope,entity_id)
    except TaskError as exc:raise HTTPException(404,str(exc)) from None
    commands=db.query(OpsCommand).join(CustomFieldChange,CustomFieldChange.command_id==OpsCommand.id).filter(
        CustomFieldChange.scope==scope,CustomFieldChange.entity_id==entity_id).order_by(OpsCommand.id.desc()).limit(51).all()
    return templates.TemplateResponse(request=request,name='custom_field_values.html',context={'scope':scope,'entity_id':entity_id,'fields':defs,'values':values,
        'options':{f.id:json.loads(f.options) for f in defs},'token':value_token(db,admin,scope,entity_id),
        'history':[{'command':c,'data':json.loads(c.payload)} for c in commands[:50]],'truncated':len(commands)>50})


@router.post('/operations/custom-fields/{scope}/{entity_id}')
async def value_save(scope:str,entity_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request);data=await request.form()
    try:
        values={int(key[6:]):str(value) for key,value in data.items() if key.startswith('field_')}
        save_values(db,actor=admin,scope=scope,entity_id=entity_id,token=str(data.get('token','')),values=values,note=str(data.get('note','')))
    except (TaskError,ValueError) as exc:raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Invalid field identifier') from None
    return RedirectResponse(f'/operations/custom-fields/{scope}/{entity_id}',status_code=303)
