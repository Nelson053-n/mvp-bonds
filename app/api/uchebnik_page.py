"""Public educational page: /uchebnik.

Rendered through the shared public page shell from bond_pages so it matches the
rest of the public site (theme toggle, mesh background, unified topbar/footer,
Yandex.Metrika). Only the content-specific styles (edu-*, svg-*) and the
educational copy live below.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.bond_pages import _BASE_URL, _PUBLIC_CACHE, _page_shell

router = APIRouter(tags=["public-uchebnik"])

# JSON-LD: Article + FAQPage (the shell escapes "<" and renders these as
# <script type="application/ld+json"> blocks in <head>).
_JSONLD = [
    {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": "Учебник по облигациям: YTM, оферта, виды и риски",
        "description": "Образовательный материал по облигациям для розничных инвесторов: доходность к погашению (YTM), НКД, оферта, виды облигаций, риски и глоссарий терминов.",
        "inLanguage": "ru",
        "datePublished": "2026-06-08",
        "dateModified": "2026-06-08",
        "author": {
            "@type": "Organization",
            "name": "Bond AI",
            "url": "https://bondai.ru",
        },
        "publisher": {
            "@type": "Organization",
            "name": "Bond AI",
            "url": "https://bondai.ru",
            "logo": {
                "@type": "ImageObject",
                "url": "https://bondai.ru/og-image.png",
            },
        },
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": "https://bondai.ru/uchebnik",
        },
        "about": "Облигации, рынок облигаций, инвестиции в облигации",
    },
    {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "Что такое YTM (доходность к погашению)?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "YTM (Yield to Maturity) — это годовая доходность облигации, если держать её до даты погашения и реинвестировать купоны по той же ставке. YTM учитывает не только купонный доход, но и разницу между текущей ценой облигации и номиналом. Это основной показатель для сравнения облигаций между собой.",
                },
            },
            {
                "@type": "Question",
                "name": "Чем оферта отличается от погашения?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Погашение — это дата, когда эмитент обязательно возвращает номинал всем держателям. Оферта (put-оферта) — это дата, когда держатель имеет право, но не обязан, предъявить облигацию к досрочному выкупу по номиналу. После оферты эмитент может изменить ставку купона, поэтому за датой оферты важно следить.",
                },
            },
            {
                "@type": "Question",
                "name": "Что такое НКД (накопленный купонный доход)?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "НКД — это часть купона, накопленная с даты последней выплаты до текущего дня. При покупке облигации вы доплачиваете НКД продавцу, а при продаже — получаете НКД от покупателя. НКД включается в полную (грязную) стоимость позиции.",
                },
            },
            {
                "@type": "Question",
                "name": "Что выбрать: ОФЗ или корпоративные облигации?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "ОФЗ (облигации федерального займа) — самый надёжный рублёвый инструмент, выпускаются Минфином, купон освобождён от НДФЛ. Корпоративные облигации дают более высокую доходность за счёт кредитного риска эмитента: чем ниже рейтинг, тем выше купон и выше риск. Консервативному инвестору подходят ОФЗ и корпоративные бумаги рейтинга BBB и выше.",
                },
            },
            {
                "@type": "Question",
                "name": "Что такое флоатер и линкер?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Флоатер — облигация с переменным купоном, привязанным к ключевой ставке ЦБ или RUONIA: купон растёт вместе со ставками, что защищает от их повышения. Линкер (ОФЗ-ИН) — облигация с номиналом, индексируемым на инфляцию: защищает от обесценивания рубля.",
                },
            },
            {
                "@type": "Question",
                "name": "Облагается ли купон по облигациям НДФЛ?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Купонный доход и прибыль от продажи облигаций облагаются НДФЛ по ставке 13–15%. Используя индивидуальный инвестиционный счёт (ИИС), можно получить налоговый вычет или освобождение от НДФЛ на доход.",
                },
            },
        ],
    },
]

# Content-only styles. The shell already provides design tokens (:root + light),
# topbar/footer/theme/mesh/orbs and the .wrap column — those are NOT duplicated.
# --green-500/--yellow-400/--red-400/--blue-500/--accent-* all live in the shell.
_CONTENT_CSS = """<style>
.edu-container{max-width:760px;margin:0 auto}
.edu-back{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:var(--link);margin-bottom:28px}
.edu-title{font-size:clamp(28px,5vw,40px);font-weight:800;color:var(--head);letter-spacing:-.8px;line-height:1.12;margin-bottom:12px}
.edu-lead{font-size:16px;color:var(--text);line-height:1.7;margin-bottom:36px}
.edu-toc{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius-lg);padding:20px 24px;margin-bottom:44px}
.edu-toc h2{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;border:none;padding:0}
.edu-toc ol{list-style:none;counter-reset:toc;display:flex;flex-direction:column;gap:8px}
.edu-toc li{counter-increment:toc;font-size:15px}
.edu-toc li::before{content:counter(toc) ". ";color:var(--faint);font-weight:600}
.edu-section{margin-bottom:52px;scroll-margin-top:70px}
.edu-section>h2{font-size:clamp(20px,3vw,26px);font-weight:800;color:var(--head);letter-spacing:-.4px;margin:0 0 18px;padding-bottom:10px;border-bottom:1px solid var(--line-2)}
.edu-section h3{font-size:17px;font-weight:700;color:var(--text-strong);margin:24px 0 10px}
.edu-section h4{font-size:15px;font-weight:700;color:var(--head);margin-bottom:6px}
.edu-section p{font-size:15px;color:var(--text);line-height:1.8;margin-bottom:14px}
.edu-section strong{color:var(--text-soft)}
.edu-section ul{list-style:none;padding:0;display:flex;flex-direction:column;gap:9px;margin-bottom:14px}
.edu-section ul li{font-size:15px;color:var(--text);line-height:1.7;padding-left:20px;position:relative}
.edu-section ul li::before{content:'';position:absolute;left:0;top:10px;width:6px;height:6px;border-radius:50%;background:var(--link)}
.edu-fig{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius-lg);padding:20px 22px 16px;margin:20px 0}
.edu-fig svg{display:block;width:100%;height:auto}
.edu-fig figcaption{font-size:13px;color:var(--muted);line-height:1.6;margin-top:12px;text-align:center}
.edu-fig figcaption b{color:var(--text-soft);font-weight:600}
.svg-text{fill:var(--text);font:600 13px Inter,sans-serif}
.svg-text-strong{fill:var(--head);font:700 14px Inter,sans-serif}
.svg-text-sm{fill:var(--muted);font:500 11px Inter,sans-serif}
.svg-axis{stroke:var(--line-3);stroke-width:1.5}
.svg-grid{stroke:var(--line);stroke-width:1}
.svg-track{stroke:var(--line-3);stroke-width:2.5;fill:none}
.svg-node{fill:var(--panel);stroke:var(--blue-500);stroke-width:2.5}
.svg-node-g{fill:var(--panel);stroke:var(--green-500);stroke-width:2.5}
.fill-blue{fill:var(--blue-500)}.fill-green{fill:var(--green-500)}
.fill-yellow{fill:var(--yellow-400)}.fill-red{fill:var(--red-400)}
.stroke-blue{stroke:var(--blue-500);fill:none;stroke-width:2.5}
.stroke-green{stroke:var(--green-500);fill:none;stroke-width:2.5}
.stroke-red{stroke:var(--red-400);fill:none;stroke-width:2.5}
.edu-card{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius);padding:16px 18px;margin-bottom:12px}
.edu-card p{margin-bottom:0}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-left:6px;vertical-align:middle}
.badge-green{background:rgba(22,163,74,.15);color:var(--green)}
.badge-yellow{background:rgba(245,158,11,.15);color:var(--yellow-400)}
.badge-red{background:rgba(248,113,113,.15);color:var(--red)}
.badge-blue{background:rgba(96,165,250,.15);color:var(--link)}
.edu-dl{display:flex;flex-direction:column}
.edu-dl .row{padding:14px 0;border-bottom:1px solid var(--line)}
.edu-dl .row:last-child{border-bottom:none}
.edu-dl dt{font-size:15px;font-weight:700;color:var(--head);margin-bottom:4px}
.edu-dl dd{font-size:14px;color:var(--text);line-height:1.7}
.edu-example{background:var(--accent-bg);border:1px solid var(--accent-bd);border-radius:var(--radius);padding:16px 18px;margin:16px 0}
.edu-example h4{color:var(--link);margin-bottom:8px}
.edu-example p{font-size:14px;margin-bottom:6px}
.edu-faq .row{padding:16px 0;border-bottom:1px solid var(--line)}
.edu-faq .row:last-child{border-bottom:none}
.edu-faq .q{font-size:16px;font-weight:700;color:var(--head);margin-bottom:8px}
.edu-faq .a{font-size:14px;color:var(--text);line-height:1.75}
.edu-cta{background:linear-gradient(135deg,rgba(37,99,235,.14),rgba(99,102,241,.1));border:1px solid rgba(37,99,235,.25);border-radius:var(--radius-lg);padding:28px 24px;text-align:center;margin:52px 0 0}
.edu-cta h2{font-size:22px;font-weight:800;color:var(--head);margin-bottom:8px;border:none;padding:0}
.edu-cta p{font-size:15px;color:var(--text);margin-bottom:18px}
.edu-cta a.btn{display:inline-block;background:var(--blue-600);color:#fff;font-weight:700;font-size:15px;padding:11px 28px;border-radius:var(--radius)}
.edu-cta a.btn:hover{background:var(--blue-500);color:#fff}
@media(max-width:640px){.edu-container{max-width:none}}
</style>"""

_BODY = _CONTENT_CSS + """
<article class="edu-container">
  <a href="/" class="edu-back">&larr; На главную</a>
  <h1 class="edu-title">Учебник по облигациям</h1>
  <p class="edu-lead">Понятное руководство для тех, кто начинает инвестировать в облигации: что такое облигация и как она работает, какие бывают виды, как читать ключевые показатели — YTM, НКД, оферту — и как управлять рисками. С примерами и глоссарием терминов.</p>

  <nav class="edu-toc" aria-label="Содержание">
    <h2>Содержание</h2>
    <ol>
      <li><a href="#osnovy">Что такое облигация и как она работает</a></li>
      <li><a href="#vidy">Виды облигаций</a></li>
      <li><a href="#pokazateli">Ключевые показатели: YTM, НКД, оферта</a></li>
      <li><a href="#riski">Риски и как их снизить</a></li>
      <li><a href="#slovar">Глоссарий терминов</a></li>
      <li><a href="#faq">Частые вопросы</a></li>
    </ol>
  </nav>

  <!-- ── 1. ОСНОВЫ ── -->
  <section class="edu-section" id="osnovy">
    <h2>Что такое облигация и как она работает</h2>
    <p>Облигация — это долговая ценная бумага. Покупая облигацию, вы <strong>даёте деньги в долг</strong> эмитенту (государству, региону или компании). Взамен эмитент обязуется регулярно выплачивать вам проценты (купоны) и вернуть номинал в дату погашения.</p>
    <h3>Как это работает по шагам</h3>
    <div class="edu-card">
      <h4>1. Вы покупаете облигацию</h4>
      <p>На бирже по рыночной цене. Цена выражается в процентах от номинала — например, 97,5% при номинале 1000&nbsp;₽ означает цену 975&nbsp;₽.</p>
    </div>
    <div class="edu-card">
      <h4>2. Получаете купоны</h4>
      <p>Периодически (обычно раз в полгода или квартал) эмитент перечисляет купонный доход на ваш брокерский счёт.</p>
    </div>
    <div class="edu-card">
      <h4>3. В дату погашения</h4>
      <p>Эмитент возвращает вам номинальную стоимость (обычно 1000&nbsp;₽ за облигацию) независимо от рыночной цены.</p>
    </div>
    <figure class="edu-fig">
      <svg viewBox="0 0 640 180" role="img" aria-label="Денежный поток по облигации во времени">
        <!-- timeline axis -->
        <line x1="40" y1="120" x2="600" y2="120" class="svg-axis"/>
        <polygon points="600,120 590,115 590,125" class="fill-blue"/>
        <!-- buy (outflow) -->
        <line x1="80" y1="120" x2="80" y2="70" class="stroke-red"/>
        <polygon points="80,70 75,80 85,80" class="fill-red"/>
        <text x="80" y="58" text-anchor="middle" class="svg-text-sm">−975 ₽</text>
        <text x="80" y="140" text-anchor="middle" class="svg-text-sm">Покупка</text>
        <!-- coupons (inflows) -->
        <g class="fill-green">
          <line x1="190" y1="120" x2="190" y2="95" class="stroke-green"/><polygon points="190,95 185,105 195,105" /></g>
        <text x="190" y="85" text-anchor="middle" class="svg-text-sm">+42 ₽</text>
        <g class="fill-green"><line x1="300" y1="120" x2="300" y2="95" class="stroke-green"/><polygon points="300,95 295,105 305,105" /></g>
        <text x="300" y="85" text-anchor="middle" class="svg-text-sm">+42 ₽</text>
        <g class="fill-green"><line x1="410" y1="120" x2="410" y2="95" class="stroke-green"/><polygon points="410,95 405,105 415,105" /></g>
        <text x="410" y="85" text-anchor="middle" class="svg-text-sm">+42 ₽</text>
        <text x="300" y="140" text-anchor="middle" class="svg-text-sm">Купоны каждые полгода</text>
        <!-- redemption (big inflow) -->
        <g class="fill-green"><line x1="540" y1="120" x2="540" y2="55" class="stroke-green"/><polygon points="540,55 535,65 545,65" /></g>
        <text x="540" y="45" text-anchor="middle" class="svg-text-sm">+1042 ₽</text>
        <text x="540" y="140" text-anchor="middle" class="svg-text-sm">Погашение</text>
        <text x="320" y="170" text-anchor="middle" class="svg-text-sm">время →</text>
      </svg>
      <figcaption>Денежный поток по облигации: <b>вы платите цену сейчас</b> (отток), затем регулярно
      получаете купоны, а в дату погашения — последний купон <b>плюс номинал</b>.</figcaption>
    </figure>
    <h3>Облигации, акции и вклад — в чём разница</h3>
    <div class="edu-card">
      <h4>🏦 Банковский вклад</h4>
      <p>Фиксированный доход, страховка АСВ до 1,4&nbsp;млн&nbsp;₽. Простой, но низкая доходность, нельзя продать досрочно без потери процентов.</p>
    </div>
    <div class="edu-card">
      <h4>📄 Облигации</h4>
      <p>Фиксированный купонный доход, можно продать в любой момент. Доходность выше вклада, риски умеренные. Нет страховки АСВ.</p>
    </div>
    <div class="edu-card">
      <h4>📈 Акции</h4>
      <p>Потенциально высокий доход, но цена может упасть в разы. Подходят для долгосрочных инвесторов с аппетитом к риску.</p>
    </div>
  </section>

  <!-- ── 2. ВИДЫ ── -->
  <section class="edu-section" id="vidy">
    <h2>Виды облигаций</h2>
    <h3>По эмитенту</h3>
    <div class="edu-card">
      <h4>🇷🇺 ОФЗ — облигации федерального займа <span class="badge badge-green">Минимальный риск</span></h4>
      <p>Выпускаются Минфином России. Самый надёжный рублёвый инструмент. Купонный доход освобождён от НДФЛ. Ликвидный рынок.</p>
    </div>
    <div class="edu-card">
      <h4>🏙️ Муниципальные (субфедеральные) <span class="badge badge-green">Низкий риск</span></h4>
      <p>Выпускают регионы и муниципалитеты. Чуть выше доходность, чем у ОФЗ. Риск дефолта минимален при поддержке федерального бюджета.</p>
    </div>
    <div class="edu-card">
      <h4>🏢 Корпоративные <span class="badge badge-yellow">Средний риск</span></h4>
      <p>Выпускают компании. Доходность выше ОФЗ за счёт кредитного риска. Надёжность зависит от рейтинга: ААА–ВВВ — инвестиционный уровень, BB и ниже — высокодоходные (ВДО).</p>
    </div>
    <div class="edu-card">
      <h4>⚡ ВДО — высокодоходные облигации <span class="badge badge-red">Высокий риск</span></h4>
      <p>Облигации небольших компаний с рейтингом BB и ниже или без рейтинга. Купон 18–25%+, но риск дефолта значительно выше. Требуют диверсификации и анализа.</p>
    </div>
    <figure class="edu-fig">
      <svg viewBox="0 0 640 230" role="img" aria-label="Соотношение риска и доходности по типам облигаций">
        <!-- axes -->
        <line x1="60" y1="190" x2="610" y2="190" class="svg-axis"/>
        <line x1="60" y1="190" x2="60" y2="20" class="svg-axis"/>
        <text x="335" y="220" text-anchor="middle" class="svg-text-sm">риск дефолта →</text>
        <text x="20" y="105" text-anchor="middle" class="svg-text-sm" transform="rotate(-90 20 105)">доходность →</text>
        <!-- trend line -->
        <path d="M 90 175 Q 350 130 580 45" class="stroke-blue" stroke-dasharray="5 5" opacity=".55"/>
        <!-- points -->
        <circle cx="110" cy="170" r="9" class="fill-green"/>
        <text x="110" y="150" text-anchor="middle" class="svg-text-sm">ОФЗ</text>
        <circle cx="240" cy="150" r="9" class="fill-green"/>
        <text x="240" y="130" text-anchor="middle" class="svg-text-sm">Муни</text>
        <circle cx="400" cy="110" r="9" class="fill-yellow"/>
        <text x="400" y="90" text-anchor="middle" class="svg-text-sm">Корп. (BBB+)</text>
        <circle cx="560" cy="55" r="9" class="fill-red"/>
        <text x="560" y="35" text-anchor="middle" class="svg-text-sm">ВДО</text>
      </svg>
      <figcaption>Чем выше риск дефолта эмитента — <b>тем выше доходность</b>, которую требует рынок.
      ОФЗ надёжнее всего и доходнее депозита ненамного; ВДО платят больше, но и теряют чаще.</figcaption>
    </figure>
    <h3>По типу купона</h3>
    <div class="edu-card">
      <h4>Фиксированный купон</h4>
      <p>Ставка неизменна весь срок. Удобно планировать доход. Но при росте ставок ЦБ цена облигации падает.</p>
    </div>
    <div class="edu-card">
      <h4>Переменный (флоатер)</h4>
      <p>Купон привязан к ключевой ставке ЦБ или RUONIA. Защищает от роста ставок — купон растёт вместе с ними.</p>
    </div>
    <div class="edu-card">
      <h4>Индексируемый (линкер)</h4>
      <p>Номинал индексируется на инфляцию (ОФЗ-ИН). Защита от обесценивания рубля, но доходность ниже фиксированных.</p>
    </div>
    <h3>По сроку</h3>
    <ul>
      <li><strong>Краткосрочные</strong> — до 1–2 лет. Меньше процентный риск.</li>
      <li><strong>Среднесрочные</strong> — 2–5 лет. Баланс доходности и риска.</li>
      <li><strong>Долгосрочные</strong> — 5+ лет. Выше доходность, но сильнее реагируют на ставки ЦБ.</li>
    </ul>
  </section>

  <!-- ── 3. ПОКАЗАТЕЛИ ── -->
  <section class="edu-section" id="pokazateli">
    <h2>Ключевые показатели: YTM, НКД, оферта</h2>
    <dl class="edu-dl">
      <div class="row"><dt>Чистая цена</dt><dd>Рыночная цена облигации без учёта НКД. Именно её вы видите на бирже и в котировках. Выражается в процентах от номинала (для MOEX — от 1000&nbsp;₽).</dd></div>
      <div class="row"><dt>НКД — накопленный купонный доход</dt><dd>Часть купона, накопленная с даты последней выплаты. При покупке вы доплачиваете НКД продавцу, при продаже — получаете НКД от покупателя. Включается в стоимость позиции.</dd></div>
      <div class="row"><dt>Грязная цена и стоимость позиции</dt><dd>Стоимость позиции считается по грязной цене: (чистая цена + НКД) × количество. Отражает реальные деньги, которые вы получите при продаже сегодня.</dd></div>
      <div class="row"><dt>Купон, ₽</dt><dd>Размер одной купонной выплаты на одну облигацию в рублях. Например, 42,38&nbsp;₽ за полгода.</dd></div>
      <div class="row"><dt>Купон, %</dt><dd>Годовая купонная ставка от номинала. Например, 8,5% от 1000&nbsp;₽ = 85&nbsp;₽ в год.</dd></div>
      <div class="row"><dt>YTM — доходность к погашению</dt><dd>Годовая доходность, если держать облигацию до погашения и реинвестировать купоны по той же ставке. Учитывает разницу между текущей ценой и номиналом. Основной показатель для сравнения облигаций.</dd></div>
      <div class="row"><dt>Кредитный рейтинг</dt><dd>Оценка надёжности эмитента от АКРА, Эксперт РА или S&amp;P. AAA — максимальная надёжность, D — дефолт. Инвестиционный уровень: BBB и выше.</dd></div>
      <div class="row"><dt>Оферта (put-оферта)</dt><dd>Дата, когда эмитент обязан выкупить облигации по номиналу по вашему требованию. После оферты купон может измениться. Важно следить за датой.</dd></div>
    </dl>
    <div class="edu-example">
      <h4>Пример: как YTM отличается от купона</h4>
      <p>Допустим, облигация с номиналом 1000&nbsp;₽ и купоном 8,5% годовых торгуется по чистой цене 97% (970&nbsp;₽), до погашения 3 года.</p>
      <p>Купонная ставка — это просто 8,5% от номинала. Но вы купили дешевле номинала (за 970&nbsp;₽), а при погашении получите полные 1000&nbsp;₽ — то есть ещё +30&nbsp;₽ прибыли «в теле». Эта разница, распределённая на 3 года, добавляется к купонному доходу.</p>
      <p>Поэтому <strong>YTM окажется выше купонной ставки</strong> — примерно 9,6% против 8,5%. И наоборот: если бы цена была выше номинала, YTM был бы ниже купона. Именно поэтому для сравнения облигаций смотрят на YTM, а не на купон.</p>
    </div>
    <figure class="edu-fig">
      <svg viewBox="0 0 640 200" role="img" aria-label="Сравнение купонной ставки и YTM при покупке ниже номинала">
        <!-- baseline номинал -->
        <line x1="60" y1="60" x2="600" y2="60" class="svg-grid" stroke-dasharray="4 4"/>
        <text x="606" y="64" class="svg-text-sm">1000 ₽ — номинал</text>
        <!-- price bar 970 -->
        <rect x="120" y="95" width="90" height="70" rx="4" class="fill-blue" opacity=".85"/>
        <text x="165" y="185" text-anchor="middle" class="svg-text-sm">Цена покупки</text>
        <text x="165" y="88" text-anchor="middle" class="svg-text-strong">970 ₽</text>
        <!-- redemption bar 1000 -->
        <rect x="430" y="60" width="90" height="105" rx="4" class="fill-green" opacity=".85"/>
        <text x="475" y="185" text-anchor="middle" class="svg-text-sm">Погашение</text>
        <text x="475" y="52" text-anchor="middle" class="svg-text-strong">1000 ₽</text>
        <!-- gain arrow -->
        <path d="M 230 130 H 410" class="stroke-green"/>
        <polygon points="410,130 400,125 400,135" class="fill-green"/>
        <text x="320" y="122" text-anchor="middle" class="svg-text-sm">+30 ₽ «в теле» → добавляется к купону</text>
      </svg>
      <figcaption>Купон фиксирован (8,5%), но вы купили за <b>970 ₽</b>, а вернут <b>1000 ₽</b>.
      Эти +30 ₽ за срок поднимают итоговую доходность: <b>YTM ≈ 9,6% &gt; купона 8,5%</b>.</figcaption>
    </figure>
    <div class="edu-example">
      <h4>Осторожно: доходность к оферте</h4>
      <p>Если у облигации скоро оферта, биржа может показывать <strong>доходность к оферте</strong>, а не к погашению. При близкой дате оферты это число бывает аномально большим (сотни процентов годовых) — это нормально математически, но не отражает реальную долгосрочную доходность. Всегда проверяйте, к какой дате считается доходность.</p>
    </div>
  </section>

  <!-- ── 4. РИСКИ ── -->
  <section class="edu-section" id="riski">
    <h2>Риски и как их снизить</h2>
    <div class="edu-card">
      <h4>💥 Кредитный риск (риск дефолта) <span class="badge badge-red">Высокий</span></h4>
      <p>Эмитент может не выплатить купон или не погасить номинал. Характерен для корпоративных облигаций с низким рейтингом (ВДО). Снижается диверсификацией и выбором надёжных эмитентов.</p>
    </div>
    <div class="edu-card">
      <h4>📉 Процентный риск <span class="badge badge-yellow">Средний</span></h4>
      <p>При росте ключевой ставки ЦБ цены облигаций падают (и наоборот). Чем длиннее срок — тем сильнее реакция. Решение: держать до погашения или выбирать флоатеры.</p>
    </div>
    <figure class="edu-fig">
      <svg viewBox="0 0 640 200" role="img" aria-label="Обратная зависимость цены облигации от ключевой ставки">
        <!-- left: ставка вверх -->
        <line x1="70" y1="170" x2="70" y2="30" class="svg-axis"/>
        <line x1="70" y1="170" x2="280" y2="170" class="svg-axis"/>
        <path d="M 80 150 L 270 50" class="stroke-red"/>
        <polygon points="270,50 260,52 266,61" class="fill-red"/>
        <text x="175" y="20" text-anchor="middle" class="svg-text-sm">Ставка ЦБ ↑</text>
        <text x="175" y="190" text-anchor="middle" class="svg-text-sm">время</text>
        <!-- right: цена вниз -->
        <line x1="360" y1="30" x2="360" y2="170" class="svg-axis"/>
        <line x1="360" y1="170" x2="570" y2="170" class="svg-axis"/>
        <path d="M 370 50 L 560 150" class="stroke-blue"/>
        <polygon points="560,150 550,141 554,150" class="fill-blue"/>
        <text x="465" y="20" text-anchor="middle" class="svg-text-sm">Цена облигации ↓</text>
        <text x="465" y="190" text-anchor="middle" class="svg-text-sm">время</text>
        <!-- linking arrow -->
        <path d="M 290 100 H 350" class="svg-axis" stroke-dasharray="4 4"/>
        <polygon points="350,100 341,95 341,105" fill="var(--muted)"/>
      </svg>
      <figcaption>Ставки и цены движутся в <b>разные стороны</b>: когда ЦБ поднимает ключевую ставку,
      новые выпуски платят больше, и старые с низким купоном дешевеют. Чем длиннее облигация — тем резче падение.</figcaption>
    </figure>
    <div class="edu-card">
      <h4>💧 Риск ликвидности <span class="badge badge-yellow">Средний</span></h4>
      <p>Некоторые выпуски торгуются редко — трудно продать быстро по справедливой цене. Особенно актуально для ВДО и небольших выпусков. Проверяйте объём торгов перед покупкой.</p>
    </div>
    <div class="edu-card">
      <h4>📋 Риск оферты <span class="badge badge-yellow">Средний</span></h4>
      <p>После оферты эмитент может сильно снизить купон. Если не предъявить к выкупу и «прозевать» оферту — рискуете получить невыгодный купон. Следите за датами оферт.</p>
    </div>
    <div class="edu-card">
      <h4>📊 Инфляционный риск <span class="badge badge-blue">Низкий</span></h4>
      <p>Реальная доходность может оказаться ниже инфляции. Частично решается выбором ОФЗ-ИН (линкеров) или флоатеров с привязкой к ставке ЦБ.</p>
    </div>
    <div class="edu-card">
      <h4>🏛️ Налоговый риск <span class="badge badge-blue">Низкий</span></h4>
      <p>Купонный доход и прибыль от продажи облагаются НДФЛ 13–15%. Исключение: ОФЗ — купон не облагается НДФЛ. Используйте ИИС для налоговых льгот.</p>
    </div>
    <h3>Как снизить риски</h3>
    <ul>
      <li><strong>Диверсификация</strong> — не более 5–7% в одного эмитента, не более 15–20% в одну отрасль.</li>
      <li><strong>Рейтинг</strong> — для консервативного портфеля выбирайте BBB и выше (АКРА / Эксперт РА).</li>
      <li><strong>Лесенка по срокам</strong> — разные даты погашения снижают процентный риск.</li>
      <li><strong>Следите за офертами</strong> — заранее решайте: предъявлять или держать дальше.</li>
      <li><strong>ИИС</strong> — налоговый вычет до 52&nbsp;000&nbsp;₽/год или освобождение от НДФЛ на доход.</li>
    </ul>
  </section>

  <!-- ── 5. ГЛОССАРИЙ ── -->
  <section class="edu-section" id="slovar">
    <h2>Глоссарий терминов</h2>
    <dl class="edu-dl">
      <div class="row"><dt>Номинал</dt><dd>Базовая стоимость облигации, которую эмитент возвращает при погашении. Обычно 1000&nbsp;₽.</dd></div>
      <div class="row"><dt>Купон</dt><dd>Периодическая выплата процентов держателю облигации.</dd></div>
      <div class="row"><dt>НКД</dt><dd>Накопленный купонный доход — часть купона от последней выплаты до сегодня.</dd></div>
      <div class="row"><dt>Чистая цена</dt><dd>Рыночная цена без НКД. Отображается в котировках.</dd></div>
      <div class="row"><dt>Грязная цена</dt><dd>Чистая цена + НКД. Реальная сумма, уплачиваемая при покупке.</dd></div>
      <div class="row"><dt>YTM</dt><dd>Yield to Maturity — доходность к погашению с учётом реинвестирования купонов.</dd></div>
      <div class="row"><dt>Дюрация</dt><dd>Средневзвешенный срок до получения денежных потоков. Мера процентного риска: выше дюрация — сильнее реакция цены на изменение ставок.</dd></div>
      <div class="row"><dt>Оферта</dt><dd>Право держателя потребовать досрочного погашения по номиналу в определённую дату.</dd></div>
      <div class="row"><dt>Флоатер</dt><dd>Облигация с переменным купоном, привязанным к ставке ЦБ или RUONIA.</dd></div>
      <div class="row"><dt>Линкер (ОФЗ-ИН)</dt><dd>Облигация с номиналом, индексируемым на инфляцию.</dd></div>
      <div class="row"><dt>ОФЗ</dt><dd>Облигации федерального займа — государственные долговые бумаги России.</dd></div>
      <div class="row"><dt>ВДО</dt><dd>Высокодоходные облигации — с рейтингом ниже BB или без рейтинга, купон 18%+.</dd></div>
      <div class="row"><dt>АКРА / Эксперт РА</dt><dd>Российские рейтинговые агентства. Рейтинги: ААА → D (АКРА), ruAAA → ruD (Эксперт РА).</dd></div>
      <div class="row"><dt>ИИС</dt><dd>Индивидуальный инвестиционный счёт — даёт налоговые льготы (вычет тип А или освобождение тип Б).</dd></div>
      <div class="row"><dt>Листинг (уровень)</dt><dd>Уровень допуска к торгам на MOEX: 1-й — наивысший, 3-й — минимальные требования.</dd></div>
      <div class="row"><dt>Амортизация</dt><dd>Погашение номинала частями в течение срока обращения, а не единым платежом в конце.</dd></div>
    </dl>
  </section>

  <!-- ── 6. FAQ ── -->
  <section class="edu-section" id="faq">
    <h2>Частые вопросы</h2>
    <div class="edu-faq">
      <div class="row">
        <div class="q">Что такое YTM (доходность к погашению)?</div>
        <div class="a">YTM (Yield to Maturity) — годовая доходность облигации, если держать её до даты погашения и реинвестировать купоны по той же ставке. Учитывает не только купонный доход, но и разницу между текущей ценой и номиналом. Это основной показатель для сравнения облигаций.</div>
      </div>
      <div class="row">
        <div class="q">Чем оферта отличается от погашения?</div>
        <div class="a">Погашение — дата, когда эмитент обязательно возвращает номинал всем держателям. Оферта (put-оферта) — дата, когда держатель имеет право, но не обязан, предъявить облигацию к досрочному выкупу по номиналу. После оферты эмитент может изменить ставку купона.</div>
      </div>
      <div class="row">
        <div class="q">Что такое НКД?</div>
        <div class="a">НКД — часть купона, накопленная с даты последней выплаты до текущего дня. При покупке вы доплачиваете НКД продавцу, при продаже — получаете НКД от покупателя. Включается в полную (грязную) стоимость позиции.</div>
      </div>
      <div class="row">
        <div class="q">Что выбрать: ОФЗ или корпоративные облигации?</div>
        <div class="a">ОФЗ — самый надёжный рублёвый инструмент, выпускаются Минфином, купон освобождён от НДФЛ. Корпоративные дают более высокую доходность за счёт кредитного риска: чем ниже рейтинг, тем выше купон и риск. Консервативному инвестору подходят ОФЗ и бумаги рейтинга BBB и выше.</div>
      </div>
      <div class="row">
        <div class="q">Что такое флоатер и линкер?</div>
        <div class="a">Флоатер — облигация с переменным купоном, привязанным к ключевой ставке ЦБ или RUONIA: купон растёт вместе со ставками. Линкер (ОФЗ-ИН) — облигация с номиналом, индексируемым на инфляцию.</div>
      </div>
      <div class="row">
        <div class="q">Облагается ли купон по облигациям НДФЛ?</div>
        <div class="a">Купонный доход и прибыль от продажи облигаций облагаются НДФЛ 13–15%. Используя ИИС, можно получить налоговый вычет или освобождение от НДФЛ на доход.</div>
      </div>
    </div>
  </section>

  <div class="edu-cta">
    <h2>Соберите портфель облигаций с Bond AI</h2>
    <p>AI подберёт облигации под ваш риск-профиль, рассчитает YTM и купонный календарь по данным MOEX. Базовый тариф — бесплатно навсегда.</p>
    <a href="/app" class="btn">Попробовать бесплатно</a>
  </div>
</article>
"""

_UCHEBNIK_HTML = _page_shell(
    "Учебник по облигациям — что такое YTM, оферта, виды и риски | Bond AI",
    "Понятный учебник по облигациям для начинающих: что такое YTM (доходность к погашению), НКД, оферта, виды облигаций (ОФЗ, корпоративные, ВДО), флоатеры и линкеры, риски и как их снизить. С примерами и глоссарием.",
    _BASE_URL + "/uchebnik",
    _JSONLD,
    _BODY,
    og_type="article",
)


@router.api_route("/uchebnik", response_class=HTMLResponse, methods=["GET", "HEAD"])
async def uchebnik_page() -> HTMLResponse:
    return HTMLResponse(_UCHEBNIK_HTML, headers=_PUBLIC_CACHE)
