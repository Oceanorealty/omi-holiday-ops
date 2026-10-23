"""Versioned guest declarations and drafts; no identity authenticity claim."""
import csv,hashlib,io,json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
from app.models import RegistrationConfig,GuestRegistration,GuestRegistrationEvent,GuestCheckin,Property,Booking
from app.operations.guest_checkin import find_guest_booking,booking_fingerprint,guest_actor
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token


def configuration(db,property_id):
    row=db.get(RegistrationConfig,property_id)
    data={'version':row.version if row else 0,'require_adults':bool(row and row.require_adults),'questions':json.loads(row.questions) if row else []}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    return data,digest


def config_token(db,actor,property_id):
    row=db.get(RegistrationConfig,property_id)
    return form_token(actor,SimpleNamespace(id=property_id,version=row.version if row else 0),purpose='registration-config')


def configure(db,*,actor,property_id,token,require_adults,questions,note):
    try:
        lock_stock(db);db.expire_all()
        if not db.get(Property,property_id):raise TaskError('Property not found')
        expected=verify_token(token,actor,property_id,purpose='registration-config')
        before,_=configuration(db,property_id)
        if expected!=before['version']:raise TaskError('Registration requirements changed; reload first')
        lines=[s.strip() for s in questions.splitlines() if s.strip()]
        if len(lines)>20 or any(len(s)>240 for s in lines) or len(set(lines))!=len(lines):raise TaskError('Use up to 20 unique questions, each up to 240 characters')
        items=[]
        for line in lines:
            required=line.startswith('*');label=line[1:].strip() if required else line
            if not label:raise TaskError('Enter a question after the required marker')
            key=hashlib.sha256(label.encode()).hexdigest()[:20]
            if any(q['key']==key for q in items):raise TaskError('Remove duplicate questions')
            items.append({'key':key,'label':label,'required':required})
        row=db.get(RegistrationConfig,property_id)
        if not row:row=RegistrationConfig(property_id=property_id);db.add(row)
        row.version=expected+1;row.require_adults=require_adults;row.questions=json.dumps(items)
        record_command(db,token,'registration-config',actor,note,{'property_id':property_id,'before':before,'after':{'version':row.version,'require_adults':require_adults,'questions':items}})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Requirements already saved; reload first') from None
    except Exception:db.rollback();raise


def registration_current(db,booking):
    config,digest=configuration(db,booking.property_id)
    if not config['require_adults'] and not any(q['required'] for q in config['questions']):return True
    row=db.get(GuestRegistration,booking.id)
    basic=db.query(GuestCheckin).filter_by(booking_id=booking.id).first()
    return bool(row and row.state=='submitted' and row.booking_hash==booking_fingerprint(booking) and row.config_hash==digest and (basic is None or json.loads(row.payload)['party_size']==basic.party_size))


def registration_token(db,booking):
    row=db.get(GuestRegistration,booking.id);_,digest=configuration(db,booking.property_id)
    return form_token(guest_actor(booking),SimpleNamespace(id=booking.id,version=row.version if row else 0),purpose='registration:'+booking_fingerprint(booking)+':'+digest)


def context(db,booking):
    config,digest=configuration(db,booking.property_id);row=db.get(GuestRegistration,booking.id)
    current=bool(row and row.booking_hash==booking_fingerprint(booking) and row.config_hash==digest)
    return {'booking':booking,'config':config,'registration':row,'data':json.loads(row.payload) if current else {},'stale':bool(row and not current),'token':registration_token(db,booking)}


