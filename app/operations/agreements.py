"""Versioned operator text and immutable per-stay acknowledgement evidence."""
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
from app.auth import _secret
from app.models import (AgreementAcceptance, AgreementPolicy, AgreementVersion, AgreementWithdrawal,
    Booking, BookingAgreement, BookingStatus, Property)
from app.operations.guest_checkin import find_guest_booking, guest_actor
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError, form_token, verify_token


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def booking_fingerprint(booking):
    return digest(json.dumps([booking.property_id,booking.check_in.isoformat(),booking.check_out.isoformat(),
        booking.status.value,str(booking.amount) if booking.amount is not None else None,
        booking.guest.name if booking.guest else None,booking.guest.email if booking.guest else None,
        booking.property.time_zone],separators=(',',':')))


def policy_token(db,actor,property_id):
    policy=db.get(AgreementPolicy,property_id)
    return form_token(actor,SimpleNamespace(id=property_id,version=policy.revision if policy else 0),purpose='agreement-policy')


def create_version(db, *, actor, property_id, token, title, body, note):
    title,body=title.strip(),body.strip()
    if not 1<=len(title)<=160 or not 20<=len(body)<=20000:
        raise TaskError('Use a title of 1–160 characters and agreement text of 20–20000 characters')
    expected=verify_token(token,actor,property_id,purpose='agreement-policy')
    try:
        lock_stock(db);db.expire_all()
        if not db.get(Property,property_id):raise TaskError('Property not found')
        policy=db.get(AgreementPolicy,property_id)
        if expected!=(policy.revision if policy else 0):raise TaskError('Agreement policy changed; reload before saving')
        if not policy:policy=AgreementPolicy(property_id=property_id,revision=0);db.add(policy)
        row=AgreementVersion(property_id=property_id,revision=expected+1,title=title,body=body,actor=actor)
        db.add(row);db.flush();policy.revision=expected+1;policy.active_version_id=row.id
        record_command(db,token,'agreement-policy',actor,note,{'property_id':property_id,'version_id':row.id,'revision':row.revision,'text_hash':digest(body)})
        db.commit();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('This agreement policy was already saved; reload the page') from None
    except Exception:
        db.rollback();raise


def latest_agreement(db,booking_id):
    return db.query(BookingAgreement).filter_by(booking_id=booking_id).order_by(BookingAgreement.id.desc()).first()


def issue_purpose(db,booking):
    policy=db.get(AgreementPolicy,booking.property_id)
    latest=latest_agreement(db,booking.id)
    return 'agreement-issue:'+digest(json.dumps([booking_fingerprint(booking),
        policy.active_version_id if policy else None,latest.id if latest else 0]))


def issue_token(db,actor,booking):
    return form_token(actor,SimpleNamespace(id=booking.id,version=0),purpose=issue_purpose(db,booking))


def issue_agreement(db, *, actor, booking_id, token, note):
    try:
        lock_stock(db);db.expire_all()
        booking=db.get(Booking,booking_id)
        if not booking or booking.is_block or booking.status!=BookingStatus.confirmed:
            raise TaskError('Choose a confirmed guest reservation')
        verify_token(token,actor,booking.id,purpose=issue_purpose(db,booking))
        policy=db.get(AgreementPolicy,booking.property_id)
        version=db.get(AgreementVersion,policy.active_version_id) if policy else None
        if not version or version.property_id!=booking.property_id:raise TaskError('Create an agreement version for this property first')
        previous=latest_agreement(db,booking.id)
        fingerprint=booking_fingerprint(booking)
        if previous and previous.version_id==version.id and previous.booking_fingerprint==fingerprint and not db.query(AgreementWithdrawal).filter_by(agreement_id=previous.id).first():
            raise TaskError('This version is already issued for the current reservation details')
        snapshot=json.dumps({'title':version.title,'body':version.body,'revision':version.revision,
            'property_name':booking.property.name,'booking_id':booking.id,
            'check_in':booking.check_in.isoformat(),'check_out':booking.check_out.isoformat(),
            'time_zone':booking.property.time_zone,'guest_name':booking.guest.name if booking.guest else '',
            'booking_amount':str(booking.amount) if booking.amount is not None else None,
            'currency_note':'AUD assumed for legacy booking amounts; amount is not a payment receipt.'},sort_keys=True,ensure_ascii=False)
        row=BookingAgreement(booking_id=booking.id,version_id=version.id,snapshot=snapshot,
            snapshot_hash=digest(snapshot),booking_fingerprint=fingerprint,actor=actor)
        db.add(row);db.flush()
        record_command(db,token,'agreement-issue',actor,note,{'agreement_id':row.id,'booking_id':booking.id,'snapshot_hash':row.snapshot_hash})
        db.commit();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('This agreement was already issued; reload the page') from None
    except Exception:
        db.rollback();raise


def document_state(db,row,booking):
    if digest(row.snapshot)!=row.snapshot_hash:return 'document integrity needs review'
    if db.query(AgreementWithdrawal).filter_by(agreement_id=row.id).first():return 'withdrawn'
    latest=latest_agreement(db,booking.id)
    if latest.id!=row.id:return 'superseded'
    if booking.status!=BookingStatus.confirmed:return 'reservation cancelled'
    if row.booking_fingerprint!=booking_fingerprint(booking):return 'reservation changed — reissue required'
    return 'acknowledged' if db.query(AgreementAcceptance).filter_by(agreement_id=row.id).first() else 'awaiting acknowledgement'


