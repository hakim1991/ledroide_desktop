from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class LedroideError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def error_body(message: str, code: str, **extra) -> dict:
    return {"error": message, "code": code, **extra}


def install_handlers(app: FastAPI):
    @app.exception_handler(LedroideError)
    async def _ledroide(_: Request, exc: LedroideError):
        return JSONResponse(error_body(exc.message, exc.code), status_code=exc.status)

    # "detail" is kept because existing module frontends read it.
    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = error_body(str(detail["error"]), str(detail.get("code", f"HTTP_{exc.status_code}")), detail=detail)
        else:
            body = error_body(str(detail), f"HTTP_{exc.status_code}", detail=detail)
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors()
        ]
        message = errors[0]["msg"].removeprefix("Value error, ") if errors else "Paramètres invalides"
        return JSONResponse(error_body(message, "VALIDATION_ERROR", detail=errors), status_code=422)
