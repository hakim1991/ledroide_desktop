import random
import string
import unicodedata
import uuid
from datetime import date, datetime, timedelta

FIRST_NAMES = (
    "Adam Agathe Alice Alexandre Amandine Anaïs André Antoine Arthur Aurélie Baptiste Bastien Benjamin Bernard Camille "
    "Capucine Caroline Céline Charles Charlotte Chloé Christophe Claire Clément Corentin Damien Daniel David Delphine "
    "Diane Dominique Élise Élodie Emma Émilie Enzo Éric Estelle Étienne Fabien Fanny Florian Françoise François "
    "Gabriel Gaëlle Guillaume Hélène Hugo Inès Isabelle Jacques Jade Jean Jeanne Jérôme Julie Julien Juliette Justine "
    "Laura Laurent Léa Léo Léon Lina Louis Louise Luc Lucas Lucie Manon Marc Margaux Marie Marine Martin Mathilde "
    "Mathis Maxime Mélanie Michel Nathalie Nicolas Noah Noémie Océane Olivier Pascal Paul Pauline Philippe Pierre "
    "Quentin Raphaël Rémi Romain Rose Sandrine Sarah Sébastien Simon Sophie Stéphane Sylvie Théo Thomas Valentin "
    "Valérie Victor Vincent Virginie Yann Zoé"
).split()

LAST_NAMES = (
    "Martin Bernard Thomas Petit Robert Richard Durand Dubois Moreau Laurent Simon Michel Lefèvre Leroy Roux David "
    "Bertrand Morel Fournier Girard Bonnet Dupont Lambert Fontaine Rousseau Vincent Muller Lefebvre Faure André Mercier "
    "Blanc Guérin Boyer Garnier Chevalier François Legrand Gauthier Garcia Perrin Robin Clément Morin Nicolas Henry "
    "Roussel Mathieu Gautier Masson Marchand Duval Denis Dumont Marie Lemaire Noël Meyer Dufour Meunier Brun Blanchard "
    "Giraud Joly Rivière Lucas Brunet Gaillard Barbier Arnaud Martinez Gérard Roche Renard Schmitt Roy Leroux Colin "
    "Vidal Caron Picard Roger Fabre Aubert Lemoine Renaud Dumas Lacroix Olivier Philippe Bourgeois Pierre Benoît Rey "
    "Leclerc Payet Rolland Leclercq Guillaume Lecomte Lopez Jean Dupuy Guillot Hubert Berger Carpentier Sanchez"
).split()

