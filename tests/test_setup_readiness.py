"""Synthetic readiness checks; no private data, external connections or sends."""
from types import SimpleNamespace
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Cleaner, PropertyPhoto, Booking, CleaningTask, MessageLog, OpsCommand
from app.operations.parity_router import router, property_readiness
from app.operations.router import require_admin
from test_operations import db, prop


def complete(**changes):
    values = dict(address='Synthetic unit reference', description='Synthetic description',
        bedrooms=0, bathrooms=1, max_guests=2, photo_url='https://images.example.com/synthetic.jpg',
        time_zone='Australia/Brisbane', commission_pct=Decimal('0'), base_nightly_rate=Decimal('100'),
        check_in_instructions='Synthetic arrival instructions', house_rules='Synthetic rules',
        wifi_info='Synthetic private Wi-Fi value', default_cleaner='Synthetic cleaner')
    values.update(changes)
    return SimpleNamespace(**values)


def test_recorded_information_does_not_reject_studio_or_zero_commission():
    row = property_readiness(complete(), 2, {'Synthetic cleaner'})
    assert row['recorded'] == row['total'] == 14
    assert row['missing'] == [] and row['guest_info_ready'] and row['commission_ready']


@pytest.mark.parametrize('value', [None, '', '  ', '-1', '101', 'NaN', 'Infinity', 'invalid'])
def test_missing_or_invalid_commission_never_counts_as_configured(value):
    row = property_readiness(complete(commission_pct=value), 1, {'Synthetic cleaner'})
    assert not row['commission_ready']
    assert 'Commission from owner agreement' in row['missing']


def test_whitespace_instructions_and_invalid_timezone_are_explicit_gaps():
    row = property_readiness(complete(address=' ', check_in_instructions=' ', wifi_info='\n',
        house_rules='\t', bedrooms=None, time_zone='Not/A_Zone', base_nightly_rate=0), 0, set())
    for field in ['Address / unit reference', 'Bedroom count', 'Property timezone',
        'Arrival instructions', 'House rules', 'Wi-Fi information', 'Active photo gallery',
        'Active default cleaner', 'Base nightly rate for direct quotes']:
        assert field in row['missing']
    assert not row['guest_info_ready'] and not row['zone_valid']


def test_setup_is_admin_only_actionable_read_only_and_does_not_expose_private_values(db):
    p = prop(db, name='<Synthetic source>')
    p.check_in_instructions='Private synthetic arrival value'
    p.wifi_info='Private synthetic Wi-Fi value'
    p.house_rules='Synthetic rules'
    db.add(Cleaner(name='Synthetic cleaner', active=True))
    for active in [True, True, False]:
        db.add(PropertyPhoto(property_id=p.id, url='https://images.example.com/synthetic.jpg',
            caption='Synthetic', alt_text='Synthetic', position=0, active=active, actor='synthetic-admin'))
    db.commit()
    app=FastAPI(); app.include_router(router); app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as c:
        assert c.get('/operations/setup').status_code == 403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        response=c.get('/operations/setup')
    assert response.status_code == 200
    body=response.text
    assert f'/properties?property_id={p.id}' in body
    assert f'/operations/properties/{p.id}/photos' in body and '2 active photos' in body
    assert 'Not connected — live availability unknown' in body
    assert 'fields to complete' in body and 'Address / unit reference' in body
    assert '&lt;Synthetic source&gt;' in body and '<Synthetic source>' not in body
    assert p.check_in_instructions not in body and p.wifi_info not in body
    assert all(db.query(model).count()==0 for model in [Booking,CleaningTask,MessageLog,OpsCommand])
