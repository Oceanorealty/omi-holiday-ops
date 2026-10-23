from datetime import date
from decimal import Decimal
import pytest
from app.models import InventoryDay, OpsCommand, PriceRule, PriceRuleType, PricingLimit
from app.operations.pricing_limits import limits_token, save_limits
from app.operations.tasks import TaskError
from app.pricing import suggested_price
from app.public_api import get_calendar, get_quote
from test_operations import db,prop


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-pricing-key-'+'x'*32)
    return 'pricing-admin'


def limits(db,actor,p,**changes):
    values=dict(actor=actor,property_id=p.id,token=limits_token(db,actor,p.id),minimum_rate='100',maximum_rate='250',note='Reviewed seasonal nightly rate guardrails')
    values.update(changes)
    save_limits(db,**values)


def test_limits_apply_after_rules_and_overrides_and_manual_mode_ignores_rules(db,actor):
    p=prop(db,base_nightly_rate=200,pricing_mode='dynamic')
    db.add(PriceRule(name='Seasonal increase',rule_type=PriceRuleType.day_of_week,days_of_week='0,1,2,3,4,5,6',multiplier=2,active=True));db.commit()
    limits(db,actor,p)
    assert suggested_price(db,p,date(2026,11,10))==250
    db.add(InventoryDay(property_id=p.id,night='2026-11-10',nightly_rate=50));db.commit()
    assert suggested_price(db,p,date(2026,11,10))==100
    p.pricing_mode='manual';db.commit()
    assert suggested_price(db,p,date(2026,11,11))==200


def test_calendar_and_quote_agree_on_guardrail_prices(db,actor):
    p=prop(db,base_nightly_rate=500,published=True)
    limits(db,actor,p)
    calendar=get_calendar(property_id=p.id,start='2026-11-10',end='2026-11-12',db=db)['result']
    quote=get_quote(property_id=p.id,check_in='2026-11-10',check_out='2026-11-12',guests=1,db=db)['result']
    assert all(day['price']=='250.00' for day in calendar['days']) and quote['total']=='500.00'


@pytest.mark.parametrize('changes',[dict(minimum_rate='0'),dict(maximum_rate='-1'),dict(minimum_rate='NaN'),dict(minimum_rate='300',maximum_rate='100'),dict(token='forged'),dict(note='short')])
def test_invalid_limits_are_atomic(db,actor,changes):
    p=prop(db)
    with pytest.raises(TaskError):limits(db,actor,p,**changes)
    assert db.query(PricingLimit).count()==db.query(OpsCommand).count()==0


def test_stale_and_repeated_submissions_cannot_overwrite_limits(db,actor):
    p=prop(db);old=limits_token(db,actor,p.id)
    limits(db,actor,p,token=old)
    with pytest.raises(TaskError):limits(db,actor,p,token=old,minimum_rate='120')
    assert db.get(PricingLimit,p.id).minimum_rate==100
    limits(db,actor,p,minimum_rate='',maximum_rate='')
    assert db.get(PricingLimit,p.id).minimum_rate is None