# (ville, code postal, département)
CITIES = (
    ("Paris", "75001"), ("Paris", "75011"), ("Paris", "75015"), ("Marseille", "13001"), ("Marseille", "13008"),
    ("Lyon", "69001"), ("Lyon", "69007"), ("Toulouse", "31000"), ("Nice", "06000"), ("Nantes", "44000"),
    ("Montpellier", "34000"), ("Strasbourg", "67000"), ("Bordeaux", "33000"), ("Lille", "59000"), ("Rennes", "35000"),
    ("Reims", "51100"), ("Toulon", "83000"), ("Saint-Étienne", "42000"), ("Le Havre", "76600"), ("Grenoble", "38000"),
    ("Dijon", "21000"), ("Angers", "49000"), ("Nîmes", "30000"), ("Villeurbanne", "69100"), ("Clermont-Ferrand", "63000"),
    ("Le Mans", "72000"), ("Aix-en-Provence", "13100"), ("Brest", "29200"), ("Tours", "37000"), ("Amiens", "80000"),
    ("Limoges", "87000"), ("Annecy", "74000"), ("Perpignan", "66000"), ("Boulogne-Billancourt", "92100"),
    ("Metz", "57000"), ("Besançon", "25000"), ("Orléans", "45000"), ("Saint-Denis", "93200"), ("Argenteuil", "95100"),
    ("Rouen", "76000"), ("Mulhouse", "68100"), ("Montreuil", "93100"), ("Caen", "14000"), ("Nancy", "54000"),
    ("Tourcoing", "59200"), ("Roubaix", "59100"), ("Nanterre", "92000"), ("Vitry-sur-Seine", "94400"),
    ("Avignon", "84000"), ("Créteil", "94000"), ("Poitiers", "86000"), ("Versailles", "78000"), ("Pau", "64000"),
    ("Courbevoie", "92400"), ("Colombes", "92700"), ("Aubervilliers", "93300"), ("La Rochelle", "17000"),
    ("Calais", "62100"), ("Cannes", "06400"), ("Antibes", "06600"), ("Béziers", "34500"), ("Colmar", "68000"),
    ("Bourges", "18000"), ("Quimper", "29000"), ("Valence", "26000"), ("Troyes", "10000"), ("Chambéry", "73000"),
    ("Lorient", "56100"), ("Niort", "79000"), ("Vannes", "56000"), ("Ajaccio", "20000"), ("Bastia", "20200"),
    ("Saint-Malo", "35400"), ("Bayonne", "64100"), ("Arles", "13200"), ("Laval", "53000"), ("Belfort", "90000"),
    ("Évreux", "27000"), ("Chartres", "28000"), ("Angoulême", "16000"), ("Blois", "41000"), ("Albi", "81000"),
    ("Tarbes", "65000"), ("Carcassonne", "11000"), ("Auxerre", "89000"), ("Périgueux", "24000"),
)

STREET_TYPES = ("rue", "rue", "rue", "avenue", "boulevard", "place", "allée", "impasse", "chemin", "quai", "route")
STREET_NAMES = (
    "de la République", "Victor Hugo", "Jean Jaurès", "de la Paix", "des Lilas", "du Général de Gaulle", "Pasteur",
    "de la Gare", "des Écoles", "du Moulin", "de l'Église", "Gambetta", "Voltaire", "des Roses", "du Château",
    "Émile Zola", "de la Liberté", "Jules Ferry", "des Tilleuls", "du Commerce", "Nationale", "de Verdun",
    "Carnot", "de la Mairie", "des Acacias", "Saint-Michel", "du Port", "des Peupliers", "Aristide Briand",
    "Léon Blum", "de Strasbourg", "du Stade", "des Vignes", "de Bretagne", "Montaigne", "Molière", "des Prés",
)
EMAIL_DOMAINS = ("gmail.com", "yahoo.fr", "hotmail.fr", "outlook.fr", "orange.fr", "free.fr", "laposte.net",
                 "sfr.fr", "wanadoo.fr", "exemple.fr", "exemple.com")
COMPANY_FORMS = ("SAS", "SARL", "SA", "EURL", "SASU", "SCOP")
COMPANY_WORDS = ("Groupe", "Ateliers", "Établissements", "Société", "Compagnie", "Laboratoires", "Transports",
                 "Boulangerie", "Cabinet", "Agence", "Solutions", "Consulting", "Industries", "Services")
COMPANY_SECTORS = ("Informatique", "Bâtiment", "Conseil", "Logistique", "Énergie", "Digital", "Immobilier",
                   "Distribution", "Santé", "Médias", "Bio", "Technologies", "Innovation", "Patrimoine")
URL_WORDS = ("blog", "shop", "app", "docs", "news", "data", "cloud", "portail", "atelier", "studio", "maison", "info")
TLDS = ("fr", "com", "net", "org", "io", "eu")
URL_PATHS = ("accueil", "produits", "contact", "articles", "a-propos", "services", "aide", "compte", "panier", "blog")
LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore "
    "magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo "
    "consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur excepteur sint "
    "occaecat cupidatat non proident sunt culpa qui officia deserunt mollit anim id est laborum"
).split()


def ascii_slug(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in norm if ch.isalnum() or ch in ".-")


