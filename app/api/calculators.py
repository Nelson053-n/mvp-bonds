"""Public SEO calculator pages: /calc (index), /calc/nkd, /calc/ytm.

Fully static, client-side vanilla-JS calculators wrapped in the shared public
page shell from bond_pages. Rendered once per process and served with public
Cache-Control — zero per-request work.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.bond_pages import _BASE_URL, _page_shell

router = APIRouter(tags=["public-calculators"])

_CACHE_HEADERS = {"Cache-Control": "public, max-age=300, stale-while-revalidate=86400"}

# Extra styles for calculator forms (on top of the shared shell CSS).
_CALC_CSS = """<style>
.calc-card{background:var(--panel);border:1px solid var(--line-2);
border-radius:var(--radius-lg);padding:22px;margin:18px 0}
.calc-card h2{margin:0 0 16px;font-size:17px;border:none;padding:0}
.calc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.field label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:5px}
.field input,.field select{width:100%;background:var(--input-bg);border:1px solid var(--line-3);
border-radius:var(--radius);color:var(--text-strong);padding:9px 11px;font-size:14px;font-family:inherit;
transition:border-color .15s}
.field input:focus,.field select:focus{outline:none;border-color:var(--blue-500)}
.calc-result{margin-top:16px;padding:16px 18px;background:var(--accent-bg);
border:1px solid var(--accent-bd);border-radius:var(--radius);font-size:14px}
.calc-result .big{font-size:26px;font-weight:800;color:var(--green);letter-spacing:-.5px}
.calc-result .row{display:flex;justify-content:space-between;gap:10px;padding:3px 0;color:var(--text)}
.calc-result .row b{color:var(--text-strong);font-weight:600;white-space:nowrap}
.formula{background:var(--accent-bg);border:1px solid var(--accent-bd);
padding:14px 18px;border-radius:var(--radius);font-size:14px;color:var(--text-soft);margin:16px 0;
font-family:ui-monospace,monospace}
.calc-links{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:18px 0}
.calc-links a{display:block;background:var(--panel);border:1px solid var(--line-2);
border-radius:var(--radius-lg);padding:20px;transition:border-color .15s}
.calc-links a:hover{border-color:var(--line-strong)}
.calc-links .t{font-size:15px;font-weight:700;color:var(--head);margin-bottom:4px}
.calc-links .d{font-size:13px;color:var(--text);line-height:1.6}
.edu-fig{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius-lg);
padding:20px 22px 16px;margin:20px 0}
.edu-fig svg{display:block;width:100%;height:auto}
.edu-fig figcaption{font-size:13px;color:var(--muted);line-height:1.6;margin-top:12px;text-align:center}
.edu-fig figcaption b{color:var(--text-soft);font-weight:600}
.svg-text-sm{fill:var(--muted);font:500 11px Inter,sans-serif}
.svg-text-strong{fill:var(--head);font:700 14px Inter,sans-serif}
.svg-axis{stroke:var(--line-3);stroke-width:1.5}
.svg-grid{stroke:var(--line);stroke-width:1}
.fill-blue{fill:var(--blue-500)}.fill-green{fill:var(--green-400)}.fill-red{fill:var(--red-400)}
.stroke-blue{stroke:var(--blue-500);fill:none;stroke-width:2.5}
.stroke-green{stroke:var(--green-400);fill:none;stroke-width:2.5}
</style>"""

_FOOT_LINKS = ('<p>Смотрите также: <a href="/bond">каталог облигаций MOEX</a> с готовым НКД и доходностью '
               'по каждой бумаге · <a href="/uchebnik">учебник по облигациям</a>.</p>')

_CTA = """
<div class="cta-box">
  <h2>Не считайте вручную — Bond AI сделает это сам</h2>
  <p>Добавьте облигации в бесплатный портфель: НКД, доходность, купонный календарь и
  Telegram-уведомления о выплатах обновляются автоматически по данным MOEX.</p>
  <a class="btn btn-primary" href="/app?auth=register">Создать портфель бесплатно</a>
