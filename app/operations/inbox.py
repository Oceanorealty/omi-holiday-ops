"""Persistent portal conversations. Posting is not SMTP or OTA delivery."""
import hashlib
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
from app.models import Booking, BookingStatus, PortalConversation, PortalMessage, PortalReadCursor, StaffUser
from app.operations.guest_checkin import find_guest_booking, guest_actor
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError, form_token, verify_token


def message_token(actor, booking):
    return form_token(actor, SimpleNamespace(id=booking.id, version=0), purpose='portal-message')


def link_purpose(booking):
    return 'portal-link:'+hashlib.sha256((booking.guest_portal_token or '').encode()).hexdigest()


def issue_link(db, *, actor, booking_id, token, note):
    try:
        lock_stock(db);db.expire_all()
        booking=db.get(Booking,booking_id)
        if not booking or booking.is_block or booking.status!=BookingStatus.confirmed:
            raise TaskError('Choose a confirmed guest reservation')
        verify_token(token,actor,booking_id,purpose=link_purpose(booking))
        replaced=bool(booking.guest_portal_token)
        booking.guest_portal_token=secrets.token_urlsafe(32)
        record_command(db,token,'portal-link',actor,note,{'booking_id':booking_id,'replaced_prior_link':replaced})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This link request was already used') from None
    except Exception:
        db.rollback();raise


def visible_messages(db, conversation, guest=False, before=None):
    if not conversation:return []
    query=db.query(PortalMessage).filter_by(conversation_id=conversation.id)
    if guest:query=query.filter(PortalMessage.kind!='internal')
    if before is not None:query=query.filter(PortalMessage.id<before)
    return list(reversed(query.order_by(PortalMessage.id.desc()).limit(100).all()))


def can_guest_post(booking):
    return bool(booking and datetime.now(ZoneInfo(booking.property.time_zone)).date()
        <=booking.check_out.date()+timedelta(days=30))


def post_message(db, *, actor, booking_id, token, kind, body, portal_token=None):
    if kind not in ('guest','reply','internal') or not 1<=len(body.strip())<=4000:
        raise TaskError('Use a message of 1–4000 characters')
    try:
        # Shared inventory lock also serializes cancellation/source updates with
        # guest authorization and per-conversation append/read operations.
        lock_stock(db);db.expire_all()
        booking=db.get(Booking,booking_id)
        if kind=='guest':
            booking=find_guest_booking(db,portal_token)
            if not booking or booking.id!=booking_id or actor!=guest_actor(booking) or not can_guest_post(booking):
                raise TaskError('This stay is no longer available for portal messages')
        elif not booking or booking.is_block:
            raise TaskError('Reservation not found')
        elif kind=='reply' and booking.status!=BookingStatus.confirmed:
            raise TaskError('Cancelled reservations cannot receive portal replies')
        verify_token(token,actor,booking_id,purpose='portal-message')
        digest=hashlib.sha256(token.encode()).hexdigest()
        prior=db.query(PortalMessage).filter_by(token_digest=digest).first()
        conversation=db.query(PortalConversation).filter_by(booking_id=booking_id).first()
        if prior:
            if not conversation or prior.conversation_id!=conversation.id or prior.kind!=kind or prior.body!=body.strip() or prior.actor!=actor:
                raise TaskError('This submission was already used for another message')
            db.rollback();return prior.id
        if not conversation:
            conversation=PortalConversation(booking_id=booking_id);db.add(conversation);db.flush()
        count=db.query(PortalMessage).filter_by(conversation_id=conversation.id)
        if count.count()>=5000:raise TaskError('Conversation message limit reached; contact the team directly')
        if kind=='guest' and count.filter(PortalMessage.kind=='guest',
                PortalMessage.created_at>=datetime.utcnow()-timedelta(minutes=5)).count()>=5:
            raise TaskError('Please wait a few minutes before sending more messages')
        row=PortalMessage(conversation_id=conversation.id,kind=kind,actor=actor,body=body.strip(),token_digest=digest)
        db.add(row)
        # A new guest question reopens the conversation. Replies/notes preserve
        # its triage state until an administrator explicitly changes it.
        if kind=='guest':conversation.state='open'
        conversation.version+=1;conversation.updated_at=datetime.utcnow()
        db.commit();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('Message submission changed; reload the conversation') from None
    except Exception:
        db.rollback();raise


def manage_conversation(db, *, actor, booking_id, token, state, assignee_id, note):
    if state not in ('open','waiting','resolved'):raise TaskError('Unknown conversation state')
    expected=verify_token(token,actor,booking_id,purpose='portal-manage')
    try:
        lock_stock(db);db.expire_all()
        row=db.query(PortalConversation).filter_by(booking_id=booking_id).first()
        if not row or row.version!=expected:raise TaskError('Conversation changed; reload before saving')
        if assignee_id is not None:
            user=db.get(StaffUser,assignee_id)
            if not user or not user.active:raise TaskError('Choose an active staff member')
        before={'state':row.state,'assignee_id':row.assignee_id}
        row.state=state;row.assignee_id=assignee_id;row.version+=1;row.updated_at=datetime.utcnow()
        record_command(db,token,'portal-manage',actor,note,{'booking_id':booking_id,'before':before,
            'after':{'state':state,'assignee_id':assignee_id},'version':row.version})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This change was already submitted') from None
    except Exception:
        db.rollback();raise


def read_token(actor, booking_id, last_id):
    return form_token(actor,SimpleNamespace(id=booking_id,version=last_id),purpose='portal-read')


def mark_read(db, *, actor, booking_id, token, guest=False):
    last_id=verify_token(token,actor,booking_id,purpose='portal-read')
    try:
        lock_stock(db);db.expire_all()
        row=db.query(PortalConversation).filter_by(booking_id=booking_id).first()
        message=db.get(PortalMessage,last_id)
        if not row or not message or message.conversation_id!=row.id or (guest and message.kind=='internal'):
            raise TaskError('Message not available')
        cursor=db.query(PortalReadCursor).filter_by(conversation_id=row.id,actor=actor).first()
        if not cursor:cursor=PortalReadCursor(conversation_id=row.id,actor=actor,last_id=0);db.add(cursor)
        cursor.last_id=max(cursor.last_id,last_id)
        db.commit()
    except Exception:
        db.rollback();raise


def unread_count(db, conversation, actor, guest=False):
    cursor=db.query(PortalReadCursor).filter_by(conversation_id=conversation.id,actor=actor).first()
    query=db.query(PortalMessage).filter(PortalMessage.conversation_id==conversation.id,
        PortalMessage.id>(cursor.last_id if cursor else 0))
    return query.filter(PortalMessage.kind=='reply' if guest else PortalMessage.kind=='guest').count()
