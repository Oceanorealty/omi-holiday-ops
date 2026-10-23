import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import CustomFieldValue,CustomFieldDefinition,WorkOrder
from app.operations.custom_field_search import search_page,export_values
from app.operations.custom_field_router import router
from app.operations.custom_fields import archive_definition
from app.operations.tasks import TaskError,form_token
from test_custom_fields import db,property,define,save
from test_operations import prop,booking


def test_search_literal_case_scoped_archived_and_clear(db,property):
    field=define(db);save(db,property,{field.id:'Literal 100%_A'})
    other=prop(db);save(db,other,{field.id:'Other record'})
    assert len(search_page(db,scope='property',needle='100%_A')['rows'])==1
    assert not search_page(db,scope='property',needle='100%_a')['rows']
    assert not search_page(db,scope='property',property_id=other.id,needle='100%')['rows']
    archive_definition(db,actor='fields-admin',field_id=field.id,token=form_token('fields-admin',field,purpose='custom-field-archive'),archived=True,note='Synthetic archived field search')
    assert not search_page(db,scope='property')['rows']
    assert len(search_page(db,scope='property',include_archived=True)['rows'])==2


def test_cross_scope_orphans_and_reservation_blocks_excluded(db,property):
    booking_field=define(db,scope='reservation');task_field=define(db,key='task_scope',scope='task')
    normal=booking(db,property);blocked=booking(db,property,is_block=True)
    for id in [normal.id,blocked.id,999999]:db.add(CustomFieldValue(field_id=booking_field.id,entity_id=id,value='Scoped value',version=1))
    db.add(CustomFieldValue(field_id=task_field.id,entity_id=normal.id,value='Not a reservation field',version=1));db.commit()
    rows=search_page(db,scope='reservation')['rows']
    assert len(rows)==1 and rows[0][2].id==normal.id
    with pytest.raises(TaskError):search_page(db,scope='reservation',field_id=task_field.id)


def test_export_same_filters_and_formula_protection(db,property):
    field=define(db);field.label='=SYNTHETIC_LABEL()';db.commit()
    save(db,property,{field.id:'\t=SUM(1,1)'})
    csv=export_values(db,scope='property',property_id=property.id)
    assert "'=SYNTHETIC_LABEL()" in csv and "'=SUM(1,1)" in csv
    assert '=SUM' not in export_values(db,scope='property',needle='missing')
    save(db,property,{field.id:''})
    assert not search_page(db,scope='property')['rows']


def test_pagination_and_export_limit(db,property,monkeypatch):
    field=define(db)
    # Two fields per record show limits count values rather than records.
    second=define(db,key='second')
    for i in range(26):
        p=prop(db,name=f'Synthetic page {i}')
        db.add_all([CustomFieldValue(field_id=f.id,entity_id=p.id,value=f'value {i}',version=1) for f in [field,second]])
    db.commit()
    first=search_page(db,scope='property');second_page=search_page(db,scope='property',page=2)
    assert len(first['rows'])==50 and first['has_next'] and not first['has_previous']
    assert len(second_page['rows'])==2 and not second_page['has_next'] and second_page['has_previous']
    monkeypatch.setattr('app.operations.custom_field_search.EXPORT_LIMIT',3)
    with pytest.raises(TaskError):export_values(db,scope='property')


@pytest.mark.parametrize('changes',[{'scope':'unknown'},{'page':0},{'page':1001},{'needle':'x'*201},{'property_id':999999},{'field_id':999999}])
def test_invalid_search_filters(db,property,changes):
    filters={'scope':'property'};filters.update(changes)
    with pytest.raises(TaskError):search_page(db,**filters)


def test_search_export_private_admin_escaped_and_filter_urls(db,property):
    field=define(db);save(db,property,{field.id:'<script>SYNTHETIC_PRIVATE</script>'})
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/custom-fields/search').status_code==403
        assert client.get('/operations/custom-fields/export').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('fields-admin'))
        page=client.get('/operations/custom-fields/search?needle=SYNTHETIC_PRIVATE')
        assert page.status_code==200 and '&lt;script&gt;SYNTHETIC_PRIVATE&lt;/script&gt;' in page.text
        assert '<script>SYNTHETIC_PRIVATE</script>' not in page.text
        assert 'needle=SYNTHETIC_PRIVATE' in page.text
        response=client.get('/operations/custom-fields/export?needle=SYNTHETIC_PRIVATE')
        assert response.status_code==200 and response.headers['Cache-Control']=='private, no-store'
        assert response.headers['Content-Disposition'].startswith('attachment;')
        assert client.get('/operations/custom-fields/search?scope=invalid').status_code==422
        assert client.get('/operations/custom-fields/export?field_id=999999').status_code==422
