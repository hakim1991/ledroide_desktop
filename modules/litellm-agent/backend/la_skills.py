import io
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SKILL_FILE = "SKILL.md"
MAX_SKILL_CHARS = 200_000
MAX_FILE_CHARS = 100_000
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ZIP_FILES = 500


class SkillError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    return s[:64]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    key = None
    for line in lines[1:end]:
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1).lower()
            val = m.group(2).strip()
            meta[key] = "" if val in (">", "|", ">-", "|-") else val.strip("'\"")
        elif key and line.strip():
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def _first_paragraph(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        t = " ".join(l.strip() for l in block.splitlines() if l.strip() and not l.lstrip().startswith("#"))
        if t:
            return t[:300]
    return ""


def _skill_dir(root: Path, name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise SkillError("INVALID_NAME", "Nom de skill invalide (minuscules, chiffres et tirets, 64 caractères max)")
    return root / name


def info(root: Path, name: str) -> dict | None:
    d = _skill_dir(root, name)
    f = d / SKILL_FILE
    if not f.is_file():
        return None
    text = f.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    files = [p for p in d.rglob("*") if p.is_file()]
    return {
        "name": name,
        "title": meta.get("name") or name,
        "description": (meta.get("description") or _first_paragraph(body))[:1024],
        "files": sorted(str(p.relative_to(d).as_posix()) for p in files if p.name != SKILL_FILE)[:200],
        "size": sum(p.stat().st_size for p in files),
    }


def list_skills(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and NAME_RE.match(d.name):
            i = info(root, d.name)
            if i:
                out.append(i)
    return out


def read_skill(root: Path, name: str) -> str:
    f = _skill_dir(root, name) / SKILL_FILE
    if not f.is_file():
        raise SkillError("NOT_FOUND", f"Skill introuvable : {name}")
    return f.read_text(encoding="utf-8", errors="replace")[:MAX_SKILL_CHARS]


def read_skill_file(root: Path, name: str, rel: str) -> str:
    d = _skill_dir(root, name).resolve()
    target = (d / rel).resolve()
    if d not in target.parents:
        raise SkillError("FORBIDDEN", "Chemin hors du dossier du skill")
    if not target.is_file():
        raise SkillError("NOT_FOUND", f"Fichier introuvable dans le skill {name} : {rel}")
    data = target.read_bytes()
    if b"\x00" in data[:4096]:
        raise SkillError("BINARY", f"Fichier binaire ({len(data)} octets), non lisible comme texte")
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n\n[… tronqué : {len(text)} caractères au total]"
    return text


def save_skill(root: Path, name: str, content: str) -> dict:
    if len(content) > MAX_SKILL_CHARS:
        raise SkillError("TOO_LARGE", "SKILL.md trop volumineux")
    d = _skill_dir(root, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILE).write_text(content, encoding="utf-8")
    return info(root, name)


def delete_skill(root: Path, name: str) -> bool:
    d = _skill_dir(root, name)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def import_zip(root: Path, data: bytes, overwrite: bool = False) -> list[str]:
    if len(data) > MAX_ZIP_BYTES:
        raise SkillError("TOO_LARGE", "Archive trop volumineuse (20 Mo max)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("BAD_ZIP", "Archive zip invalide")
    with zf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        if len(entries) > MAX_ZIP_FILES or sum(i.file_size for i in entries) > MAX_ZIP_BYTES * 3:
            raise SkillError("TOO_LARGE", "Archive trop volumineuse")
        for i in entries:
            p = PurePosixPath(i.filename.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts:
                raise SkillError("BAD_ZIP", f"Chemin interdit dans l'archive : {i.filename}")
        skill_roots = sorted({str(PurePosixPath(i.filename.replace("\\", "/")).parent)
                              for i in entries if PurePosixPath(i.filename).name == SKILL_FILE})
        if not skill_roots:
            raise SkillError("NO_SKILL", "Aucun fichier SKILL.md dans l'archive")
        imported = []
        for sroot in skill_roots:
            prefix = "" if sroot == "." else sroot + "/"
            meta, _ = parse_frontmatter(zf.read(prefix + SKILL_FILE).decode("utf-8", errors="replace"))
            name = slugify(meta.get("name") or (PurePosixPath(sroot).name if sroot != "." else "skill"))
            if not NAME_RE.match(name):
                name = "skill"
            dest = root / name
            if dest.exists():
                if not overwrite:
                    raise SkillError("EXISTS", f"Le skill « {name} » existe déjà")
                shutil.rmtree(dest)
            for i in entries:
                fn = i.filename.replace("\\", "/")
                if not fn.startswith(prefix):
                    continue
                rel = fn[len(prefix):]
                if any(rel.startswith(o[len(prefix):] + "/") for o in skill_roots if o != sroot and o.startswith(prefix)):
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(i))
            imported.append(name)
        return imported
