import asyncio
import base64
import binascii
import email
import email.policy
import email.utils
import json
import logging
import mimetypes
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage

from ledroide_sdk import Module

log = logging.getLogger("ledroide.smtp-tester")

module = Module(__file__)
router = module.router

HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]_]{1,253}$")
ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")
MAX_ATTACHMENT = 20 * 1024 * 1024
LINE_LIMIT = 1000
SERVER_NAME = "ledroide-capture"


def _err(message: str, code: str) -> dict:
    return {"error": message, "code": code}


# ── Client : envoi de test ───────────────────────────────────────────────────

class _Transcript:
    """Captures smtplib debug output (instead of stderr) and masks credentials."""

    def _init_transcript(self):
        self.transcript: list[str] = []
        self._in_auth = False

    def _print_debug(self, *args):
        text = " ".join(str(a) for a in args)
        if text.startswith("send:"):
            if re.match(r"send: '?AUTH ", text, re.I):
                self._in_auth = True
                text = re.sub(r"(AUTH \S+).*", r"\1 ******", text, flags=re.I)
            elif self._in_auth:
                text = "send: ******"
        elif text.startswith("reply: retcode"):
            m = re.match(r"reply: retcode \((\d+)\)", text)
            if m and m.group(1) != "334":
                self._in_auth = False
        if len(text) > 600:
            text = text[:600] + f"… ({len(text)} caractères)"
        self.transcript.append(text)


class DebugSMTP(_Transcript, smtplib.SMTP):
    def __init__(self, timeout: float, local_hostname: str | None = None):
        smtplib.SMTP.__init__(self, timeout=timeout, local_hostname=local_hostname)
        self._init_transcript()


class DebugSMTP_SSL(_Transcript, smtplib.SMTP_SSL):
    def __init__(self, timeout: float, context: ssl.SSLContext, local_hostname: str | None = None):
        smtplib.SMTP_SSL.__init__(self, timeout=timeout, context=context, local_hostname=local_hostname)
        self._init_transcript()


def _ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _validate_server(p: dict) -> tuple[str, int, str, float]:
    host = str(p.get("host", "")).strip()
    if not host or not HOST_RE.match(host):
        raise ValueError("Adresse du serveur SMTP invalide")
    security = p.get("security", "none")
    if security not in ("none", "starttls", "ssl"):
        raise ValueError("Mode de sécurité invalide")
    try:
        port = int(p.get("port") or {"none": 25, "starttls": 587, "ssl": 465}[security])
    except (TypeError, ValueError):
        raise ValueError("Port invalide")
    if not 1 <= port <= 65535:
        raise ValueError("Port invalide (1-65535)")
    timeout = max(2.0, min(120.0, float(p.get("timeout") or 20)))
    return host, port, security, timeout


def _open(p: dict, host: str, port: int, security: str, timeout: float):
    ctx = _ssl_context(p.get("verify_ssl", True) is not False)
    smtp = DebugSMTP_SSL(timeout, ctx) if security == "ssl" else DebugSMTP(timeout)
    smtp._host = host
    smtp.set_debuglevel(1)
    code, banner = smtp.connect(host, port)
    smtp.ehlo()
    if security == "starttls":
        smtp.starttls(context=ctx)
        smtp.ehlo()
    return smtp, (code, banner.decode("utf-8", "replace") if isinstance(banner, bytes) else str(banner))


def _tls_info(smtp) -> dict | None:
    sock = getattr(smtp, "sock", None)
    if isinstance(sock, ssl.SSLSocket):
        cipher = sock.cipher()
        return {"version": sock.version(), "cipher": cipher[0] if cipher else None}
    return None


