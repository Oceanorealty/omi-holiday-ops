"""Bounded property settings list; child units remain individually reachable."""
from urllib.parse import urlencode
from sqlalchemy import or_
from app.models import Property
from app.operations.tasks import TaskError


def index_data(db,*,q='',kind='all',publication='all',property_id=None,page=1):
    q=q.strip()
    if len(q)>120 or kind not in ('all','standalone','units') or publication not in ('all','published','unpublished'):
        raise TaskError('Choose valid property filters')
    if page<1 or page>1000000 or property_id is not None and property_id<1:raise TaskError('Invalid property or page')
    query=db.query(Property)
    if property_id is not None:query=query.filter(Property.id==property_id)
    if q:
        match=or_(Property.name.contains(q,autoescape=True),Property.address.contains(q,autoescape=True),Property.suburb.contains(q,autoescape=True))
        if q.isascii() and q.isdigit() and len(q)<=18:match=or_(match,Property.id==int(q))
        query=query.filter(match)
    if kind=='standalone':query=query.filter(Property.parent_property_id.is_(None))
    elif kind=='units':query=query.filter(Property.parent_property_id.isnot(None))
    if publication!='all':query=query.filter(Property.published.is_(publication=='published'))
    total=query.count();pages=max(1,(total+24)//25);page=min(page,pages)
    rows=query.order_by(Property.name,Property.id).offset((page-1)*25).limit(25).all()
    filters={'q':q,'kind':kind,'publication':publication}
    if property_id is not None:filters['property_id']=property_id
    url=lambda number:'/properties?'+urlencode({**filters,'page':number})
    return {'properties':rows,'q':q,'kind':kind,'publication':publication,'selected_property_id':property_id,
        'property_page':page,'property_pages':pages,'property_total':total,
        'property_previous_url':url(page-1) if page>1 else None,'property_next_url':url(page+1) if page<pages else None}
