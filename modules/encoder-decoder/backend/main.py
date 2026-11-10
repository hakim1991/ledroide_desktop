import logging
import quopri

from ledroide_sdk import Module

log = logging.getLogger("ledroide.encoder-decoder")

module = Module(__file__)
router = module.router

MAX_CHARS = 5_000_000


class CodecError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def idna_encode(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        try:
            out_lines.append(stripped.encode("idna").decode("ascii"))
        except UnicodeError as e:
            raise CodecError("IDNA_ERROR", f"Nom de domaine invalide « {stripped} » : {_idna_reason(e)}") from e
    return "\n".join(out_lines)


def idna_decode(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        try:
            out_lines.append(stripped.encode("ascii").decode("idna"))
        except UnicodeError as e:
            raise CodecError("IDNA_ERROR", f"Domaine IDNA invalide « {stripped} » : {_idna_reason(e)}") from e
    return "\n".join(out_lines)


def _idna_reason(e: UnicodeError) -> str:
    msg = str(e)
    if "empty or too long" in msg or "label too long" in msg:
        return "étiquette vide ou trop longue (63 caractères max entre deux points)."
    if "ordinal not in range" in msg:
        return "caractères non ASCII dans une forme encodée."
    return msg


def punycode_encode(text: str) -> str:
    return text.encode("punycode").decode("ascii")


def punycode_decode(text: str) -> str:
    raw = text.strip()
    if raw.lower().startswith("xn--"):
        raw = raw[4:]
    try:
        return raw.encode("ascii").decode("punycode")
    except UnicodeError as e:
        raise CodecError("PUNYCODE_ERROR", f"Punycode invalide : {e}") from e


def qp_encode(text: str, header: bool = False) -> str:
    return quopri.encodestring(text.encode("utf-8"), quotetabs=False, header=header).decode("ascii")


def qp_decode(text: str, header: bool = False) -> str:
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as e:
        raise CodecError("QP_ERROR", "Le quoted-printable ne contient que des caractères ASCII.") from e
    data = quopri.decodestring(raw, header=header)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


OPS = {
    ("idna", "encode"): lambda t, o: idna_encode(t),
    ("idna", "decode"): lambda t, o: idna_decode(t),
    ("punycode", "encode"): lambda t, o: punycode_encode(t),
    ("punycode", "decode"): lambda t, o: punycode_decode(t),
    ("qp", "encode"): lambda t, o: qp_encode(t, bool(o.get("header"))),
    ("qp", "decode"): lambda t, o: qp_decode(t, bool(o.get("header"))),
}


@module.on("transform")
async def transform(payload: dict):
    op = str(payload.get("op") or "")
    direction = str(payload.get("direction") or "encode")
    text = payload.get("text")
    if not isinstance(text, str):
        return {"error": "Le champ « text » doit être une chaîne.", "code": "BAD_PAYLOAD"}
    if len(text) > MAX_CHARS:
        return {"error": "Texte trop volumineux (5 millions de caractères max).", "code": "TOO_LARGE"}
    fn = OPS.get((op, direction))
    if fn is None:
        return {"error": f"Transformation inconnue : {op}/{direction}", "code": "UNKNOWN_OP"}
    try:
        return {"text": fn(text, payload.get("options") or {})}
    except CodecError as e:
        return {"error": e.message, "code": e.code}