def _failure(exc: Exception, smtp=None) -> dict:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        code, msg = "AUTH_FAILED", f"Authentification refusée ({exc.smtp_code}) : {_dec(exc.smtp_error)}"
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        details = "; ".join(f"{r} : {c} {_dec(m)}" for r, (c, m) in exc.recipients.items())
        code, msg = "RECIPIENTS_REFUSED", f"Destinataire(s) refusé(s) — {details}"
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        code, msg = "SENDER_REFUSED", f"Expéditeur refusé ({exc.smtp_code}) : {_dec(exc.smtp_error)}"
    elif isinstance(exc, smtplib.SMTPNotSupportedError):
        code, msg = "NOT_SUPPORTED", f"Fonction non prise en charge par le serveur : {exc}"
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        code, msg = "DISCONNECTED", f"Le serveur a fermé la connexion : {exc or 'connexion perdue'}"
    elif isinstance(exc, smtplib.SMTPResponseException):
        code, msg = "SMTP_ERROR", f"Erreur SMTP {exc.smtp_code} : {_dec(exc.smtp_error)}"
    elif isinstance(exc, ssl.SSLCertVerificationError):
        code, msg = "SSL_ERROR", f"Certificat invalide : {exc.verify_message}. Décochez « Vérifier le certificat » pour forcer."
    elif isinstance(exc, ssl.SSLError):
        code, msg = "SSL_ERROR", f"Erreur TLS : {exc.reason or exc}. Vérifiez le mode de sécurité et le port."
    elif isinstance(exc, socket.gaierror):
        code, msg = "RESOLVE_FAILED", "Nom de serveur introuvable (DNS)"
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        code, msg = "TIMEOUT", "Délai dépassé"
    elif isinstance(exc, ConnectionRefusedError):
        code, msg = "CONNECT_FAILED", "Connexion refusée (aucun serveur SMTP sur ce port ?)"
    elif isinstance(exc, OSError):
        code, msg = "CONNECT_FAILED", f"Erreur réseau : {exc.strerror or exc}"
    else:
        code, msg = "SMTP_ERROR", str(exc)
    return {"ok": False, "failure": msg, "failure_code": code, "transcript": getattr(smtp, "transcript", [])}


def _dec(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def _close(smtp):
    if smtp is None:
        return
    try:
        smtp.quit()
    except Exception:
        try:
            smtp.close()
        except Exception:
            pass


def _connection_test(p: dict, host: str, port: int, security: str, timeout: float) -> dict:
    smtp = None
    started = time.perf_counter()
    try:
        smtp, (code, banner) = _open(p, host, port, security, timeout)
        result = {
            "ok": True, "banner": f"{code} {banner}", "ehlo": _dec(smtp.ehlo_resp or b""),
            "features": {k: v for k, v in smtp.esmtp_features.items()},
            "tls": _tls_info(smtp), "starttls_available": smtp.has_extn("starttls"),
        }
        user, password = str(p.get("username") or ""), str(p.get("password") or "")
        if user and password:
            smtp.login(user, password)
            result["auth"] = "Authentification réussie"
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        _close(smtp)
        result["transcript"] = smtp.transcript
        return result
    except Exception as exc:
        out = _failure(exc, smtp)
        _close(smtp)
        out["transcript"] = getattr(smtp, "transcript", [])
        return out


def _addresses(raw) -> list[str]:
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw or ""))
    out = []
    for a in items:
        a = str(a).strip()
        if not a:
            continue
        addr = email.utils.parseaddr(a)[1]
        if not ADDR_RE.match(addr or ""):
            raise ValueError(f"Adresse e-mail invalide : {a}")
        out.append(addr)
    return out


