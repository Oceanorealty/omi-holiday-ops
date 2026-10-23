"""Preview-bound, atomic CSV updates to existing internal records and fields."""
import csv
import hashlib
import io
import json
from app.operations.custom_fields import SCOPES,definitions,parse_value,target,value_token,values_state,save_values
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token

MAX_CSV=512*1024
MAX_ROWS=100
REQUIRED={'record_type','record_id','field_key','value'}
ALLOWED=REQUIRED|{'property_id','field_label','field_type','archived','revision'}


def import_purpose(plan):return 'custom-import:'+hashlib.sha256(plan.encode()).hexdigest()


def preview_import(db,actor,content):
    if len(content)>MAX_CSV:raise TaskError('CSV must be at most 512 KiB')
    try:text=content.decode('utf-8-sig')
    except UnicodeDecodeError:raise TaskError('Use UTF-8 CSV') from None
    try:
        reader=csv.DictReader(io.StringIO(text,newline=''))
        header=reader.fieldnames or []
        if not REQUIRED.issubset(header) or set(header)-ALLOWED or len(header)!=len(set(header)):raise TaskError('Use the CSV export columns; record_type, record_id, field_key and value are required')
        records={};seen=set();scope=None;preview=[];count=0
        for item in reader:
            count+=1
            if count>MAX_ROWS:raise TaskError('Import up to 100 field values per batch')
            if None in item or any(v is None for v in item.values()):raise TaskError('CSV row has missing or extra columns')
            if item['record_type'] not in SCOPES:raise TaskError('Unknown record type')
            if scope and scope!=item['record_type']:raise TaskError('Import one record type per batch')
            scope=item['record_type']
            if not item['record_id'].isdigit() or not 0<int(item['record_id'])<=2147483647:raise TaskError('Use a positive whole record ID')
            id=int(item['record_id']);target(db,scope,id)
            field=next((f for f in definitions(db,scope) if f.key==item['field_key'] and not f.archived),None)
            if not field:raise TaskError('Field key is missing or archived: '+item['field_key'][:40])
            if (id,field.id) in seen:raise TaskError('Each record/field may appear once per batch')
            seen.add((id,field.id));canonical=parse_value(field,item['value'])
            _,current,_=values_state(db,scope,id);old=current.get(field.id)
            before=old.value if old else None
            records.setdefault(id,{'id':id,'token':value_token(db,actor,scope,id),'updates':{}})['updates'][str(field.id)]=canonical or ''
            preview.append({'id':id,'key':field.key,'label':field.label,'before':before,'after':canonical})
        if not count:raise TaskError('CSV has no values to import')
    except (csv.Error,ValueError) as exc:raise TaskError('Invalid CSV; check quoting and record IDs') from None
    plan=json.dumps({'scope':scope,'records':list(records.values())},sort_keys=True,ensure_ascii=False)
    if len(plan.encode())>1024*1024:raise TaskError('Reduce the CSV batch size')
    return {'plan':plan,'token':form_token(actor,purpose=import_purpose(plan)),'preview':preview,'scope':scope}


def apply_import(db,*,actor,plan,token,note):
    try:
        if len(plan.encode())>1024*1024:raise TaskError('Import plan exceeds its size limit')
        verify_token(token,actor,0,purpose=import_purpose(plan))
        data=json.loads(plan);scope=data['scope'];records=data['records']
        if scope not in SCOPES or not 1<=len(records)<=MAX_ROWS:raise TaskError('Invalid import plan')
        lock_stock(db);db.expire_all()
        for record in records:
            id=record['id'];defs,current,_=values_state(db,scope,id)
            values={f.id:(current[f.id].value or '' if f.id in current else '') for f in defs if not f.archived}
            updates={int(k):v for k,v in record['updates'].items()}
            if not set(updates).issubset(values):raise TaskError('Fields changed; preview the CSV again')
            values.update(updates)
            save_values(db,actor=actor,scope=scope,entity_id=id,token=record['token'],values=values,note=note,commit=False)
        record_command(db,token,'custom-import',actor,note,{'scope':scope,'records':[r['id'] for r in records],'plan_digest':hashlib.sha256(plan.encode()).hexdigest()})
        db.commit();return len(records)
    except (ValueError,TypeError,KeyError):
        db.rollback();raise TaskError('Invalid import plan; preview the CSV again') from None
    except Exception:db.rollback();raise