</div>"""


def _crumbs_ld(page_name: str, page_url: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Главная", "item": _BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Калькуляторы", "item": _BASE_URL + "/calc"},
            {"@type": "ListItem", "position": 3, "name": page_name, "item": page_url},
        ],
    }


def _faq_ld(faq: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq
        ],
    }


def _faq_html(faq: list[tuple[str, str]]) -> str:
    from html import escape as e
    items = "".join(f'<div class="faq-item"><h3>{e(q)}</h3><p>{e(a)}</p></div>' for q, a in faq)
    return f"<h2>Вопросы и ответы</h2>{items}"


# ── /calc — index ────────────────────────────────────────────────────────────

_CALC_INDEX_FAQ = [
    ("Какие калькуляторы доступны?",
     "Калькулятор НКД (накопленного купонного дохода) и калькулятор доходности к погашению (YTM). "
     "Оба бесплатны и работают прямо в браузере, без регистрации."),
    ("Откуда брать данные для расчёта?",
     "Параметры облигации (номинал, ставка купона, даты выплат, цена) есть на странице каждой бумаги "
     "в каталоге облигаций bondai.ru/bond — данные обновляются с Московской биржи."),
]


def _render_calc_index() -> str:
    url = _BASE_URL + "/calc"
    body = f"""{_CALC_CSS}
<nav class="crumbs"><a href="/">Главная</a> / Калькуляторы</nav>
<h1>Калькуляторы облигаций</h1>
<p class="sub">Бесплатные онлайн-калькуляторы для инвесторов в облигации</p>
<div class="calc-links">
  <a href="/calc/nkd">
    <div class="t">Калькулятор НКД</div>
    <div class="d">Накопленный купонный доход: сколько вы доплатите при покупке облигации
    или получите при продаже.</div>
  </a>
  <a href="/calc/ytm">
    <div class="t">Калькулятор доходности (YTM)</div>
    <div class="d">Доходность к погашению: эффективная, простая и текущая доходность
    облигации по цене покупки.</div>
  </a>
</div>
<figure class="edu-fig">
  <svg viewBox="0 0 640 200" role="img" aria-label="Что считает каждый калькулятор: НКД и доходность к погашению">
    <!-- left: НКД sawtooth -->
    <line x1="40" y1="150" x2="290" y2="150" class="svg-axis"/>
    <line x1="40" y1="150" x2="40" y2="35" class="svg-axis"/>
    <path d="M 40 150 L 130 65 L 130 150 L 220 65 L 220 150 L 285 90" class="stroke-green"/>
    <circle cx="130" cy="150" r="4" class="fill-blue"/><circle cx="220" cy="150" r="4" class="fill-blue"/>
    <text x="165" y="30" text-anchor="middle" class="svg-text-strong">НКД</text>
    <text x="165" y="175" text-anchor="middle" class="svg-text-sm">копится и обнуляется в купон</text>
    <!-- divider -->
    <line x1="320" y1="30" x2="320" y2="175" class="svg-grid"/>
    <!-- right: YTM discount bars -->
    <line x1="360" y1="150" x2="610" y2="150" class="svg-axis"/>
    <rect x="362" y="95" width="22" height="55" rx="2" class="fill-blue"/>
    <g class="fill-green" opacity=".28">
      <rect x="410" y="110" width="18" height="40" rx="2"/>
      <rect x="470" y="110" width="18" height="40" rx="2"/>
      <rect x="530" y="60" width="18" height="90" rx="2"/></g>
    <g class="fill-green">
      <rect x="410" y="122" width="18" height="28" rx="2"/>
      <rect x="470" y="130" width="18" height="20" rx="2"/>
      <rect x="530" y="98" width="18" height="52" rx="2"/></g>
    <text x="485" y="30" text-anchor="middle" class="svg-text-strong">YTM</text>
    <text x="485" y="175" text-anchor="middle" class="svg-text-sm">платежи приводятся к цене сейчас</text>
  </svg>
  <figcaption><b>НКД</b> — сколько купона «накапало» к дате сделки. <b>YTM</b> — годовая доходность,
  если держать бумагу до погашения. Выберите нужный калькулятор выше.</figcaption>