def acceptance_token(booking,row):
    return form_token(guest_actor(booking),SimpleNamespace(id=row.id,version=0),purpose='agreement-accept:'+row.snapshot_hash)


def accept_agreement(db, *, portal_token, agreement_id, token, typed_name, acknowledged):
    if not _secret():raise TaskError('Secure portal configuration is required before acknowledgement')
    name=typed_name.strip()
    if not acknowledged or not 1<=len(name)<=160 or any(ord(c)<32 for c in name):
        raise TaskError('Enter your name and confirm that you have read and agree to the displayed document')
    try:
        lock_stock(db);db.expire_all()
        booking=find_guest_booking(db,portal_token)
        row=db.get(BookingAgreement,agreement_id)
        if not booking or not row or row.booking_id!=booking.id:raise LookupError('Agreement not available')
        actor=guest_actor(booking)
        verify_token(token,actor,row.id,purpose='agreement-accept:'+row.snapshot_hash)
        if document_state(db,row,booking) not in ('awaiting acknowledgement','acknowledged'):
            raise TaskError('Agreement or reservation changed; ask the team for an updated document')
        if datetime.now(ZoneInfo(booking.property.time_zone)).date()>booking.check_out.date():
            raise TaskError('This stay has ended; contact the team about agreement records')
        prior=db.query(AgreementAcceptance).filter_by(agreement_id=row.id).first()
        token_digest=digest(token)
        if prior:
            if prior.token_digest==token_digest and prior.typed_name==name and prior.actor==actor:
                result=prior.id;db.rollback();return result
            raise TaskError('This agreement already has an acknowledgement; it cannot be changed')
        acceptance=AgreementAcceptance(agreement_id=row.id,typed_name=name,actor=actor,token_digest=token_digest,snapshot_hash=row.snapshot_hash)
        db.add(acceptance);db.commit();return acceptance.id
    except IntegrityError:
        db.rollback();raise TaskError('This agreement was already acknowledged; reload the page') from None
    except Exception:
        db.rollback();raise


def withdraw_agreement(db, *, actor, agreement_id, token, note):
    verify_token(token,actor,agreement_id,purpose='agreement-withdraw')
    try:
        lock_stock(db);db.expire_all()
        row=db.get(BookingAgreement,agreement_id)
        if not row:raise TaskError('Agreement not found')
        if db.query(AgreementWithdrawal).filter_by(agreement_id=row.id).first():raise TaskError('Agreement already withdrawn')
        record_command(db,token,'agreement-withdraw',actor,note,{'agreement_id':row.id,'snapshot_hash':row.snapshot_hash})
        db.add(AgreementWithdrawal(agreement_id=row.id,actor=actor,note=note.strip()))
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Agreement already withdrawn') from None
    except Exception:
        db.rollback();raise


def guest_agreement_context(db,booking):
    row=latest_agreement(db,booking.id) if booking else None
    if not row:return {}
    state=document_state(db,row,booking)
    history=db.query(BookingAgreement).join(AgreementAcceptance,AgreementAcceptance.agreement_id==BookingAgreement.id).filter(
        BookingAgreement.booking_id==booking.id,BookingAgreement.id!=row.id).order_by(BookingAgreement.id.desc()).limit(20).all()
    return {'stay_agreement':row,'stay_agreement_state':state,
        'stay_agreement_history':history,
        'stay_agreement_data':json.loads(row.snapshot),
        'stay_agreement_acceptance':db.query(AgreementAcceptance).filter_by(agreement_id=row.id).first(),
        'stay_agreement_token':acceptance_token(booking,row) if _secret() and state=='awaiting acknowledgement'
            and datetime.now(ZoneInfo(booking.property.time_zone)).date()<=booking.check_out.date() else ''}


def download_text(row,acceptance=None,withdrawal=None):
    data=json.loads(row.snapshot)
    parts=[data['title'],f"Document #{row.id} · text revision {data['revision']}",data['property_name'],
        f"Reservation #{data['booking_id']} · {data['check_in']} → {data['check_out']} ({data['time_zone']})",
        'Reserved guest: '+(data['guest_name'] or 'Not recorded'),
        'Booking amount: '+(data['booking_amount'] or 'Not recorded'),data['currency_note'],'',data['body'],'',
        'Snapshot SHA-256 (UTF-8 canonical JSON below): '+row.snapshot_hash,'Issued at: '+row.created_at.isoformat()+' UTC']
    if acceptance:parts += ['Acknowledged name: '+acceptance.typed_name,'Acknowledged at: '+acceptance.accepted_at.isoformat()+' UTC',
        'Acknowledged document SHA-256: '+acceptance.snapshot_hash]
    if withdrawal:parts += ['Withdrawn at: '+withdrawal.created_at.isoformat()+' UTC']
    parts += ['Portal acknowledgement records a typed name and time; identity was not independently verified.']
    parts += ['', 'Canonical snapshot JSON (hash this exact single line without its trailing newline):',row.snapshot]
    return '\n'.join(parts)+'\n'
