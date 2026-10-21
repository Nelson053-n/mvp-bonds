"""Тесты текст-движка разбора ОФЗ (app/services/ofz_commentary.py)."""
import copy
import itertools
import re
import subprocess
import sys

import pytest

from app.services import ofz_commentary as oc

NB = " "

FIXED = {
    "secid": "SU26238RMFS4", "number": "26238", "kind": "fixed",
    "close": 61.35, "min52": 55.1, "max52": 70.2, "pos52": 0.4,
    "spread_bp": -16.0, "spread_median_bp": -5.0,
    "md": 6.26, "conv_up_minus_down_pp": 0.46,
    "dp_up100_pct": -5.94, "dp_down100_pct": 6.4,
    "p0_rub": 613.5, "p_up100_rub": 577.05, "p_down100_rub": 652.77,
    "coupon_pct": 7.1, "ytm": 16.23,
    "liq_rank": 3, "liq_total": 50, "liq_value_mln": 1450.0,
    "years_to_maturity": 14.4, "discount_pct": 38.65,
}
LINKER = {
    "secid": "SU52002RMFS3", "number": "52002", "kind": "linker",
    "close": 98.2, "min52": 92.0, "max52": 99.0,
    "spread_bp": 10.0, "spread_median_bp": 5.0,  # для ИН должен игнорироваться
    "md": 3.1, "dp_up100_pct": -3.0, "dp_down100_pct": 3.2,
    "conv_up_minus_down_pp": 0.2, "real_yield": 8.76, "bei": 5.32,
    "liq_rank": 40, "liq_total": 50, "liq_value_mln": 12.5,
    "years_to_maturity": 5.0,
}
FLOAT = {
    "secid": "SU29016RMFS8", "number": "29016", "kind": "float",
    "close": 99.8, "min52": 98.0, "max52": 100.4,
    "md": 0.2, "dp_up100_pct": -0.2, "dp_down100_pct": 0.2,
    "benchmark": "RUONIA", "benchmark_spread_pp": 0.0, "last_coupon_pct": 15.9,
    "liq_rank": 5, "liq_total": 50, "liq_value_mln": 800.0,
    "years_to_maturity": 3.5,
}
AMORT = {
    "secid": "SU46020RMFS1", "number": "46020", "kind": "amort",
    "close": 80.5, "min52": 70.0, "max52": 82.0,
    "coupon_pct": 6.9, "ytm": 14.2, "discount_pct": 19.5,
    "md": 5.0, "dp_up100_pct": -4.8, "dp_down100_pct": 5.1,
    "conv_up_minus_down_pp": 0.3,
    "next_amort_date": "2034-03-15", "amort_share_pct": 25.0,
    "years_to_maturity": 9.0,
}


def _grid():
    """Наборы фактов, покрывающие все ветки правил (spread: рядом/выше/ниже,
    pos52: низ/середина/верх, купон vs доходность: дисконт/премия/нейтрально,
    разные виды)."""
    out = [FIXED, LINKER, FLOAT, AMORT]
    for delta, close, (cp, ytm, disc) in itertools.product(
            (0.0, 30.0, -30.0), (56.0, 62.0, 69.5),
            ((7.1, 16.0, 38.0), (14.0, 9.0, -5.0), (10.0, 10.0, 0.0))):
        f = copy.deepcopy(FIXED)
        f.update(spread_bp=-5.0 + delta, close=close, pos52=None,
                 coupon_pct=cp, ytm=ytm, discount_pct=disc)
        out.append(f)
    # частичные блоки: все ветки linker / float / amort
    for drop in (("real_yield",), ("bei",)):
        f = copy.deepcopy(LINKER)
        for k in drop:
            f.pop(k)
        out.append(f)
    for drop in (("last_coupon_pct",), ("benchmark_spread_pp",), ("benchmark",)):
        f = copy.deepcopy(FLOAT)
        for k in drop:
            f.pop(k)
        out.append(f)
    for drop in (("next_amort_date",), ("amort_share_pct",)):
        f = copy.deepcopy(AMORT)
        for k in drop:
            f.pop(k)
        out.append(f)
    f = copy.deepcopy(FIXED)  # без рублёвых цен и без down-сценария
    for k in ("p0_rub", "p_up100_rub", "dp_down100_pct", "liq_rank", "years_to_maturity"):
        f.pop(k)
    out.append(f)
    f = copy.deepcopy(FIXED)
    f.pop("liq_rank"), f.pop("md")
    out.append(f)
    f = copy.deepcopy(FIXED)
    for k in ("md", "liq_rank", "liq_value_mln"):
        f.pop(k)
    out.append(f)
    f = copy.deepcopy(FIXED)
    for k in ("liq_value_mln", "md"):
        f.pop(k)
    out.append(f)
    return out


