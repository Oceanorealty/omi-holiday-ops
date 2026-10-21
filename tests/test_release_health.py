from sqlalchemy import create_engine
from app.operations.health import database_health
from app.deployment import ensure_release_snapshot


def test_health_checks_database_and_hides_failure_details():
    engine=create_engine('sqlite:///:memory:')
    assert database_health(engine).status_code==200
    class Broken:
        def begin(self):raise RuntimeError('private connection detail')
    response=database_health(Broken())
    assert response.status_code==503 and b'private' not in response.body
    assert response.headers['Cache-Control']=='no-store'
    assert ensure_release_snapshot(engine)['state']=='not_postgresql'
    engine.dispose()
