"""Collection uploads go through the RAG folder pipeline: presigned PUT -> complete-upload ->
first stage WAITING + one outbox message for the consumers. Access is the collection's."""
import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from pymicroservices.app.models import OutboxEvent, UnstructuredData
from tests.api.conftest import SessionLocal, as_admin, as_user, matter_id

PDF = b"%PDF-1.4 fictional record"
STORE = None



@pytest.fixture(autouse=True)
def _store(file_store):
    global STORE
    STORE = file_store


def _start(api, collection_id: str, content: bytes = PDF, filename: str = "ED records.pdf"):
    return api.post(f"/api/collections/{collection_id}/uploads",
                    {"filename": filename, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})


def _upload(api, collection_id: str, content: bytes = PDF, filename: str = "ED records.pdf"):
    r = _start(api, collection_id, content, filename)
    assert r.status_code == 201, r.text
    doc, upload = r.json()["document"], r.json()["upload"]
    assert doc["status"] == "pending_upload" and upload["method"] == "PUT"
    # The browser PUTs the file straight to storage with the presigned URL and its headers.
    assert STORE.browser.put(upload["url"], content=content, headers=upload["headers"]).status_code == 200
    return doc


def _collection(api) -> str:
    return api.get(f"/api/matters/{matter_id('Morgan v. Westfield')}/collections").json()["items"][0]["id"]


def _consumers_finish(doc_id: str):
    """What the consumers repository does when every stage succeeds."""
    with SessionLocal() as db:
        db.execute(update(UnstructuredData).where(UnstructuredData.id == doc_id).values(
            standardization_status="COMPLETED", markdown_status="COMPLETED", image_status="INAPPLICABLE",
            metaclassifier_status="COMPLETED"))
        db.commit()


def test_upload_flow_end_to_end():
    priya = as_user("priya@morganlaw.test")  # Editor through PI Team A
    col = _collection(priya)
    doc = _upload(priya, col)
    assert priya.get(f"/api/collections/{col}/documents").json()["items"] == []  # in flight: not listed yet
    r = priya.post(f"/api/documents/{doc['id']}/complete")
    assert r.status_code == 200, r.text
    assert r.json()["document"]["status"] == "queued"
    with SessionLocal() as db:
        row = db.get(UnstructuredData, doc["id"])
        assert str(row.thread_id) == col and row.standardization_status == "WAITING"
        events = db.scalars(select(OutboxEvent)).all()
        assert [(e.event_type, e.payload) for e in events] == [("thread.standardization", {"unstructured_thread_id": col})]
    _consumers_finish(doc["id"])
    assert priya.get(f"/api/documents/{doc['id']}").json()["document"]["status"] == "ready"
    assert [d["id"] for d in priya.get(f"/api/collections/{col}/documents").json()["items"]] == [doc["id"]]
    url = priya.get(f"/api/documents/{doc['id']}/download").json()["url"]
    assert STORE.browser.get(url).content == PDF


def test_viewer_cannot_upload_and_type_rules_apply():
    ava = as_user("ava@morganlaw.test")
    col = _collection(ava)
    r = _start(ava, col, b"MZ binary", "x.exe")
    assert r.status_code == 415 and r.json()["code"] == "FILE_TYPE_NOT_ALLOWED"
    felix = as_user("felix@morganlaw.test")  # Firm Viewer: read-only everywhere
    assert _start(felix, col).status_code == 403


def test_tampered_upload_is_rejected_and_can_be_redone():
    ava = as_user("ava@morganlaw.test")
    col = _collection(ava)
    r = _start(ava, col)
    upload = r.json()["upload"]
    # Different bytes than declared: storage refuses the PUT (checksum bound to the presigned URL).
    assert STORE.browser.put(upload["url"], content=b"%PDF-1.4 other bytes!!!!", headers=upload["headers"]).status_code == 400
    assert ava.post(f"/api/documents/{r.json()['document']['id']}/complete").json()["code"] == "UPLOAD_MISSING"
    # Declared as a PDF but isn't one: the content check at complete-upload rejects it.
    fake = b"not a pdf at all, really"
    doc = _upload(ava, col, fake, "fake.pdf")
    r = ava.post(f"/api/documents/{doc['id']}/complete")
    assert r.status_code == 400 and r.json()["code"] == "UPLOAD_REJECTED"
    assert fake not in [body for body, _ in STORE.objects.values()]  # rejected content is not kept
    # The same file can simply be uploaded again; a completed one is a duplicate.
    doc = _upload(ava, col)
    assert ava.post(f"/api/documents/{doc['id']}/complete").json()["document"]["status"] == "queued"
    assert _start(ava, col).json()["code"] == "DUPLICATE_FILE"


def test_failed_stage_can_be_retried():
    priya = as_user("priya@morganlaw.test")
    col = _collection(priya)
    doc = _upload(priya, col)
    priya.post(f"/api/documents/{doc['id']}/complete")
    with SessionLocal() as db:
        db.execute(update(UnstructuredData).where(UnstructuredData.id == doc["id"]).values(
            standardization_status="FAILED", standardization_error="OCR crashed",
            standardization_updated_at=datetime.now(timezone.utc)))
        db.commit()
    failed = priya.get(f"/api/documents/{doc['id']}").json()["document"]
    assert failed["status"] == "failed" and failed["error"] == "OCR crashed"
    assert priya.post(f"/api/documents/{doc['id']}/retry").json()["document"]["status"] == "queued"
    with SessionLocal() as db:
        assert db.scalar(select(OutboxEvent.event_type).order_by(OutboxEvent.created_at.desc()).limit(1)) == \
            "thread.standardization"


def test_walled_user_cannot_reach_documents():
    priya = as_user("priya@morganlaw.test")
    col = _collection(priya)
    doc = _upload(priya, col)
    priya.post(f"/api/documents/{doc['id']}/complete")
    walled = as_user("ben@morganlaw.test")  # walled off Morgan: the document doesn't exist for him
    for path in (f"/api/documents/{doc['id']}", f"/api/documents/{doc['id']}/download"):
        assert walled.get(path).status_code == 404
    assert walled.post(f"/api/collections/{col}/search", {"query": "records"}).status_code == 404
    assert walled.get("/api/documents/not-a-uuid").status_code == 404


def test_delete_removes_file_and_object():
    priya = as_user("priya@morganlaw.test")
    col = _collection(priya)
    doc = _upload(priya, col)
    priya.post(f"/api/documents/{doc['id']}/complete")
    admin = as_admin()
    assert admin.delete(f"/api/documents/{doc['id']}").json()["status"] == "success"
    assert STORE.objects == {}
    assert priya.get(f"/api/documents/{doc['id']}").status_code == 404


def test_admin_download_is_audited():
    priya = as_user("priya@morganlaw.test")
    doc = _upload(priya, _collection(priya))
    priya.post(f"/api/documents/{doc['id']}/complete")
    admin = as_admin()
    assert admin.get(f"/api/documents/{doc['id']}/download").status_code == 200
    actions = [e["action"] for e in admin.get("/api/admin/audit").json()["items"]]
    assert "admin.document_access" in actions and "document.uploaded" in actions
    assert admin.get("/api/admin/overview").json()["documents_by_status"] == {"queued": 1}
