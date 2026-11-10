import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from ledroide_sdk import Module

log = logging.getLogger("ledroide.jwt-inspector")

module = Module(__file__)
router = module.router

HMAC_ALGS = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}
EC_ALGS = {"ES256": ("SECP256R1", "SHA256", 32), "ES384": ("SECP384R1", "SHA384", 48), "ES512": ("SECP521R1", "SHA512", 66)}
RSA_ALGS = {"RS256": "SHA256", "RS384": "SHA384", "RS512": "SHA512"}
PSS_ALGS = {"PS256": "SHA256", "PS384": "SHA384", "PS512": "SHA512"}
ALGS = [*HMAC_ALGS, *RSA_ALGS, *PSS_ALGS, *EC_ALGS, "EdDSA", "none"]
TIME_CLAIMS = ("exp", "iat", "nbf", "auth_time", "updated_at")
CLAIM_LABELS = {
    "iss": "Émetteur", "sub": "Sujet", "aud": "Audience", "exp": "Expiration", "nbf": "Pas avant",
    "iat": "Émis le", "jti": "Identifiant du jeton", "auth_time": "Authentifié le", "scope": "Portée",
    "azp": "Partie autorisée", "nonce": "Nonce", "name": "Nom", "email": "E-mail", "roles": "Rôles",
    "updated_at": "Mis à jour le",
}


class JwtError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(e: JwtError) -> dict:
    return {"error": e.message, "code": e.code}


