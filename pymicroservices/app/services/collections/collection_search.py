"""Hybrid retrieval (BGE-M3 vectors + full text + legal references, reranked) over collections.

RetrievalService scopes by a project the caller owns (X-User-Id). Here the scope is the set of
collection folders the caller may read, decided beforehand by security/access.py, so walls and
grants apply to search exactly as they do to listing and downloading."""
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pymicroservices.app.models import UnstructuredData, UnstructuredThread
from pymicroservices.app.schemas import RetrievalFilters, RetrievalSearchRequest
from pymicroservices.app.services.collections.collection_files import thread_id
from pymicroservices.app.services.retrieval.retrieval_service import RetrievalService
from pymicroservices.app.utilities.errors import ApiError
from pymicroservices.app.utilities.file_validator import UploadError


class SearchIn(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    max_per_document: int = Field(default=3, ge=1, le=50)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)


class FolderRetrievalService(RetrievalService):
    """RetrievalService whose `user_id` argument is the list of folder ids the caller may read."""

    def threads(self, folder_ids, project_id):
        return select(UnstructuredThread.id).where(UnstructuredThread.id.in_(folder_ids))

    async def authorize(self, session, folder_ids, project_id, filters=None):
        if filters:
            for ids, scoped, column in [(filters.thread_ids, self.threads(folder_ids, project_id), UnstructuredThread.id),
                                        (filters.doc_ids, self.scope(folder_ids, project_id), UnstructuredData.id)]:
                if ids and len(set(await session.scalars(scoped.where(column.in_(ids))))) != len(set(ids)):
                    raise UploadError('Folder or file not found.', 404)


async def search(state, collection_ids: list[str], scope_id: str, body: SearchIn) -> dict:
    """scope_id (the matter or collection searched) only labels the request; collection_ids is the scope."""
    folders = [thread_id(cid) for cid in collection_ids]
    request = RetrievalSearchRequest(project_id=UUID(scope_id), **body.model_dump())
    service = FolderRetrievalService(state.db, state.settings)
    try:
        result = await service.search(folders, request, state.query_embedder, state.reranker)
    except UploadError as error:
        raise ApiError(error.status_code, 'SEARCH_UNAVAILABLE' if error.status_code == 503 else 'NOT_FOUND',
                       str(error)) from None
    return result.model_dump(mode='json')
