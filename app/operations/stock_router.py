from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import Property,StockBinding,StockResource
from app.operations.parity_router import same_origin
from app.operations.router import require_admin,templates
from app.operations.stock import config_token,configure_stock
from app.operations.tasks import TaskError

router=APIRouter()


@router.get('/operations/resources')
def resources(request: Request,db=Depends(get_db),admin=Depends(require_admin)):
    return templates.TemplateResponse(request=request,name='resources.html',context={
        'properties':db.query(Property).order_by(Property.name).all(),
        'resources':db.query(StockResource).order_by(StockResource.name).all(),
        'bindings':db.query(StockBinding).order_by(StockBinding.property_id,StockBinding.resource_id).all(),
        'token':config_token(db,admin)})


@router.post('/operations/resources')
def resource_save(request: Request,token: str=Form(...),action: str=Form(...),name: str=Form(''),capacity: int=Form(1),
                  time_zone: str=Form('UTC'),property_id: str=Form(''),resource_id: str=Form(''),quantity: int=Form(1),
                  note: str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:
        configure_stock(db,actor=admin,token=token,action=action,name=name,capacity=capacity,time_zone=time_zone,
            property_id=int(property_id) if property_id else None,resource_id=int(resource_id) if resource_id else None,
            quantity=quantity,note=note)
    except ValueError as exc:raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Invalid property or resource') from None
    return RedirectResponse('/operations/resources',status_code=303)
