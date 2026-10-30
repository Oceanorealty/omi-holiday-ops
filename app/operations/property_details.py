"""Audited core property metadata; never dispatches or publishes anything."""
import hashlib,json
from types import SimpleNamespace
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from app.models import Property,Cleaner,Booking,CleaningTask
from app.operations.tasks import TaskError,form_token,verify_token
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command

FIELDS=('name','address','time_zone','default_cleaner')


def state(prop):
    return {k:getattr(prop,k) for k in FIELDS}


def digest(prop):
    return hashlib.sha256(json.dumps(state(prop),sort_keys=True).encode()).hexdigest()


def details_token(actor,prop):
    return form_token(actor,SimpleNamespace(id=prop.id,version=0),purpose='property-details:'+digest(prop))


def validate_zone(value):
    try:
        value=value.strip()
        if not 1<=len(value)<=64:raise ValueError()
        ZoneInfo(value)
        return value
    except (ValueError,TypeError,AttributeError,ZoneInfoNotFoundError):
        raise TaskError('Choose a valid IANA timezone, such as Australia/Brisbane') from None


def save_details(db,*,actor,property_id,token_value,name,address,time_zone,default_cleaner,note):
    name=name.strip();address=address.strip();default_cleaner=default_cleaner.strip()
    if not 1<=len(name)<=160 or len(address)>500 or len(default_cleaner)>160:
        raise TaskError('Use a property name of 1–160 characters and an address up to 500 characters')
    zone=validate_zone(time_zone)
    try:
        lock_stock(db);db.expire_all();prop=db.get(Property,property_id)
        if not prop:raise TaskError('Property not found')
        before=state(prop)
        verify_token(token_value,actor,prop.id,purpose='property-details:'+digest(prop))
        if zone!=prop.time_zone and (db.query(Booking).filter_by(property_id=prop.id).first() or db.query(CleaningTask).filter_by(property_id=prop.id).first()):
            raise TaskError('Timezone changes after reservations or cleaning records exist require a separately reviewed migration')
        if default_cleaner and default_cleaner!=prop.default_cleaner and not db.query(Cleaner).filter_by(name=default_cleaner,active=True).first():
            raise TaskError('Choose an existing active cleaner or leave the default unset')
        prop.name=name;prop.address=address or None;prop.time_zone=zone;prop.default_cleaner=default_cleaner or None
        record_command(db,token_value,'property-details',actor,note,{'property_id':prop.id,'before':before,'after':state(prop)})
        db.commit()
        return prop.id
    except Exception:
        db.rollback();raise