def _all_texts(facts):
    texts = list(oc._all_variants(facts))
    texts += oc.build_comment(facts)
    for q, a in oc.build_faq(facts):
        texts += [q, a]
    return texts


# --- (1) запрещённые слова -------------------------------------------------

@pytest.mark.parametrize("facts", _grid(), ids=lambda f: f"{f['kind']}-{id(f) % 9973}")
def test_no_forbidden_words_in_any_variant(facts):
    texts = _all_texts(facts)
    assert texts
    for t in texts:
        assert oc.find_forbidden(t) == [], t


def test_find_forbidden_detector_catches_each_category():
    bad = ["Это выгодно.", "Лучший выбор", "Стоит взять выпуск", "рекомендуем",
           "недооценена", "Переоценён", "дёшево", "дорого", "Идея дня", "прогноз",
           "цель по цене 100", "гарантированно", "безрисково", "купите", "продавайте",
           "Цена вырастет."]
    for t in bad:
        assert oc.find_forbidden(t), t
    assert oc.find_forbidden("При сценарии роста цена вырастет.") == []
    # «купон» — не «купить»; рекомендация допустима только в дисклеймере
    assert oc.find_forbidden("Купон 7,1 %") == []
    assert oc.find_forbidden(oc.disclaimer("21.10.2026")) == []
    assert oc.find_forbidden("Это рекомендация.")


def test_disclaimer_has_date_placeholder():
    s = oc.disclaimer("21.10.2026 16:10")
    assert "21.10.2026 16:10" in s and "{date}" not in s
    assert "не является индивидуальной инвестиционной рекомендацией" in s


# --- (2) числа ∈ facts -----------------------------------------------------

_NUM_RE = re.compile(r"\d{1,3}(?: \d{3})+(?:,\d+)?|\d+(?:,\d+)?")
# Константы сценария («1 п.п.», «100 б.п.») зашиты в схему, а не в facts.
_CONSTANTS = {1.0, 100.0}


