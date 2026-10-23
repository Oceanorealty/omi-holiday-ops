"""Direct-channel readiness and explicit, read-only room discovery."""
import os
from datetime import datetime
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from app.db import get_db
from app.integrations.booking_com import ChannelError, configured_client
from app.models import IntegrationRun
from app.operations.parity_router import same_origin
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError, form_token, verify_token

router = APIRouter()


def channel_context(db, actor, **extra):
    approved = os.environ.get('BOOKING_CONNECTIVITY_APPROVED') == '1'
    credentials = bool(os.environ.get('BOOKING_CONNECTIVITY_CLIENT_ID') and os.environ.get('BOOKING_CONNECTIVITY_CLIENT_SECRET'))
    data = {'booking_approval_recorded': approved, 'booking_credentials_present': credentials,
        'booking_test_available': approved and credentials,
        'token': form_token(actor, purpose='booking-room-test'),
        'channel_runs': db.query(IntegrationRun).filter_by(provider='booking_com').order_by(IntegrationRun.id.desc()).limit(20).all()}
    data.update(extra)
    return data


@router.get('/operations/channels')
def channels(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    return templates.TemplateResponse(request=request, name='channels.html', context=channel_context(db, admin))


@router.post('/operations/channels/booking-com/rooms')
def test_booking_rooms(request: Request, token: str = Form(...), hotel_id: str = Form(...),
                       db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        verify_token(token, admin, 0, purpose='booking-room-test')
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    context = channel_context(db, admin)
    if not context['booking_test_available']:
        raise HTTPException(409, 'Record Booking.com Connectivity approval and configure its machine-account credentials first')
    try:
        client = configured_client(os.environ['BOOKING_CONNECTIVITY_CLIENT_ID'], os.environ['BOOKING_CONNECTIVITY_CLIENT_SECRET'])
        rooms = client.rooms(hotel_id)
        error = None
    except ChannelError as exc:
        rooms, error = [], exc.code
    # Only allow-listed result codes and counts are recorded, never remote response bodies.
    db.add(IntegrationRun(provider='booking_com', account='direct-connectivity', state='failed' if error else 'rooms_read_verified',
        record_count=len(rooms), error_code=error, observed_at=datetime.utcnow()))
    db.commit()
    context = channel_context(db, admin, tested=True, rooms=rooms, connection_error=error)
    return templates.TemplateResponse(request=request, name='channels.html', context=context, status_code=502 if error else 200)
