from datetime import date,datetime
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import CalendarNote,CalendarNoteChange,OpsCommand
from app.operations.calendar_notes import notes_token,save_notes
from app.operations.calendar import calendar_data
from app.operations.calendar_note_router import router
from app.operations.router import router as calendar_router
from app.public_api import router as public_router
from app.operations.tasks import TaskError
from test_operations import db,prop,booking


@pytest.fixture
def property(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-notes-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','notes-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    return prop(db,name='Synthetic notes listing',published=True)


def save(db,property,**changes):
    args=dict(actor='notes-admin',property_id=property.id,start='2026-11-10',end='2026-11-13',
        token=notes_token(db,'notes-admin',property.id,'2026-11-10','2026-11-13'),action='replace',
        body='Synthetic operational reminder',note='Record synthetic calendar reminder')
    args.update(changes);return save_notes(db,**args)


def test_bulk_notes_clear_retains_audit_and_does_not_affect_stock(db,property):
    b=booking(db,property,check_in=datetime(2026,11,10),check_out=datetime(2026,11,12))
    before=calendar_data(db,date(2026,11,10),date(2026,11,14))['rows'][0]
    assert save(db,property)==3
    after=calendar_data(db,date(2026,11,10),date(2026,11,14))['rows'][0]
    assert [r['available_units'] for r in before['days']]==[r['available_units'] for r in after['days']]
    assert [r['internal_note'] for r in after['days']]==['Synthetic operational reminder']*3+['']
    assert after['days'][0]['note_end']=='2026-11-11'
    assert save(db,property,action='clear')==3
    assert all(r.body=='' and r.version==2 for r in db.query(CalendarNote).all())
    command=db.query(OpsCommand).order_by(OpsCommand.id.desc()).first();changes=json.loads(command.payload)['changes']
    assert changes[0]['before']['body']=='Synthetic operational reminder' and changes[0]['after']['body']==''
    assert db.query(CalendarNoteChange).count()==2
    assert b.status.value=='confirmed'


def test_overlapping_note_updates_reject_stale_form_and_changed_range(db,property):
    stale=notes_token(db,'notes-admin',property.id,'2026-11-10','2026-11-13')
    save(db,property)
    with pytest.raises(TaskError):save(db,property,token=stale,body='Must not overwrite')
    with pytest.raises(TaskError):save(db,property,token=stale,start='2026-11-11')
    assert db.query(CalendarNoteChange).count()==1


@pytest.mark.parametrize('changes',[{'start':'bad'},{'end':'2026-11-10'},{'end':'2027-01-20'},{'action':'delete'},{'body':''},{'body':'x'*1001},{'note':'short'}])
def test_note_invalid_input_has_no_partial_writes(db,property,changes):
    with pytest.raises(TaskError):save(db,property,**changes)
    assert db.query(CalendarNote).count()==db.query(CalendarNoteChange).count()==db.query(OpsCommand).count()==0


def test_notes_are_admin_only_escaped_and_absent_from_public_api(db,property):
    save(db,property,body='<script>INTERNAL_ONLY_739</script>')
    app=FastAPI();app.include_router(router);app.include_router(calendar_router);app.include_router(public_router)
    app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/calendar-notes').status_code==403
        assert client.get('/api/ops/calendar').status_code==403
        for path in [f'/api/public/listings/{property.id}',f'/api/public/listings/{property.id}/calendar?start=2026-11-10&end=2026-11-13']:
            response=client.get(path);assert response.status_code==200 and 'INTERNAL_ONLY_739' not in response.text
        client.cookies.set(SESSION_COOKIE,make_session_cookie('notes-admin'))
        response=client.get('/operations/calendar?start=2026-11-10&end=2026-11-13')
        assert response.status_code==200 and '&lt;script&gt;INTERNAL_ONLY_739&lt;/script&gt;' in response.text
        page=client.get(f'/operations/calendar-notes?property_id={property.id}&start=2026-11-10&end=2026-11-13')
        assert page.status_code==200 and 'Change history' in page.text and 'INTERNAL_ONLY_739' in page.text
        assert '<script>INTERNAL_ONLY_739</script>' not in page.text
        token=notes_token(db,'notes-admin',property.id,'2026-11-10','2026-11-13')
        assert client.post(f'/operations/calendar-notes/{property.id}',data={'start':'2026-11-10','end':'2026-11-13','token':token,'action':'clear','note':'Synthetic calendar clear'},headers={'Origin':'https://evil.invalid'}).status_code==403
        other=prop(db,name='Other synthetic property')
        isolated=client.get(f'/operations/calendar-notes?property_id={other.id}&start=2026-11-10&end=2026-11-13')
        assert 'INTERNAL_ONLY_739' not in isolated.text