def _allowed(facts):
    vals = set(_CONSTANTS)
    nums = [v for v in facts.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for v in nums:
        for d in (0, 1, 2):
            vals.add(round(v, d))
            vals.add(round(abs(v), d))
    s, m = facts.get("spread_bp"), facts.get("spread_median_bp")
    if s is not None and m is not None:
        for d in (0, 1, 2):
            vals.add(round(abs(s - m), d))
    disc = facts.get("discount_pct")
    if disc is not None:
        for d in (0, 1, 2):
            vals.add(round(abs(disc), d))
    elif facts.get("close") is not None:
        for d in (0, 1, 2):
            vals.add(round(abs(100 - facts["close"]), d))
    if facts.get("number"):
        vals.add(float(facts["number"]))
    if facts.get("next_amort_date"):
        y, mo, d_ = (int(x) for x in facts["next_amort_date"].split("-"))
        vals.update({float(y), float(d_)})
    return vals


def _numbers(text):
    return [float(n.replace(NB, "").replace(",", ".")) for n in _NUM_RE.findall(text)]


@pytest.mark.parametrize("facts", _grid(), ids=lambda f: f"{f['kind']}-{id(f) % 9973}")
def test_every_variant_has_number_and_numbers_come_from_facts(facts):
    allowed = _allowed(facts)
    for t in oc._all_variants(facts):
        nums = _numbers(t)
        assert nums, f"нет числа: {t}"
        for n in nums:
            assert round(n, 2) in allowed, f"{n} не из facts: {t}"
    for q, a in oc.build_faq(facts):
        nums = _numbers(a)
        assert nums, f"нет числа в ответе: {a}"
        for n in nums:
            assert round(n, 2) in allowed, f"{n} не из facts: {a}"


def test_russian_number_format():
    assert oc._fmt(1778.41, 2) == f"1{NB}778,41"
    assert oc._fmt(-16.0, 0) == "−16"
    assert oc._fmt(-0.2, 0) == "0"  # не «−0»
    assert oc._pct(16.23, 2) == f"16,23{NB}%"
    assert oc._rub(613.5) == f"613,50{NB}₽"
    assert oc._bp(-16) == f"−16{NB}б.п."
    assert oc._pp(0.0) == f"0,00{NB}п.п."
    text = " ".join(oc.build_comment(FIXED))
    assert not re.search(r"\d\.\d", text)  # десятичной точки нет
    assert "<" not in text and ">" not in text


def test_no_html_in_output():
    for f in _grid():
        for t in _all_texts(f):
            assert "<" not in t and ">" not in t and "&" not in t


# --- (3) стабильность ------------------------------------------------------

def test_deterministic_two_calls_equal():
    for f in _grid():
        assert oc.build_comment(f) == oc.build_comment(copy.deepcopy(f))
        assert oc.build_faq(f) == oc.build_faq(copy.deepcopy(f))


def test_stable_across_processes():
    """Встроенный hash() рандомизируется между процессами; md5 — нет."""
    code = (
        "import json;from tests.test_ofz_commentary import FIXED;"
        "from app.services.ofz_commentary import build_comment;"
        "print(json.dumps(build_comment(FIXED),ensure_ascii=False))"
    )
    outs = set()
    for seed in ("1", "2", "3"):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "", "MVP_JWT_SECRET": "x"},
                           cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout)
    assert len(outs) == 1


def test_different_secids_give_different_variants():
    seen = set()
    for i in range(40):
        f = dict(FIXED, secid=f"SU{26000 + i}RMFS{i % 10}")
        seen.add(tuple(oc.build_comment(f)))
    assert len(seen) >= 4


def test_variants_have_three_or_four_options_per_rule():
    for f in _grid():
        for name, rule in oc._RULES:
            v = rule(f)
            if v:
                assert 3 <= len(v) <= 4, name


# --- (4) пустые факты ------------------------------------------------------

def test_empty_facts_give_nothing():
    assert oc.build_comment({}) == []
    assert oc.build_faq({}) == []
    assert oc.build_comment({"secid": "X", "number": "1", "kind": "fixed"}) == []
    assert oc.build_faq({"secid": "X", "number": "1", "kind": "fixed"}) == []
    none_facts = {k: None for k in FIXED}
    none_facts.update(secid="X", kind="fixed")
    assert oc.build_comment(none_facts) == []
    assert oc.build_faq(none_facts) == []
    assert oc._all_variants(none_facts) == []


def test_garbage_values_are_treated_as_missing():
    f = {"secid": "X", "kind": "fixed", "close": float("nan"), "min52": "a",
         "max52": True, "md": float("inf"), "spread_bp": None}
    assert oc.build_comment(f) == []


def test_rules_stay_silent_without_their_own_facts():
    only_md = {"secid": "X", "md": 4.0}
    out = oc.build_comment(only_md)
    assert len(out) == 1 and "4,0" in out[0]
    assert oc.build_comment({"kind": "linker", "close": 1.0}) == []
    assert oc.build_comment({"kind": "float", "benchmark": "RUONIA"}) == []


# --- (5) блоки по kind -----------------------------------------------------

def test_linker_has_real_yield_block_fixed_does_not():
    lk = " ".join(oc.build_comment(LINKER))
    assert "8,76" in lk and "5,32" in lk
    fx = " ".join(oc.build_comment(FIXED))
    assert "Реальн" not in fx and "BEI" not in fx
    assert not any("Реальн" in v for v in oc._all_variants(FIXED))
    # ИН не кладётся на номинальную кривую
    assert not any("пред" in s.lower() for s in oc.build_comment(LINKER))
    assert oc._r_spread(LINKER) is None