def _build_message(p: dict) -> tuple[EmailMessage, str, list[str]]:
    sender = str(p.get("from", "")).strip()
    from_addr = email.utils.parseaddr(sender)[1]
    if not ADDR_RE.match(from_addr or ""):
        raise ValueError("Adresse de l'expéditeur invalide")
    to = _addresses(p.get("to"))
    cc = _addresses(p.get("cc"))
    if not to:
        raise ValueError("Au moins un destinataire est requis")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = str(p.get("subject") or "(sans objet)")
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=from_addr.split("@")[-1])
    msg["X-Mailer"] = "LedroideDesktop SMTP Tester"
    text = str(p.get("text") or "")
    html = str(p.get("html") or "")
    msg.set_content(text or ("Ce message contient une version HTML." if html else ""))
    if html:
        msg.add_alternative(html, subtype="html")
    att = p.get("attachment")
    if isinstance(att, dict) and att.get("data"):
        try:
            data = base64.b64decode(att["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Pièce jointe illisible (base64 invalide)")
        if len(data) > MAX_ATTACHMENT:
            raise ValueError("Pièce jointe trop volumineuse (20 Mo maximum)")
        name = re.sub(r"[\\/\r\n\"]", "_", str(att.get("name") or "piece-jointe"))[:150]
        ctype = att.get("type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg, from_addr, to + cc


def _send(p: dict, host: str, port: int, security: str, timeout: float, msg: EmailMessage, from_addr: str, rcpts: list[str]) -> dict:
    smtp = None
    started = time.perf_counter()
    try:
        smtp, _ = _open(p, host, port, security, timeout)
        user, password = str(p.get("username") or ""), str(p.get("password") or "")
        if user:
            smtp.login(user, password)
        refused = smtp.send_message(msg, from_addr, rcpts)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        tls = _tls_info(smtp)
        _close(smtp)
        return {"ok": True, "refused": {k: [c, _dec(m)] for k, (c, m) in refused.items()},
                "accepted": [r for r in rcpts if r not in refused], "message_id": msg["Message-ID"],
                "size": len(msg.as_bytes()), "elapsed_ms": elapsed, "tls": tls, "transcript": smtp.transcript}
    except Exception as exc:
        out = _failure(exc, smtp)
        _close(smtp)
        out["transcript"] = getattr(smtp, "transcript", [])
        return out


@module.on("test_connection")
async def test_connection(payload: dict):
    try:
        host, port, security, timeout = _validate_server(payload)
    except ValueError as exc:
        return _err(str(exc), "BAD_REQUEST")
    log.info("smtp test connection %s:%s (%s)", host, port, security)
    return await asyncio.to_thread(_connection_test, payload, host, port, security, timeout)


@module.on("send_test")
async def send_test(payload: dict):
    try:
        host, port, security, timeout = _validate_server(payload)
        msg, from_addr, rcpts = _build_message(payload)
    except ValueError as exc:
        return _err(str(exc), "BAD_REQUEST")
    log.info("smtp send test via %s:%s (%s) to %d recipient(s)", host, port, security, len(rcpts))
    return await asyncio.to_thread(_send, payload, host, port, security, timeout, msg, from_addr, rcpts)


# ── Faux serveur SMTP (capture) ──────────────────────────────────────────────

class Capture:
    def __init__(self, server: asyncio.base_events.Server, host: str, port: int):
        self.server = server
        self.host = host
        self.port = port
        self.started = time.time()
        self.sessions: set[asyncio.Task] = set()


_capture: Capture | None = None
_schema_ready = False


async def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    await module.db.executescript("""
        CREATE TABLE IF NOT EXISTS mails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at REAL NOT NULL,
            peer TEXT, helo TEXT, mail_from TEXT, rcpt_to TEXT,
            subject TEXT, from_hdr TEXT, to_hdr TEXT,
            size INTEGER, attachments INTEGER DEFAULT 0, has_html INTEGER DEFAULT 0,
            seen INTEGER DEFAULT 0, raw BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS mails_received ON mails(received_at);
    """)
    _schema_ready = True


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


def _hdr(msg, name: str) -> str:
    try:
        v = msg.get(name)
        return str(v) if v is not None else ""
    except Exception:
        raw = msg.get_all(name, [])
        return str(raw[0]) if raw else ""


def _is_attachment(part) -> bool:
    if part.is_multipart():
        return False
    disp = part.get_content_disposition()
    if disp == "attachment":
        return True
    if part.get_filename():
        return True
    return part.get_content_maintype() not in ("text",) and disp != "inline" and not part.get("Content-ID")


def summarize(raw: bytes) -> dict:
    msg = _parse(raw)
    parts = list(msg.walk())
    return {
        "subject": _hdr(msg, "Subject"),
        "from_hdr": _hdr(msg, "From"),
        "to_hdr": _hdr(msg, "To"),
        "attachments": sum(1 for p in parts if _is_attachment(p)),
        "has_html": any(p.get_content_type() == "text/html" and not _is_attachment(p) for p in parts),
    }


def _content(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", "replace")


def detail(raw: bytes) -> dict:
    msg = _parse(raw)
    text = html = None
    attachments, cids = [], {}
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if _is_attachment(part):
            data = part.get_payload(decode=True) or b""
            attachments.append({"index": idx, "filename": part.get_filename() or f"piece-{idx}",
                                "content_type": ctype, "size": len(data)})
            continue
        cid = (part.get("Content-ID") or "").strip("<> ")
        if cid and part.get_content_maintype() == "image":
            data = part.get_payload(decode=True) or b""
            if len(data) <= 2 * 1024 * 1024:
                cids[cid] = f"data:{ctype};base64,{base64.b64encode(data).decode()}"
            continue
        if ctype == "text/plain" and text is None:
            text = _content(part)
        elif ctype == "text/html" and html is None:
            html = _content(part)
    if html and cids:
        html = re.sub(r"cid:([^\"'\s)>]+)", lambda m: cids.get(m.group(1), m.group(0)), html)
    headers = [[k, str(v)] for k, v in msg.raw_items()]
    return {"headers": headers, "text": text, "html": html, "attachments": attachments}


async def _store(raw: bytes, peer: str, helo: str, mail_from: str, rcpts: list[str]) -> dict:
    await _ensure_schema()
    try:
        info = await asyncio.to_thread(summarize, raw)
    except Exception:
        log.warning("capture: message illisible")
        info = {"subject": "", "from_hdr": "", "to_hdr": "", "attachments": 0, "has_html": False}
    now = time.time()
    await module.db.execute(
        "INSERT INTO mails(received_at, peer, helo, mail_from, rcpt_to, subject, from_hdr, to_hdr, size, attachments, has_html, raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, peer, helo, mail_from, json.dumps(rcpts), info["subject"], info["from_hdr"], info["to_hdr"],
         len(raw), info["attachments"], int(info["has_html"]), raw))
    row = await module.db.fetch_one("SELECT " + SUMMARY_COLS + " FROM mails ORDER BY id DESC LIMIT 1")
    keep = int(module.config.get("max_messages", 200))
    await module.db.execute("DELETE FROM mails WHERE id NOT IN (SELECT id FROM mails ORDER BY id DESC LIMIT ?)", (keep,))
    return _row(row)


SUMMARY_COLS = "id, received_at, peer, helo, mail_from, rcpt_to, subject, from_hdr, to_hdr, size, attachments, has_html, seen"


def _row(r: dict) -> dict:
    r = dict(r)
    try:
        r["rcpt_to"] = json.loads(r.get("rcpt_to") or "[]")
    except ValueError:
        r["rcpt_to"] = []
    r["has_html"] = bool(r.get("has_html"))
    r["seen"] = bool(r.get("seen"))
    return r


def _addr_arg(arg: str, keyword: str) -> str | None:
    m = re.match(rf"{keyword}:\s*<([^>]*)>", arg, re.I) or re.match(rf"{keyword}:\s*(\S+)", arg, re.I)
    return m.group(1).strip() if m else None


async def _session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer_info = writer.get_extra_info("peername") or ("?", 0)
    peer = f"{peer_info[0]}:{peer_info[1]}"
    max_size = int(module.config.get("max_size_mb", 25)) * 1024 * 1024
    helo, mail_from, rcpts = "", None, []

    async def reply(line: str):
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    async def readline(timeout: float = 300) -> bytes:
        return await asyncio.wait_for(reader.readline(), timeout)

    try:
        await reply(f"220 {SERVER_NAME} ESMTP LedroideDesktop capture server (no mail is delivered)")
        while True:
            try:
                line = await readline()
            except ValueError:
                await reply("500 5.5.2 Line too long")
                break
            if not line:
                break
            if len(line) > LINE_LIMIT + 2:
                await reply("500 5.5.2 Line too long")
                continue
            cmdline = line.decode("utf-8", "replace").rstrip("\r\n")
            verb, _, arg = cmdline.partition(" ")
            verb = verb.upper()
            if verb == "HELO":
                helo = arg.strip()
                mail_from, rcpts = None, []
                await reply(f"250 {SERVER_NAME}")
            elif verb == "EHLO":
                helo = arg.strip()
                mail_from, rcpts = None, []
                writer.write((f"250-{SERVER_NAME} Hello {peer_info[0]}\r\n250-SIZE {max_size}\r\n250-8BITMIME\r\n"
                              "250-SMTPUTF8\r\n250-AUTH PLAIN LOGIN\r\n250 HELP\r\n").encode())
                await writer.drain()
            elif verb == "MAIL":
                if mail_from is not None:
                    await reply("503 5.5.1 Sender already specified")
                    continue
                addr = _addr_arg(arg, "FROM")
                if addr is None:
                    await reply("501 5.5.4 Syntax: MAIL FROM:<address>")
                    continue
                size = re.search(r"\bSIZE=(\d+)", arg, re.I)
                if size and int(size.group(1)) > max_size:
                    await reply("552 5.3.4 Message too big")
                    continue
                mail_from = addr
                await reply("250 2.1.0 OK")
            elif verb == "RCPT":
                if mail_from is None:
                    await reply("503 5.5.1 Need MAIL command")
                    continue
                addr = _addr_arg(arg, "TO")
                if not addr:
                    await reply("501 5.5.4 Syntax: RCPT TO:<address>")
                    continue
                if len(rcpts) >= 100:
                    await reply("452 4.5.3 Too many recipients")
                    continue
                rcpts.append(addr)
                await reply("250 2.1.5 OK")
            elif verb == "DATA":
                if not rcpts:
                    await reply("503 5.5.1 Need RCPT command")
                    continue
                await reply("354 End data with <CR><LF>.<CR><LF>")
                chunks, size, too_big = [], 0, False
                while True:
                    try:
                        data_line = await readline(120)
                    except ValueError:
                        too_big = True
                        continue
                    if not data_line:
                        return
                    if data_line in (b".\r\n", b".\n"):
                        break
                    if data_line.startswith(b"."):
                        data_line = data_line[1:]
                    size += len(data_line)
                    if size > max_size:
                        too_big = True
                    elif not too_big:
                        chunks.append(data_line)
                if too_big:
                    await reply("552 5.3.4 Message too big")
                else:
                    raw = b"".join(chunks)
                    summary = await _store(raw, peer, helo, mail_from, rcpts)
                    log.info("capture: mail #%s reçu (%d octets)", summary["id"], len(raw))
                    await module.events.emit("mail", summary)
                    await reply(f"250 2.0.0 OK captured as #{summary['id']}")
                mail_from, rcpts = None, []
            elif verb == "RSET":
                mail_from, rcpts = None, []
                await reply("250 2.0.0 OK")
            elif verb == "NOOP":
                await reply("250 2.0.0 OK")
            elif verb == "QUIT":
                await reply("221 2.0.0 Bye")
                break
            elif verb == "VRFY":
                await reply("252 2.1.5 Cannot verify user, but will accept message")
            elif verb == "HELP":
                await reply("214 Commands: HELO EHLO MAIL RCPT DATA RSET NOOP QUIT AUTH VRFY HELP")
            elif verb == "AUTH":
                mech, _, initial = arg.strip().partition(" ")
                mech = mech.upper()
                if mech == "PLAIN":
                    if not initial:
                        await reply("334 ")
                        await readline(60)
                elif mech == "LOGIN":
                    if not initial:
                        await reply("334 VXNlcm5hbWU6")
                        await readline(60)
                    await reply("334 UGFzc3dvcmQ6")
                    await readline(60)
                else:
                    await reply("504 5.5.4 Unrecognized authentication type")
                    continue
                await reply("235 2.7.0 Authentication accepted (not verified)")
            elif verb == "STARTTLS":
                await reply("454 4.7.0 TLS not available on capture server")
            else:
                await reply("502 5.5.2 Command not recognized")
    except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("capture: erreur de session")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception:
            pass


def _server_status() -> dict:
    if _capture is None:
        return {"running": False}
    return {"running": True, "host": _capture.host, "port": _capture.port, "since": _capture.started}


@module.on("server_start")
async def server_start(payload: dict):
    global _capture
    if _capture is not None:
        return _err(f"Le serveur de capture tourne déjà sur {_capture.host}:{_capture.port}", "ALREADY_RUNNING")
    host = str(payload.get("host") or "127.0.0.1").strip()
    if host not in ("127.0.0.1", "0.0.0.0", "::1", "localhost"):
        return _err("Adresse d'écoute non autorisée (127.0.0.1, ::1 ou 0.0.0.0)", "BAD_HOST")
    try:
        port = int(payload.get("port") or 2525)
    except (TypeError, ValueError):
        return _err("Port invalide", "BAD_PORT")
    if not 1 <= port <= 65535:
        return _err("Port invalide (1-65535)", "BAD_PORT")
    await _ensure_schema()

    async def client_connected(reader, writer):
        cap, task = _capture, asyncio.current_task()
        if cap is not None and task is not None:
            cap.sessions.add(task)
        try:
            await _session(reader, writer)
        finally:
            if cap is not None:
                cap.sessions.discard(task)

    try:
        server = await asyncio.start_server(client_connected, host, port, limit=LINE_LIMIT * 4)
    except OSError as exc:
        return _err(f"Impossible d'écouter sur {host}:{port} : {exc.strerror or exc} (port déjà utilisé ?)", "BIND_FAILED")
    actual = server.sockets[0].getsockname()[1] if server.sockets else port
    _capture = Capture(server, host, actual)
    log.info("capture server listening on %s:%s", host, actual)
    status = _server_status()
    await module.events.emit("server", status)
    return status


async def _stop_capture():
    global _capture
    cap = _capture
    if cap is None:
        return
    _capture = None
    cap.server.close()
    for task in list(cap.sessions):
        task.cancel()
    try:
        await asyncio.wait_for(cap.server.wait_closed(), 3)
    except (asyncio.TimeoutError, Exception):
        pass
    await module.events.emit("server", _server_status())


@module.on("server_stop")
async def server_stop(payload: dict):
    if _capture is None:
        return _err("Le serveur de capture n'est pas démarré", "NOT_RUNNING")
    await _stop_capture()
    return _server_status()


@module.on("server_status")
async def server_status(payload: dict):
    return _server_status()


@module.on("mail_list")
async def mail_list(payload: dict):
    await _ensure_schema()
    q = str(payload.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        rows = await module.db.fetch_all(
            f"SELECT {SUMMARY_COLS} FROM mails WHERE subject LIKE ? OR from_hdr LIKE ? OR to_hdr LIKE ? OR mail_from LIKE ? OR rcpt_to LIKE ? "
            "ORDER BY id DESC LIMIT 1000", (like,) * 5)
    else:
        rows = await module.db.fetch_all(f"SELECT {SUMMARY_COLS} FROM mails ORDER BY id DESC LIMIT 1000")
    return {"items": [_row(r) for r in rows]}


async def _raw(payload: dict) -> tuple[dict | None, bytes | None]:
    await _ensure_schema()
    try:
        mail_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return None, None
    row = await module.db.fetch_one(f"SELECT {SUMMARY_COLS}, raw FROM mails WHERE id=?", (mail_id,))
    if row is None:
        return None, None
    raw = row.pop("raw")
    return row, bytes(raw)


@module.on("mail_get")
async def mail_get(payload: dict):
    row, raw = await _raw(payload)
    if row is None:
        return _err("Mail introuvable", "NOT_FOUND")
    if not row.get("seen"):
        await module.db.execute("UPDATE mails SET seen=1 WHERE id=?", (row["id"],))
        row["seen"] = 1
    info = await asyncio.to_thread(detail, raw)
    return {**_row(row), **info}


@module.on("mail_raw")
async def mail_raw(payload: dict):
    row, raw = await _raw(payload)
    if row is None:
        return _err("Mail introuvable", "NOT_FOUND")
    return {"id": row["id"], "source": raw.decode("utf-8", "replace"), "data": base64.b64encode(raw).decode()}


@module.on("mail_attachment")
async def mail_attachment(payload: dict):
    row, raw = await _raw(payload)
    if row is None:
        return _err("Mail introuvable", "NOT_FOUND")

    def extract():
        msg = _parse(raw)
        for idx, part in enumerate(msg.walk()):
            if idx == int(payload.get("index", -1)) and not part.is_multipart():
                return {"filename": part.get_filename() or f"piece-{idx}", "content_type": part.get_content_type(),
                        "data": base64.b64encode(part.get_payload(decode=True) or b"").decode()}
        return None
    try:
        att = await asyncio.to_thread(extract)
    except (TypeError, ValueError):
        att = None
    return att or _err("Pièce jointe introuvable", "NOT_FOUND")


@module.on("mail_delete")
async def mail_delete(payload: dict):
    await _ensure_schema()
    try:
        mail_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err("Identifiant invalide", "BAD_REQUEST")
    n = await module.db.execute("DELETE FROM mails WHERE id=?", (mail_id,))
    return {"ok": bool(n)}


@module.on("mail_clear")
async def mail_clear(payload: dict):
    await _ensure_schema()
    n = await module.db.execute("DELETE FROM mails")
    return {"ok": True, "deleted": n}


SECRET_KEYS = {"password"}


@module.on("settings_get")
async def settings_get(payload: dict):
    return await module.db.get("settings", "last") or {"_id": "last", "client": {}, "server": {}}


@module.on("settings_save")
async def settings_save(payload: dict):
    client = {k: v for k, v in (payload.get("client") or {}).items() if k not in SECRET_KEYS and k != "attachment"}
    server = {k: v for k, v in (payload.get("server") or {}).items() if k in ("host", "port")}
    return await module.db.save("settings", {"_id": "last", "client": client, "server": server})


@module.on_shutdown
async def shutdown():
    await _stop_capture()