def luhn_check_digit(digits: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_valid(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_check(country: str, bban: str) -> str:
    rearranged = bban + country + "00"
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return f"{98 - int(numeric) % 97:02d}"


def iban_valid(iban: str) -> bool:
    s = iban.replace(" ", "").upper()
    numeric = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(numeric) % 97 == 1


def rib_key(bank: str, branch: str, account: str) -> str:
    return f"{97 - (89 * int(bank) + 15 * int(branch) + 3 * int(account)) % 97:02d}"


# ── Simple regex-driven string generator ─────────────────────────────────────

class RegexError(ValueError):
    pass


PRINTABLE = string.ascii_letters + string.digits + " .,-_!?@#"
CLASS_ESCAPES = {
    "d": string.digits,
    "w": string.ascii_letters + string.digits + "_",
    "s": " ",
    "D": string.ascii_letters + "_-",
    "W": " .-,;:!?",
}
STAR_MAX = 5


class _RegexParser:
    """Supports literals, '.', escapes (\\d \\w \\s \\D \\W), classes [a-z0-9_] / [^...],
    groups ( ) (?: ), alternation |, quantifiers ? * + {n} {n,} {n,m}. Anchors ^ $ are ignored."""

    def __init__(self, pattern: str):
        self.p = pattern
        self.i = 0

    def parse(self):
        node = self._alt()
        if self.i < len(self.p):
            raise RegexError(f"caractère inattendu « {self.p[self.i]} » en position {self.i + 1}")
        return node

    def _peek(self):
        return self.p[self.i] if self.i < len(self.p) else None

    def _alt(self):
        branches = [self._seq()]
        while self._peek() == "|":
            self.i += 1
            branches.append(self._seq())
        return ("alt", branches)

    def _seq(self):
        items = []
        while self._peek() not in (None, "|", ")"):
            atom = self._atom()
            if atom is None:
                continue
            lo, hi = self._quant()
            items.append((atom, lo, hi))
        return items

    def _quant(self):
        c = self._peek()
        if c == "?":
            self.i += 1
            return 0, 1
        if c == "*":
            self.i += 1
            return 0, STAR_MAX
        if c == "+":
            self.i += 1
            return 1, STAR_MAX
        if c == "{":
            end = self.p.find("}", self.i)
            if end < 0:
                raise RegexError("accolade non fermée")
            body = self.p[self.i + 1:end]
            self.i = end + 1
            try:
                if "," in body:
                    a, b = body.split(",", 1)
                    lo = int(a or 0)
                    hi = int(b) if b.strip() else lo + STAR_MAX
                else:
                    lo = hi = int(body)
            except ValueError:
                raise RegexError(f"quantificateur invalide {{{body}}}")
            if lo > hi or hi > 1000:
                raise RegexError(f"quantificateur invalide {{{body}}}")
            return lo, hi
        return 1, 1

    def _atom(self):
        c = self.p[self.i]
        self.i += 1
        if c in "^$":
            return None
        if c == ".":
            return ("set", PRINTABLE)
        if c == "(":
            if self.p.startswith("?:", self.i):
                self.i += 2
            node = self._alt()
            if self._peek() != ")":
                raise RegexError("parenthèse non fermée")
            self.i += 1
            return node
        if c == "[":
            return ("set", self._class())
        if c == "\\":
            if self.i >= len(self.p):
                raise RegexError("échappement incomplet")
            e = self.p[self.i]
            self.i += 1
            if e in CLASS_ESCAPES:
                return ("set", CLASS_ESCAPES[e])
            return ("lit", {"n": "\n", "t": "\t"}.get(e, e))
        if c in "*+?{":
            raise RegexError(f"quantificateur « {c} » sans élément à répéter")
        if c == ")":
            raise RegexError("parenthèse fermante inattendue")
        return ("lit", c)

    def _class(self):
        negate = self._peek() == "^"
        if negate:
            self.i += 1
        chars = []
        first = True
        while True:
            c = self._peek()
            if c is None:
                raise RegexError("crochet non fermé")
            if c == "]" and not first:
                self.i += 1
                break
            first = False
            self.i += 1
            if c == "\\":
                e = self.p[self.i] if self.i < len(self.p) else ""
                self.i += 1
                if e in CLASS_ESCAPES:
                    chars.extend(CLASS_ESCAPES[e])
                    continue
                c = e
            if self._peek() == "-" and self.i + 1 < len(self.p) and self.p[self.i + 1] != "]":
                end = self.p[self.i + 1]
                self.i += 2
                if ord(end) < ord(c):
                    raise RegexError(f"intervalle invalide {c}-{end}")
                chars.extend(chr(x) for x in range(ord(c), ord(end) + 1))
            else:
                chars.append(c)
        if negate:
            excluded = set(chars)
            chars = [ch for ch in PRINTABLE if ch not in excluded]
        if not chars:
            raise RegexError("classe de caractères vide")
        return "".join(dict.fromkeys(chars))


def compile_regex(pattern: str):
    if len(pattern) > 500:
        raise RegexError("motif trop long (500 caractères max.)")
    return _RegexParser(pattern).parse()


def gen_regex(node, rng: random.Random) -> str:
    kind = node[0]
    if kind == "alt":
        branch = rng.choice(node[1])
        out = []
        for atom, lo, hi in branch:
            for _ in range(rng.randint(lo, hi)):
                out.append(gen_regex(atom, rng))
        return "".join(out)
    if kind == "set":
        return rng.choice(node[1])
    return node[1]


# ── Field generators ─────────────────────────────────────────────────────────

class Row:
    """Per-row context so that linked fields (prénom/nom/email, ville/code postal) stay consistent."""

    def __init__(self, rng: random.Random, index: int):
        self.rng = rng
        self.index = index
        self._person = None
        self._city = None

    @property
    def person(self):
        if self._person is None:
            self._person = (self.rng.choice(FIRST_NAMES), self.rng.choice(LAST_NAMES))
        return self._person

    @property
    def city(self):
        if self._city is None:
            self._city = self.rng.choice(CITIES)
        return self._city


def _parse_date(text, fallback: date) -> date:
    if not text:
        return fallback
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        raise ValueError(f"date invalide « {text} » (format AAAA-MM-JJ)")


def _num(opts: dict, key: str, default, cast=float):
    v = opts.get(key, default)
    if v in (None, ""):
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        raise ValueError(f"« {key} » doit être un nombre")


def _phone(rng, opts):
    first = rng.choice("6667") if rng.random() < 0.6 else rng.choice("123459")
    digits = first + "".join(rng.choice(string.digits) for _ in range(8))
    if opts.get("format") == "international":
        return "+33 " + digits[0] + " " + " ".join(digits[i:i + 2] for i in range(1, 9, 2))
    if opts.get("format") == "compact":
        return "0" + digits
    return "0" + digits[0] + " " + " ".join(digits[i:i + 2] for i in range(1, 9, 2))


def _email(row: Row, opts):
    rng = row.rng
    first, last = row.person
    style = rng.random()
    local = f"{ascii_slug(first)}.{ascii_slug(last)}" if style < 0.6 else f"{ascii_slug(first)[0]}{ascii_slug(last)}"
    if rng.random() < 0.35:
        local += str(rng.randint(1, 99))
    domain = opts.get("domain") or rng.choice(EMAIL_DOMAINS)
    return f"{local}@{domain}"


def _address(row: Row, opts):
    rng = row.rng
    num = rng.randint(1, 180)
    suffix = rng.choice(("", "", "", "", " bis", " ter"))
    street = f"{num}{suffix} {rng.choice(STREET_TYPES)} {rng.choice(STREET_NAMES)}"
    if opts.get("full"):
        city, cp = row.city
        return f"{street}, {cp} {city}"
    return street


def _company(row: Row, opts):
    rng = row.rng
    r = rng.random()
    last = rng.choice(LAST_NAMES)
    if r < 0.3:
        name = f"{rng.choice(COMPANY_WORDS)} {last}"
    elif r < 0.55:
        name = f"{last} & {rng.choice(('Fils', 'Associés', 'Cie', rng.choice(LAST_NAMES)))}"
    elif r < 0.8:
        name = f"{last} {rng.choice(COMPANY_SECTORS)}"
    else:
        name = f"{rng.choice(COMPANY_SECTORS)} {rng.choice(('Plus', 'Concept', 'France', 'Ouest', 'Sud', 'Avenir'))}"
    return f"{name} {rng.choice(COMPANY_FORMS)}" if opts.get("legal_form", True) else name


def _iban(rng, opts):
    bank = f"{rng.randint(10000, 99999)}"
    branch = f"{rng.randint(0, 99999):05d}"
    account = f"{rng.randint(0, 10 ** 11 - 1):011d}"
    bban = bank + branch + account + rib_key(bank, branch, account)
    iban = "FR" + iban_check("FR", bban) + bban
    if opts.get("format", "spaced") == "spaced":
        return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))
    return iban


