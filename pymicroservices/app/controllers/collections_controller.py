"""Collections (data folders), their documents and direct uploads to storage."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import bad_request, not_found
from pymicroservices.app.firm_models import Collection, CollectionGrant, IamUser
from pymicroservices.app.security import access
from pymicroservices.app.security.guard import Ctx, require
from pymicroservices.app.services.collections import collection_files as files
from pymicroservices.app.services.collections.collection_search import SearchIn, search
from pymicroservices.app.services.firm import audit
from pymicroservices.app.services.firm.serialize import collection_matters, iso

router = APIRouter(prefix="/api", tags=["collections"])


def _now():
    return datetime.now(timezone.utc)


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class CollectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class Grant(BaseModel):
    user_id: str
    level: Literal["read", "write"]


class GrantsIn(BaseModel):
    grants: list[Grant] = Field(max_length=200)


def _collection_out(db: Session, ctx: Ctx, col: Collection, perms: set[str], visible_ids: set[str] | None) -> dict:
    return {"id": col.id, "name": col.name, "description": col.description, "created_at": iso(col.created_at),
            "documents": files.collection_counts(db, [col.id]).get(col.id, 0),
            "matters": collection_matters(db, [col.id], visible_ids).get(col.id, []),
            "flags": access.collection_flags(perms)}


@router.get("/collections")
def list_collections(q: str = "", db: Session = Depends(get_db), ctx: Ctx = Depends(require("collections.list"))):
    stmt = access.visible_collections(ctx.who)
    if q:
        stmt = stmt.where(Collection.name.ilike(f"%{q}%"))
    cols = db.scalars(stmt.order_by(Collection.created_at.desc()).limit(500)).all()
    visible_ids = None if ctx.who.is_admin else set(db.scalars(access.visible_matter_ids(ctx.who)))
    counts = files.collection_counts(db, [c.id for c in cols])
    links = collection_matters(db, [c.id for c in cols], visible_ids)
    items = []
    for c in cols:
        perms, _ = access.collection_perms(db, ctx.who, c)
        items.append({"id": c.id, "name": c.name, "description": c.description, "created_at": iso(c.created_at),
                      "documents": counts.get(c.id, 0), "matters": links.get(c.id, []),
                      "flags": access.collection_flags(perms)})
    return {"items": items}


@router.post("/collections", status_code=201)
def create_collection(body: CollectionIn, db: Session = Depends(get_db),
                      ctx: Ctx = Depends(require("collections.create_unlinked"))):
    col = Collection(firm_id=ctx.who.firm_id, name=body.name.strip(), description=body.description.strip(),
                     created_by=None if ctx.who.is_admin else ctx.who.id,
                     created_by_admin=ctx.who.id if ctx.who.is_admin else None)
    db.add(col)
    db.flush()
    audit.record_ctx(db, ctx, "collection.created", "collection", col.id, name=col.name)
    db.commit()
    perms, _ = access.collection_perms(db, ctx.who, col)
    return {"collection": _collection_out(db, ctx, col, perms, None)}


@router.get("/collections/{collection_id}")
def get_collection(collection_id: str, db: Session = Depends(get_db),
                   ctx: Ctx = Depends(require("collections.read", collection="collection_id"))):
    visible_ids = None if ctx.who.is_admin else set(db.scalars(access.visible_matter_ids(ctx.who)))
    grants = []
    if "collections.share" in ctx.perms:
        rows = db.execute(select(CollectionGrant, IamUser).join(IamUser, IamUser.id == CollectionGrant.user_id)
                          .where(CollectionGrant.collection_id == collection_id)).all()
        grants = [{"user_id": u.id, "name": u.name, "email": u.email, "level": g.level} for g, u in rows]
    return {"collection": dict(_collection_out(db, ctx, ctx.collection, ctx.perms, visible_ids), grants=grants,
                               can_share="collections.share" in ctx.perms)}


@router.patch("/collections/{collection_id}")
def update_collection(collection_id: str, body: CollectionPatch, db: Session = Depends(get_db),
                      ctx: Ctx = Depends(require("collections.update", collection="collection_id"))):
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(ctx.collection, key, value.strip())
    audit.record_ctx(db, ctx, "collection.updated", "collection", collection_id, **changes)
    db.commit()
    return {"collection": _collection_out(db, ctx, ctx.collection, ctx.perms, None)}


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: str, db: Session = Depends(get_db),
                      ctx: Ctx = Depends(require("collections.delete", collection="collection_id"))):
    ctx.collection.deleted_at = _now()
    audit.record_ctx(db, ctx, "collection.deleted", "collection", collection_id)
    db.commit()
    return {"status": "success"}


@router.put("/collections/{collection_id}/grants")
def set_grants(collection_id: str, body: GrantsIn, db: Session = Depends(get_db),
               ctx: Ctx = Depends(require("collections.share", collection="collection_id"))):
    ids = {g.user_id for g in body.grants}
    valid = set(db.scalars(select(IamUser.id).where(IamUser.firm_id == ctx.who.firm_id, IamUser.id.in_(ids))))
    if ids - valid:
        raise bad_request("UNKNOWN_USER", "One of those people isn't in your firm.")
    db.execute(delete(CollectionGrant).where(CollectionGrant.collection_id == collection_id))
    for g in body.grants:
        db.add(CollectionGrant(collection_id=collection_id, user_id=g.user_id, level=g.level, granted_by=ctx.who.id))
    audit.record_ctx(db, ctx, "collection.grants_changed", "collection", collection_id,
                     grants=[g.model_dump() for g in body.grants])
    db.commit()
    return {"status": "success"}


# Documents ---------------------------------------------------------------------------------
# A collection's documents are the files of its folder in the RAG pipeline (services/collections).
class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$", description="Hex SHA-256 of the file, computed in the browser.")
    content_type: str | None = Field(default=None, max_length=120)  # informational; the type comes from the extension


@router.get("/collections/{collection_id}/documents")
def list_documents(collection_id: str, db: Session = Depends(get_db),
                   ctx: Ctx = Depends(require("documents.read", collection="collection_id"))):
    docs = files.list_documents(db, collection_id)
    return {"items": [files.document_out(d) for d in docs], "flags": access.collection_flags(ctx.perms)}


@router.post("/collections/{collection_id}/uploads", status_code=201)
async def start_upload(collection_id: str, body: UploadIn, request: Request, db: Session = Depends(get_db),
                       ctx: Ctx = Depends(require("documents.upload", collection="collection_id"))):
    doc, upload = await files.start_upload(request.app.state.threads, ctx.collection, ctx.who.id,
                                           body.filename.strip(), body.size_bytes, body.sha256.lower())
    return {"document": files.document_out(doc), "upload": upload}


@router.get("/documents/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db),
                 ctx: Ctx = Depends(require("documents.read", document="document_id"))):
    return {"document": files.document_out(ctx.document)}


@router.post("/documents/{document_id}/complete")
async def complete_upload(document_id: str, request: Request, db: Session = Depends(get_db),
                          ctx: Ctx = Depends(require("documents.upload", document="document_id"))):
    doc = ctx.document
    if doc.upload_status != "RUNNING":
        return {"document": files.document_out(doc)}
    doc = await files.complete_upload(request.app.state.threads, doc)
    if doc.upload_status == "FAILED":
        raise bad_request("UPLOAD_REJECTED", doc.upload_error or "The uploaded file was rejected. Try uploading again.")
    audit.record_ctx(db, ctx, "document.uploaded", "document", str(doc.id), filename=doc.file_name,
                     size=doc.byte_count, collection_id=str(doc.thread_id))
    db.commit()
    return {"document": files.document_out(doc)}


@router.post("/documents/{document_id}/retry")
async def retry_document(document_id: str, request: Request, db: Session = Depends(get_db),
                         ctx: Ctx = Depends(require("documents.upload", document="document_id"))):
    # The retrigger service decides: failed stages, stalled workers and stuck queues can run again.
    doc = await files.retry(request.app.state.retrigger, ctx.who.id, ctx.document)
    audit.record_ctx(db, ctx, "document.retried", "document", str(doc.id))
    db.commit()
    return {"document": files.document_out(doc)}


@router.get("/documents/{document_id}/download")
def download_document(document_id: str, request: Request, db: Session = Depends(get_db),
                      ctx: Ctx = Depends(require("documents.download", document="document_id"))):
    doc = ctx.document
    if doc.upload_status != "COMPLETED":
        raise not_found("Document")
    action = "admin.document_access" if ctx.who.is_admin else "document.downloaded"
    audit.record_ctx(db, ctx, action, "document", str(doc.id), filename=doc.file_name)
    db.commit()
    state = request.app.state
    return {"url": state.storage.presign_get(doc.s3_path, doc.file_name, state.settings.download_url_ttl_seconds)}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, request: Request, db: Session = Depends(get_db),
                          ctx: Ctx = Depends(require("documents.delete", document="document_id"))):
    await files.delete_document(request.app.state.threads, ctx.document)
    audit.record_ctx(db, ctx, "document.deleted", "document", document_id, filename=ctx.document.file_name)
    db.commit()
    return {"status": "success"}


@router.post("/collections/{collection_id}/search")
async def search_collection(collection_id: str, body: SearchIn, request: Request, db: Session = Depends(get_db),
                            ctx: Ctx = Depends(require("documents.read", collection="collection_id"))):
    return await search(request.app.state, [collection_id], collection_id, body)
