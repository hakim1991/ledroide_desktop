import asyncio
import io

import numpy as np
from ledroide_sdk.http import UploadFile, File, Form
from ledroide_sdk.http import JSONResponse
from PIL import Image
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

CHARS = "@%#*+=-:. "


@router.post("/convert")
async def convert(
    file: UploadFile = File(...),
    width: int = Form(100),
    invert: bool = Form(False),
):
    width = max(20, min(width, 200))
    raw = await file.read()

    def _process():
        img = Image.open(io.BytesIO(raw)).convert("L")
        orig_w, orig_h = img.size
        new_h = max(1, int(orig_h * width / orig_w * 0.45))
        img = img.resize((width, new_h))
        pixels = np.array(img)
        if invert:
            pixels = 255 - pixels
        chars_arr = np.array(list(CHARS))
        indices = (pixels / 255 * (len(chars_arr) - 1)).astype(int)
        rows = ["".join(chars_arr[row]) for row in indices]
        return "\n".join(rows), new_h

    ascii_text, lines = await asyncio.to_thread(_process)
    return {"ascii": ascii_text, "width": width, "lines": lines}
