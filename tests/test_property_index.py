import pytest
from app.operations.property_index import index_data
from app.operations.tasks import TaskError
from app.operations.router import templates
from starlette.requests import Request
from app.models import PlatformName
from test_operations import db,prop


def test_property_filters_units_and_literal_search(db):
    parent=prop(db,name='Parent',published=True);unit=prop(db,name='Unit 50%_',parent_property_id=parent.id)
    for index in range(26):prop(db,name=f'Synthetic {index:02d}')
    data=index_data(db);assert data['property_total']==28 and len(data['properties'])==25 and data['property_next_url']
    assert len(index_data(db,page=2)['properties'])==3
    assert index_data(db,kind='units')['properties']==[unit]
    assert index_data(db,publication='published')['properties']==[parent]
    assert index_data(db,q='50%_')['properties']==[unit]
    assert index_data(db,property_id=unit.id)['properties']==[unit]
    for changes in [{'kind':'bad'},{'publication':'bad'},{'page':0},{'q':'x'*121}]:
        with pytest.raises(TaskError):index_data(db,**changes)


def test_property_settings_keep_parent_choices_and_one_expanded_record(db):
    parent=prop(db,name='Synthetic parent');unit=prop(db,name='Synthetic unit',parent_property_id=parent.id)
    data=index_data(db,property_id=unit.id)
    request=Request({'type':'http','path':'/properties','headers':[],'scheme':'http','server':('test',80),'query_string':b''})
    html=templates.env.get_template('properties.html').render(request=request,**data,all_properties=[parent,unit],platforms=list(PlatformName))
    assert f'id="property-{unit.id}" open' in html and f'id="property-{parent.id}"' not in html
    assert f'Unit under: {parent.name}' in html
    assert f'/operations/properties/{unit.id}/photos' in html
