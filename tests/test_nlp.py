import pytest

from dps_bot.nlp import analyze, extract_plate, normalize


@pytest.mark.parametrize("text,kind", [
    ("У ТЦ Маслянино стоит ДПС", "police"),
    ("lgc на мосту в бажинске", "police"),            # неверная раскладка
    ("ДПC в Егорьевском", "police"),                   # латинская C
    ("дпсссс большой изырак", "police"),               # повторы букв
    ("гайцы с феном на выезде из маслянки", "radar"),  # сленг: фен = радар
    ("тренога на ул. Ленина у школы", "camera"),
    ("мусора тормозят всех подряд у заправки", "raid"),
    ("ДТП на 45 км, перевернулся камаз", "accident"),
    ("гололёд на спуске к мосту, аккуратнее", "hazard"),
    ("Стоят на посту у поворота на Берёзовку", "police"),
])
def test_report_kinds(text, kind):
    a = analyze(text)
    assert a.is_report, text
    assert a.kind == kind, (text, a.kind, a.terms)


@pytest.mark.parametrize("text", [
    "Новый пост в нашем канале",
    "Привет всем, как дела?",
    "Продам гараж",
    "Постоянно опаздываю на работу",
])
def test_not_reports(text):
    assert not analyze(text).is_report


@pytest.mark.parametrize("text", ["ДПС уехали с моста, чисто", "гаишников нет", "на трассе дпс нет уже"])
def test_clear(text):
    a = analyze(text)
    assert a.is_clear and not a.is_report


def test_question_is_not_report():
    a = analyze("Есть ли дпс на трассе в сторону Черепаново?")
    assert a.is_question and not a.is_report
    assert a.direction == "Черепаново"


def test_places_and_roles():
    a = analyze("гайцы на выезде из маслянки в сторону новосиба")
    assert a.place.name == "Маслянино"
    assert a.direction == "Новосибирск"
    assert "выезд" in a.landmarks

    a = analyze("Стоят у поворота на Берёзовку")
    assert a.place.name == "Берёзовка" and a.place_role == "turn"

    a = analyze("дпс в Большом Изыраке")
    assert a.place.name == "Большой Изырак"

    a = analyze("тренога на ул. Ленина у школы")
    assert a.street == "ленина" and "школа" in a.landmarks

    assert analyze("ДТП на 45-м км").road_km == 45


def test_plates():
    assert extract_plate("номер а123вс 54") == "А123ВС 54"
    assert extract_plate("машина A123BC154") == "А123ВС 154"   # латиница
    assert extract_plate("полиция у 1234 54 стоит") == "У1234 54"
    assert extract_plate("звоните 89001234567") is None


def test_normalize_abbr():
    n = normalize("ДПС у пов. на АЗС щас")
    assert "поворот" in n and "заправка" in n and "сейчас" in n
