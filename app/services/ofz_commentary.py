"""Детерминированный текст-движок для страниц разбора ОФЗ (/ofz/{номер}).

Без I/O и без LLM: facts (словарь чисел) -> предикаты -> готовые предложения.
Возвращается ЧИСТЫЙ текст (не HTML) — экранирует вызывающий.

Публичный интерфейс
-------------------
* ``build_comment(facts) -> list[str]`` — предложения комментария (абзацы
  собирает вызывающий). Правило, которому не хватает фактов, молчит.
* ``build_faq(facts) -> list[tuple[str, str]]`` — до 6 пар «вопрос, ответ».
* ``disclaimer(date_label) -> str`` и ``DISCLAIMER`` — текст дисклеймера.
* ``FORBIDDEN_PATTERNS`` / ``find_forbidden(text)`` — запрещённая лексика
  (для тестов и для T6: прогонять по ЛЮБОМУ тексту страницы).

Схема ``facts``
---------------
Все ключи необязательны; None и отсутствие ключа равнозначны (правило молчит).
Значения — int/float (bool, NaN, inf считаются отсутствующими). Проценты —
числа вида 16.23 (не 0.1623). Денежные суммы — рубли за ОДНУ бумагу.

Идентификация:
  secid        str   SECID MOEX, например "SU26238RMFS4". Участвует в выборе
                     варианта формулировки (md5(secid + имя правила)).
  number       str   номер выпуска, например "26238" (в вопросах FAQ).
  kind         str   'fixed' (ОФЗ-ПД) | 'float' (ПК) | 'linker' (ИН) |
                     'amort' (АД). Иной/пустой kind: блоки по типам молчат.

Цена и диапазон:
  close        float последняя цена, % номинала (например 61.35).
  min52, max52 float минимум и максимум цены за год, % номинала.
  pos52        float (close-min52)/(max52-min52), 0..1. Если None, а close,
                     min52, max52 есть — считается внутри. Нужен max52 > min52.
  discount_pct float 100 - close, п.п.: >0 дисконт, <0 премия. Если None,
                     считается из close.

Спред к кривой (игнорируется при kind='linker': ИН нельзя класть на
номинальную кривую):
  spread_bp         float YIELDCLOSE - curve(дюрация), базисные пункты, со
                          знаком (−16 = ниже кривой).
  spread_median_bp  float медиана spread_bp за год, б.п.

Чувствительность к ставке:
  md   float модифицированная дюрация, годы (>0).
  dp_up100_pct   float изменение цены при росте доходности на +100 б.п., %
                       (обычно отрицательное, например −5.9).
  dp_down100_pct float изменение цены при снижении доходности на −100 б.п.,
                       % (обычно положительное, например 6.4).
  conv_up_minus_down_pp float dp_down100_pct + dp_up100_pct, п.п. (разница
                       эффектов ±100 б.п.; >0 — эффект выпуклости. Значение
                       <=0 текст не выводит).
  p0_rub, p_up100_rub, p_down100_rub float цена бумаги в рублях: сейчас, при
                       +100 б.п., при −100 б.п. (p_down100_rub в тексте не
                       используется, но входит в схему для T6).

Доходность и купон (для kind 'fixed'/'amort'; у 'float'/'linker' ytm и
coupon_pct в сравнении «купон vs доходность» не используются):
  coupon_pct  float годовой купон, % от номинала.
  ytm         float доходность к погашению, % годовых.

Ликвидность:
  liq_rank        int   место по медианному дневному обороту (1 = самый
                        торгуемый) среди разбираемых выпусков.
  liq_total       int   сколько выпусков в этой выборке.
  liq_value_mln   float медианный дневной оборот, млн ₽.

Срок:
  years_to_maturity float лет до погашения.

Блок linker (kind='linker'):
  real_yield  float реальная доходность, % годовых (IRICPICLOSE).
  bei         float вменённая инфляция, % годовых (BEICLOSE).

Блок float (kind='float'):
  benchmark            str   название бенчмарка ("RUONIA"); может быть None.
  benchmark_spread_pp  float надбавка к бенчмарку, п.п. (0 — это число,
                             «RUONIA + 0,00 п.п.»; None — неизвестно).
  last_coupon_pct      float последний установленный купон, % годовых.
  Блок молчит, если нет ни benchmark_spread_pp, ни last_coupon_pct.

Блок amort (kind='amort'):
  next_amort_date  str   ближайшая амортизация, "YYYY-MM-DD" (иной формат
                         выводится как есть).
  amort_share_pct  float доля номинала в ближайшей амортизации, %.
  Блок молчит, если нет ни даты, ни доли.

Контракт текста: каждое предложение содержит число из facts (либо
константы сценария «1 п.п.» / «100 б.п.»), без оценок «дёшево/дорого» и
рекомендаций; выбор из 3–4 синонимичных формулировок детерминирован.
"""
import hashlib
import math
import re
from datetime import date

