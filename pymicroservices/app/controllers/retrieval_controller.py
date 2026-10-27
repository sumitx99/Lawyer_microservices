"""Evidence tools for a project owned by the caller."""
from fastapi import APIRouter, Request

from .unstructured_thread_controller import UserId, user
from ..schemas import (RetrievalDocumentsRequest, RetrievalDocumentsResponse, RetrievalExpandRequest,
                       RetrievalExpandResponse, RetrievalFactsRequest, RetrievalFactsResponse,
                       RetrievalSearchRequest, RetrievalSearchResponse)

router = APIRouter(prefix='/retrieval', tags=['retrieval'])


@router.post('/search', response_model=RetrievalSearchResponse)
async def search(payload: RetrievalSearchRequest, request: Request, user_id: UserId):
    state = request.app.state
    return await state.retrieval.search(user(user_id), payload, state.query_embedder, state.reranker)


@router.post('/list-documents', response_model=RetrievalDocumentsResponse)
async def list_documents(payload: RetrievalDocumentsRequest, request: Request, user_id: UserId):
    return await request.app.state.retrieval.list_documents(user(user_id), payload)


@router.post('/document-facts', response_model=RetrievalFactsResponse)
async def document_facts(payload: RetrievalFactsRequest, request: Request, user_id: UserId):
    return await request.app.state.retrieval.document_facts(user(user_id), payload)


@router.post('/expand', response_model=RetrievalExpandResponse)
async def expand(payload: RetrievalExpandRequest, request: Request, user_id: UserId):
    return await request.app.state.retrieval.expand(user(user_id), payload)
