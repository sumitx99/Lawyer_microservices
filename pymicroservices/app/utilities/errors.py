"""One error shape everywhere: {"status": "failed", "code": ..., "message": ...}.
Real HTTP status codes (401/403/404/409/422/429), unlike SLR's HTTP 200 failures."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import logging

log = logging.getLogger("lexaior")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra


def unauthorized(message: str = "Please sign in again.") -> ApiError:
    return ApiError(401, "NOT_AUTHORIZED", message)


def forbidden(message: str = "You don't have access to do this.") -> ApiError:
    return ApiError(403, "ACCESS_DENIED", message)


def not_found(what: str = "Item") -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} not found.")


def conflict(code: str, message: str, **extra) -> ApiError:
    return ApiError(409, code, message, **extra)


def bad_request(code: str, message: str, **extra) -> ApiError:
    return ApiError(400, code, message, **extra)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        headers = {"Retry-After": str(exc.extra.pop("retry_after"))} if "retry_after" in exc.extra else None
        return JSONResponse({"status": "failed", "code": exc.code, "message": exc.message, **exc.extra},
                            status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        return JSONResponse({"status": "failed", "code": "INVALID_INPUT", "message": f"Invalid {field}"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"status": "failed", "code": "INTERNAL", "message": "An internal error occurred."}, status_code=500)
