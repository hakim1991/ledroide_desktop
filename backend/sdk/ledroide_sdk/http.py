"""HTTP primitives for module routers, so modules never import fastapi directly."""
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)


def error(status: int, message: str, code: str) -> HTTPException:
    """`raise error(404, "Note introuvable", "NOT_FOUND")` → {"error", "code"} body."""
    return HTTPException(status_code=status, detail={"error": message, "code": code})


__all__ = [
    "APIRouter", "Body", "Depends", "File", "Form", "Header", "HTTPException", "Path",
    "Query", "Request", "UploadFile", "FileResponse", "HTMLResponse", "JSONResponse",
    "PlainTextResponse", "Response", "StreamingResponse", "error",
]