def save_registration(db,*,portal_token,token,action,adult_names,children,party_size,answers,confirmed):
    try:
        lock_stock(db);db.expire_all();booking=find_guest_booking(db,portal_token)
        if not booking:raise LookupError('Stay link not available')
        if datetime.now(ZoneInfo(booking.property.time_zone)).date()>booking.check_out.date():raise TaskError('This stay can no longer be updated')
        config,digest=configuration(db,booking.property_id)
        expected=verify_token(token,guest_actor(booking),booking.id,purpose='registration:'+booking_fingerprint(booking)+':'+digest)
        row=db.get(GuestRegistration,booking.id)
        if expected!=(row.version if row else 0):raise TaskError('Registration changed; reload before saving')
        if action not in ('draft','submit'):raise TaskError('Choose draft or submit')
        names=[s.strip() for s in adult_names.splitlines() if s.strip()]
        maximum=min(100,booking.property.max_guests if booking.property.max_guests is not None else 100)
        if not 1<=party_size<=maximum or not 0<=children<=party_size or len(names)>party_size or any(len(s)>160 for s in names):raise TaskError('Check guest counts and adult names (up to 160 characters each)')
        if len({s.casefold() for s in names})!=len(names):raise TaskError('Check repeated adult names')
        keys={q['key'] for q in config['questions']}
        if set(answers)-keys or any(len(v)>2000 for v in answers.values()):raise TaskError('Questions changed or answer exceeds 2000 characters')
        answers={q['key']:answers.get(q['key'],'').strip() for q in config['questions']}
        if action=='submit':
            if not confirmed:raise TaskError('Confirm the information before submitting')
            if config['require_adults'] and (not names or len(names)+children!=party_size):raise TaskError('List every adult and the number of children to match the total guests')
            if any(q['required'] and not answers[q['key']] for q in config['questions']):raise TaskError('Answer all required questions')
        data={'adult_names':names,'children':children,'party_size':party_size,'answers':answers,'questions':config['questions'],'require_adults':config['require_adults']}
        if not row:row=GuestRegistration(booking_id=booking.id);db.add(row)
        row.version=expected+1;row.state='submitted' if action=='submit' else 'draft';row.payload=json.dumps(data)
        row.booking_hash=booking_fingerprint(booking);row.config_hash=digest;row.updated_at=datetime.utcnow()
        db.add(GuestRegistrationEvent(booking_id=booking.id,token_digest=hashlib.sha256(token.encode()).hexdigest(),payload=json.dumps({'version':row.version,'state':row.state,'booking_hash':row.booking_hash,'config_hash':digest,'data':data})))
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Registration already saved; reload first') from None
    except Exception:db.rollback();raise


def csv_cell(value):
    value=str(value)
    return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value


def registration_export(db,property_id,start,end):
    from app.operations.task_plans import iso_date
    start,end=iso_date(start),iso_date(end)
    if not 0<(end-start).days<=366:raise TaskError('Choose a range of 1–366 days; end date excluded')
    query=db.query(GuestRegistration,Booking).join(Booking,Booking.id==GuestRegistration.booking_id).filter(GuestRegistration.state=='submitted',Booking.check_in>=datetime.combine(start,datetime.min.time()),Booking.check_in<datetime.combine(end,datetime.min.time()))
    if property_id is not None:query=query.filter(Booking.property_id==property_id)
    rows=query.order_by(Booking.id).limit(5001).all()
    if len(rows)>5000:raise TaskError('Narrow the date range or property filter (maximum 5000 records)')
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['booking_id','property_id','arrival_date','departure_date','status','registration_current','version','submitted_utc','party_size','children','adult_names','questions_and_answers'])
    for row,b in rows:
        data=json.loads(row.payload);questions='\n'.join(q['label']+': '+data['answers'].get(q['key'],'') for q in data['questions'])
        # Explicit fingerprint/config checks also mark optional submissions stale.
        current=row.booking_hash==booking_fingerprint(b) and row.config_hash==configuration(db,b.property_id)[1]
        writer.writerow([csv_cell(v) for v in [b.id,b.property_id,b.check_in.date(),b.check_out.date(),b.status.value,current,row.version,row.updated_at,data['party_size'],data['children'],'\n'.join(data['adult_names']),questions]])
    return stream.getvalue()