def _siret(rng, opts):
    siren = "".join(rng.choice(string.digits) for _ in range(8))
    siren += luhn_check_digit(siren)
    nic = "".join(rng.choice(string.digits) for _ in range(4))
    nic += luhn_check_digit(siren + nic)
    if opts.get("kind") == "siren":
        return siren
    return siren + nic


def _url(row: Row, opts):
    rng = row.rng
    host = f"{rng.choice(('www.', '', ''))}{ascii_slug(rng.choice(LAST_NAMES))}-{rng.choice(URL_WORDS)}.{rng.choice(TLDS)}"
    path = "/".join(rng.choice(URL_PATHS) for _ in range(rng.randint(0, 2)))
    return f"https://{host}/{path}"


def _ipv4(rng, opts):
    if opts.get("private"):
        prefix = rng.choice(("10.", "192.168.", "172.16."))
        parts = 4 - prefix.count(".")
        return prefix + ".".join(str(rng.randint(0, 255)) for _ in range(parts - 1)) + "." + str(rng.randint(1, 254))
    return f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def _ipv6(rng, opts):
    return ":".join(f"{rng.getrandbits(16):x}" for _ in range(8))


def _lorem_sentence(rng, lo=6, hi=14):
    words = [rng.choice(LOREM) for _ in range(rng.randint(lo, hi))]
    return " ".join(words).capitalize() + "."


