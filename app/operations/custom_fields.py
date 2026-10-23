"""Administrator-only typed metadata. No values flow to public or guest APIs."""
import hashlib,json,re
from datetime import date
from decimal import Decimal,InvalidOperation
from types import SimpleNamespace
from sqlalchemy.exc import IntegrityError
from app.models import (Property,Booking,WorkOrder,CustomFieldDefinition,CustomFieldValue,CustomFieldChange,OpsCommand)
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token

SCOPES={'property':Property,'reservation':Booking,'task':WorkOrder}
KINDS=('text','number','date','boolean','choice')


def target(db,scope,entity_id):
    if scope not in SCOPES:raise TaskError('Unknown record type')
    row=db.get(SCOPES[scope],entity_id)
    if not row or (scope=='reservation' and row.is_block):raise TaskError('Record not found')
    return row


def create_definition(db,*,actor,token,scope,key,label,kind,options,note):
    verify_token(token,actor,0,purpose='custom-field-create')
    key,label=key.strip(),label.strip()
    if scope not in SCOPES or kind not in KINDS or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',key) or not 1<=len(label)<=100:
        raise TaskError('Choose a scope/type, lowercase key and label up to 100 characters')
    choices=[v.strip() for v in options.splitlines() if v.strip()]
    if kind=='choice' and (not 1<=len(choices)<=50 or any(len(v)>100 for v in choices) or len(set(choices))!=len(choices)):
        raise TaskError('Use 1–50 unique choices, up to 100 characters each')
    if kind!='choice' and choices:raise TaskError('Only choice fields accept options')
    try:
        lock_stock(db)
        if db.query(CustomFieldDefinition).filter_by(scope=scope).count()>=50:raise TaskError('This record type supports 50 definitions including archived fields')
        row=CustomFieldDefinition(scope=scope,key=key,label=label,kind=kind,options=json.dumps(choices));db.add(row);db.flush()
        record_command(db,token,'custom-field-create',actor,note,{'field_id':row.id,'scope':scope,'key':key,'label':label,'kind':kind,'options':choices})
        db.commit();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('Field key or submission already exists') from None
    except Exception:
        db.rollback();raise


def definitions(db,scope):
    return db.query(CustomFieldDefinition).filter_by(scope=scope).order_by(CustomFieldDefinition.id).all()


def values_state(db,scope,entity_id):
    target(db,scope,entity_id);defs=definitions(db,scope)
    vals={v.field_id:v for v in db.query(CustomFieldValue).filter(CustomFieldValue.field_id.in_([f.id for f in defs]),CustomFieldValue.entity_id==entity_id).all()}
    digest=hashlib.sha256(json.dumps([[f.id,f.version,f.archived,vals[f.id].version if f.id in vals else 0,vals[f.id].value if f.id in vals else None] for f in defs],sort_keys=True).encode()).hexdigest()
    return defs,vals,'custom-values:'+scope+':'+digest


def value_token(db,actor,scope,entity_id):
    return form_token(actor,SimpleNamespace(id=entity_id,version=0),purpose=values_state(db,scope,entity_id)[2])


def parse_value(field,raw):
    raw=raw.strip()
    if not raw:return None
    try:
        if field.kind=='text':
            if len(raw)>4000:raise ValueError()
            return raw
        if field.kind=='number':
            value=Decimal(raw)
            if not value.is_finite() or abs(value)>Decimal('1000000000000') or value!=value.quantize(Decimal('.000001')):raise ValueError()
            return str(value)
        if field.kind=='date':
            if date.fromisoformat(raw).isoformat()!=raw:raise ValueError()
            return raw
        if field.kind=='boolean' and raw in ('true','false'):return raw
        if field.kind=='choice' and raw in json.loads(field.options):return raw
    except (ValueError,InvalidOperation):pass
    raise TaskError('Invalid value for '+field.label)


def save_values(db,*,actor,scope,entity_id,token,values,note):
    try:
        lock_stock(db);db.expire_all();defs,existing,purpose=values_state(db,scope,entity_id)
        verify_token(token,actor,entity_id,purpose=purpose)
        active={f.id:f for f in defs if not f.archived}
        if set(values)!=set(active):raise TaskError('Fields changed or incomplete form; reload before saving')
        parsed={id:parse_value(field,values[id]) for id,field in active.items()};changes=[]
        for id,value in parsed.items():
            row=existing.get(id);before=row.value if row else None
            if before==value:continue
            if not row:row=CustomFieldValue(field_id=id,entity_id=entity_id,version=0);db.add(row)
            row.value=value;row.version+=1
            changes.append({'field_id':id,'label':active[id].label,'kind':active[id].kind,'before':before,'after':value,'version':row.version})
        record_command(db,token,'custom-field-values',actor,note,{'scope':scope,'entity_id':entity_id,'changes':changes})
        db.flush();command=db.query(OpsCommand).filter_by(token_digest=hashlib.sha256(token.encode()).hexdigest()).one()
        db.add(CustomFieldChange(scope=scope,entity_id=entity_id,command_id=command.id));db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This field submission was already recorded') from None
    except Exception:
        db.rollback();raise


def archive_definition(db,*,actor,field_id,token,archived,note):
    expected=verify_token(token,actor,field_id,purpose='custom-field-archive')
    try:
        lock_stock(db);db.expire_all();row=db.get(CustomFieldDefinition,field_id)
        if not row or row.version!=expected or row.archived==archived:raise TaskError('Field changed; reload before saving')
        row.archived=archived;row.version+=1
        record_command(db,token,'custom-field-archive',actor,note,{'field_id':row.id,'archived':archived,'version':row.version})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This field submission was already recorded') from None
    except Exception:
        db.rollback();raise