def b64url_decode(part: str) -> bytes:
    part = part.strip().rstrip("=")
    bad = next((ch for ch in part if not (ch.isascii() and (ch.isalnum() or ch in "-_"))), None)
    if bad is not None:
        raise JwtError("BAD_BASE64", f"Base64url invalide : caractère « {bad} » interdit.")
    if len(part) % 4 == 1:
        raise JwtError("BAD_BASE64", "Base64url invalide : longueur incorrecte (jeton tronqué ?).")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as e:
        raise JwtError("BAD_BASE64", f"Base64url invalide : {e}") from e


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _clean_token(raw: Any) -> str:
    token = str(raw or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return "".join(token.split())


def split_token(raw: Any) -> tuple[str, str, str]:
    token = _clean_token(raw)
    if not token:
        raise JwtError("EMPTY_TOKEN", "Aucun jeton fourni.")
    parts = token.split(".")
    if len(parts) == 5:
        raise JwtError("JWE_UNSUPPORTED", "Ce jeton a 5 parties : c'est un JWE (chiffré), il ne peut pas être décodé sans la clé.")
    if len(parts) != 3:
        raise JwtError("BAD_FORMAT", f"Un JWT doit comporter 3 parties séparées par des points (trouvé : {len(parts)}).")
    return parts[0], parts[1], parts[2]


def _json_part(part: str, label: str) -> tuple[Any, str]:
    raw = b64url_decode(part)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise JwtError("BAD_ENCODING", f"Le {label} n'est pas de l'UTF-8 valide.") from e
    try:
        return json.loads(text), text
    except json.JSONDecodeError as e:
        raise JwtError("BAD_JSON", f"Le {label} n'est pas du JSON valide : {e.msg} (position {e.pos}).") from e


def _time_info(value: Any, now: float) -> dict | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds > 1e11:
        seconds /= 1000
    return {"timestamp": value, "epoch_ms": int(seconds * 1000), "delta_s": round(seconds - now)}


def decode_token(raw: Any, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    h, p, s = split_token(raw)
    header, header_text = _json_part(h, "header")
    if not isinstance(header, dict):
        raise JwtError("BAD_HEADER", "Le header doit être un objet JSON.")
    payload_raw = b64url_decode(p)
    try:
        payload_text = payload_raw.decode("utf-8")
        payload = json.loads(payload_text)
        payload_is_json = True
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload_text = payload_raw.decode("utf-8", errors="replace")
        payload = None
        payload_is_json = False
    signature = b64url_decode(s) if s else b""

    alg = header.get("alg")
    warnings: list[dict] = []
    if alg is None:
        warnings.append({"level": "err", "text": "Le header ne précise pas d'algorithme (« alg » absent)."})
    elif str(alg).lower() == "none":
        warnings.append({"level": "err", "text": "alg = « none » : le jeton n'est PAS signé. N'importe qui peut le forger ; un serveur ne doit jamais l'accepter."})
        if s:
            warnings.append({"level": "warn", "text": "alg « none » mais une signature est présente : jeton incohérent."})
    elif alg not in ALGS:
        warnings.append({"level": "warn", "text": f"Algorithme « {alg} » non pris en charge pour la vérification."})
    if alg and str(alg).lower() != "none" and not s:
        warnings.append({"level": "err", "text": "Signature vide alors que l'algorithme en exige une."})

    claims: list[dict] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            info = _time_info(value, now) if key in TIME_CLAIMS else None
            claims.append({"key": key, "label": CLAIM_LABELS.get(key, ""), "value": value, "time": info})
        exp = _time_info(payload.get("exp"), now)
        nbf = _time_info(payload.get("nbf"), now)
        iat = _time_info(payload.get("iat"), now)
        if "exp" not in payload:
            warnings.append({"level": "warn", "text": "Pas de claim « exp » : le jeton n'expire jamais."})
        elif exp is None:
            warnings.append({"level": "err", "text": "« exp » doit être un nombre (secondes depuis 1970)."})
        elif exp["delta_s"] <= 0:
            warnings.append({"level": "err", "text": "Le jeton est expiré."})
        if nbf and nbf["delta_s"] > 0:
            warnings.append({"level": "warn", "text": "« nbf » est dans le futur : le jeton n'est pas encore valide."})
        if iat and iat["delta_s"] > 60:
            warnings.append({"level": "warn", "text": "« iat » est dans le futur (horloge décalée ?)."})
        if any(isinstance(payload.get(k), (int, float)) and payload.get(k) > 1e11 for k in ("exp", "iat", "nbf")):
            warnings.append({"level": "warn", "text": "Dates exprimées en millisecondes : la norme impose des secondes."})
    elif payload_is_json:
        warnings.append({"level": "warn", "text": "Le payload est du JSON mais pas un objet : aucun claim standard."})
    else:
        warnings.append({"level": "warn", "text": "Le payload n'est pas du JSON (contenu brut affiché)."})

    status = "unsigned" if str(alg).lower() == "none" else "valid_time"
    if isinstance(payload, dict):
        exp = _time_info(payload.get("exp"), now)
        nbf = _time_info(payload.get("nbf"), now)
        if exp and exp["delta_s"] <= 0:
            status = "expired"
        elif nbf and nbf["delta_s"] > 0:
            status = "not_yet_valid"
    return {
        "header": header,
        "header_text": header_text,
        "payload": payload,
        "payload_text": payload_text,
        "payload_is_json": payload_is_json,
        "signature_b64": s,
        "signature_hex": signature.hex(),
        "signature_bytes": len(signature),
        "alg": alg,
        "claims": claims,
        "warnings": warnings,
        "status": status,
        "now": now,
    }


# ── Keys & signatures ───────────────────────────────────────────────────────

def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, padding, rsa, utils
    except ImportError as e:
        raise JwtError(
            "DEPENDENCY_MISSING",
            "La bibliothèque « cryptography » est requise pour les algorithmes RS/PS/ES/EdDSA "
            "(réactivez le module ou installez-la via le menu Bibliothèques). HS256/384/512 fonctionnent sans elle.",
        ) from e
    return {
        "InvalidSignature": InvalidSignature, "hashes": hashes, "serialization": serialization, "ec": ec,
        "ed25519": ed25519, "ed448": ed448, "padding": padding, "rsa": rsa, "utils": utils,
    }


def _secret_bytes(secret: Any, encoding: str) -> bytes:
    secret = "" if secret is None else str(secret)
    if encoding == "base64":
        s = secret.strip().replace("-", "+").replace("_", "/")
        try:
            return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
        except (binascii.Error, ValueError) as e:
            raise JwtError("BAD_SECRET", "Le secret n'est pas du Base64 valide.") from e
    if encoding == "hex":
        try:
            return bytes.fromhex(secret.strip())
        except ValueError as e:
            raise JwtError("BAD_SECRET", "Le secret n'est pas de l'hexadécimal valide.") from e
    return secret.encode("utf-8")


def _load_public_key(pem: str, c: dict):
    data = (pem or "").strip().encode()
    if not data:
        raise JwtError("NO_KEY", "Collez une clé publique PEM (ou un certificat, ou la clé privée).")
    ser = c["serialization"]
    if b"CERTIFICATE" in data:
        from cryptography import x509
        try:
            return x509.load_pem_x509_certificate(data).public_key()
        except ValueError as e:
            raise JwtError("BAD_KEY", f"Certificat PEM illisible : {e}") from e
    if b"PRIVATE KEY" in data:
        return _load_private_key(pem, c).public_key()
    try:
        return ser.load_pem_public_key(data)
    except (ValueError, TypeError) as e:
        if data.startswith((b"ssh-", b"ecdsa-")):
            try:
                return ser.load_ssh_public_key(data)
            except Exception:
                pass
        raise JwtError("BAD_KEY", f"Clé publique PEM illisible : {e}") from e


def _load_private_key(pem: str, c: dict):
    data = (pem or "").strip().encode()
    if not data:
        raise JwtError("NO_KEY", "Collez une clé privée PEM.")
    try:
        return c["serialization"].load_pem_private_key(data, password=None)
    except TypeError as e:
        raise JwtError("BAD_KEY", "Clé privée chiffrée par mot de passe : non prise en charge.") from e
    except ValueError as e:
        raise JwtError("BAD_KEY", f"Clé privée PEM illisible : {e}") from e


def _check_key_type(alg: str, key, c: dict, private: bool):
    rsa, ec, ed25519, ed448 = c["rsa"], c["ec"], c["ed25519"], c["ed448"]
    if alg in RSA_ALGS or alg in PSS_ALGS:
        expected = rsa.RSAPrivateKey if private else rsa.RSAPublicKey
        if not isinstance(key, expected):
            raise JwtError("KEY_MISMATCH", f"{alg} exige une clé RSA.")
    elif alg in EC_ALGS:
        expected = ec.EllipticCurvePrivateKey if private else ec.EllipticCurvePublicKey
        if not isinstance(key, expected):
            raise JwtError("KEY_MISMATCH", f"{alg} exige une clé à courbe elliptique.")
        curve = EC_ALGS[alg][0]
        if key.curve.name.upper() != curve:
            names = {"SECP256R1": "P-256", "SECP384R1": "P-384", "SECP521R1": "P-521"}
            raise JwtError("KEY_MISMATCH", f"{alg} exige une courbe {names[curve]} (clé fournie : {key.curve.name}).")
    elif alg == "EdDSA":
        types = (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey) if private else (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)
        if not isinstance(key, types):
            raise JwtError("KEY_MISMATCH", "EdDSA exige une clé Ed25519 ou Ed448.")


def _sign(alg: str, signing_input: bytes, key_material: str, secret_encoding: str) -> bytes:
    if alg in HMAC_ALGS:
        secret = _secret_bytes(key_material, secret_encoding)
        if not secret:
            raise JwtError("NO_KEY", "Saisissez un secret.")
        return hmac.new(secret, signing_input, HMAC_ALGS[alg]).digest()
    if alg == "none":
        return b""
    c = _crypto()
    key = _load_private_key(key_material, c)
    _check_key_type(alg, key, c, private=True)
    hashes, padding = c["hashes"], c["padding"]
    if alg in RSA_ALGS:
        return key.sign(signing_input, padding.PKCS1v15(), getattr(hashes, RSA_ALGS[alg])())
    if alg in PSS_ALGS:
        h = getattr(hashes, PSS_ALGS[alg])()
        return key.sign(signing_input, padding.PSS(mgf=padding.MGF1(h), salt_length=h.digest_size), h)
    if alg in EC_ALGS:
        _, hname, size = EC_ALGS[alg]
        der = key.sign(signing_input, c["ec"].ECDSA(getattr(hashes, hname)()))
        r, s = c["utils"].decode_dss_signature(der)
        return r.to_bytes(size, "big") + s.to_bytes(size, "big")
    if alg == "EdDSA":
        return key.sign(signing_input)
    raise JwtError("UNSUPPORTED_ALG", f"Algorithme non pris en charge : {alg}")


def _verify(alg: str, signing_input: bytes, signature: bytes, key_material: str, secret_encoding: str) -> bool:
    if alg in HMAC_ALGS:
        secret = _secret_bytes(key_material, secret_encoding)
        if not secret:
            raise JwtError("NO_KEY", "Saisissez le secret partagé.")
        return hmac.compare_digest(hmac.new(secret, signing_input, HMAC_ALGS[alg]).digest(), signature)
    c = _crypto()
    key = _load_public_key(key_material, c)
    _check_key_type(alg, key, c, private=False)
    hashes, padding, invalid = c["hashes"], c["padding"], c["InvalidSignature"]
    try:
        if alg in RSA_ALGS:
            key.verify(signature, signing_input, padding.PKCS1v15(), getattr(hashes, RSA_ALGS[alg])())
        elif alg in PSS_ALGS:
            h = getattr(hashes, PSS_ALGS[alg])()
            key.verify(signature, signing_input, padding.PSS(mgf=padding.MGF1(h), salt_length=padding.PSS.AUTO), h)
        elif alg in EC_ALGS:
            _, hname, size = EC_ALGS[alg]
            if len(signature) != 2 * size:
                return False
            r = int.from_bytes(signature[:size], "big")
            s = int.from_bytes(signature[size:], "big")
            der = c["utils"].encode_dss_signature(r, s)
            key.verify(der, signing_input, c["ec"].ECDSA(getattr(hashes, hname)()))
        elif alg == "EdDSA":
            key.verify(signature, signing_input)
        else:
            raise JwtError("UNSUPPORTED_ALG", f"Algorithme non pris en charge : {alg}")
    except invalid:
        return False
    return True


# ── Actions ─────────────────────────────────────────────────────────────────

@module.on("decode")
async def decode(payload: dict):
    try:
        return decode_token(payload.get("token"))
    except JwtError as e:
        return _err(e)


@module.on("verify")
async def verify(payload: dict):
    try:
        h, p, s = split_token(payload.get("token"))
        header, _ = _json_part(h, "header")
        alg = str(payload.get("alg") or (header.get("alg") if isinstance(header, dict) else "") or "")
        if alg.lower() == "none":
            return {"valid": False, "alg": alg, "message": "alg « none » : aucune signature à vérifier, jeton à rejeter."}
        if alg not in ALGS:
            raise JwtError("UNSUPPORTED_ALG", f"Algorithme non pris en charge : {alg or '(vide)'}")
        signature = b64url_decode(s)
        ok = await _to_thread(_verify, alg, f"{h}.{p}".encode("ascii"), signature,
                              payload.get("key") or "", payload.get("secret_encoding") or "utf8")
        return {"valid": ok, "alg": alg,
                "message": "Signature valide." if ok else "Signature INVALIDE : clé/secret incorrect ou jeton modifié."}
    except JwtError as e:
        return _err(e)


@module.on("sign")
async def sign(payload: dict):
    try:
        alg = str(payload.get("alg") or "HS256")
        if alg not in ALGS:
            raise JwtError("UNSUPPORTED_ALG", f"Algorithme non pris en charge : {alg}")
        header = _parse_obj(payload.get("header"), "header")
        claims = payload.get("payload")
        if isinstance(claims, str):
            claims = _parse_obj(claims, "payload")
        if not isinstance(claims, dict):
            raise JwtError("BAD_PAYLOAD", "Le payload doit être un objet JSON.")
        header = {"alg": alg, **{k: v for k, v in header.items() if k != "alg"}}
        header.setdefault("typ", "JWT")
        h = b64url_encode(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode())
        p = b64url_encode(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
        sig = await _to_thread(_sign, alg, f"{h}.{p}".encode("ascii"),
                               payload.get("key") or "", payload.get("secret_encoding") or "utf8")
        return {"token": f"{h}.{p}.{b64url_encode(sig)}", "alg": alg}
    except JwtError as e:
        return _err(e)


@module.on("generate_keys")
async def generate_keys(payload: dict):
    alg = str(payload.get("alg") or "RS256")
    try:
        if alg in HMAC_ALGS:
            import secrets
            size = {"HS256": 32, "HS384": 48, "HS512": 64}[alg]
            return {"secret": secrets.token_urlsafe(size), "alg": alg}
        if alg == "none" or alg not in ALGS:
            raise JwtError("UNSUPPORTED_ALG", f"Pas de clé à générer pour {alg}.")
        return await _to_thread(_generate_pair, alg)
    except JwtError as e:
        return _err(e)


@module.on("algorithms")
async def algorithms(payload: dict):
    try:
        _crypto()
        crypto = True
    except JwtError:
        crypto = False
    return {"algorithms": ALGS, "crypto_available": crypto}


def _generate_pair(alg: str) -> dict:
    c = _crypto()
    ser = c["serialization"]
    if alg in RSA_ALGS or alg in PSS_ALGS:
        key = c["rsa"].generate_private_key(public_exponent=65537, key_size=2048)
    elif alg in EC_ALGS:
        key = c["ec"].generate_private_key(getattr(c["ec"], EC_ALGS[alg][0])())
    else:
        key = c["ed25519"].Ed25519PrivateKey.generate()
    private = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()).decode()
    public = key.public_key().public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo).decode()
    return {"private_key": private, "public_key": public, "alg": alg}


def _parse_obj(value: Any, label: str) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value))
    except json.JSONDecodeError as e:
        raise JwtError("BAD_JSON", f"{label.capitalize()} : JSON invalide ligne {e.lineno}, colonne {e.colno} ({e.msg}).") from e
    if not isinstance(data, dict):
        raise JwtError("BAD_JSON", f"Le {label} doit être un objet JSON.")
    return data


async def _to_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)