</figure>
{_faq_html(_CALC_INDEX_FAQ)}
{_CTA}
{_FOOT_LINKS}
"""
    jsonld = [
        {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Калькуляторы облигаций — НКД и доходность к погашению",
            "url": url,
            "inLanguage": "ru",
        },
        _faq_ld(_CALC_INDEX_FAQ),
    ]
    return _page_shell(
        "Калькуляторы облигаций: НКД и доходность к погашению (YTM) | Bond AI",
        "Бесплатные онлайн-калькуляторы облигаций: накопленный купонный доход (НКД) и доходность к погашению (YTM). Считают в браузере, без регистрации.",
        url, jsonld, body,
    )


# ── /calc/nkd ────────────────────────────────────────────────────────────────

_NKD_FAQ = [
    ("Что такое НКД простыми словами?",
     "НКД (накопленный купонный доход) — часть купона, которая «накапала» с даты последней выплаты. "
     "Покупатель облигации доплачивает НКД продавцу, чтобы тот не потерял доход за дни владения; "
     "взамен покупатель получит весь следующий купон целиком."),
    ("Как рассчитать НКД по облигации?",
     "НКД = номинал × ставка купона / 100 × число дней с последней выплаты / 365. "
     "Например, при номинале 1000 ₽, ставке 12% годовых и 45 днях после выплаты: "
     "1000 × 0,12 × 45 / 365 = 14,79 ₽."),
    ("Где посмотреть НКД без расчёта?",
     "На странице каждой облигации в каталоге bondai.ru/bond НКД уже рассчитан по данным "
     "Московской биржи и обновляется в течение торгового дня."),
    ("Платится ли налог с НКД?",
     "При продаже полученный НКД увеличивает доход и облагается НДФЛ 13–15%, а уплаченный при "
     "покупке НКД уменьшает налоговую базу. Брокер учитывает это автоматически."),
]

_NKD_JS = """<script>
function nkdByRate(){
  var nom=parseFloat(document.getElementById('n-nom').value);
  var rate=parseFloat(document.getElementById('n-rate').value);
  var d1=document.getElementById('n-last').value, d2=document.getElementById('n-calc').value;
  var out=document.getElementById('n-res');
  if(!(nom>0)||!(rate>0)||!d1||!d2){out.style.display='none';return;}
  var days=Math.round((new Date(d2)-new Date(d1))/86400000);
  if(days<0){out.style.display='none';return;}
  var nkd=nom*rate/100*days/365;
  out.style.display='';
  document.getElementById('n-val').textContent=nkd.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽';
  document.getElementById('n-days').textContent=days+' дн.';
}
function nkdByCoupon(){
  var c=parseFloat(document.getElementById('c-coupon').value);
  var p=parseFloat(document.getElementById('c-period').value);
  var d=parseFloat(document.getElementById('c-days').value);
  var out=document.getElementById('c-res');
  if(!(c>0)||!(p>0)||!(d>=0)||d>p){out.style.display='none';return;}
  var nkd=c*d/p;
  out.style.display='';
  document.getElementById('c-val').textContent=nkd.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽';
}
document.addEventListener('DOMContentLoaded',function(){
  var t=new Date();
  document.getElementById('n-calc').value=t.toISOString().slice(0,10);
  ['n-nom','n-rate','n-last','n-calc'].forEach(function(id){document.getElementById(id).addEventListener('input',nkdByRate);});
  ['c-coupon','c-period','c-days'].forEach(function(id){document.getElementById(id).addEventListener('input',nkdByCoupon);});
});
</script>"""


def _render_nkd() -> str:
    url = _BASE_URL + "/calc/nkd"
    body = f"""{_CALC_CSS}
<nav class="crumbs"><a href="/">Главная</a> / <a href="/calc">Калькуляторы</a> / НКД</nav>
<h1>Калькулятор НКД — накопленный купонный доход</h1>
<p class="sub">Посчитайте, сколько вы доплатите при покупке облигации (или получите при продаже)</p>

<div class="calc-card">
  <h2>По ставке купона</h2>
  <div class="calc-grid">
    <div class="field"><label>Номинал, ₽</label><input id="n-nom" type="number" value="1000" min="0" step="any"></div>
    <div class="field"><label>Ставка купона, % годовых</label><input id="n-rate" type="number" placeholder="например 12" min="0" step="any"></div>
    <div class="field"><label>Дата последней выплаты купона</label><input id="n-last" type="date"></div>
    <div class="field"><label>Дата расчёта</label><input id="n-calc" type="date"></div>
  </div>
  <div class="calc-result" id="n-res" style="display:none">
    <div class="big" id="n-val">—</div>
    <div class="row"><span>Дней накопления</span><b id="n-days">—</b></div>
  </div>
</div>

<div class="calc-card">
  <h2>По размеру купона</h2>
  <div class="calc-grid">
    <div class="field"><label>Купон за период, ₽</label><input id="c-coupon" type="number" placeholder="например 35,4" min="0" step="any"></div>
    <div class="field"><label>Купонный период, дней</label><input id="c-period" type="number" placeholder="например 182" min="1" step="1"></div>
    <div class="field"><label>Дней прошло с выплаты</label><input id="c-days" type="number" placeholder="например 45" min="0" step="1"></div>
  </div>
  <div class="calc-result" id="c-res" style="display:none">
    <div class="big" id="c-val">—</div>
  </div>
