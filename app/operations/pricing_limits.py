import hashlib
import json
from types import SimpleNamespace
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.models import PricingLimit, Property
from app.operations.reservations import money
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError, form_token, verify_token


def revision(row):
    values=[str(row.minimum_rate) if row and row.minimum_rate is not None else None,
            str(row.maximum_rate) if row and row.maximum_rate is not None else None]
    return int(hashlib.sha256(json.dumps(values).encode()).hexdigest()[:15],16)


def limits_token(db,actor,property_id):
    return form_token(actor,SimpleNamespace(id=property_id,version=revision(db.get(PricingLimit,property_id))),purpose='pricing-limits')


def save_limits(db,*,actor,property_id,token,minimum_rate,maximum_rate,note):
    expected=verify_token(token,actor,property_id,purpose='pricing-limits')
    lower,upper=money(minimum_rate),money(maximum_rate)
    if (lower is not None and lower<=0) or (upper is not None and upper<=0) or (lower is not None and upper is not None and lower>upper):
        raise TaskError('Use positive rate limits with maximum at least minimum')
    try:
        result=db.execute(update(Property).where(Property.id==property_id).values(name=Property.name))
        if result.rowcount!=1:raise TaskError('Property not found')
        db.expire_all();row=db.get(PricingLimit,property_id)
        if revision(row)!=expected:raise TaskError('Rate limits changed; reload before saving')
        before={'minimum_rate':str(row.minimum_rate) if row and row.minimum_rate is not None else None,
                'maximum_rate':str(row.maximum_rate) if row and row.maximum_rate is not None else None}
        if not row:row=PricingLimit(property_id=property_id);db.add(row)
        row.minimum_rate,row.maximum_rate=lower,upper
        record_command(db,token,'pricing-limits',actor,note,{'property_id':property_id,'before':before,
            'after':{'minimum_rate':str(lower) if lower is not None else None,'maximum_rate':str(upper) if upper is not None else None}})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback();raise