NBSP = " "
MINUS = "−"

# Дисклеймер — одна константа: юридическую формулировку правит владелец
# (бриф, п.10 спеки). {date} подставляет disclaimer().
DISCLAIMER = (
    "Страница сформирована автоматически из данных Московской биржи на {date}. "
    "Материал носит аналитический и справочный характер и не является "
    "индивидуальной инвестиционной рекомендацией; сценарные расчёты — "
    "модельные, прошлая динамика не определяет будущую. Bond AI не является "
    "брокером и не получает вознаграждения за упоминание выпусков."
)


def disclaimer(date_label: str) -> str:
    return DISCLAIMER.format(date=date_label)


# Единственная допустимая форма слова «рекомендация» — внутри дисклеймера.
_ALLOWED_PHRASE = "не является индивидуальной инвестиционной рекомендацией"

FORBIDDEN_PATTERNS = [
    r"\bкупи(?:ть|те)\b",
    r"\bпокупа(?:й|йте)\b",
    r"\bпродат[ьи]\b",
    r"\bпродайте\b",
    r"\bпродавайте\b",
    r"\bвыгодн\w*",
    r"\b(?:лучш|худш)\w*",
    r"\bстоит\s+взять\b",
    r"\bрекоменд\w*",
    r"\b(?:недо|пере)оценен\w*",
    r"\b(?:недо|пере)оценён\w*",
    r"\bдешев\w*",
    r"\bдёшев\w*",
    r"\bдорог(?:о|ой|ая|ое|ие)\b",
    r"\bиде(?:я|и|ю|ей)\b",
    r"\bцел[ьи]\s+по\s+цене\b",
    r"\bпрогноз\w*",
    r"\bгарантирован\w*",
    r"\bбезрисков\w*",
]
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS]
# «вырастет/упадёт» допустимо только в предложении с «при сценарии».
_PREDICT_RE = re.compile(r"\b(?:вырастет|упадёт|упадет)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def find_forbidden(text: str) -> list:
    """Список найденных запрещённых фрагментов (пусто — текст чистый)."""
    text = text.replace(_ALLOWED_PHRASE, "")
    found = []
    for rx in _FORBIDDEN_RE:
        found.extend(m.group(0) for m in rx.finditer(text))
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if "при сценарии" in sentence.lower():
            continue
        found.extend(m.group(0) for m in _PREDICT_RE.finditer(sentence))
    return found


# ---------------------------------------------------------------- форматирование

def _num(f, key):
    """float или None; bool/NaN/inf/нечисла считаются отсутствием факта."""
    v = f.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return float(v)


def _fmt(x, digits=1):
    """Русский формат: десятичная запятая, неразрывный пробел в тысячах,
    настоящий минус; «-0» не выводится."""
    s = f"{x:,.{digits}f}"
    if float(s.replace(",", "")) == 0:
        s = s.lstrip("-")
    s = s.replace(",", NBSP).replace(".", ",")
    if s.startswith("-"):
        s = MINUS + s[1:]
    return s


def _pct(x, digits=1):
    return f"{_fmt(x, digits)}{NBSP}%"


def _rub(x, digits=2):
    return f"{_fmt(x, digits)}{NBSP}₽"


def _bp(x):
    return f"{_fmt(x, 0)}{NBSP}б.п."


def _pp(x, digits=2):
    return f"{_fmt(x, digits)}{NBSP}п.п."


def _yrs(x):
    # Дробное число лет всегда в родительном падеже единственного числа
    # («6,8 года», «7,0 года») — склонять по числу не нужно.
    return f"{_fmt(x, 1)}{NBSP}года"


def _mln(x):
    return f"{_fmt(x, 1)}{NBSP}млн{NBSP}₽"


_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def _date_ru(s):
    try:
        d = date.fromisoformat(str(s)[:10])
    except ValueError:
        return str(s)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _pick(secid, rule, n):
    """Стабильный индекс варианта. Встроенный hash() не годится: он
    рандомизируется между процессами и страница «моргала» бы формулировками."""
    h = hashlib.md5(f"{secid}{rule}".encode("utf-8"), usedforsecurity=False)
    return int(h.hexdigest(), 16) % n


# ---------------------------------------------------------------------- правила
# Каждое правило возвращает список ВСЕХ синонимичных вариантов для текущей
# ветки (или None, если фактов не хватает). Выбор одного — в build_comment.

def _r_range52(f):
    close, lo, hi = _num(f, "close"), _num(f, "min52"), _num(f, "max52")
    if close is None or lo is None or hi is None or hi <= lo:
        return None
    pos = _num(f, "pos52")
    if pos is None:
        pos = (close - lo) / (hi - lo)
    c, a, b = _pct(close), _pct(lo), _pct(hi)
    if pos < 0.2:
        return [
            f"Цена {c} номинала — вблизи нижней границы годового диапазона {a}–{b}.",
            f"За год цена двигалась в пределах {a}–{b} номинала; сейчас она у нижней границы — {c}.",
            f"Текущая цена {c} номинала расположена в нижней части годового диапазона ({a}–{b}).",
        ]
    if pos > 0.8:
        return [
            f"Цена {c} номинала — вблизи верхней границы годового диапазона {a}–{b}.",
            f"За год цена двигалась в пределах {a}–{b} номинала; сейчас она у верхней границы — {c}.",
            f"Текущая цена {c} номинала расположена в верхней части годового диапазона ({a}–{b}).",
        ]
    return [
        f"Цена {c} номинала — в середине годового диапазона {a}–{b}.",
        f"За год цена двигалась в пределах {a}–{b} номинала; сейчас она в средней части — {c}.",
        f"Текущая цена {c} номинала расположена между границами годового диапазона ({a}–{b}).",
    ]


def _r_spread(f):
    if f.get("kind") == "linker":
        return None
    s, m = _num(f, "spread_bp"), _num(f, "spread_median_bp")
    if s is None or m is None:
        return None
    d = s - m
    ss, ms, ds = _bp(s), _bp(m), _bp(abs(d))
    if abs(d) < 10:
        return [
            f"Спред к кривой ОФЗ ({ss}) находится около своей годовой медианы ({ms}).",
            f"Спред доходности к кривой ОФЗ — {ss}; годовая медиана — {ms}, отклонение небольшое.",
            f"Относительно кривой ОФЗ выпуск торгуется со спредом {ss}, что близко к его медиане за год ({ms}).",
        ]
    if d > 0:
        return [
            f"Спред к кривой ({ss}) выше годовой медианы ({ms}) на {ds}: доходность выпуска относительно кривой на той же дюрации выше, чем обычно за год.",
            f"Относительно кривой ОФЗ спред составляет {ss} — на {ds} выше своей годовой медианы ({ms}).",
            f"Текущий спред к кривой — {ss} при годовой медиане {ms}; он выше медианы на {ds}.",
        ]
    return [
        f"Спред к кривой ({ss}) ниже годовой медианы ({ms}) на {ds}: доходность выпуска относительно кривой на той же дюрации ниже, чем обычно за год.",
        f"Относительно кривой ОФЗ спред составляет {ss} — на {ds} ниже своей годовой медианы ({ms}).",
        f"Текущий спред к кривой — {ss} при годовой медиане {ms}; он ниже медианы на {ds}.",
    ]


def _r_coupon_ytm(f):
    if f.get("kind") not in ("fixed", "amort"):
        return None
    cp, y = _num(f, "coupon_pct"), _num(f, "ytm")
    if cp is None or y is None:
        return None
    disc = _num(f, "discount_pct")
    if disc is None:
        close = _num(f, "close")
        disc = None if close is None else 100.0 - close
    c, ys = _pct(cp), _pct(y)
    if disc is not None and cp < y and disc > 0:
        d = _pct(disc)
        return [
            f"Купон {c} годовых ниже доходности к погашению ({ys}), поэтому выпуск торгуется с дисконтом {d} к номиналу, и часть дохода приходится на рост цены к погашению.",
            f"При купоне {c} и доходности {ys} цена ниже номинала на {d}: к погашению часть дохода обеспечивает сближение цены с номиналом.",
            f"Доходность к погашению ({ys}) превышает купон ({c}), что соответствует дисконту {d} к номиналу.",
        ]
    if disc is not None and cp > y and disc < 0:
        d = _pct(abs(disc))
        return [
            f"Купон {c} годовых выше доходности к погашению ({ys}), поэтому выпуск торгуется с премией {d} к номиналу, которая к погашению по расчёту сходится к нулю.",
            f"При купоне {c} и доходности {ys} цена выше номинала на {d}: к погашению эта премия амортизируется.",
            f"Доходность к погашению ({ys}) ниже купона ({c}), что соответствует премии {d} к номиналу.",
        ]
    return [
        f"Купон составляет {c} годовых, доходность к погашению — {ys}.",
        f"Выпуск имеет купон {c} годовых; расчётная доходность к погашению — {ys}.",
        f"Годовой купон — {c}, доходность к погашению по текущей цене — {ys}.",
    ]


def _r_linker(f):
    if f.get("kind") != "linker":
        return None
    r, b = _num(f, "real_yield"), _num(f, "bei")
    if r is not None and b is not None:
        rs, bs = _pct(r, 2), _pct(b, 2)
        return [
            f"Реальная доходность выпуска — {rs}, вменённая инфляция (BEI) — {bs}; номинал индексируется на инфляцию, поэтому реальную доходность нельзя напрямую сравнивать с доходностями ОФЗ с фиксированным купоном.",
            f"Цена выпуска соответствует реальной доходности {rs} и вменённой инфляции {bs}; это шкала сверх инфляции, не сопоставимая с номинальными доходностями.",
            f"По данным биржи реальная доходность составляет {rs}, а вменённая инфляция (BEI) — {bs}; сравнивать эти числа с номинальной кривой ОФЗ некорректно.",
        ]
    if r is not None:
        rs = _pct(r, 2)
        return [
            f"Реальная доходность выпуска — {rs}: это доходность сверх инфляции, её нельзя напрямую сравнивать с номинальными доходностями ОФЗ.",
            f"Цена выпуска соответствует реальной доходности {rs}; номинал индексируется на инфляцию.",
            f"По данным биржи реальная доходность составляет {rs} годовых сверх индексации номинала.",
        ]
    if b is not None:
        bs = _pct(b, 2)
        return [
            f"Вменённая инфляция (BEI), заложенная в цену выпуска, — {bs}.",
            f"Разница между номинальной и реальной доходностью даёт вменённую инфляцию (BEI) — {bs}.",
            f"По данным биржи вменённая инфляция (BEI) составляет {bs}.",
        ]
    return None


def _r_float(f):
    if f.get("kind") != "float":
        return None
    s, lc = _num(f, "benchmark_spread_pp"), _num(f, "last_coupon_pct")
    if s is None and lc is None:
        return None
    bench = f.get("benchmark")
    bench = bench.strip() if isinstance(bench, str) and bench.strip() else None
    parts_a = "Купон плавающий" + (f" и привязан к {bench}" if bench else "")
    if s is not None:
        parts_a += f" с надбавкой {_pp(s)}"
    last = f"последний установленный купон — {_pct(lc, 2)} годовых" if lc is not None else None
    tail = f"; {last}" if last else ""
    alt_head = "Ставка купона зависит от " + (bench if bench else "плавающей ставки")
    if s is not None:
        alt_head += f", надбавка составляет {_pp(s)}"
    if last:
        return [
            parts_a + tail + ".",
            alt_head + f"; {last}.",
            f"Размер будущих купонов заранее неизвестен: {last}" + (
                f", надбавка к {bench or 'базовой ставке'} — {_pp(s)}" if s is not None else "") + ".",
        ]
    return [
        parts_a + ".",
        alt_head + ".",
        f"Размер будущих купонов заранее неизвестен; надбавка к {bench or 'базовой ставке'} — {_pp(s)}.",
    ]


def _r_amort(f):
    if f.get("kind") != "amort":
        return None
    share, dt = _num(f, "amort_share_pct"), f.get("next_amort_date")
    dt = _date_ru(dt) if dt else None
    if share is not None and dt:
        sh = _pct(share)
        return [
            f"Номинал гасится частями: ближайшая амортизация — {dt}, {sh} номинала.",
            f"Ближайшая амортизационная выплата приходится на {dt} и составляет {sh} номинала.",
            f"Выпуск амортизируется: {dt} запланировано погашение {sh} номинала.",
        ]
    if share is not None:
        sh = _pct(share)
        return [
            f"Номинал гасится частями: ближайшая амортизация — {sh} номинала.",
            f"Ближайшая амортизационная выплата составляет {sh} номинала.",
            f"Выпуск амортизируется: ближайшая выплата в счёт погашения номинала — {sh}.",
        ]
    if dt:
        return [
            f"Номинал гасится частями: ближайшая амортизация — {dt}.",
            f"Ближайшая амортизационная выплата приходится на {dt}.",
            f"Выпуск амортизируется: ближайшая выплата в счёт погашения номинала — {dt}.",
        ]
    return None


def _r_duration(f):
    md, y = _num(f, "md"), _num(f, "years_to_maturity")
    if md is not None and y is not None:
        return [
            f"Модифицированная дюрация выпуска — {_yrs(md)} при сроке до погашения {_yrs(y)}.",
            f"До погашения {_yrs(y)}, модифицированная дюрация — {_yrs(md)}: чувствительность цены к доходности определяется дюрацией, а не сроком.",
            f"Срок до погашения составляет {_yrs(y)}, а модифицированная дюрация — {_yrs(md)}.",
        ]
    if md is not None:
        return [
            f"Модифицированная дюрация выпуска — {_yrs(md)}.",
            f"Чувствительность цены к доходности описывает модифицированная дюрация: {_yrs(md)}.",
            f"Модифицированная дюрация по текущей цене составляет {_yrs(md)}.",
        ]
    if y is not None:
        return [
            f"До погашения {_yrs(y)}.",
            f"Срок до погашения выпуска — {_yrs(y)}.",
            f"Погашение запланировано через {_yrs(y)}.",
        ]
    return None


def _r_scenario(f):
    up, dn = _num(f, "dp_up100_pct"), _num(f, "dp_down100_pct")
    if up is None:
        return None
    p0, pu = _num(f, "p0_rub"), _num(f, "p_up100_rub")
    verb_up = "снижается" if up < 0 else "растёт"
    rub = f" — с {_rub(p0)} до {_rub(pu)} за бумагу" if p0 is not None and pu is not None else ""
    ups = _pct(abs(up))
    if dn is None:
        return [
            f"При сценарии роста доходности на 1{NBSP}п.п. модельная цена {verb_up} примерно на {ups}{rub}.",
            f"Если доходность выпуска сместится на +1{NBSP}п.п. (сценарий), расчётная цена {verb_up} примерно на {ups}{rub}.",
            f"Модельное изменение цены при сдвиге доходности на +100{NBSP}б.п. — {ups}{rub}.",
        ]
    verb_dn = "снижается" if dn < 0 else "растёт"
    dns = _pct(abs(dn))
    return [
        f"При сценарии роста доходности на 1{NBSP}п.п. модельная цена {verb_up} примерно на {ups}{rub}; при сценарии снижения на 1{NBSP}п.п. она {verb_dn} примерно на {dns}.",
        f"Если доходность выпуска сместится на +1{NBSP}п.п. (сценарий), расчётная цена {verb_up} примерно на {ups}{rub}; при сдвиге на −1{NBSP}п.п. она {verb_dn} на {dns}.",
        f"Модельное изменение цены при сдвиге доходности на +100{NBSP}б.п. — {ups}{rub}, на −100{NBSP}б.п. — {dns}.",
    ]


def _r_convexity(f):
    c = _num(f, "conv_up_minus_down_pp")
    if c is None or c <= 0:
        return None
    cs = _pp(c)
    return [
        f"Разница между эффектами снижения и роста доходности на 1{NBSP}п.п. составляет {cs} — это эффект выпуклости: при снижении доходности цена растёт сильнее, чем снижается при её росте на столько же.",
        f"Эффект выпуклости: при сдвиге доходности на ±1{NBSP}п.п. цена реагирует несимметрично, разница составляет {cs}.",
        f"Из-за выпуклости цена при снижении доходности на 1{NBSP}п.п. меняется сильнее, чем при её росте на столько же; разница — {cs}.",
    ]


def _r_liquidity(f):
    rank, total = _num(f, "liq_rank"), _num(f, "liq_total")
    val = _num(f, "liq_value_mln")
    has_rank = rank is not None and total is not None and rank >= 1
    if has_rank and val is not None:
        r, t, v = _fmt(rank, 0), _fmt(total, 0), _mln(val)
        return [
            f"По медианному дневному обороту ({v}) выпуск занимает {r}-е место из {t} разбираемых выпусков ОФЗ.",
            f"Медианный дневной оборот — {v}; по этому показателю выпуск на {r}-м месте из {t}.",
            f"Среди разбираемых выпусков ОФЗ он занимает {r}-е место из {t} при медианном обороте {v} в день.",
        ]
    if has_rank:
        r, t = _fmt(rank, 0), _fmt(total, 0)
        return [
            f"По медианному дневному обороту выпуск занимает {r}-е место из {t} разбираемых выпусков ОФЗ.",
            f"По обороту торгов выпуск находится на {r}-м месте из {t} выпусков ОФЗ в выборке.",
            f"Место выпуска по медианному обороту — {r}-е из {t}.",
        ]
    if val is not None:
        v = _mln(val)
        return [
            f"Медианный дневной оборот выпуска — {v}.",
            f"В среднем за торговый день проходит оборот около {v} (медиана).",
            f"Медианный объём торгов за день составляет {v}.",
        ]
    return None


# Порядок правил = порядок предложений в тексте.
_RULES = [
    ("range52", _r_range52),
    ("spread", _r_spread),
    ("coupon_ytm", _r_coupon_ytm),
    ("linker", _r_linker),
    ("float", _r_float),
    ("amort", _r_amort),
    ("duration", _r_duration),
    ("scenario", _r_scenario),
    ("convexity", _r_convexity),
    ("liquidity", _r_liquidity),
]


def _tidy(text: str) -> str:
    # Единицы «б.п.»/«п.п.» кончаются точкой, а шаблон добавляет свою — без
    # склейки в тексте получалось «на 28 б.п..».
    while ".." in text:
        text = text.replace("..", ".")
    return text


def build_comment(facts: dict) -> list:
    if not isinstance(facts, dict) or not facts:
        return []
    secid = str(facts.get("secid") or "")
    out = []
    for name, rule in _RULES:
        variants = rule(facts)
        if variants:
            out.append(_tidy(variants[_pick(secid, name, len(variants))]))
    return out


def _all_variants(facts: dict) -> list:
    """ТОЛЬКО для тестов: все варианты всех активных правил (без выбора)."""
    if not isinstance(facts, dict) or not facts:
        return []
    out = []
    for _name, rule in _RULES:
        out.extend(_tidy(v) for v in rule(facts) or [])
    return out


# -------------------------------------------------------------------------- FAQ

_KIND_LABEL = {"fixed": "ОФЗ-ПД", "float": "ОФЗ-ПК", "linker": "ОФЗ-ИН", "amort": "ОФЗ-АД"}


def _faq_label(f):
    base = _KIND_LABEL.get(f.get("kind"), "ОФЗ")
    number = f.get("number")
    return f"{base} {number}" if number else base


def _faq_duration(f, label):
    md, y = _num(f, "md"), _num(f, "years_to_maturity")
    if md is None:
        return None
    a = (f"Модифицированная дюрация выпуска — {_yrs(md)}. Она показывает приближённую "
         f"чувствительность цены к доходности: при изменении доходности на 1{NBSP}п.п. цена "
         f"меняется примерно на {_pct(md)}.")
    if y is not None:
        a += f" Срок до погашения — {_yrs(y)}."
    return (f"Что такое дюрация {label}?", a)


def _faq_sensitivity(f, label):
    up, dn = _num(f, "dp_up100_pct"), _num(f, "dp_down100_pct")
    if up is None:
        return None
    p0, pu = _num(f, "p0_rub"), _num(f, "p_up100_rub")
    rub = f" (с {_rub(p0)} до {_rub(pu)} за бумагу)" if p0 is not None and pu is not None else ""
    verb = "снижается" if up < 0 else "растёт"
    a = (f"При сценарии роста доходности на 1{NBSP}п.п. модельная цена {verb} примерно "
         f"на {_pct(abs(up))}{rub}.")
    if dn is not None:
        a += (f" При сценарии снижения на 1{NBSP}п.п. она {'растёт' if dn >= 0 else 'снижается'} "
              f"примерно на {_pct(abs(dn))}.")
    a += " Это расчёт по дюрации с поправкой на выпуклость, а не оценка будущего движения рынка."
    return (f"Как изменится цена {label} при росте ставки на 1{NBSP}п.п.?", a)


def _faq_kind(f, label):
    kind = f.get("kind")
    if kind == "linker":
        r, b = _num(f, "real_yield"), _num(f, "bei")
        if r is None and b is None:
            return None
        a = ("Номинал ИН-ОФЗ индексируется на инфляцию, а купон начисляется на индексированный номинал.")
        if r is not None:
            a += (f" Реальная доходность выпуска — {_pct(r, 2)}: это доходность сверх инфляции, "
                  "её нельзя напрямую сравнивать с доходностями ОФЗ с фиксированным купоном.")
        if b is not None:
            a += f" Вменённая инфляция (BEI), заложенная в цену, — {_pct(b, 2)}."
        return (f"Чем {label} отличается от обычной ОФЗ?", a)
    if kind == "float":
        s, lc = _num(f, "benchmark_spread_pp"), _num(f, "last_coupon_pct")
        if s is None and lc is None:
            return None
        bench = f.get("benchmark")
        bench = bench.strip() if isinstance(bench, str) and bench.strip() else None
        a = "Ставка купона плавающая" + (f" и привязана к {bench}" if bench else "")
        if s is not None:
            a += f", надбавка составляет {_pp(s)}"
        a += "."
        if lc is not None:
            a += f" Последний установленный купон — {_pct(lc, 2)} годовых."
        a += (" Размер будущих купонов заранее неизвестен, поэтому единая доходность "
              "к погашению для таких выпусков не приводится.")
        return (f"Как считается купон {label}?", a)
    if kind == "amort":
        share, dt = _num(f, "amort_share_pct"), f.get("next_amort_date")
        if share is None and not dt:
            return None
        a = "Номинал гасится частями, а не одной выплатой в дату погашения."
        if dt and share is not None:
            a += f" Ближайшая амортизация — {_date_ru(dt)}, {_pct(share)} номинала."
        elif share is not None:
            a += f" Ближайшая амортизация составляет {_pct(share)} номинала."
        else:
            a += f" Ближайшая амортизация — {_date_ru(dt)}."
        a += " По мере амортизации уменьшается база, на которую начисляется купон."
        return (f"Как погашается номинал {label}?", a)
    return None


def _faq_coupon(f, label):
    if f.get("kind") not in ("fixed", "amort"):
        return None
    cp, y, close = _num(f, "coupon_pct"), _num(f, "ytm"), _num(f, "close")
    if cp is None and y is None:
        return None
    parts = []
    if cp is not None:
        parts.append(f"Купон — {_pct(cp)} годовых от номинала")
    if close is not None:
        parts.append(f"цена на последних торгах — {_pct(close)} номинала")
    if y is not None:
        parts.append(f"доходность к погашению по этой цене — {_pct(y)}")
    a = "; ".join(parts) + "."
    return (f"Какой купон и какая доходность у {label}?", a)


def _faq_range(f, label):
    close, lo, hi = _num(f, "close"), _num(f, "min52"), _num(f, "max52")
    if lo is None or hi is None:
        return None
    a = f"За год цена двигалась в диапазоне от {_pct(lo)} до {_pct(hi)} номинала"
    a += f"; на последних торгах — {_pct(close)}." if close is not None else "."
    return (f"В каком диапазоне торговалась цена {label} за год?", a)


def _faq_spread(f, label):
    if f.get("kind") == "linker":
        return None
    s, m = _num(f, "spread_bp"), _num(f, "spread_median_bp")
    if s is None:
        return None
    a = (f"Спред доходности выпуска к кривой ОФЗ на той же дюрации — {_bp(s)}"
         + (f", медиана за год — {_bp(m)}." if m is not None else "."))
    return (f"Как {label} расположена относительно кривой доходности ОФЗ?", a)


def _faq_liquidity(f, label):
    rank, total, val = _num(f, "liq_rank"), _num(f, "liq_total"), _num(f, "liq_value_mln")
    if val is None:
        return None
    a = f"Медианный дневной оборот выпуска — {_mln(val)}"
    if rank is not None and total is not None and rank >= 1:
        a += f"; по этому показателю он на {_fmt(rank, 0)}-м месте из {_fmt(total, 0)} разбираемых выпусков."
    else:
        a += "."
    return (f"Насколько ликвидна {label}?", a)


_FAQ_BUILDERS = [_faq_duration, _faq_sensitivity, _faq_kind, _faq_coupon,
                 _faq_range, _faq_spread, _faq_liquidity]
_FAQ_MAX = 6


def build_faq(facts: dict) -> list:
    """До 6 пар (вопрос, ответ) в порядке важности; вопрос без фактов
    для ответа не создаётся. Ответы содержат числа из facts."""
    if not isinstance(facts, dict) or not facts:
        return []
    label = _faq_label(facts)
    out = []
    for builder in _FAQ_BUILDERS:
        qa = builder(facts, label)
        if qa:
            out.append((qa[0], _tidy(qa[1])))
    return out[:_FAQ_MAX]
