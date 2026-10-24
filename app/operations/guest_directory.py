"""Administrator-only guest directory; identities are never inferred or merged."""
import csv,io
from urllib.parse import urlencode
from sqlalchemy import func,or_
from app.models import Booking,BookingStatus,Guest
from app.operations.custom_field_search import csv_cell

PAGE_SIZE=25
EXPORT_LIMIT=5000


def directory_query(db,q='',repeat=False):
    counts=db.query(Booking.guest_id.label('guest_id'),func.count(Booking.id).label('reservations')).filter(
        Booking.is_block.is_(False),Booking.status==BookingStatus.confirmed).group_by(Booking.guest_id).subquery()
    query=db.query(Guest,func.coalesce(counts.c.reservations,0).label('reservations')).outerjoin(counts,counts.c.guest_id==Guest.id)
    q=q.strip()[:120]
    if q:
        terms=[col.contains(q,autoescape=True) for col in (Guest.name,Guest.email,Guest.phone)]
        if q.isascii() and q.isdigit() and len(q)<=18:terms.append(Guest.id==int(q))
        query=query.filter(or_(*terms))
    if repeat:query=query.filter(counts.c.reservations>=2)
    return query.order_by(Guest.id.desc())


def directory_data(db,q='',repeat=False,page=1):
    q=q.strip()[:120];query=directory_query(db,q,repeat);total=query.count()
    pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE);page=min(max(1,page),pages)
    url=lambda p:'/operations/guests?'+urlencode({'q':q,'repeat':int(repeat),'page':p})
    return {'rows':query.offset((page-1)*PAGE_SIZE).limit(PAGE_SIZE).all(),'total':total,'pages':pages,'page':page,'q':q,'repeat':repeat,
        'previous':url(page-1) if page>1 else None,'next':url(page+1) if page<pages else None,
        'export_url':'/operations/guests/export.csv?'+urlencode({'q':q,'repeat':int(repeat)})}


def export_directory(db,q='',repeat=False):
    rows=directory_query(db,q,repeat).limit(EXPORT_LIMIT+1).all()
    if len(rows)>EXPORT_LIMIT:raise ValueError('Narrow the search to 5000 guests or fewer before exporting')
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['guest_id','name','email','phone','language','confirmed_reservations'])
    for guest,count in rows:writer.writerow([csv_cell('' if x is None else x) for x in [guest.id,guest.name,guest.email,guest.phone,guest.language,count]])
    return '\ufeff'+out.getvalue()