def _lorem(rng, opts):
    unit = opts.get("unit", "words")
    lo = int(_num(opts, "min", 3, int))
    hi = int(_num(opts, "max", max(lo, 8), int))
    lo, hi = max(1, min(lo, hi)), min(max(lo, hi), 200)
    n = rng.randint(lo, hi)
    if unit == "sentences":
        return " ".join(_lorem_sentence(rng) for _ in range(n))
    if unit == "paragraphs":
        return "\n\n".join(" ".join(_lorem_sentence(rng) for _ in range(rng.randint(3, 6))) for _ in range(n))
    return " ".join(rng.choice(LOREM) for _ in range(n))


def build(field: dict):
    """Returns a function(row) -> value for a field spec; raises ValueError on bad options."""
    t = field.get("type")
    opts = field.get("options") or {}

    if t == "sequence":
        start, step = int(_num(opts, "start", 1, int)), int(_num(opts, "step", 1, int))
        return lambda row: start + row.index * step
    if t == "uuid":
        return lambda row: str(uuid.UUID(int=row.rng.getrandbits(128), version=4))
    if t == "integer":
        lo, hi = int(_num(opts, "min", 0, int)), int(_num(opts, "max", 1000, int))
        if lo > hi:
            raise ValueError("le minimum doit être inférieur ou égal au maximum")
        return lambda row: row.rng.randint(lo, hi)
    if t == "decimal":
        lo, hi = _num(opts, "min", 0.0), _num(opts, "max", 1000.0)
        dec = max(0, min(10, int(_num(opts, "decimals", 2, int))))
        if lo > hi:
            raise ValueError("le minimum doit être inférieur ou égal au maximum")
        return lambda row: round(row.rng.uniform(lo, hi), dec)
    if t == "boolean":
        pct = _num(opts, "true_pct", 50.0)
        return lambda row: row.rng.random() * 100 < pct
    if t == "date":
        start = _parse_date(opts.get("start"), date(2020, 1, 1))
        end = _parse_date(opts.get("end"), date.today())
        if start > end:
            raise ValueError("la date de début doit précéder la date de fin")
        with_time = bool(opts.get("with_time"))
        days = (end - start).days
        if with_time:
            base = datetime.combine(start, datetime.min.time())
            span = days * 86400 + 86399
            return lambda row: (base + timedelta(seconds=row.rng.randint(0, span))).isoformat()
        return lambda row: (start + timedelta(days=row.rng.randint(0, days))).isoformat()
    if t == "first_name":
        return lambda row: row.person[0]
    if t == "last_name":
        return lambda row: row.person[1]
    if t == "full_name":
        return lambda row: f"{row.person[0]} {row.person[1]}"
    if t == "email":
        return lambda row: _email(row, opts)
    if t == "phone_fr":
        return lambda row: _phone(row.rng, opts)
    if t == "address_fr":
        return lambda row: _address(row, opts)
    if t == "city":
        return lambda row: row.city[0]
    if t == "postal_code":
        return lambda row: row.city[1]
    if t == "company":
        return lambda row: _company(row, opts)
    if t == "iban_fr":
        return lambda row: _iban(row.rng, opts)
    if t == "siret":
        return lambda row: _siret(row.rng, opts)
    if t == "url":
        return lambda row: _url(row, opts)
    if t == "ipv4":
        return lambda row: _ipv4(row.rng, opts)
    if t == "ipv6":
        return lambda row: _ipv6(row.rng, opts)
    if t == "color":
        return lambda row: f"#{row.rng.getrandbits(24):06x}"
    if t == "lorem":
        _lorem(random.Random(0), opts)
        return lambda row: _lorem(row.rng, opts)
    if t == "enum":
        raw = opts.get("values", "")
        values = raw if isinstance(raw, list) else [v.strip() for v in str(raw).replace("\n", ",").split(",")]
        values = [str(v) for v in values if str(v).strip() != ""]
        if not values:
            raise ValueError("indiquez au moins une valeur (séparées par des virgules)")
        return lambda row: row.rng.choice(values)
    if t == "regex":
        pattern = str(opts.get("pattern") or "")
        if not pattern:
            raise ValueError("indiquez un motif")
        try:
            node = compile_regex(pattern)
        except RegexError as exc:
            raise ValueError(f"regex : {exc}")
        return lambda row: gen_regex(node, row.rng)
    raise ValueError(f"type inconnu « {t} »")


