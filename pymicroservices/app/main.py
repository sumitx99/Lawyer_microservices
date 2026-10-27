"""Local RAG HTTP application. Run with uvicorn, bound to loopback."""
import asyncio
from contextlib import asynccontextmanager, suppress

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .controllers.retrieval_controller import router as retrieval_router
from .controllers.project_controller import router as project_router
from .controllers.rag_controller import router
from .controllers.unstructured_thread_controller import router as thread_router
from .services.retrieval.retrieval_service import RetrievalService
from .utilities.retrieval_models import BGEM3, Reranker, warmup_models
from .services.projects.project_service import ProjectService
from .services.rag.document_service import DocumentService
from .services.rag.outbox_service import OutboxPublisher
from .services.rag.status_service import StatusService
from .services.retrigger.retrigger_service import RetriggerService
from .services.threads.unstructured.thread_service import ThreadService
from .utilities.file_validator import UploadError
from .utilities.postgresql_helper import Database
from .utilities.secrets_control import get_settings
from .utilities.storage_helper import Storage
from .utilities.valkey_helper import Cache


class UploadBodyLimit:
    """Bound the complete multipart body, including clients using chunked transfer."""
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST' or scope['path'] not in {'/documents', '/documents/'}:
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'detail': 'Invalid content length.'}, status_code=400)(scope, receive, send)
        if length < 0 or length > self.maximum:
            return await JSONResponse({'detail': 'Upload request is too large.'}, status_code=413)(scope, receive, send)
        count = 0

        async def bounded_receive():
            nonlocal count
            message = await receive()
            count += len(message.get('body', b''))
            if count > self.maximum:
                # Starlette preserves HTTPException status during multipart parsing.
                from starlette.exceptions import HTTPException
                raise HTTPException(413, 'Upload request is too large.')
            return message

        await self.app(scope, bounded_receive, send)


def create_app(settings=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        db, storage, cache = Database(settings), Storage(settings), Cache(settings)
        app.state.settings = settings
        app.state.db, app.state.storage, app.state.cache = db, storage, cache
        app.state.projects = ProjectService(db)
        app.state.query_embedder, app.state.reranker = BGEM3(settings), Reranker(settings)
        app.state.retrieval = RetrievalService(db, settings)
        app.state.documents = DocumentService(db, storage)
        app.state.status = StatusService(app.state.documents, cache)
        app.state.threads = ThreadService(db, storage, cache, settings)
        app.state.retrigger = RetriggerService(db, settings, app.state.threads)
        publisher = OutboxPublisher(db, settings)
        tasks = [asyncio.create_task(publisher.run())] if settings.outbox_enabled else []
        if settings.upload_cleanup_enabled:
            tasks.append(asyncio.create_task(app.state.threads.run_cleanup()))
        try:
            if settings.retrieval_warmup:
                await warmup_models(app.state.query_embedder, app.state.reranker, settings)
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await publisher.close()
            await cache.close()
            await storage.close()
            await db.close()

    app = FastAPI(title='Lexaior RAG API', version='0.1.0', lifespan=lifespan,
                  description='Local development API. Project IDs are filters; authentication is not implemented yet.')
    app.add_middleware(UploadBodyLimit, maximum=settings.max_upload_bytes + 1024 * 1024)
    app.include_router(router)
    app.include_router(thread_router)
    app.include_router(project_router)
    app.include_router(retrieval_router)

    @app.exception_handler(UploadError)
    async def invalid_upload(request, error):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    async def service_unavailable(request, error):
        return JSONResponse({'detail': 'A required storage or database service is unavailable. Retry later.'}, status_code=503)

    for error_type in [SQLAlchemyError, BotoCoreError, ClientError]:
        app.add_exception_handler(error_type, service_unavailable)

    @app.get('/health', tags=['health'])
    async def health(request: Request):
        try:
            async with asyncio.timeout(5):
                await request.app.state.db.ping()
                await request.app.state.storage.ping()
        except Exception:
            return JSONResponse({'status': 'unavailable'}, status_code=503)
        return {'status': 'ok'}

    return app