def test_kind_blocks_ignore_foreign_facts():
    fx = dict(FIXED, real_yield=8.76, bei=5.32, benchmark_spread_pp=0.0,
              last_coupon_pct=15.9, amort_share_pct=25.0)
    txt = " ".join(oc.build_comment(fx))
    assert "Реальн" not in txt and "BEI" not in txt
    assert "Купон плавающий" not in txt and "амортиз" not in txt.lower()


def test_float_amort_blocks_only_for_their_kind():
    fl = " ".join(oc.build_comment(FLOAT))
    assert "RUONIA" in fl and "0,00" in fl and "15,90" in fl
    am = " ".join(oc.build_comment(AMORT))
    assert "15 марта 2034" in am and "25,0" in am
    assert "RUONIA" not in " ".join(oc.build_comment(FIXED))
    assert "амортизац" not in " ".join(oc.build_comment(FIXED)).lower()
    assert "купон плавающий" not in " ".join(oc.build_comment(AMORT)).lower()


def test_spread_branches():
    def sp(s, m):
        return oc._r_spread(dict(FIXED, spread_bp=s, spread_median_bp=m))[0]
    assert "около" in sp(-5, -8)
    assert "выше годовой медианы" in sp(20, -5)
    assert "ниже годовой медианы" in sp(-40, -5)


def test_range_branches():
    def rg(close):
        return oc._r_range52(dict(FIXED, close=close, pos52=None))[0]
    assert "нижней" in rg(56.0)
    assert "верхней" in rg(69.5)
    assert "середине" in rg(62.0)
    assert oc._r_range52(dict(FIXED, min52=70.2, max52=70.2)) is None


# --- FAQ -------------------------------------------------------------------

def test_faq_size_and_kinds():
    for f in (FIXED, LINKER, FLOAT, AMORT):
        faq = oc.build_faq(f)
        assert 4 <= len(faq) <= 6, (f["kind"], len(faq))
        assert len({q for q, _ in faq}) == len(faq)
    qs = lambda f: " ".join(q for q, _ in oc.build_faq(f))
    assert "отличается" in qs(LINKER) and "отличается" not in qs(FIXED)
    assert "Как считается купон" in qs(FLOAT) and "Как считается купон" not in qs(FIXED)
    assert "Как погашается номинал" in qs(AMORT)
    assert "26238" in qs(FIXED) and "дюрация" in qs(FIXED)
    assert "ОФЗ-ИН 52002" in qs(LINKER)


def test_faq_answers_contain_facts_values():
    faq = dict(oc.build_faq(FIXED))
    dur = next(a for q, a in faq.items() if "дюрация" in q)
    assert "6,3" in dur and "14,4" in dur
    sens = next(a for q, a in faq.items() if "росте ставки" in q)
    assert "5,9" in sens and "613,50" in sens and "577,05" in sens


def test_no_double_dot_after_units():
    # «б.п.» + точка шаблона давали «..» в тексте страницы.
    facts = dict(secid="SU26238RMFS4", number="26238", kind="fixed", close=51.5,
                 min52=48.0, max52=58.0, spread_bp=-16, spread_median_bp=12,
                 md=5.8, dp_up100_pct=-5.6, dp_down100_pct=6.2,
                 conv_up_minus_down_pp=0.6, p0_rub=515, p_up100_rub=487,
                 p_down100_rub=547, coupon_pct=7.1, ytm=16.2,
                 liq_rank=40, liq_total=50, liq_value_mln=120.0,
                 years_to_maturity=6.8)
    texts = oc._all_variants(facts) + oc.build_comment(facts)
    texts += [a for _q, a in oc.build_faq(facts)]
    assert texts and not [t for t in texts if ".." in t]
    # место 40 из 50 не называется «одним из наиболее торгуемых»
    assert not [t for t in texts if "наиболее торгуем" in t]