TYPES = [
    {"type": "sequence", "label": "Séquence (1, 2, 3…)", "group": "Identifiants",
     "options": [{"key": "start", "label": "Début", "kind": "number", "default": 1},
                 {"key": "step", "label": "Pas", "kind": "number", "default": 1}]},
    {"type": "uuid", "label": "UUID v4", "group": "Identifiants", "options": []},
    {"type": "integer", "label": "Entier", "group": "Nombres",
     "options": [{"key": "min", "label": "Min", "kind": "number", "default": 0},
                 {"key": "max", "label": "Max", "kind": "number", "default": 1000}]},
    {"type": "decimal", "label": "Décimal", "group": "Nombres",
     "options": [{"key": "min", "label": "Min", "kind": "number", "default": 0},
                 {"key": "max", "label": "Max", "kind": "number", "default": 1000},
                 {"key": "decimals", "label": "Décimales", "kind": "number", "default": 2}]},
    {"type": "boolean", "label": "Booléen", "group": "Nombres",
     "options": [{"key": "true_pct", "label": "% de vrai", "kind": "number", "default": 50}]},
    {"type": "date", "label": "Date", "group": "Dates",
     "options": [{"key": "start", "label": "Du", "kind": "date", "default": "2020-01-01"},
                 {"key": "end", "label": "Au", "kind": "date", "default": ""},
                 {"key": "with_time", "label": "Avec l'heure", "kind": "bool", "default": False}]},
    {"type": "first_name", "label": "Prénom", "group": "Personnes", "options": []},
    {"type": "last_name", "label": "Nom", "group": "Personnes", "options": []},
    {"type": "full_name", "label": "Nom complet", "group": "Personnes", "options": []},
    {"type": "email", "label": "Email", "group": "Personnes",
     "options": [{"key": "domain", "label": "Domaine imposé", "kind": "text", "default": ""}]},
    {"type": "phone_fr", "label": "Téléphone FR", "group": "Personnes",
     "options": [{"key": "format", "label": "Format", "kind": "select", "default": "spaced",
                  "choices": [["spaced", "06 12 34 56 78"], ["compact", "0612345678"], ["international", "+33 6 12 34 56 78"]]}]},
    {"type": "address_fr", "label": "Adresse FR", "group": "Adresses",
     "options": [{"key": "full", "label": "Avec code postal et ville", "kind": "bool", "default": False}]},
    {"type": "city", "label": "Ville", "group": "Adresses", "options": []},
    {"type": "postal_code", "label": "Code postal", "group": "Adresses", "options": []},
    {"type": "company", "label": "Entreprise", "group": "Entreprises",
     "options": [{"key": "legal_form", "label": "Forme juridique (SAS…)", "kind": "bool", "default": True}]},
    {"type": "iban_fr", "label": "IBAN FR (valide)", "group": "Entreprises",
     "options": [{"key": "format", "label": "Format", "kind": "select", "default": "spaced",
                  "choices": [["spaced", "Par blocs de 4"], ["compact", "Compact"]]}]},
    {"type": "siret", "label": "SIRET / SIREN (valide)", "group": "Entreprises",
     "options": [{"key": "kind", "label": "Type", "kind": "select", "default": "siret",
                  "choices": [["siret", "SIRET (14 chiffres)"], ["siren", "SIREN (9 chiffres)"]]}]},
    {"type": "url", "label": "URL", "group": "Internet", "options": []},
    {"type": "ipv4", "label": "Adresse IPv4", "group": "Internet",
     "options": [{"key": "private", "label": "Plages privées", "kind": "bool", "default": False}]},
    {"type": "ipv6", "label": "Adresse IPv6", "group": "Internet", "options": []},
    {"type": "color", "label": "Couleur hex", "group": "Divers", "options": []},
    {"type": "lorem", "label": "Lorem ipsum", "group": "Texte",
     "options": [{"key": "unit", "label": "Unité", "kind": "select", "default": "words",
                  "choices": [["words", "Mots"], ["sentences", "Phrases"], ["paragraphs", "Paragraphes"]]},
                 {"key": "min", "label": "Min", "kind": "number", "default": 3},
                 {"key": "max", "label": "Max", "kind": "number", "default": 8}]},
    {"type": "enum", "label": "Valeur dans une liste", "group": "Texte",
     "options": [{"key": "values", "label": "Valeurs (séparées par des virgules)", "kind": "text", "default": "rouge, vert, bleu"}]},
    {"type": "regex", "label": "Motif regex", "group": "Texte",
     "options": [{"key": "pattern", "label": "Motif", "kind": "text", "default": "[A-Z]{3}-\\d{4}"}]},
]
TYPE_IDS = {t["type"] for t in TYPES}
