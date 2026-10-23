import json
from fastapi import APIRouter,Depends,Form,HTTPException,Request,UploadFile,File
from fastapi.responses import RedirectResponse,Response
from app.db import get_db
from app.models import CustomFieldDefinition,CustomFieldChange,OpsCommand,Property
from app.operations.custom_fields import SCOPES,KINDS,create_definition,value_token,values_state,save_values,archive_definition
from app.operations.parity_router import same_origin,optional_property
from app.operations.custom_field_search import search_page,export_values,query_string
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError,form_token

from app.operations.custom_field_import import preview_import,apply_import,MAX_CSV
from app.operations.task_attachment_router import BoundedUploadRoute
router=APIRouter(route_class=BoundedUploadRoute)


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




@router.get('/operations/custom-fields/search')
def field_search(request:Request,scope:str='property',field_id:str='',property_id:str='',needle:str='',include_archived:bool=False,page:int=1,db=Depends(get_db),admin=Depends(require_admin)):
    filters=dict(scope=scope,field_id=optional_property(field_id),property_id=optional_property(property_id),needle=needle,include_archived=include_archived)
    try:result=search_page(db,page=page,**filters)
    except TaskError as exc:raise HTTPException(422,str(exc)) from None
    return templates.TemplateResponse(request=request,name='custom_field_search.html',context={**result,**filters,'scopes':SCOPES,
        'fields':db.query(CustomFieldDefinition).filter_by(scope=scope).order_by(CustomFieldDefinition.id).all(),
        'properties':db.query(Property).order_by(Property.name).all(),'export_query':query_string(filters),
        'previous_query':query_string(filters,page=page-1),'next_query':query_string(filters,page=page+1)},headers={'Cache-Control':'private, no-store'})


@router.get('/operations/custom-fields/export')
def field_export(scope:str='property',field_id:str='',property_id:str='',needle:str='',include_archived:bool=False,db=Depends(get_db),admin=Depends(require_admin)):
    try:body=export_values(db,scope=scope,field_id=optional_property(field_id),property_id=optional_property(property_id),needle=needle,include_archived=include_archived)
    except TaskError as exc:raise HTTPException(422,str(exc)) from None
    return Response('\ufeff'+body,media_type='text/csv',headers={'Cache-Control':'private, no-store','Content-Disposition':'attachment; filename="custom-field-values.csv"','X-Content-Type-Options':'nosniff'})




@router.get('/operations/custom-fields/import')
def import_page(request:Request,admin=Depends(require_admin)):
    return templates.TemplateResponse(request=request,name='custom_field_import.html',context={'preview':None},headers={'Cache-Control':'private, no-store'})


@router.post('/operations/custom-fields/import/preview')
async def import_preview(request:Request,file:UploadFile=File(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:
        content=await file.read(MAX_CSV+1)
        data=preview_import(db,admin,content)
    except TaskError as exc:raise HTTPException(422,str(exc)) from None
    finally:await file.close()
    return templates.TemplateResponse(request=request,name='custom_field_import.html',context=data,headers={'Cache-Control':'private, no-store'})


@router.post('/operations/custom-fields/import/apply')
def import_apply(request:Request,plan:str=Form(...),token:str=Form(...),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:apply_import(db,actor=admin,plan=plan,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/custom-fields/search',status_code=303)


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
