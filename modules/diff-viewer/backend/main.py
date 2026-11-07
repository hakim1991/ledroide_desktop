"""Comparateur de texte — difflib (lignes + différences intra-ligne)."""
import asyncio
import difflib
import re

from ledroide_sdk.http import UploadFile, File
from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

MAX_BYTES = 5 * 1024 * 1024
MAX_LINES = 100_000
INTRA_MAX = 4000          # longueur max d'une ligne pour le détail intra-ligne
_WORD_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _err(msg: str, status: int = 400):
    return JSONResponse(status_code=status, content={"error": msg})


class DiffIn(BaseModel):
    a: str = ""
    b: str = ""
    name_a: str = "original"
    name_b: str = "modifié"
    ignore_ws: bool = False
    ignore_case: bool = False
    granularity: str = "word"     # "char" | "word"
    context: int = 3


def _line_key(line: str, o: DiffIn) -> str:
    if o.ignore_ws:
        line = _WS_RE.sub("", line)
    if o.ignore_case:
        line = line.casefold()
    return line


def _tokens(line: str, gran: str):
    return list(line) if gran == "char" else _WORD_RE.findall(line)


def _tok_key(t: str, o: DiffIn) -> str:
    if o.ignore_ws and t.isspace():
        return ""
    return t.casefold() if o.ignore_case else t


def _merge(segs):
    out = []
    for text, ch in segs:
        if not text:
            continue
        if out and out[-1][1] == ch:
            out[-1][0] += text
        else:
            out.append([text, ch])
    return out


def _intra(la: str, lb: str, o: DiffIn):
    """Retourne (segments_gauche, segments_droite) avec [texte, 0|1] (1 = modifié)."""
    if len(la) > INTRA_MAX or len(lb) > INTRA_MAX:
        return [[la, 1]] if la else [], [[lb, 1]] if lb else []
    ta, tb = _tokens(la, o.granularity), _tokens(lb, o.granularity)
    ka, kb = [_tok_key(t, o) for t in ta], [_tok_key(t, o) for t in tb]
    if o.ignore_ws:
        # les blancs ne comptent pas : on les retire de la comparaison mais on les garde à l'affichage
        ia = [i for i, k in enumerate(ka) if k != ""]
        ib = [i for i, k in enumerate(kb) if k != ""]
    else:
        ia, ib = list(range(len(ta))), list(range(len(tb)))
    sm = difflib.SequenceMatcher(None, [ka[i] for i in ia], [kb[i] for i in ib], autojunk=False)
    chg_a = [0] * len(ta)
    chg_b = [0] * len(tb)
    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag != "equal":
            for i in ia[a1:a2]:
                chg_a[i] = 1
            for j in ib[b1:b2]:
                chg_b[j] = 1
    return _merge([[t, c] for t, c in zip(ta, chg_a)]), _merge([[t, c] for t, c in zip(tb, chg_b)])


def _fmt_range(start: int, length: int) -> str:
    beginning = start + 1
    if length == 1:
        return f"{beginning}"
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def _diff(o: DiffIn) -> dict:
    a = o.a.splitlines()
    b = o.b.splitlines()
    ka = [_line_key(x, o) for x in a]
    kb = [_line_key(x, o) for x in b]
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=(len(a) + len(b)) > 40_000)
    ops = sm.get_opcodes()

    rows = []
    added = removed = changed = unchanged = 0
    for tag, a1, a2, b1, b2 in ops:
        if tag == "equal":
            for i, j in zip(range(a1, a2), range(b1, b2)):
                unchanged += 1
                if a[i] == b[j]:
                    rows.append({"t": "equal", "ln": i + 1, "rn": j + 1, "l": [[a[i], 0]], "r": [[b[j], 0]]})
                else:
                    # égal au sens des options (casse/blancs) mais texte différent : on montre le détail atténué
                    la, lb = _intra(a[i], b[j], DiffIn(ignore_ws=False, ignore_case=False,
                                                          granularity=o.granularity))
                    rows.append({"t": "equal", "soft": True, "ln": i + 1, "rn": j + 1, "l": la, "r": lb})
        elif tag == "delete":
            for i in range(a1, a2):
                removed += 1
                rows.append({"t": "del", "ln": i + 1, "rn": None, "l": [[a[i], 1]], "r": None})
        elif tag == "insert":
            for j in range(b1, b2):
                added += 1
                rows.append({"t": "add", "ln": None, "rn": j + 1, "l": None, "r": [[b[j], 1]]})
        else:  # replace
            n = max(a2 - a1, b2 - b1)
            for k in range(n):
                i, j = a1 + k, b1 + k
                if i < a2 and j < b2:
                    changed += 1
                    la, lb = _intra(a[i], b[j], o)
                    rows.append({"t": "change", "ln": i + 1, "rn": j + 1, "l": la, "r": lb})
                elif i < a2:
                    removed += 1
                    rows.append({"t": "del", "ln": i + 1, "rn": None, "l": [[a[i], 1]], "r": None})
                else:
                    added += 1
                    rows.append({"t": "add", "ln": None, "rn": j + 1, "l": None, "r": [[b[j], 1]]})

    # diff unifié (texte) — basé sur les clés normalisées mais affichant le texte original
    ctx = max(0, min(int(o.context), 50))
    out = [f"--- {o.name_a}", f"+++ {o.name_b}"]
    for group in sm.get_grouped_opcodes(ctx):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        out.append(f"@@ -{_fmt_range(i1, i2 - i1)} +{_fmt_range(j1, j2 - j1)} @@")
        for tag, x1, x2, y1, y2 in group:
            if tag == "equal":
                out += [" " + line for line in a[x1:x2]]
                continue
            out += ["-" + line for line in a[x1:x2]]
            out += ["+" + line for line in b[y1:y2]]
    unified = "\n".join(out) + "\n" if len(out) > 2 else ""

    return {
        "rows": rows,
        "stats": {
            "added": added, "removed": removed, "changed": changed, "unchanged": unchanged,
            "lines_a": len(a), "lines_b": len(b),
            "similarity": round(sm.ratio() * 100, 1) if (a or b) else 100.0,
            "identical": added == removed == changed == 0,
            "exact": o.a == o.b,
        },
        "unified": unified,
    }


@router.get("/health")
async def health():
    return {"ok": True}


@router.post("/diff")
async def diff(body: DiffIn):
    if body.granularity not in ("char", "word"):
        return _err("Granularité invalide (char ou word).")
    if len(body.a.encode("utf-8", "ignore")) > MAX_BYTES or len(body.b.encode("utf-8", "ignore")) > MAX_BYTES:
        return _err("Texte trop volumineux (max 5 Mo par côté).")
    if body.a.count("\n") > MAX_LINES or body.b.count("\n") > MAX_LINES:
        return _err(f"Trop de lignes (max {MAX_LINES:,} par côté).".replace(",", " "))
    return await asyncio.to_thread(_diff, body)


def _decode(data: bytes):
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace"), "UTF-8 (BOM)"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "UTF-16"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "Windows-1252"


@router.post("/read-file")
async def read_file(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_BYTES:
        return _err("Fichier trop volumineux (max 5 Mo).")
    sample = data[:8192]
    if b"\x00" in sample and sample[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return _err("Ce fichier semble binaire : seuls les fichiers texte peuvent être comparés.")
    text, enc = _decode(data)
    return {"name": file.filename or "fichier", "text": text, "encoding": enc, "size": len(data),
            "eol": "CRLF" if b"\r\n" in data else ("LF" if b"\n" in data else "—")}
