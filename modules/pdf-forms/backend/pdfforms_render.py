import base64
import io
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field as PField, field_validator

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DATA_TYPES = {"text", "textarea", "number", "date", "email", "phone", "select", "radio", "checkbox", "table", "signature"}
INLINE_TYPES = {"text", "textarea", "number", "date", "email", "phone", "select", "radio", "checkbox"}
MAX_LOGO_BYTES = 2 * 1024 * 1024

FONT_FILES = {
    "arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "segoe": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "times": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
}
CORE_FALLBACK = {"times": "times", "georgia": "times"}
FORMATS = {"A4", "A5", "A3", "Letter", "Legal"}


class RenderError(Exception):
    def __init__(self, message: str, code: str = "RENDER_ERROR", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


class Column(BaseModel):
    label: str = PField("", max_length=80)
    type: Literal["text", "number"] = "text"
    width: float = PField(1, ge=0.2, le=10)
    total: bool = False
    decimals: int | None = PField(None, ge=0, le=6)


class FormField(BaseModel):
    key: str = PField("", max_length=64)
    label: str = PField("", max_length=200)
    type: Literal["section", "paragraph", "text", "textarea", "number", "date", "email", "phone",
                  "select", "radio", "checkbox", "table", "signature", "pagebreak", "spacer"] = "text"
    required: bool = False
    options: list[str] = PField(default_factory=list, max_length=100)
    default: Any = None
    placeholder: str = PField("", max_length=200)
    help: str = PField("", max_length=300)
    width: Literal["full", "half"] = "full"
    text: str = PField("", max_length=20000)
    decimals: int = PField(2, ge=0, le=6)
    unit: str = PField("", max_length=12)
    columns: list[Column] = PField(default_factory=list, max_length=12)
    min_rows: int = PField(1, ge=0, le=50)


class Header(BaseModel):
    enabled: bool = True
    logo: str = ""
    logo_height: float = PField(18, ge=5, le=60)
    logo_position: Literal["left", "right"] = "left"
    title: str = PField("", max_length=300)
    subtitle: str = PField("", max_length=300)
    text: str = PField("", max_length=2000)
    align: Literal["L", "C", "R"] = "L"
    line: bool = True
    first_page_only: bool = False


class Footer(BaseModel):
    enabled: bool = True
    left: str = PField("", max_length=300)
    center: str = PField("", max_length=300)
    right: str = PField("Page {{page}} / {{pages}}", max_length=300)
    line: bool = True


class Page(BaseModel):
    format: str = "A4"
    orientation: Literal["P", "L"] = "P"
    margin: float = PField(15, ge=5, le=40)
    font: str = "arial"
    font_size: float = PField(10, ge=7, le=16)
    accent: str = "#4f46e5"
    style: Literal["stacked", "table"] = "stacked"
    hide_empty: bool = False
    show_title: bool = True

    @field_validator("format")
    @classmethod
    def _fmt(cls, v):
        if v not in FORMATS:
            raise ValueError("format de page inconnu")
        return v

    @field_validator("accent")
    @classmethod
    def _accent(cls, v):
        return v if HEX_RE.match(v or "") else "#4f46e5"


class Template(BaseModel):
    name: str = PField(..., min_length=1, max_length=120)
    description: str = PField("", max_length=500)
    doc_title: str = PField("{{template}}", max_length=300)
    file_name: str = PField("{{doc_title}} {{date}}", max_length=200)
    page: Page = PField(default_factory=Page)
    header: Header = PField(default_factory=Header)
    footer: Footer = PField(default_factory=Footer)
    fields: list[FormField] = PField(default_factory=list, max_length=300)

    @field_validator("fields")
    @classmethod
    def _keys(cls, fields):
        seen = set()
        for f in fields:
            if f.type in DATA_TYPES:
                if not KEY_RE.match(f.key):
                    raise ValueError(f"clé invalide pour « {f.label or f.type} » (lettres, chiffres, _ ; pas de chiffre en tête)")
                if f.key in seen:
                    raise ValueError(f"clé en double : {f.key}")
                seen.add(f.key)
            if f.type == "table" and not f.columns:
                raise ValueError(f"le tableau « {f.label or f.key} » n'a aucune colonne")
        return fields


def validation_message(exc) -> str:
    parts = []
    for e in exc.errors()[:4]:
        loc = ".".join(str(p) for p in e.get("loc", ()))
        msg = str(e.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{loc} : {msg}" if loc else msg)
    return "Modèle invalide — " + " ; ".join(parts)


# ── Values ───────────────────────────────────────────────────────────────────

def parse_number(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
    if s == "":
        return None
    return float(s)


def fmt_number(v: float, decimals: int, unit: str = "") -> str:
    s = f"{v:,.{decimals}f}".replace(",", "\u202f").replace(".", ",").replace("\u202f", " ")
    return f"{s} {unit}".strip() if unit else s


def fmt_date(raw) -> str:
    s = str(raw or "").strip()
    try:
        return date.fromisoformat(s[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return s


def is_empty(field: FormField, value) -> bool:
    if field.type == "checkbox":
        return False
    if field.type == "table":
        return not any(any(str(c).strip() for c in row) for row in (value or []))
    return value is None or str(value).strip() == ""


def normalize_values(tpl: Template, values: dict, strict: bool = True) -> dict:
    if not isinstance(values, dict):
        raise RenderError("Valeurs invalides", "INVALID_VALUES")
    out, missing, bad = {}, [], []
    for f in tpl.fields:
        if f.type not in DATA_TYPES:
            continue
        v = values.get(f.key, f.default)
        if f.type == "checkbox":
            v = v in (True, 1, "1", "true", "True", "oui", "Oui", "on", "x", "X")
        elif f.type == "table":
            rows = v if isinstance(v, list) else []
            n = len(f.columns)
            clean = []
            for row in rows[:500]:
                cells = list(row)[:n] if isinstance(row, (list, tuple)) else []
                cells = ["" if c is None else str(c)[:2000] for c in cells] + [""] * (n - len(cells))
                if any(c.strip() for c in cells):
                    clean.append(cells)
            for row in clean:
                for i, col in enumerate(f.columns):
                    if col.type == "number" and row[i].strip():
                        try:
                            parse_number(row[i])
                        except ValueError:
                            bad.append(f"{f.label or f.key} (colonne {col.label or i + 1})")
            v = clean
        elif f.type == "number":
            try:
                parse_number(v)
            except (ValueError, TypeError):
                bad.append(f.label or f.key)
            v = "" if v is None else str(v).strip()
        else:
            v = "" if v is None else str(v)[:20000]
        if f.required and is_empty(f, v):
            missing.append(f.label or f.key)
        out[f.key] = v
    if missing and strict:
        raise RenderError("Champs obligatoires manquants : " + ", ".join(missing), "REQUIRED_FIELDS", missing)
    if bad:
        raise RenderError("Nombre invalide : " + ", ".join(dict.fromkeys(bad)), "INVALID_NUMBER", bad)
    return out


def table_totals(f: FormField, rows: list[list[str]]) -> list[float | None]:
    totals = []
    for i, col in enumerate(f.columns):
        if col.type == "number" and col.total:
            totals.append(sum((parse_number(r[i]) or 0.0) for r in rows))
        else:
            totals.append(None)
    return totals


def display_value(f: FormField, v) -> str:
    if f.type == "checkbox":
        return "Oui" if v else "Non"
    if v is None or v == "":
        return ""
    if f.type == "number":
        n = parse_number(v)
        return "" if n is None else fmt_number(n, f.decimals, f.unit)
    if f.type == "date":
        return fmt_date(v)
    return str(v)


def build_vars(tpl: Template, values: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    vars_ = {"date": now.strftime("%d/%m/%Y"), "heure": now.strftime("%H:%M"),
             "datetime": now.strftime("%d/%m/%Y %H:%M"), "annee": str(now.year), "template": tpl.name}
    for f in tpl.fields:
        if f.type not in DATA_TYPES or f.key not in values:
            continue
        if f.type == "table":
            rows = values[f.key]
            vars_[f.key] = str(len(rows))
            for i, t in enumerate(table_totals(f, rows)):
                if t is not None:
                    c = f.columns[i]
                    s = fmt_number(t, f.decimals if c.decimals is None else c.decimals, f.unit)
                    vars_[f"{f.key}_total"] = s
                    vars_[f"{f.key}_total_{i + 1}"] = s
        else:
            vars_[f.key] = display_value(f, values[f.key])
    return vars_


def subst(text: str, vars_: dict) -> str:
    return VAR_RE.sub(lambda m: vars_.get(m.group(1), ""), text or "")


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)[:120].strip()
    if not name or name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        name = "document"
    return name + ".pdf"


# ── PDF ──────────────────────────────────────────────────────────────────────

_LATIN = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                        "\u2026": "...", "\u20ac": "EUR", "\u2022": "-", "\u00a0": " ", "\u202f": " ", "\u0153": "oe",
                        "\u0152": "OE"})


def _hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _mix(rgb, t: float) -> tuple[int, int, int]:
    return tuple(round(c + (255 - c) * t) for c in rgb)


def decode_logo(data_url: str) -> bytes | None:
    if not data_url:
        return None
    m = re.match(r"^data:image/[\w.+-]+;base64,(.+)$", data_url, re.S)
    if not m:
        raise RenderError("Logo invalide (image attendue)", "INVALID_LOGO")
    try:
        raw = base64.b64decode(m.group(1), validate=False)
    except ValueError:
        raise RenderError("Logo invalide (base64)", "INVALID_LOGO")
    if len(raw) > MAX_LOGO_BYTES:
        raise RenderError("Logo trop volumineux (2 Mo max.)", "LOGO_TOO_LARGE")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            if im.format in ("PNG", "JPEG"):
                return raw
            buf = io.BytesIO()
            im.convert("RGBA").save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        raise RenderError("Logo illisible (PNG, JPEG, WebP, GIF…)", "INVALID_LOGO")


def fonts_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def render_pdf(tpl: Template, values: dict, now: datetime | None = None, strict: bool = True) -> tuple[bytes, int]:
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos, MethodReturnValue
        from fpdf.fonts import FontFace
    except ImportError:
        raise RenderError("La bibliothèque fpdf2 n'est pas installée (réactivez le module ou installez-la depuis « Bibliothèques »)",
                          "DEPENDENCY_MISSING")

    values = normalize_values(tpl, values, strict)
    vars_ = build_vars(tpl, values, now)
    doc_title = subst(tpl.doc_title, vars_).strip()
    vars_["doc_title"] = doc_title
    page, hdr, ftr = tpl.page, tpl.header, tpl.footer
    logo = decode_logo(hdr.logo) if hdr.enabled else None
    logo_ratio = 1.0
    if logo:
        from PIL import Image
        with Image.open(io.BytesIO(logo)) as im:
            logo_ratio = im.width / max(im.height, 1)
    accent = _hex_rgb(page.accent)
    light = _mix(accent, 0.88)
    dark, muted, rule = (24, 24, 27), (100, 100, 110), (205, 205, 212)
    fs = page.font_size
    lh = fs * 0.5

    class Doc(FPDF):
        def header(self):
            if hdr.enabled and not (hdr.first_page_only and self.page_no() > 1):
                draw_header(self)
            else:
                self.set_y(page.margin)

        def footer(self):
            if ftr.enabled:
                draw_footer(self)

    pdf = Doc(orientation=page.orientation, unit="mm", format=page.format)
    pdf.set_margins(page.margin, page.margin, page.margin)
    footer_space = 11 if ftr.enabled else 0
    pdf.set_auto_page_break(True, margin=page.margin + footer_space)
    pdf.alias_nb_pages("{nb}")
    pdf.set_title(doc_title or tpl.name)
    pdf.set_creator("LedroideDesktop — Formulaires PDF")

    family = "helvetica"
    unicode_font = False
    files = FONT_FILES.get(page.font)
    if files:
        paths = [fonts_dir() / f for f in files]
        if all(p.is_file() for p in paths):
            for style, p in zip(("", "B", "I", "BI"), paths):
                pdf.add_font("doc", style, str(p))
            family, unicode_font = "doc", True
        else:
            family = CORE_FALLBACK.get(page.font, "helvetica")
    elif page.font in ("helvetica", "times", "courier"):
        family = page.font

    def T(s: str) -> str:
        s = s or ""
        if unicode_font:
            return s
        s = s.translate(_LATIN)
        return s.encode("latin-1", "replace").decode("latin-1")

    def font(style: str = "", size: float | None = None, color=dark):
        pdf.set_font(family, style, size or fs)
        pdf.set_text_color(*color)

    def hf(text: str) -> str:
        return T(subst(text, {**vars_, "page": str(pdf.page_no()), "pages": "{nb}"}))

    def draw_header(p):
        top = page.margin * 0.7
        x0, w = p.l_margin, p.epw
        tx, tw = x0, w
        logo_bottom = top
        if logo:
            lw = min(hdr.logo_height * logo_ratio, w * 0.4)
            lx = x0 if hdr.logo_position == "left" else x0 + w - lw
            p.image(io.BytesIO(logo), x=lx, y=top, w=lw)
            logo_bottom = top + lw / logo_ratio
            tw = w - lw - 6
            tx = x0 + lw + 6 if hdr.logo_position == "left" else x0
        p.set_xy(tx, top)
        title, sub, text = hf(hdr.title), hf(hdr.subtitle), hf(hdr.text)
        if title:
            font("B", fs + 5)
            p.multi_cell(tw, (fs + 5) * 0.45, title, align=hdr.align, new_x=XPos.LEFT, new_y=YPos.NEXT)
        if sub:
            p.set_x(tx)
            font("", fs + 1, accent)
            p.multi_cell(tw, (fs + 1) * 0.5, sub, align=hdr.align, new_x=XPos.LEFT, new_y=YPos.NEXT)
        if text:
            p.set_x(tx)
            font("", fs - 1.5, muted)
            p.multi_cell(tw, (fs - 1.5) * 0.45, text, align=hdr.align, new_x=XPos.LEFT, new_y=YPos.NEXT)
        bottom = max(p.get_y(), logo_bottom) + 2.5
        if hdr.line:
            p.set_draw_color(*accent)
            p.set_line_width(0.5)
            p.line(x0, bottom, x0 + w, bottom)
        p.set_y(bottom + 6)
        font()

    def draw_footer(p):
        y = p.h - page.margin - 7
        if ftr.line:
            p.set_draw_color(*rule)
            p.set_line_width(0.2)
            p.line(p.l_margin, y, p.l_margin + p.epw, y)
        font("", fs - 2, muted)
        for text, align in ((ftr.left, "L"), (ftr.center, "C"), (ftr.right, "R")):
            t = hf(text).replace("\n", " · ")
            if t:
                p.set_xy(p.l_margin, y + 1.8)
                p.cell(p.epw, 5, t, align=align)
        font()

    def ensure_space(h: float):
        if pdf.get_y() + h > pdf.page_break_trigger and h < pdf.page_break_trigger - page.margin:
            pdf.add_page()

    def text_height(w: float, text: str, size: float, style: str = "") -> float:
        font(style, size)
        return pdf.multi_cell(w, size * 0.5, text or " ", dry_run=True, output=MethodReturnValue.HEIGHT)

    def block_height(f: FormField, w: float) -> float:
        lab = T(subst(f.label, vars_))
        h = text_height(w, lab, fs - 1.5, "B") + 1 if lab else 0
        if f.type == "signature":
            return h + 28
        return h + text_height(w, T(display_value(f, values.get(f.key)) or " "), fs) + 4

    def draw_block(f: FormField, x: float, w: float) -> float:
        lab = T(subst(f.label, vars_))
        if lab:
            font("B", fs - 1.5, muted)
            pdf.set_x(x)
            pdf.multi_cell(w, (fs - 1.5) * 0.5, lab, new_x=XPos.LEFT, new_y=YPos.NEXT)
            pdf.set_y(pdf.get_y() + 1)
        if f.type == "signature":
            top = pdf.get_y()
            bw = min(w, 85)
            pdf.set_draw_color(*rule)
            pdf.set_line_width(0.3)
            pdf.rect(x, top, bw, 24)
            if f.text:
                font("I", fs - 2.5, muted)
                pdf.set_xy(x + 2, top + 19)
                pdf.cell(bw - 4, 4, T(subst(f.text, vars_)))
            pdf.set_y(top + 28)
            return pdf.get_y()
        val = T(display_value(f, values.get(f.key)))
        font()
        pdf.set_x(x)
        pdf.multi_cell(w, lh, val or " ", new_x=XPos.LEFT, new_y=YPos.NEXT)
        yl = pdf.get_y() + 0.8
        pdf.set_draw_color(*rule)
        pdf.set_line_width(0.2)
        pdf.line(x, yl, x + w, yl)
        pdf.set_y(yl + 3.5)
        return pdf.get_y()

    def draw_pair(a: FormField, b: FormField | None):
        gap = 8
        w = (pdf.epw - gap) / 2
        ensure_space(max(block_height(a, w), block_height(b, w) if b else 0))
        y, start_page = pdf.get_y(), pdf.page_no()
        y1 = draw_block(a, pdf.l_margin, w)
        if not b:
            return
        if pdf.page_no() != start_page:
            draw_block(b, pdf.l_margin + w + gap, w)
            return
        pdf.set_y(y)
        y2 = draw_block(b, pdf.l_margin + w + gap, w)
        if pdf.page_no() == start_page:
            pdf.set_y(max(y1, y2))

    def draw_kv_table(fields: list[FormField]):
        rows = [(T(f.label), T(display_value(f, values.get(f.key)))) for f in fields]
        if not rows:
            return
        font()
        pdf.set_draw_color(*rule)
        pdf.set_line_width(0.2)
        with pdf.table(col_widths=(34, 66), first_row_as_headings=False, line_height=lh + 1.2,
                       padding=1.6, width=pdf.epw) as table:
            for lab, val in rows:
                r = table.row()
                r.cell(lab, style=FontFace(emphasis="BOLD", color=dark, fill_color=light))
                r.cell(val)
        pdf.set_y(pdf.get_y() + 4)

    def draw_table(f: FormField):
        rows = values.get(f.key) or []
        if page.hide_empty and not rows:
            return
        if f.label:
            ensure_space(lh * 4 + 8)
            font("B", fs - 1.5, muted)
            pdf.multi_cell(pdf.epw, (fs - 1.5) * 0.5, T(f.label), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_y(pdf.get_y() + 1)
        cols = f.columns
        aligns = tuple("RIGHT" if c.type == "number" else "LEFT" for c in cols)
        totals = table_totals(f, rows)
        font("", fs - 0.5)
        pdf.set_draw_color(*rule)
        pdf.set_line_width(0.2)
        head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=accent)
        with pdf.table(col_widths=tuple(c.width for c in cols), text_align=aligns, headings_style=head,
                       line_height=lh + 1, padding=1.4, width=pdf.epw, repeat_headings=1) as table:
            hr = table.row()
            for c in cols:
                hr.cell(T(c.label))
            body = rows or [[""] * len(cols) for _ in range(max(f.min_rows, 1))]
            for row in body:
                r = table.row()
                for i, c in enumerate(cols):
                    cell = row[i]
                    if c.type == "number" and cell.strip():
                        cell = fmt_number(parse_number(cell), f.decimals if c.decimals is None else c.decimals)
                    r.cell(T(cell))
            if any(t is not None for t in totals):
                r = table.row()
                tot = FontFace(emphasis="BOLD", fill_color=light)
                for i, t in enumerate(totals):
                    if t is not None:
                        d = f.decimals if cols[i].decimals is None else cols[i].decimals
                        r.cell(T(fmt_number(t, d, f.unit)), style=tot)
                    else:
                        r.cell("Total" if i == 0 else "", style=tot)
        pdf.set_y(pdf.get_y() + 5)

    pdf.add_page()
    if page.show_title and doc_title:
        font("B", fs + 4)
        pdf.multi_cell(pdf.epw, (fs + 4) * 0.5, T(doc_title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_y(pdf.get_y() + 4)

    pending_half: FormField | None = None
    kv_run: list[FormField] = []

    def flush():
        nonlocal pending_half
        if pending_half:
            draw_pair(pending_half, None)
            pending_half = None
        if kv_run:
            draw_kv_table(kv_run)
            kv_run.clear()

    for f in tpl.fields:
        if f.type in DATA_TYPES and f.type != "table" and page.hide_empty and is_empty(f, values.get(f.key)) \
                and f.type != "signature":
            continue
        if f.type in INLINE_TYPES and page.style == "table":
            if pending_half:
                flush()
            kv_run.append(f)
            continue
        if f.type in INLINE_TYPES or f.type == "signature":
            if kv_run:
                flush()
            if f.width == "half":
                if pending_half:
                    draw_pair(pending_half, f)
                    pending_half = None
                else:
                    pending_half = f
                continue
            flush()
            ensure_space(block_height(f, pdf.epw))
            draw_block(f, pdf.l_margin, pdf.epw)
            continue
        flush()
        if f.type == "section":
            ensure_space(lh * 5 + 8)
            pdf.set_y(pdf.get_y() + 2)
            font("B", fs + 2, accent)
            pdf.multi_cell(pdf.epw, (fs + 2) * 0.5, T(subst(f.label, vars_)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            y = pdf.get_y() + 1
            pdf.set_draw_color(*light)
            pdf.set_line_width(0.6)
            pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
            pdf.set_y(y + 4)
        elif f.type == "paragraph":
            text = T(subst(f.text, vars_))
            if text.strip():
                font()
                pdf.multi_cell(pdf.epw, lh + 0.4, text, align="J", markdown=True,
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_y(pdf.get_y() + 4)
        elif f.type == "table":
            draw_table(f)
        elif f.type == "pagebreak":
            pdf.add_page()
        elif f.type == "spacer":
            pdf.set_y(pdf.get_y() + 8)
    flush()

    data = bytes(pdf.output())
    return data, pdf.pages_count
