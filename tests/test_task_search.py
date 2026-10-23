from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import WorkOrder
from app.operations.router import require_admin,router as workbench_router
from app.operations.task_router import router
from test_operations import db,prop


def test_task_search_pagination_filters_and_literal_wildcards(db,monkeypatch):
    monkeypatch.setenv("SECRET_KEY","synthetic-task-search-"+32*"x")
    p=prop(db)
    for i in range(53):
        db.add(WorkOrder(property_id=p.id,title=f'Synthetic repair {i}',description='literal 50%_ match' if i==0 else 'ordinary',
            category='maintenance',priority='high' if i==0 else 'normal',status='open',due_date='2000-01-01'))
    db.commit()
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic'
    with TestClient(app) as client:
        first=client.get('/operations/tasks');assert first.status_code==200
        assert '53 matching tasks' in first.text and 'Page 1 of 2' in first.text
        second=client.get('/operations/tasks?page=2');assert 'Page 2 of 2' in second.text
        assert 'Synthetic repair 0' in second.text and 'Synthetic repair 52' not in second.text
        result=client.get('/operations/tasks?q=50%25_&priority=high&assignee=unassigned&due=overdue')
        assert result.status_code==200 and '1 matching tasks' in result.text
        assert 'Synthetic repair 0' in result.text and 'Synthetic repair 1</a>' not in result.text
        assert '0 matching tasks' in client.get('/operations/tasks?q=missing').text
        for query in ['page=0','priority=invalid','assignee=-1','due=invalid','q='+'x'*121]:
            assert client.get('/operations/tasks?'+query).status_code==422


def test_tool_directory_auth_search_and_escaping(db):
    app=FastAPI();app.include_router(workbench_router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/tools').status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic'
        response=client.get('/operations/tools?q=cleaning');assert response.status_code==200
        assert 'Turnover schedules' in response.text and 'Edit reusable guest messages' not in response.text
        response=client.get('/operations/tools?q=<script>alert(1)</script>')
        assert '<script>alert(1)</script>' not in response.text and 'No tools match' in response.text
        assert 'hostaway' not in response.text.lower()
