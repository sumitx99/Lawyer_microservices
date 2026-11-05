"""Lexaior API: sign-in, firm roles and matters (/api) plus the RAG pipeline's upload and retrieval services.

Run (repo root): uvicorn pymicroservices.app.main:create_app --factory --port 8000

Everything under /api is reached through the website's Cloudflare Worker and must carry an access
guard (security/guard.py); startup fails otherwise. The RAG pipeline's own folder/project/document/
retrieval routes identify callers by X-User-Id only, so they are mounted in development and tests,
never when hosted; hosted clients reach the pipeline through /api/collections and /api/matters.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import logging

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from .controllers import (admin_controller, auth_controller, collections_controller, hosted_auth_controller,
                          matters_controller, me_controller)
from .controllers.retrieval_controller import router as retrieval_router
from .controllers.project_controller import router as project_router
from .controllers.rag_controller import router
from .controllers.unstructured_thread_controller import router as thread_router
from .security.guard import assert_all_routes_guarded
from .security.permissions import sync_permission_sets
from .services.retrieval.retrieval_service import RetrievalService
from .utilities.retrieval_models import BGEM3, Reranker, warmup_models
from .services.projects.project_service import ProjectService
from .services.rag.document_service import DocumentService
from .services.rag.outbox_service import OutboxPublisher
from .services.rag.status_service import StatusService
from .services.retrigger.retrigger_service import RetriggerService
from .services.threads.unstructured.thread_service import ThreadService
from .utilities.errors import install_handlers
from .utilities.file_validator import UploadError
from .utilities.firm_db import SessionLocal
from .utilities.postgresql_helper import Database
from .utilities.secrets_control import get_settings
from .utilities.storage_helper import Storage
from .utilities.valkey_helper import Cache

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')


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


class RequireProxy(BaseHTTPMiddleware):
    """With PROXY_SECRET set, /api is only reachable through the Cloudflare Worker."""

    async def dispatch(self, request: Request, call_next):
        secret = get_settings().proxy_secret
        if secret and request.url.path.startswith('/api') and \
                not hmac.compare_digest(request.headers.get('x-lexaior-proxy', ''), secret):
            return JSONResponse({'status': 'failed', 'code': 'NOT_FOUND', 'message': 'Not found.'}, status_code=404)
        return await call_next(request)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if not request.url.path.startswith(('/docs', '/redoc', '/openapi.json')):
            response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
        if get_settings().cookie_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-store'
        return response


def create_app(settings=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        # Fatal on failure: with no permission rows every check would deny everyone.
        with SessionLocal() as session:
            sync_permission_sets(session)
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

    hosted = settings.hosted  # staging and production hide the API docs and the X-User-Id routes
    app = FastAPI(title='Lexaior API', version='1.0.0', lifespan=lifespan,
                  docs_url=None if hosted else '/docs', redoc_url=None,
                  openapi_url=None if hosted else '/openapi.json')
    app.add_middleware(UploadBodyLimit, maximum=settings.max_upload_bytes + 1024 * 1024)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(RequireProxy)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
                       allow_headers=['Content-Type', 'X-CSRF-Token', 'X-Console'])
    install_handlers(app)

    app.include_router(hosted_auth_controller.router)  # before the /{console} routes so /callback and /config match
    app.include_router(auth_controller.build_router('admin'))
    app.include_router(auth_controller.build_router('iam'))
    app.include_router(me_controller.router)
    app.include_router(admin_controller.router)
    app.include_router(matters_controller.router)
    app.include_router(collections_controller.router)
    if not hosted:
        app.include_router(router)
        app.include_router(thread_router)
        app.include_router(project_router)
        app.include_router(retrieval_router)

    @app.exception_handler(UploadError)
    async def invalid_upload(request, error):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    async def service_unavailable(request, error):
        logging.getLogger('lexaior').warning('Dependency failure on %s: %s', request.url.path, type(error).__name__)
        message = 'A required storage or database service is unavailable. Retry later.'
        if request.url.path.startswith('/api'):
            return JSONResponse({'status': 'failed', 'code': 'SERVICE_UNAVAILABLE', 'message': message}, status_code=503)
        return JSONResponse({'detail': message}, status_code=503)

    for error_type in [SQLAlchemyError, BotoCoreError, ClientError]:
        app.add_exception_handler(error_type, service_unavailable)

    @app.get('/health', tags=['health'])
    def health():
        """Liveness, for Render's health check: the process is up."""
        return {'status': 'success'}

    @app.get('/health/ready', tags=['health'])
    async def ready(request: Request):
        """Readiness: Postgres and object storage answer."""
        try:
            async with asyncio.timeout(5):
                await request.app.state.db.ping()
                await request.app.state.storage.ping()
        except Exception:
            return JSONResponse({'status': 'unavailable'}, status_code=503)
        return {'status': 'ok'}

    assert_all_routes_guarded(app)  # refuse to start if any /api route lacks a guard
    return app