</div>

<h2>Как считается НКД</h2>
<div class="formula">НКД = Номинал × Ставка купона / 100 × Дней с выплаты / 365</div>
<figure class="edu-fig">
  <svg viewBox="0 0 640 200" role="img" aria-label="Как НКД растёт между купонами и обнуляется в дату выплаты">
    <line x1="50" y1="160" x2="610" y2="160" class="svg-axis"/>
    <line x1="50" y1="160" x2="50" y2="25" class="svg-axis"/>
    <text x="20" y="95" text-anchor="middle" class="svg-text-sm" transform="rotate(-90 20 95)">НКД, ₽</text>
    <text x="330" y="190" text-anchor="middle" class="svg-text-sm">время →</text>
    <!-- sawtooth: grows, drops to 0 at each coupon -->
    <path d="M 50 160 L 230 50 L 230 160 L 410 50 L 410 160 L 590 50" class="stroke-green"/>
    <!-- coupon payout markers -->
    <line x1="230" y1="25" x2="230" y2="160" class="svg-grid" stroke-dasharray="4 4"/>
    <line x1="410" y1="25" x2="410" y2="160" class="svg-grid" stroke-dasharray="4 4"/>
    <circle cx="230" cy="160" r="4" class="fill-blue"/><circle cx="410" cy="160" r="4" class="fill-blue"/>
    <text x="230" y="178" text-anchor="middle" class="svg-text-sm">выплата купона</text>
    <text x="410" y="178" text-anchor="middle" class="svg-text-sm">выплата купона</text>
    <!-- max marker -->
    <text x="240" y="46" class="svg-text-sm">= купон</text>
  </svg>
  <figcaption>НКД <b>растёт равными долями каждый день</b> и <b>обнуляется в дату выплаты купона</b> —
  его получает тот, кто держал облигацию. Покупая в середине периода, вы доплачиваете накопленную часть.</figcaption>
