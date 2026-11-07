import asyncio
import hashlib
import hmac
import time
import zlib
from typing import Optional

from ledroide_sdk.http import File, Form, UploadFile
from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

CHUNK = 1024 * 1024  # 1 Mo

# (id, label, hashlib constructor) — order is the display order
ALGOS = [
    ("md5", "MD5", hashlib.md5),
    ("sha1", "SHA-1", hashlib.sha1),
    ("sha256", "SHA-256", hashlib.sha256),
    ("sha512", "SHA-512", hashlib.sha512),
    ("sha3_256", "SHA3-256", hashlib.sha3_256),
    ("blake2b", "BLAKE2b", hashlib.blake2b),
]


class _Crc32:
    def __init__(self):
        self.v = 0

    def update(self, data: bytes):
        self.v = zlib.crc32(data, self.v)

    def hexdigest(self) -> str:
        return f"{self.v & 0xFFFFFFFF:08x}"


class _Multi:
    """Feeds every chunk to all hashers (and HMACs if a key is given)."""

    def __init__(self, key: Optional[bytes]):
        self.h = {aid: ctor() for aid, _, ctor in ALGOS}
        self.h["crc32"] = _Crc32()
        self.m = {aid: hmac.new(key, digestmod=ctor) for aid, _, ctor in ALGOS} if key else {}
        self.size = 0

    def update(self, data: bytes):
        self.size += len(data)
        for h in self.h.values():
            h.update(data)
        for m in self.m.values():
            m.update(data)

    def result(self) -> dict:
        out = {"hashes": {k: v.hexdigest() for k, v in self.h.items()}, "size": self.size}
        if self.m:
            out["hmac"] = {k: v.hexdigest() for k, v in self.m.items()}
        return out


class TextIn(BaseModel):
    text: str
    hmac_key: str = ""


@router.get("/algorithms")
async def algorithms():
    return {"algorithms": [{"id": a, "label": l} for a, l, _ in ALGOS] + [{"id": "crc32", "label": "CRC32"}],
            "hmac": [a for a, _, _ in ALGOS]}


@router.post("/text")
async def hash_text(body: TextIn):
    data = body.text.encode("utf-8")
    key = body.hmac_key.encode("utf-8") if body.hmac_key else None

    def _run():
        m = _Multi(key)
        m.update(data)
        return m.result()

    return await asyncio.to_thread(_run)


@router.post("/file")
async def hash_file(file: UploadFile = File(...), hmac_key: str = Form("")):
    key = hmac_key.encode("utf-8") if hmac_key else None
    m = _Multi(key)
    t0 = time.perf_counter()
    try:
        while True:
            chunk = await file.read(CHUNK)
            if not chunk:
                break
            await asyncio.to_thread(m.update, chunk)
    except Exception as e:  # pragma: no cover - I/O errors
        return JSONResponse(status_code=400, content={"error": f"Lecture du fichier impossible : {e}"})
    finally:
        await file.close()
    res = m.result()
    res["filename"] = file.filename
    res["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res
