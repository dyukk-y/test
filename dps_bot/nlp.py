"""Понимание текста сообщений о ДПС.

Умеет:
* нормализовать текст: регистр, «ё», повторы букв («дпссс»), латиница-двойники
  («ДПC» с латинской C), неверную раскладку («lgc» → «дпс»), транслит («dps»);
* раскрывать сокращения и сленг (ул, пов, азс, тц, гайцы, мусора, фен, тренога…);
* определять тип события (ДПС, радар, камера, рейд, ДТП, опасность) с оценкой
  уверенности, а также «отбой» («чисто», «уехали», «ДПС нет») и вопросы;
* извлекать госномер (в т.ч. полицейский формат «А1234 54»), населённые пункты
  в любых падежах и с опечатками, направление движения, ориентиры, улицу, км трассы.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .gazetteer import DESTINATIONS, Place, norm

# ---------------------------------------------------------------- нормализация

_LAT_TO_CYR_HOMO = str.maketrans("aceopxykmthb", "асеорхукмтнв")
_QWERTY = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_JCUKEN = "йцукенгшщзхъфывапролджэячсмитьбюё"
_LAYOUT = str.maketrans(_QWERTY, _JCUKEN)
_TRANSLIT = {
    "dps": "дпс", "gai": "гаи", "gibdd": "гибдд", "dtp": "дтп", "gaishniki": "гаишники",
    "radar": "радар", "kamera": "камера", "reid": "рейд", "post": "пост", "chisto": "чисто",
}

_VOWELS = "аеиоуыэюяй"

# Сокращения и сленг → каноническая форма
ABBR: dict[str, str] = {
    "ул": "улица", "пер": "переулок", "пр": "проспект", "пл": "площадь",
    "пов": "поворот", "поворот": "поворот", "перекр": "перекресток", "перекресток": "перекресток",
    "р": "", "н": "",  # хвосты от «р-н», «н.п.»
    "азс": "заправка", "заправка": "заправка", "заправке": "заправка", "заправки": "заправка",
    "тц": "торговый центр", "трц": "торговый центр", "маг": "магазин", "м": "",
    "км": "километр", "жд": "переезд", "напр": "направление",
    "щас": "сейчас", "ща": "сейчас", "счас": "сейчас", "тока": "только", "тож": "тоже",
    "мин": "минут", "мост": "мост", "трасса": "трасса", "трассе": "трасса",
    "гайцы": "гаишники", "гайцов": "гаишников", "гаец": "гаишник", "гаишни": "гаишники",
    "мусора": "полицейские", "мусоров": "полицейских", "менты": "полицейские", "ментов": "полицейских",
    "полосатые": "гаишники", "дпсники": "дпс", "дпсник": "дпс", "дэпээс": "дпс", "депеэс": "дпс",
    "гбдд": "гибдд", "гиббд": "гибдд", "гибд": "гибдд",
    "фен": "радар", "фены": "радар", "феном": "радар",
    "треногу": "тренога", "треноги": "тренога", "треногой": "тренога",
    "кр": "", "т": "",
}

# Для поиска по корпусу — список канонических «сильных» терминов (для раскладки)
_STRONG_WORDS = {"дпс", "гаи", "гибдд", "дтп", "радар", "рейд", "камера", "чисто", "пост", "засада", "патруль"}

# ---------------------------------------------------------------- лексикон

TERMS: list[tuple[str, str, float]] = [
    # ДПС
    (r"дпс\w*|гибдд\w*|гаи|гаишн\w*|гайщик\w*|автоинспектор\w*|инспектор\w*", "police", 1.0),
    (r"патрул\w*|наряд\w*|экипаж\w*|полици\w*|полицейск\w*|гиббон\w*|фараон\w*", "police", 0.8),
    (r"засад\w*|в кустах|за кустами|прячутся|спрятал\w*|караул\w*|пасут|дежур\w*", "police", 0.55),
    (r"жезл\w*|палк(?:а|ой|ами|у)|машут|махнул\w*|машет", "police", 0.55),
    (r"тормоз(?:ят|нули|нул|ит|ил|или)|тормозн\w*|останавлива\w*|остановил\w*", "police", 0.5),
    (r"мигалк\w*|люстр\w*|полосат\w* машин\w*", "police", 0.5),
    (r"пост(?:у|е|ом)?", "police", 0.4),
    # Радар / замер скорости
    (r"радар\w*|визир\w*|бинар\w*|скоростемер\w*", "radar", 0.9),
    (r"(?:меря|замеря|мерю|ловят)\w* скорост\w*|ловят на скорост\w*|на скорост\w* ловят", "radar", 0.7),
    (r"искр[аеуы]|сокол\w*", "radar", 0.4),
    # Камеры
    (r"камер\w*|тренога|передвижк\w*|автоураган\w*|мобильн\w* комплекс\w*|комплекс фиксации", "camera", 0.85),
    (r"арен[аеуы]|кордон\w*|крис\w*|скат\w*|автодори\w*", "camera", 0.45),
    # Рейды
    (r"рейд\w*|перехват\w*|сплошн\w*|всех подряд|всех тормоз\w*|тотальн\w*|проверк\w* документ\w*|"
     r"трезв\w* водител\w*|оцеплени\w*|омон\w*|блокпост\w*", "raid", 0.95),
    # ДТП
    (r"дтп|авари\w*|столкнул\w*|столкновени\w*|переверну\w*|кювет\w*|разбил\w*|"
     r"сбил[аио]?|лобов\w*|въехал\w* в|влетел\w*", "accident", 1.0),
    # Опасности
    (r"пробк\w*|гололед\w*|гололедиц\w*|перекрыт\w*|перекрыл\w*|ремонт\w* дорог\w*|дорожн\w* работ\w*|"
     r"яма|ямы|ямищ\w*|коров\w* на дорог\w*|скот на дорог\w*|лос[ьяи]|снежн\w* занос\w*|переметает|"
     r"не проехать|непроезд\w*|объезд\w*|разлив\w*|подтоплен\w*|упал\w* дерев\w*|дерево упал\w*", "hazard", 0.8),
]

CONTEXT = (
    r"сто(?:ят|ит|ял|яли|ять)|стаят|встал[иа]?|видел\w*|заметил\w*|засек\w*|осторожн\w*|аккуратн\w*|"
    r"внимани\w*|сейчас|только что|минут\w* назад|прямо|работают|ловят|тормозят"
)
CLEAR = (
    r"чисто|уехал[иа]?|снял(?:ись|ся|ась)|свернул(?:ись|ся)|никого нет|нет никого|пусто|"
    r"убрал(?:ись|и|ся)|ушли|уже нет|больше нет|можно ехать|проезжайте|отбой|разъехались"
)
NEGATED = (
    r"(?:нет|нету|не было|не видно|не стоят|не стоит)\s+(?:\w+\s+)?(?:дпс|гаи|гибдд|гаишник\w*|полицейск\w*|никого|радар\w*|камер\w*)|"
    r"(?:дпс|гаи|гибдд|гаишник\w*|полицейск\w*|радар\w*|камер\w*)\s+(?:уже\s+)?(?:нет|нету|не стоят|не стоит|не видно)"
)
QUESTION = (
    r"есть ли|кто знает|кто видел|подскажите|подскажи|стоят ли|стоит ли|есть кто|как там|"
    r"чисто ли|есть кто нибудь|кто в курсе|не знаете|не стоят|что там"
)

LANDMARKS: dict[str, str] = {
    # стем → каноническое имя ориентира
    "торговый центр": "торговый центр", "рынк": "рынок", "рынок": "рынок", "автовокзал": "автовокзал",
    "вокзал": "вокзал", "больниц": "больница", "поликлиник": "поликлиника", "мост": "мост",
    "заправк": "заправка", "лукойл": "заправка Лукойл", "газпром": "заправка Газпромнефть",
    "роснефт": "заправка Роснефть", "кольц": "кольцо", "светофор": "светофор", "школ": "школа",
    "церкв": "церковь", "церков": "церковь", "храм": "храм", "стадион": "стадион", "переезд": "переезд",
    "администрац": "администрация", "пожарн": "пожарная часть", "кладбищ": "кладбище",
    "магнит": "Магнит", "пятерочк": "Пятёрочка", "мария ра": "Мария-Ра", "ярче": "Ярче",
    "почт": "почта", "сбер": "Сбербанк", "аптек": "аптека", "развилк": "развилка",
    "перекресток": "перекресток", "поворот": "поворот", "въезд": "въезд", "выезд": "выезд",
    "стел": "стела", "плотин": "плотина", "дамб": "дамба", "объездн": "объездная",
    "кафе": "кафе", "шиномонтаж": "шиномонтаж", "мойк": "автомойка", "элеватор": "элеватор",
    "спуск": "спуск", "подъем": "подъём", "пруд": "пруд", "парк": "парк",
}

_TERM_RE = [(re.compile(rf"\b(?:{p})\b"), k, w) for p, k, w in TERMS]
_CONTEXT_RE = re.compile(rf"\b(?:{CONTEXT})\b")
_CLEAR_RE = re.compile(rf"\b(?:{CLEAR})\b")
_NEGATED_RE = re.compile(rf"\b(?:{NEGATED})\b")
_QUESTION_RE = re.compile(rf"\b(?:{QUESTION})\b")
_KM_RE = re.compile(r"\b(\d{1,3})\s*(?:й|ом|м|ой)?\s*километр\w*")
_STREET_RE = re.compile(r"\b(?:улица|переулок|проспект|площадь)\s+([а-я]{3,}(?:\s[а-я]{3,})?)")
_DIRECTION_RE = re.compile(
    r"\b(?:в сторону|по направлению|в направлении|направление|едут|ехать|на выезд\w* (?:в|на)|из\s+\w+\s+(?:в|на))\s+(\w+)"
)

_PLATE_UP = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")
_PL = "АВЕКМНОРСТУХ"
_CIVIL_RE = re.compile(rf"(?<![А-ЯA-Z0-9])([{_PL}])\s?(\d{{3}})\s?([{_PL}]{{2}})\s?(\d{{2,3}})(?![А-ЯA-Z0-9])")
_POLICE_RE = re.compile(rf"(?<![А-ЯA-Z0-9])([{_PL}])\s?(\d{{4}})\s?(\d{{2,3}})(?![А-ЯA-Z0-9])")


# ---------------------------------------------------------------- модель

@dataclass
class PlaceMatch:
    place: Place
    role: str       # at | turn | direction
    score: float    # 1.0 — точное совпадение, меньше — нечёткое


@dataclass
class Analysis:
    text: str
    norm: str
    kind: str | None = None
    confidence: float = 0.0
    is_report: bool = False
    is_clear: bool = False
    is_question: bool = False
    plate: str | None = None
    places: list[PlaceMatch] = field(default_factory=list)
    direction: str | None = None
    landmarks: list[str] = field(default_factory=list)
    street: str | None = None
    road_km: int | None = None
    terms: list[str] = field(default_factory=list)

    @property
    def place(self) -> Place | None:
        """Главное место события (не направление)."""
        for m in self.places:
            if m.role in ("at", "turn"):
                return m.place
        return None

    @property
    def place_role(self) -> str | None:
        for m in self.places:
            if m.role in ("at", "turn"):
                return m.role
        return None

    def memory_keys(self) -> list[str]:
        """Ключевые фразы для «самообучающегося» геокодера."""
        base = self.place.key if self.place else "_"
        keys = [f"{lm}|{base}" for lm in self.landmarks]
        if self.street:
            keys.append(f"ул {self.street}|{base}")
        if self.road_km is not None:
            keys.append(f"км {self.road_km}|{base}")
        return keys

    def summary(self) -> str:
        parts = []
        if self.place:
            role = {"turn": "поворот на ", "at": ""}.get(self.place_role or "at", "")
            parts.append(role + self.place.name)
        if self.street:
            parts.append(f"ул. {self.street.title()}")
        parts += self.landmarks[:3]
        if self.road_km is not None:
            parts.append(f"{self.road_km} км")
        if self.direction:
            parts.append(f"→ {self.direction}")
        return ", ".join(parts)


# ---------------------------------------------------------------- функции

def _fix_token(tok: str) -> str:
    has_lat = bool(re.search(r"[a-z]", tok))
    has_cyr = bool(re.search(r"[а-я]", tok))
    if has_lat and has_cyr:
        return tok.translate(_LAT_TO_CYR_HOMO)
    if has_lat and not has_cyr:
        if tok in _TRANSLIT:
            return _TRANSLIT[tok]
        swapped = tok.translate(_LAYOUT)
        if swapped in _STRONG_WORDS or swapped in ABBR:
            return swapped
    return tok


def normalize(text: str) -> str:
    s = text.lower().replace("ё", "е")
    s = re.sub(r"(\w)\1{2,}", r"\1", s)                  # «дпссс» → «дпс»
    s = re.sub(r"(\d)\s*-\s*(?:й|ом|го|м)\b", r"\1", s)  # «45-й» → «45»
    s = re.sub(r"[^\w\s?]", " ", s)
    tokens = []
    for tok in s.replace("?", " ? ").split():
        tok = _fix_token(tok)
        tok = ABBR.get(tok, tok)
        if tok:
            tokens.append(tok)
    return " ".join(tokens)


def _stem(word: str) -> str:
    w = word
    if len(w) >= 6 and w[-2:] in ("ое", "ая", "ий", "ой", "ые", "ое"):
        return w[:-2]
    if len(w) >= 5 and w[-1] in _VOWELS:
        return w[:-1]
    return w


def _token_matches(tok: str, word: str) -> float:
    stem = _stem(word)
    if len(stem) < 4:
        return 1.0 if tok == word else 0.0
    if tok.startswith(stem) and len(tok) - len(stem) <= 3:
        return 1.0
    if len(word) >= 6 and abs(len(tok) - len(word)) <= 2 and tok[:2] == word[:2]:
        r = SequenceMatcher(None, tok, word).ratio()
        if r >= 0.84:
            return r
    return 0.0


def _find_phrase(tokens: list[str], phrase: str) -> tuple[int, float] | None:
    words = phrase.split()
    n = len(words)
    best = None
    for i in range(len(tokens) - n + 1):
        score = 1.0
        for j, w in enumerate(words):
            s = _token_matches(tokens[i + j], w)
            if not s:
                break
            score = min(score, s)
        else:
            if best is None or score > best[1]:
                best = (i, score)
    return best


def _role(tokens: list[str], idx: int) -> str:
    before = tokens[max(0, idx - 3):idx]
    joined = " ".join(before)
    if re.search(r"сторон|направлени|едут|ехать", joined):
        return "direction"
    if re.search(r"поворот|свороте?|развилк", joined):
        return "turn"
    return "at"


def extract_plate(text: str) -> str | None:
    up = text.upper().replace("Ё", "Е").translate(_PLATE_UP)
    m = _CIVIL_RE.search(up)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)} {m.group(4)}"
    m = _POLICE_RE.search(up)
    if m:
        return f"{m.group(1)}{m.group(2)} {m.group(3)}"
    return None


def match_places(tokens: list[str], places: list[Place]) -> list[PlaceMatch]:
    found: dict[str, PlaceMatch] = {}
    for p in places:
        for alias in [norm(p.name), *[norm(a) for a in p.aliases]]:
            hit = _find_phrase(tokens, alias)
            if not hit:
                continue
            idx, score = hit
            # ориентиры, заведённые админом, точнее сёл
            if p.kind != "village":
                score += 0.5
            pm = PlaceMatch(p, _role(tokens, idx), score)
            prev = found.get(p.key)
            if prev is None or pm.score > prev.score:
                found[p.key] = pm
    # landmark/street админа раньше сёл, затем по точности
    return sorted(found.values(), key=lambda m: (m.role == "direction", -m.score))


def analyze(text: str, places: list[Place] | None = None) -> Analysis:
    from .gazetteer import VILLAGES

    places = places if places is not None else VILLAGES
    n = normalize(text)
    a = Analysis(text=text, norm=n)
    tokens = n.split()

    # --- тип события
    scores: dict[str, float] = {}
    for rx, k, w in _TERM_RE:
        for m in rx.finditer(n):
            a.terms.append(m.group(0))
            scores[k] = max(scores.get(k, 0.0), w) + (0.15 if k in scores else 0.0)
    ctx = len(_CONTEXT_RE.findall(n))
    a.plate = extract_plate(text)

    a.is_question = "?" in n or bool(_QUESTION_RE.search(n))
    negated = bool(_NEGATED_RE.search(n))
    clear_hit = bool(_CLEAR_RE.search(n))

    if scores:
        best_kind = max(scores, key=scores.get)
        # Более конкретный тип важнее общего «ДПС»: «гайцы с радаром» → радар
        for specific in ("accident", "raid", "radar", "camera"):
            if scores.get(specific, 0) >= 0.7:
                best_kind = specific
                break
        if best_kind == "hazard" and scores.get("police", 0) >= 0.8:
            best_kind = "police"
        scores[best_kind] = max(scores.values())
        a.kind = best_kind
        conf = scores[best_kind] + min(ctx, 3) * 0.2 + (0.25 if a.plate else 0.0)
        a.confidence = round(min(conf, 1.0), 2)
    elif a.plate and ctx:
        a.kind, a.confidence = "police", 0.6

    if negated or (clear_hit and (scores or ctx)):
        a.is_clear = True
        a.kind = a.kind if a.kind in ("police", "radar", "raid", "camera") else "police"
        a.is_report = False
    else:
        a.is_report = a.kind is not None and a.confidence >= 0.6 and not a.is_question

    # --- география
    a.places = match_places(tokens, places)
    for dest, aliases in DESTINATIONS.items():
        for al in aliases:
            hit = _find_phrase(tokens, al)
            if hit:
                role = _role(tokens, hit[0])
                if role == "direction" or not a.direction:
                    a.direction = dest
                break
    for pm in a.places:
        if pm.role == "direction" and not a.direction:
            a.direction = pm.place.name
    m = _DIRECTION_RE.search(n)
    if m and not a.direction:
        for p in places:
            if _token_matches(m.group(1), norm(p.name).split()[0]):
                a.direction = p.name
                break

    for stem, canon in LANDMARKS.items():
        if re.search(rf"\b{stem}", n) and canon not in a.landmarks:
            a.landmarks.append(canon)
    m = _STREET_RE.search(n)
    if m:
        a.street = m.group(1)
    m = _KM_RE.search(n)
    if m:
        a.road_km = int(m.group(1))
    return a