</figure>
<p>НКД растёт каждый день равными долями и обнуляется в дату выплаты купона. При покупке облигации
вы платите продавцу «чистую» цену плюс НКД — так продавец получает заработанный купонный доход
за дни владения, а вы затем получаете весь купон целиком. Размер купона и даты выплат для любой
бумаги есть на её странице в <a href="/bond">каталоге облигаций</a> — там НКД уже рассчитан.</p>
{_faq_html(_NKD_FAQ)}
{_CTA}
{_FOOT_LINKS}
{_NKD_JS}
"""
    jsonld = [_crumbs_ld("Калькулятор НКД", url), _faq_ld(_NKD_FAQ)]
    return _page_shell(
        "Калькулятор НКД онлайн — накопленный купонный доход облигации | Bond AI",
        "Бесплатный калькулятор НКД: рассчитайте накопленный купонный доход облигации по ставке купона или размеру купона. Формула, пример и ответы на вопросы.",
        url, jsonld, body,
    )


# ── /calc/ytm ────────────────────────────────────────────────────────────────

_YTM_FAQ = [
    ("Что такое доходность к погашению (YTM)?",
     "YTM (Yield to Maturity) — годовая доходность облигации при условии, что вы держите её до "
     "погашения и реинвестируете купоны. Учитывает и купоны, и разницу между ценой покупки и "
     "номиналом. Это главный показатель для сравнения облигаций между собой."),
    ("Чем эффективная доходность отличается от простой?",
     "Эффективная (YTM) предполагает реинвестирование купонов под ту же ставку и считается через "
     "сложный процент. Простая доходность реинвестирование не учитывает: сумма купонов и дисконта "
     "делится на цену и срок. Простая всегда ниже эффективной при цене ниже номинала."),
    ("Почему доходность выше, когда цена ниже номинала?",
     "Если облигация куплена за 90% номинала, при погашении вы получите 100% — этот дисконт "
     "добавляется к купонному доходу и повышает общую доходность. И наоборот: покупка дороже "
     "номинала доходность снижает."),
    ("Где посмотреть готовую YTM по облигации?",
     "На странице каждой бумаги в каталоге bondai.ru/bond — доходность к погашению рассчитывается "
     "Московской биржей и обновляется в течение торгового дня."),
]

_YTM_JS = """<script>
function calcYtm(){
  var price=parseFloat(document.getElementById('y-price').value);
  var nom=parseFloat(document.getElementById('y-nom').value);
  var rate=parseFloat(document.getElementById('y-rate').value);
  var freq=parseInt(document.getElementById('y-freq').value,10);
  var mat=document.getElementById('y-mat').value;
  var aci=parseFloat(document.getElementById('y-aci').value)||0;
  var out=document.getElementById('y-res');
  if(!(price>0)||!(nom>0)||!(rate>=0)||!mat){out.style.display='none';return;}
  var years=(new Date(mat)-new Date())/(365.25*86400000);
  if(years<=0){out.style.display='none';return;}
  var clean=price/100*nom, dirty=clean+aci;
  var coupon=nom*rate/100/freq;
  var n=Math.max(1,Math.round(years*freq));
  function pv(y){var s=0;for(var i=1;i<=n;i++){s+=coupon/Math.pow(1+y,i/freq);}return s+nom/Math.pow(1+y,n/freq);}
  var lo=0.000001,hi=10;
  if(pv(hi)>dirty){out.style.display='none';return;}
  for(var k=0;k<200;k++){var mid=(lo+hi)/2;if(pv(mid)>dirty)lo=mid;else hi=mid;}
  var ytm=(lo+hi)/2*100;
  var totalCoupons=coupon*n;
  var simple=((totalCoupons+(nom-dirty))/dirty)/years*100;
  var current=rate>0?(nom*rate/100)/clean*100:0;
  function f(v){return v.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});}
  out.style.display='';
  document.getElementById('y-val').textContent=f(ytm)+'% годовых';
  document.getElementById('y-simple').textContent=f(simple)+'%';
  document.getElementById('y-cur').textContent=current?f(current)+'%':'—';
  document.getElementById('y-total').textContent=f(totalCoupons+nom-dirty)+' ₽ на облигацию';
}
document.addEventListener('DOMContentLoaded',function(){
  ['y-price','y-nom','y-rate','y-freq','y-mat','y-aci'].forEach(function(id){
    var el=document.getElementById(id);
    el.addEventListener('input',calcYtm);el.addEventListener('change',calcYtm);
  });
});
</script>"""


def _render_ytm() -> str:
    url = _BASE_URL + "/calc/ytm"
    body = f"""{_CALC_CSS}
<nav class="crumbs"><a href="/">Главная</a> / <a href="/calc">Калькуляторы</a> / Доходность (YTM)</nav>
<h1>Калькулятор доходности облигаций к погашению (YTM)</h1>
<p class="sub">Эффективная, простая и текущая доходность по цене покупки</p>

<div class="calc-card">
  <h2>Параметры облигации</h2>
  <div class="calc-grid">
    <div class="field"><label>Цена, % от номинала</label><input id="y-price" type="number" placeholder="например 95,5" min="0" step="any"></div>
    <div class="field"><label>Номинал, ₽</label><input id="y-nom" type="number" value="1000" min="0" step="any"></div>
    <div class="field"><label>Ставка купона, % годовых</label><input id="y-rate" type="number" placeholder="например 12" min="0" step="any"></div>
    <div class="field"><label>Выплат купона в год</label><select id="y-freq">
      <option value="1">1 (раз в год)</option>
      <option value="2" selected>2 (раз в полгода)</option>
      <option value="4">4 (ежеквартально)</option>
      <option value="12">12 (ежемесячно)</option>
    </select></div>
    <div class="field"><label>Дата погашения</label><input id="y-mat" type="date"></div>
    <div class="field"><label>НКД, ₽ (необязательно)</label><input id="y-aci" type="number" placeholder="0" min="0" step="any"></div>
  </div>
  <div class="calc-result" id="y-res" style="display:none">
    <div class="row"><span>Эффективная доходность к погашению (YTM)</span></div>
    <div class="big" id="y-val">—</div>
    <div class="row"><span>Простая доходность к погашению</span><b id="y-simple">—</b></div>
    <div class="row"><span>Текущая купонная доходность</span><b id="y-cur">—</b></div>
    <div class="row"><span>Суммарный доход до погашения</span><b id="y-total">—</b></div>
  </div>
</div>

