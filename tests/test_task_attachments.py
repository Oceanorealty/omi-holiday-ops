import hashlib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import TaskAttachment,OpsCommand,WorkOrder
from app.operations.task_attachments import save_attachment,archive_attachment,MAX_FILE
from app.operations.task_attachment_router import router
from app.operations.tasks import TaskError,form_token
from test_task_time import db,work


def upload(db,work,**changes):
    task,staff=work
    args=dict(actor='time-admin',task_id=task.id,token=form_token('time-admin',task,purpose='task-attachment'),
        filename='synthetic-proof.txt',data=b'Synthetic task proof only',note='Record synthetic attachment evidence')
    args.update(changes);return save_attachment(db,**args)


def archive(db,work,entry_id,**changes):
    task,staff=work;args=dict(actor='time-admin',task_id=task.id,entry_id=entry_id,
        token=form_token('time-admin',task,purpose='task-attachment'),archived=True,note='Archive synthetic test evidence')
    args.update(changes);return archive_attachment(db,**args)


def test_bytes_hash_archive_and_restore_retain_history(db,work):
    entry=upload(db,work,filename='../../synthetic-proof.txt');row=db.get(TaskAttachment,entry)
    assert row.filename=='synthetic-proof.txt' and row.sha256==hashlib.sha256(row.data).hexdigest()
    original=row.data;archive(db,work,entry);assert db.get(TaskAttachment,entry).archived
    archive(db,work,entry,archived=False)
    assert db.get(TaskAttachment,entry).data==original and not db.get(TaskAttachment,entry).archived
    assert db.query(OpsCommand).count()==3


@pytest.mark.parametrize('filename,data',[('bad.html',b'<html>'),('bad.png',b'not-png'),('empty.txt',b''),('bad.txt',b'\xff'),('bad\nname.txt',b'ok'),('huge.txt',b'x'*(MAX_FILE+1))])
def test_invalid_uploads_rollback(db,work,filename,data):
    with pytest.raises(TaskError):upload(db,work,filename=filename,data=data)
    assert db.query(TaskAttachment).count()==0 and work[0].version==1


def test_stale_upload_and_wrong_task_cannot_change_file(db,work):
    task,staff=work;old=form_token('time-admin',task,purpose='task-attachment');entry=upload(db,work)
    with pytest.raises(TaskError):upload(db,work,token=old)
    other=WorkOrder(property_id=task.property_id,title='Other task');db.add(other);db.commit()
    with pytest.raises(TaskError):archive(db,(other,staff),entry)
    assert not db.get(TaskAttachment,entry).archived


def test_storage_quotas_count_archived_files(db,work,monkeypatch):
    from app.operations import task_attachments as files
    monkeypatch.setattr(files,'MAX_PER_TASK',1)
    entry=upload(db,work);archive(db,work,entry)
    with pytest.raises(TaskError):upload(db,work)
    monkeypatch.setattr(files,'MAX_PER_TASK',20);monkeypatch.setattr(files,'MAX_TOTAL',1)
    with pytest.raises(TaskError):upload(db,work)


def test_admin_download_headers_origin_and_tamper_checks(db,work):
    task,staff=work;entry=upload(db,work)
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    path=f'/operations/tasks/{task.id}/attachments/{entry}/download'
    with TestClient(app) as client:
        assert client.get(path).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('time-admin'))
        response=client.get(path);assert response.status_code==200 and response.content==b'Synthetic task proof only'
        assert response.headers['content-disposition'].startswith('attachment;') and response.headers['x-content-type-options']=='nosniff'
        data={'token':form_token('time-admin',task,purpose='task-attachment'),'note':'Synthetic route attachment upload'}
        response=client.post(f'/operations/tasks/{task.id}/attachments',data=data,files={'file':('proof.txt',b'Synthetic route proof','text/plain')},headers={'Origin':'https://evil.invalid'})
        assert response.status_code==403
        response=client.post(f'/operations/tasks/{task.id}/attachments',data=data,files={'file':('proof.txt',b'Synthetic route proof','text/plain')},follow_redirects=False)
        assert response.status_code==303
        archive(db,work,entry)
        assert client.get(path).status_code==404
        archive(db,work,entry,archived=False)
        row=db.get(TaskAttachment,entry);row.data=b'tampered';db.commit()
        assert client.get(path).status_code==409


def test_chunked_request_limit_applies_before_multipart_parsing(db,work):
    task,staff=work
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE,make_session_cookie('time-admin'))
        body=b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="proof.txt"\r\nContent-Type: text/plain\r\n\r\n'+b'x'*(MAX_FILE+65537)+b'\r\n--boundary--\r\n'
        response=client.post(f'/operations/tasks/{task.id}/attachments',content=iter([body]),headers={'Content-Type':'multipart/form-data; boundary=boundary'})
        assert response.status_code==413 and db.query(TaskAttachment).count()==0
