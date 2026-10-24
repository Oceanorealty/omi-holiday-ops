from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import Property,OpsCommand
from app.operations.router import require_admin,templates
from app.operations.parity_router import same_origin
from app.operations.tasks import TaskError
from app.operations.property_photos import rows,token,save
import json
router=APIRouter()


@router.get('/operations/properties/{property_id}/photos')
def photos_page(property_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    prop=db.get(Property,property_id)
    if not prop:raise HTTPException(404,'Property not found')
    history=[]
    for row in db.query(OpsCommand).filter_by(purpose='property-photos').order_by(OpsCommand.id.desc()).limit(1000).all():
        data=json.loads(row.payload)
        if data.get('property_id')==property_id:history.append({'row':row,'data':data})
        if len(history)==20:break
    return templates.TemplateResponse(request=request,name='property_photos.html',context={'property':prop,'photos':rows(db,prop.id),
        'token':token(db,admin,prop),'history':history},headers={'Cache-Control':'no-store'})


@router.post('/operations/properties/{property_id}/photos')
def save_photo(property_id:int,request:Request,token_value:str=Form(...),action:str=Form(...),photo_id:int|None=Form(None),
               url:str=Form(''),caption:str=Form(''),alt_text:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:save(db,actor=admin,property_id=property_id,token_value=token_value,action=action,photo_id=photo_id,url=url,caption=caption,alt_text=alt_text,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/properties/{property_id}/photos',status_code=303)