<h2>Как считается доходность</h2>
<div class="formula">Цена + НКД = Σ Купон / (1+YTM)<sup>t</sup> + Номинал / (1+YTM)<sup>T</sup></div>
<figure class="edu-fig">
  <svg viewBox="0 0 640 220" role="img" aria-label="Дисконтирование будущих платежей по облигации к сегодняшней цене">
    <line x1="50" y1="170" x2="610" y2="170" class="svg-axis"/>
    <text x="330" y="205" text-anchor="middle" class="svg-text-sm">срок до погашения →</text>
    <!-- future nominal cash flows (full height) -->
    <g class="fill-green" opacity=".28">
      <rect x="120" y="120" width="26" height="50" rx="2"/>
      <rect x="240" y="120" width="26" height="50" rx="2"/>
      <rect x="360" y="120" width="26" height="50" rx="2"/>
      <rect x="500" y="40" width="26" height="130" rx="2"/>
    </g>
    <!-- discounted present values (shorter, solid) -->
    <g class="fill-green">
      <rect x="120" y="135" width="26" height="35" rx="2"/>
      <rect x="240" y="143" width="26" height="27" rx="2"/>
      <rect x="360" y="150" width="26" height="20" rx="2"/>
      <rect x="500" y="105" width="26" height="65" rx="2"/>
    </g>
    <text x="133" y="112" text-anchor="middle" class="svg-text-sm">купон</text>
    <text x="253" y="112" text-anchor="middle" class="svg-text-sm">купон</text>
    <text x="373" y="112" text-anchor="middle" class="svg-text-sm">купон</text>
    <text x="513" y="32" text-anchor="middle" class="svg-text-sm">купон + номинал</text>
    <!-- today's price -->
    <rect x="55" y="95" width="34" height="75" rx="2" class="fill-blue"/>
    <text x="72" y="88" text-anchor="middle" class="svg-text-sm">цена</text>
    <text x="72" y="186" text-anchor="middle" class="svg-text-sm">сегодня</text>
    <!-- discount arrows -->
    <path d="M 133 135 Q 100 100 92 110" class="stroke-blue" opacity=".4" stroke-dasharray="3 3"/>
    <path d="M 513 105 Q 250 75 92 105" class="stroke-blue" opacity=".4" stroke-dasharray="3 3"/>
  </svg>
  <figcaption>Каждый будущий платёж <b>«уценивается» к сегодняшнему дню</b> (бледный столбик → сплошной):
  чем дальше выплата, тем сильнее. YTM — это ставка, при которой сумма приведённых платежей <b>равна цене сейчас</b>.</figcaption>
</figure>
<p>Эффективная доходность (YTM) — это ставка, при которой все будущие платежи по облигации
(купоны и номинал при погашении), приведённые к сегодняшнему дню, равны её полной цене с НКД.
Калькулятор решает это уравнение численно. Расчёт приблизительный: даты купонов берутся равными
интервалами от сегодняшнего дня, без учёта налогов, комиссий и оферт. Точную биржевую YTM по
любой бумаге смотрите на её странице в <a href="/bond">каталоге облигаций</a>.</p>
<p>Если у облигации есть <b>оферта</b>, доходность стоит считать к дате оферты, а не к погашению —
после оферты эмитент может изменить ставку купона. Подробнее — в
<a href="/uchebnik">учебнике по облигациям</a>.</p>
{_faq_html(_YTM_FAQ)}
{_CTA}
{_FOOT_LINKS}
{_YTM_JS}
"""
    jsonld = [_crumbs_ld("Калькулятор доходности (YTM)", url), _faq_ld(_YTM_FAQ)]
    return _page_shell(
        "Калькулятор доходности облигаций к погашению (YTM) онлайн | Bond AI",
        "Бесплатный калькулятор доходности облигаций: эффективная YTM, простая и текущая доходность по цене покупки. Формула, пример расчёта и ответы на вопросы.",
        url, jsonld, body,
    )


# Pages are static per process — render lazily once and reuse.
_rendered: dict[str, str] = {}


def _page(key: str, render) -> HTMLResponse:
    if key not in _rendered:
        _rendered[key] = render()
    return HTMLResponse(_rendered[key], headers=_CACHE_HEADERS)


@router.get("/calc", response_class=HTMLResponse)
async def calc_index() -> HTMLResponse:
    return _page("index", _render_calc_index)


@router.get("/calc/nkd", response_class=HTMLResponse)
async def calc_nkd() -> HTMLResponse:
    return _page("nkd", _render_nkd)


@router.get("/calc/ytm", response_class=HTMLResponse)
async def calc_ytm() -> HTMLResponse:
    return _page("ytm", _render_ytm)
