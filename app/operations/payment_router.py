"""Read-only payment readiness and sandbox reconciliation; never charges a card."""
import os
from datetime import datetime,timedelta
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from sqlalchemy import and_, or_
from app.db import get_db
from app.models import PublicPayment,PublicPaymentAccount,PublicCheckoutHold
from app.operations.payment_ledger import link_state
from app.operations.router import require_admin,templates

router=APIRouter()
STATES=('creating','awaiting_payment','confirmed','refund_pending','refund_failed','refunded','cancelled')


@router.get('/operations/payments')
def payments_page(request:Request,state:str='',review:bool=False,page:int=Query(1,ge=1,le=1000000),db=Depends(get_db),admin=Depends(require_admin)):
    if state not in ('',*STATES):raise HTTPException(422,'Unknown payment state')
    old_attempt=datetime.utcnow()-timedelta(hours=23)
    # Persistent attempts are not proof of a charge or an expired provider intent.
    attention=or_(PublicPayment.state.in_(['refund_pending','refund_failed']),
        and_(PublicPayment.state.in_(['creating','awaiting_payment']),PublicPayment.failure_code=='cancellation_unconfirmed'),
        and_(PublicPayment.state.in_(['creating','awaiting_payment']),db.query(PublicCheckoutHold).filter(
            PublicCheckoutHold.payment_id==PublicPayment.id,PublicCheckoutHold.expires_at<=datetime.utcnow()).exists()),
        and_(PublicPayment.state.in_(['creating','awaiting_payment']),PublicPayment.created_at<old_attempt),
        and_(PublicPayment.state=='confirmed',~db.query(PublicPaymentAccount).filter(
            PublicPaymentAccount.payment_id==PublicPayment.id).exists()))
    review_total=db.query(PublicPayment).filter(attention).count()
    query=db.query(PublicPayment)
    if review:query=query.filter(attention)
    if state:query=query.filter(PublicPayment.state==state)
    total=query.count();pages=max(1,(total+49)//50);page=min(page,pages)
    rows=query.order_by(PublicPayment.created_at.desc(),PublicPayment.id).offset((page-1)*50).limit(50).all()
    checks=[('Sandbox deployment selected',os.environ.get('OPS_PAYMENT_ENVIRONMENT')=='sandbox'),
        ('Sandbox checkout enabled',os.environ.get('PUBLIC_BOOKING_ENABLED')=='test'),
        ('Test payment key configured',os.environ.get('STRIPE_SECRET_KEY','').startswith(('sk_test_','rk_test_'))),
        ('Webhook signing secret configured',bool(os.environ.get('STRIPE_WEBHOOK_SECRET'))),
        ('Background scheduler disabled',os.environ.get('OPS_SCHEDULER_ENABLED')=='0'),
        ('Guest delivery disabled',os.environ.get('OPS_MESSAGE_DISPATCH_ENABLED','0')=='0'),
        ('Owner delivery disabled',os.environ.get('OPS_OWNER_EMAIL_ENABLED','0')=='0')]
    from app.auth import _secret
    checks.append(('Quote signing configured',len(_secret())>=32))
    smtp_ready=all(os.environ.get(name) for name in ('SMTP_HOST','SMTP_USER','SMTP_PASSWORD'))
    return templates.TemplateResponse(request=request,name='payments.html',context={'rows':rows,'state':state,'states':STATES,
        'holds':{r.id:db.get(PublicCheckoutHold,r.id) for r in rows},
        'account_links':{r.id:db.get(PublicPaymentAccount,r.id) for r in rows},
        'account_states':{r.id:link_state(db,db.get(PublicPaymentAccount,r.id)) for r in rows},
        'page':page,'pages':pages,'total':total,'review':review,'review_total':review_total,'checks':checks,'sandbox_ready':all(ok for _,ok in checks),
        'review_now':datetime.utcnow(),'old_attempt':old_attempt,'smtp_ready':smtp_ready},headers={'Cache-Control':'no-store'})
