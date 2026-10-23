"""Bounded administrator searches of internal typed metadata."""
import csv
import io
from urllib.parse import urlencode
from sqlalchemy import func
from app.models import Booking,CustomFieldDefinition,CustomFieldValue,Property
from app.operations.custom_fields import SCOPES
from app.operations.tasks import TaskError

PAGE_SIZE=50
EXPORT_LIMIT=5000


def search_query(db,*,scope,field_id=None,property_id=None,needle='',include_archived=False):
    if scope not in SCOPES:raise TaskError('Choose property, reservation or task')
    needle=needle.strip()
    if len(needle)>200:raise TaskError('Search text must be at most 200 characters')
    entity=SCOPES[scope]
    query=db.query(CustomFieldValue,CustomFieldDefinition,entity).join(
        CustomFieldDefinition,CustomFieldDefinition.id==CustomFieldValue.field_id).join(
        entity,entity.id==CustomFieldValue.entity_id).filter(
        CustomFieldDefinition.scope==scope,CustomFieldValue.value.is_not(None))
    if scope=='reservation':query=query.filter(Booking.is_block.is_(False))
    if not include_archived:query=query.filter(CustomFieldDefinition.archived.is_(False))
    if field_id is not None:
        field=db.get(CustomFieldDefinition,field_id)
        if not field or field.scope!=scope:raise TaskError('Field does not belong to this record type')
        query=query.filter(CustomFieldDefinition.id==field_id)
    if property_id is not None:
        if not db.get(Property,property_id):raise TaskError('Property not found')
        query=query.filter((entity.id if scope=='property' else entity.property_id)==property_id)
    if needle:
        position=func.strpos if db.bind.dialect.name=='postgresql' else func.instr
        query=query.filter(position(CustomFieldValue.value,needle)>0)
    return query.order_by(CustomFieldValue.entity_id,CustomFieldDefinition.id)


def search_page(db,*,page=1,**filters):
    if not 1<=page<=1000:raise TaskError('Use a page number from 1 to 1000; narrow filters for older results')
    rows=search_query(db,**filters).offset((page-1)*PAGE_SIZE).limit(PAGE_SIZE+1).all()
    return {'rows':rows[:PAGE_SIZE],'page':page,'has_next':len(rows)>PAGE_SIZE,'has_previous':page>1}


def csv_cell(value):
    value=str(value)
    return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value


def export_values(db,**filters):
    rows=search_query(db,**filters).limit(EXPORT_LIMIT+1).all()
    if len(rows)>EXPORT_LIMIT:raise TaskError('Narrow the search before exporting; maximum 5000 saved values')
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['record_type','record_id','property_id','field_key','field_label','field_type','archived','value','revision'])
    for value,field,entity in rows:
        writer.writerow([csv_cell(v) for v in [field.scope,entity.id,entity.id if field.scope=='property' else entity.property_id,field.key,field.label,field.kind,field.archived,value.value,value.version]])
    return stream.getvalue()


def query_string(filters,**extra):
    return urlencode({**{k:(1 if v is True else v) for k,v in filters.items() if v is not None and v is not False},**extra})
