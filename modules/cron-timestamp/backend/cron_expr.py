"""Standard 5-field cron parser (Vixie semantics), French description, next runs."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

MONTH_NAMES = {n: i + 1 for i, n in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
DOW_NAMES = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
FR_DAYS = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"]

FIELDS = [
    ("minute", "minute", 0, 59, None),
    ("hour", "heure", 0, 23, None),
    ("dom", "jour du mois", 1, 31, None),
    ("month", "mois", 1, 12, MONTH_NAMES),
    ("dow", "jour de la semaine", 0, 7, DOW_NAMES),
]

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(Exception):
    def __init__(self, message: str, field_index: int | None = None):
        super().__init__(message)
        self.message = message
        self.field_index = field_index


@dataclass
class Item:
    start: int
    end: int
    step: int
    star: bool
    single: bool


@dataclass
class Field:
    name: str
    values: set[int]
    items: list[Item] = field(default_factory=list)
    star: bool = False

    @property
    def every(self) -> bool:
        return self.star and len(self.items) == 1 and self.items[0].step == 1


@dataclass
class Cron:
    expression: str
    normalized: str
    alias: str | None
    minute: Field
    hour: Field
    dom: Field
    month: Field
    dow: Field

    def day_matches(self, d: datetime) -> bool:
        dom_ok = d.day in self.dom.values
        dow_ok = (d.weekday() + 1) % 7 in self.dow.values
        if self.dom.star or self.dow.star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def fields_dict(self) -> dict:
        return {f.name: sorted(f.values) for f in (self.minute, self.hour, self.dom, self.month, self.dow)}


def _value(tok: str, idx: int) -> int:
    name, label, lo, hi, names = FIELDS[idx]
    if tok == "":
        raise CronError(f"Champ « {label} » : valeur manquante.", idx)
    if tok.isdigit():
        v = int(tok)
    elif names and tok.upper() in names:
        v = names[tok.upper()]
    else:
        if any(c in tok.upper() for c in "LW#") and idx in (2, 4):
            raise CronError(f"Champ « {label} » : « {tok} » utilise L, W ou # (syntaxe Quartz), non pris en charge par cron standard.", idx)
        hint = ""
        if names:
            hint = " (noms acceptés : " + ", ".join(names) + ")"
        raise CronError(f"Champ « {label} » : valeur « {tok} » invalide{hint}.", idx)
    if not lo <= v <= hi:
        raise CronError(f"Champ « {label} » : {v} hors limites ({lo}-{hi}).", idx)
    return v


def _field(text: str, idx: int) -> Field:
    name, label, lo, hi, _ = FIELDS[idx]
    if text == "?" and idx in (2, 4):
        text = "*"
    values: set[int] = set()
    items: list[Item] = []
    for part in text.split(","):
        if part == "":
            raise CronError(f"Champ « {label} » : élément vide (virgule en trop ?).", idx)
        base, slash, step_txt = part.partition("/")
        step = 1
        if slash:
            if not step_txt.isdigit() or int(step_txt) == 0:
                raise CronError(f"Champ « {label} » : pas « /{step_txt} » invalide (entier positif attendu).", idx)
            step = int(step_txt)
            if step > hi - lo + 1 and not (idx == 4 and step <= 7):
                raise CronError(f"Champ « {label} » : pas /{step} supérieur à l'étendue du champ ({lo}-{hi}).", idx)
        star = base == "*"
        if star:
            start, end = lo, hi
            if idx == 4:
                end = 6
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _value(a, idx), _value(b, idx)
            if start > end:
                raise CronError(f"Champ « {label} » : plage « {base} » décroissante ({start} > {end}).", idx)
        else:
            start = _value(base, idx)
            end = (6 if idx == 4 else hi) if slash else start
        values.update(range(start, end + 1, step))
        items.append(Item(start, end, step, star, not star and "-" not in base and not slash))
    if idx == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return Field(name, values, items, star=text.startswith("*"))


def parse(expression: str) -> Cron:
    expr = " ".join(str(expression or "").split())
    if not expr:
        raise CronError("Expression vide.")
    alias = None
    if expr.startswith("@"):
        key = expr.lower()
        if key == "@reboot":
            raise CronError("@reboot s'exécute au démarrage du système : aucune date planifiable.")
        if key not in ALIASES:
            raise CronError(f"Alias « {expr} » inconnu (acceptés : {', '.join(ALIASES)}).")
        alias, expr = key, ALIASES[key]
    parts = expr.split(" ")
    if len(parts) == 6:
        raise CronError("6 champs : le format avec secondes (Quartz/Spring) n'est pas pris en charge ; utilisez 5 champs « minute heure jour mois jour-semaine ».")
    if len(parts) != 5:
        raise CronError(f"{len(parts)} champ(s) trouvé(s), 5 attendus : minute heure jour-du-mois mois jour-de-la-semaine.")
    fields = [_field(p, i) for i, p in enumerate(parts)]
    return Cron(str(expression).strip(), expr, alias, *fields)


# ── Description ─────────────────────────────────────────────────────────────

def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " et " + words[-1]


def _ord(day: int) -> str:
    return "1er" if day == 1 else str(day)


def _hh(h: int) -> str:
    return f"{h}h"


def _minute_phrase(f: Field) -> tuple[str, bool]:
    """Returns (phrase, is_specific_minutes)."""
    if f.every:
        return "chaque minute", False
    if f.star:
        return f"toutes les {f.items[0].step} minutes", False
    if all(i.single for i in f.items):
        vals = sorted(f.values)
        if len(vals) == 1:
            return f"à la minute {vals[0]}", True
        return "aux minutes " + _join([str(v) for v in vals]), True
    parts = []
    for i in f.items:
        if i.single:
            parts.append(f"à la minute {i.start}")
        elif i.step == 1:
            parts.append(f"chaque minute de {i.start} à {i.end}")
        else:
            parts.append(f"toutes les {i.step} minutes de {i.start} à {i.end}")
    return _join(parts), False


def _hour_phrase(f: Field) -> str | None:
    if f.every:
        return None
    if f.star:
        return f"toutes les {f.items[0].step} heures"
    parts = []
    singles = [i.start for i in f.items if i.single]
    if singles:
        parts.append(("pendant l'heure de " if len(singles) == 1 else "pendant les heures de ") + _join([_hh(h) for h in singles]))
    for i in f.items:
        if i.single:
            continue
        if i.step == 1:
            parts.append(f"de {i.start}h00 à {i.end}h59")
        else:
            parts.append(f"toutes les {i.step} heures de {i.start}h à {i.end}h")
    return _join(parts)


def _time_phrase(c: Cron) -> str:
    m, h = c.minute, c.hour
    if all(i.single for i in m.items) and all(i.single for i in h.items) and len(m.values) * len(h.values) <= 8:
        times = sorted(f"{hh:02d}:{mm:02d}" for hh in h.values for mm in m.values)
        return "à " + _join(times)
    mp, specific = _minute_phrase(m)
    hp = _hour_phrase(h)
    if hp is None:
        return f"{mp} de chaque heure" if specific else mp
    return f"{mp}, {hp}"


def _dom_phrase(f: Field) -> str:
    if f.star:
        return f"tous les {f.items[0].step} jours du mois (à partir du 1er)"
    parts = []
    singles = [i.start for i in f.items if i.single]
    if singles:
        parts.append(("le " if len(singles) == 1 else "les ") + _join([_ord(d) for d in singles]))
    for i in f.items:
        if i.single:
            continue
        if i.step == 1:
            parts.append(f"du {_ord(i.start)} au {i.end}")
        else:
            parts.append(f"tous les {i.step} jours du {_ord(i.start)} au {i.end}")
    return _join(parts) + " du mois"


def _dow_phrase(f: Field) -> str:
    if f.star:
        days = [FR_DAYS[d] for d in sorted(f.values)]
        return "le " + _join(days)
    parts = []
    singles = [i.start % 7 for i in f.items if i.single]
    if singles:
        parts.append("le " + _join([FR_DAYS[d] for d in singles]))
    for i in f.items:
        if i.single:
            continue
        if i.step == 1:
            parts.append(f"du {FR_DAYS[i.start % 7]} au {FR_DAYS[i.end % 7]}")
        else:
            days = [FR_DAYS[d % 7] for d in range(i.start, i.end + 1, i.step)]
            parts.append("le " + _join(days))
    return _join(parts)


def _month_phrase(f: Field) -> str | None:
    if f.every:
        return None
    if f.star:
        return f"tous les {f.items[0].step} mois (" + _join([FR_MONTHS[m - 1] for m in sorted(f.values)]) + ")"
    parts = []
    singles = [i.start for i in f.items if i.single]
    if singles:
        parts.append("en " + _join([FR_MONTHS[m - 1] for m in singles]))
    for i in f.items:
        if i.single:
            continue
        if i.step == 1:
            parts.append(f"de {FR_MONTHS[i.start - 1]} à {FR_MONTHS[i.end - 1]}")
        else:
            parts.append("en " + _join([FR_MONTHS[m - 1] for m in range(i.start, i.end + 1, i.step)]))
    return _join(parts)


def describe(c: Cron) -> str:
    pieces = [_time_phrase(c)]
    dom_r, dow_r = not c.dom.every, not c.dow.every
    if dom_r and dow_r and not c.dom.star and not c.dow.star:
        pieces.append(f"{_dom_phrase(c.dom)} ou {_dow_phrase(c.dow)}")
    elif dom_r and dow_r:
        pieces.append(f"{_dom_phrase(c.dom)}, uniquement si c'est {_dow_phrase(c.dow)}")
    elif dom_r:
        pieces.append(_dom_phrase(c.dom))
    elif dow_r:
        pieces.append(_dow_phrase(c.dow))
    month = _month_phrase(c.month)
    if month:
        pieces.append(month)
    if len(pieces) == 1 and not c.hour.star:
        pieces.append("tous les jours")
    text = ", ".join(pieces)
    return text[0].upper() + text[1:]


# ── Next runs ───────────────────────────────────────────────────────────────

def _exists(naive: datetime, tz: tzinfo) -> bool:
    aware = naive.replace(tzinfo=tz, fold=0)
    back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    return back == naive


def next_runs(c: Cron, tz: tzinfo, start: datetime, count: int = 10) -> list[datetime]:
    """Aware datetimes strictly after `start`. Wall-clock times skipped by a DST
    jump are not returned; repeated ones (fold) run once, on their first occurrence."""
    t = start.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
    limit = t.year + 30
    out: list[datetime] = []
    minutes, hours, months = c.minute.values, c.hour.values, c.month.values
    while len(out) < count and t.year <= limit:
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        if not c.day_matches(t):
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if t.hour not in hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in minutes:
            nxt = min((m for m in minutes if m > t.minute), default=None)
            t = t.replace(minute=nxt) if nxt is not None else t.replace(minute=0) + timedelta(hours=1)
            continue
        if _exists(t, tz):
            out.append(t.replace(tzinfo=tz, fold=0))
        t += timedelta(minutes=1)
    return out
