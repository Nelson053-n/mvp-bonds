      // ── THEME INIT (runs before DOMContentLoaded) ────────────────
      (function(){
        const t = localStorage.getItem('mvp_theme') || 'dark';
        if (t === 'light') document.documentElement.setAttribute('data-theme','light');
      })();

      const SVG_MOON = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
      const SVG_SUN  = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`;

      function toggleTheme() {
        const isLight = document.documentElement.getAttribute('data-theme') === 'light';
        if (isLight) {
          document.documentElement.removeAttribute('data-theme');
          localStorage.setItem('mvp_theme', 'dark');
        } else {
          document.documentElement.setAttribute('data-theme', 'light');
          localStorage.setItem('mvp_theme', 'light');
        }
        updateThemeBtn();
        // Redraw charts with new theme colors
        document.dispatchEvent(new Event('theme-change'));
      }

      function updateThemeBtn() {
        const btn = document.getElementById('btn-theme-toggle');
        if (!btn) return;
        const isLight = document.documentElement.getAttribute('data-theme') === 'light';
        btn.innerHTML = isLight ? SVG_MOON : SVG_SUN;
        btn.title = isLight ? 'Тёмная тема' : 'Светлая тема';
      }

      // ── NAV ─────────────────────────────────────────────────────
      const navBtns = document.querySelectorAll('.nav-btn');
      const panels  = document.querySelectorAll('.panel');
      function showPanel(id) {
        panels.forEach(p => {
          if (p.id === id) {
            p.classList.remove('active');
            void p.offsetWidth; // force reflow → restart animation
            p.classList.add('active');
          } else {
            p.classList.remove('active');
          }
        });
        navBtns.forEach(b => b.classList.toggle('active', b.dataset.panel === id));
        // panel-add and panel-analytics/watchlist are removed; no-op for them
      }
      navBtns.forEach(b => b.addEventListener('click', () => {
        if (!b.dataset.panel) return;  // plain links (e.g. /bond catalog) navigate on their own
        showPanel(b.dataset.panel);
        if (b.dataset.panel === 'panel-io' && window._loadTbankSyncStatus) {
          const pid = parseInt(localStorage.getItem('mvp_active_portfolio_id') || '0', 10);
          if (pid) window._loadTbankSyncStatus(pid);
        }
      }));
      document.querySelector('.topbar-brand').addEventListener('click', () => showPanel('panel-table'));

      // ── CONSTANTS ────────────────────────────────────────────────
      const TABLE_CACHE_KEY    = 'mvp_portfolio_table_cache_v2';
      const TABLE_CACHE_TS_KEY = 'mvp_portfolio_table_cache_ts_v2';
      const SYNC_INTERVAL_MS   = 900 * 1000;  // 15 min — matches server cache refresh interval
      const MONTH_NAMES_RU = ['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек'];
      const MONTH_NAMES_EN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      function getMonthNames() { return window._lang === 'en' ? MONTH_NAMES_EN : MONTH_NAMES_RU; }

      // ── LANG INIT (early, before IIFE) ───────────────────────────
      // Must run before any showDashboard/applyLang call in the async IIFE
      (function() {
        const saved = localStorage.getItem('mvp_lang');
        if (saved && saved !== 'undefined') { window._lang = saved; return; }
        const browserLang = (navigator.language || navigator.userLanguage || 'ru').toLowerCase();
        window._lang = browserLang.startsWith('ru') ? 'ru' : 'en';
        localStorage.setItem('mvp_lang', window._lang);
      })();

      // ── STATE ────────────────────────────────────────────────────
      let tableRows = [];
      let sortState = { key: null, direction: 'asc' };
      let portfolioId = null;
      let portfolios = [];
      let allPortfoliosTotalValue = 0;
      let isReadOnly = false;
      let shareToken = null;
      let sharePassword = null;
      const isAllMode = window.location.pathname === '/all';

      // ── PARSE URL ────────────────────────────────────────────────
      function parseShareUrl() {
        // Check if share token was injected by the backend
        if (window.shareToken && window.isSharedView) {
          shareToken = window.shareToken;
          const cachedPassword = sessionStorage.getItem(`mvp_share_password_${shareToken}`);
          if (cachedPassword) sharePassword = cachedPassword;
          return true;
        }
        // Fallback: detect share token directly from URL path /share/{token}
        const pathMatch = window.location.pathname.match(/^\/share\/([0-9a-f-]{36})\/?$/i);
        if (pathMatch) {
          shareToken = pathMatch[1];
          const cachedPassword = sessionStorage.getItem(`mvp_share_password_${shareToken}`);
          if (cachedPassword) sharePassword = cachedPassword;
          return true;
        }
        // Fallback to URL query parameters or hash (for backward compatibility)
        const params = new URLSearchParams(window.location.search);
        const hash = window.location.hash.slice(1);
        shareToken = params.get('share') || (hash.startsWith('share=') ? hash.substring(6) : null);
        if (shareToken) {
          const cachedPassword = sessionStorage.getItem(`mvp_share_password_${shareToken}`);
          if (cachedPassword) sharePassword = cachedPassword;
        }
        return !!shareToken;
      }

      // ── HTML ESCAPE (XSS protection) ─────────────────────────────
      function esc(s) {
        if (s == null) return '';
        return String(s)
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;');
      }

      // ── TOAST NOTIFICATIONS ──────────────────────────────────────
      (function() {
        const TOAST_STYLES = `
          #_toast_container {
            position: fixed; bottom: 24px; right: 24px; z-index: 9999;
            display: flex; flex-direction: column; gap: 8px; pointer-events: none;
          }
          .toast {
            display: flex; align-items: center; gap: 10px;
            padding: 12px 16px; border-radius: 10px; font-size: 13px; font-weight: 500;
            max-width: 360px; pointer-events: auto; cursor: pointer;
            box-shadow: 0 4px 20px rgba(0,0,0,.4);
            animation: toastIn .2s ease; color: #fff;
            backdrop-filter: blur(8px);
          }
          .toast.success { background: rgba(22,163,74,.92); }
          .toast.error   { background: rgba(220,38,38,.92); }
          .toast.info    { background: rgba(37,99,235,.92); }
          .toast.warn    { background: rgba(202,138,4,.92); }
          .toast-icon    { font-size: 16px; flex-shrink: 0; }
          .toast-msg     { flex: 1; line-height: 1.4; }
          .toast-close   { opacity: .7; font-size: 16px; padding: 0 2px; }
          @keyframes toastIn { from { opacity:0; transform:translateY(8px) scale(.97); } to { opacity:1; transform:none; } }
          @keyframes toastOut { to { opacity:0; transform:translateY(4px) scale(.97); } }
        `;
        const style = document.createElement('style');
        style.textContent = TOAST_STYLES;
        document.head.appendChild(style);
        const container = document.createElement('div');
        container.id = '_toast_container';
        container.setAttribute('aria-live', 'polite');
        container.setAttribute('aria-atomic', 'false');
        document.body.appendChild(container);
      })();

      function toast(msg, type = 'info', duration = 4000) {
        const container = document.getElementById('_toast_container');
        if (!container) return;
        const icons = { success: '✓', error: '✕', info: 'ℹ', warn: '⚠' };
        const el = document.createElement('div');
        el.className = `toast ${type}`;
        el.setAttribute('role', type === 'error' ? 'alert' : 'status');
        el.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span class="toast-msg">${esc(msg)}</span><span class="toast-close">✕</span>`;
        const close = () => {
          el.style.animation = 'toastOut .2s ease forwards';
          setTimeout(() => el.remove(), 200);
        };
        el.querySelector('.toast-close').onclick = close;
        container.appendChild(el);
        if (duration > 0) setTimeout(close, duration);
        return el;
      }

      // ── API HELPER ───────────────────────────────────────────────
      async function apiFetch(url, opts = {}) {
        const headers = { ...(opts.headers || {}) };

        if (isReadOnly && shareToken) {
          if (sharePassword) {
            headers['X-Share-Password'] = sharePassword;
          }
        } else {
          const token = localStorage.getItem('mvp_auth_token');
          if (token) {
            headers['Authorization'] = `Bearer ${token}`;
          }
        }
        const fetchOpts = { ...opts, headers };
        const response = await fetch(url, fetchOpts);
        if (response.status === 401 && !isReadOnly) {
          clearUserState();
          toast(t('auth.sessionExpired', 'Сессия истекла, войдите заново'), 'warn');
          setTimeout(() => showLoginScreen(), 1200);
          throw new Error('session_expired');
        }
        return response;
      }

      // ── UTILS ────────────────────────────────────────────────────
      function roundTo2(v) { return Math.round(v * 100) / 100; }
      function fmt(v) {
        if (v === null || v === undefined) return '—';
        if (typeof v === 'number') return v.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
        const s = String(v);
        // Format ISO dates (YYYY-MM-DD) to DD-MM-YYYY
        if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
          const [year, month, day] = s.split('-');
          return `${day}-${month}-${year}`;
        }
        return s;
      }
      function setStatus(el, msg, isError) {
        el.textContent = msg;
        el.className = isError ? 'status error' : 'status ok';
      }

      // ── CACHE ────────────────────────────────────────────────────
      function _cacheKey(pid) { return `${TABLE_CACHE_KEY}_${pid || 0}`; }
      function _cacheTsKey(pid) { return `${TABLE_CACHE_TS_KEY}_${pid || 0}`; }
      function persistTableCache() {
        if (!portfolioId) return;
        localStorage.setItem(_cacheKey(portfolioId), JSON.stringify(tableRows));
        localStorage.setItem(_cacheTsKey(portfolioId), String(Date.now()));
      }
      function loadTableCache() {
        const savedId = localStorage.getItem('mvp_active_portfolio_id');
        if (!savedId) return false;
        try {
          const raw = localStorage.getItem(_cacheKey(savedId));
          if (!raw) return false;
          const p = JSON.parse(raw);
          if (!Array.isArray(p) || p.length === 0) return false;
          tableRows = p; return true;
        } catch { return false; }
      }
      function getLastSyncTimestamp() {
        const p = Number(localStorage.getItem(_cacheTsKey(portfolioId)));
        return Number.isFinite(p) ? p : 0;
      }

      // ── RATING ───────────────────────────────────────────────────
      // Canonical order from highest to lowest (matches АКРА/Эксперт РА шкала)
      const RATING_ORDER = [
        'AAA','AA+','AA','AA-',
        'A+','A','A-',
        'BBB+','BBB','BBB-',
        'BB+','BB','BB-',
        'B+','B','B-',
        'CCC','CC','C',
        'RD','SD','D',
      ];

      function parseRating(raw) {
        if (!raw) return null;
        // Uppercase, strip whitespace, remove date/agency suffix like " (15.03.2024)" or "(RU)"
        let s = String(raw).toUpperCase().replace(/\s+/g, '').replace(/\([^)]*\)/g, '');
        // Strip leading "RU" prefix that MOEX adds (ruAAA → AAA, ruAA+ → AA+)
        s = s.replace(/^RU(?=[A-Z])/, '');
        // Match longest first to avoid 'AA' matching 'AAA'
        const sorted = [...RATING_ORDER].sort((a, b) => b.length - a.length);
        for (const r of sorted) {
          if (s === r) return r;
        }
        return null;
      }
      function ratingToScore(r) {
        if (!r) return null;
        const idx = RATING_ORDER.indexOf(r);
        if (idx === -1) return null;
        // 100 = best (AAA), 0 = worst (D)
        return Math.round(100 - (idx / (RATING_ORDER.length - 1)) * 100);
      }
      function createRatingCell(raw) {
        const td = document.createElement('td');
        td.className = 'rating-cell';
        const score = ratingToScore(parseRating(raw));
        if (score === null) { td.textContent = fmt(raw); return td; }
        const track = document.createElement('div'); track.className = 'rating-track';
        const fill = document.createElement('div'); fill.className = 'rating-fill';
        fill.style.width = score + '%';
        fill.style.backgroundColor = 'hsl(' + Math.round(score * 1.2) + ' 68% 42%)';
        track.appendChild(fill);
        const lbl = document.createElement('span'); lbl.className = 'rating-label'; lbl.textContent = String(raw);
        track.appendChild(lbl);
        td.appendChild(track); return td;
      }

      // ── SORT ─────────────────────────────────────────────────────
      function compareValues(a, b) {
        const an = Number(a), bn = Number(b);
        if (Number.isFinite(an) && Number.isFinite(bn)) return an - bn;
        const as = String(a ?? '').toLowerCase(), bs = String(b ?? '').toLowerCase();
        return as < bs ? -1 : as > bs ? 1 : 0;
      }
      // True if the portfolio has full-profit data (T-Bank synced: НКД + coupons).
      function portfolioSupportsFull() {
        try { return (tableRows || []).some(r => r.full_profit !== null && r.full_profit !== undefined); }
        catch(e) { return false; }
      }
      function _tProfit(key, fallback) {
        try {
          if (typeof TRANSLATIONS !== 'undefined' && TRANSLATIONS[window._lang]) {
            return TRANSLATIONS[window._lang][key] || fallback;
          }
        } catch(e) {}
        return fallback;
      }
      (function initProfitMode() {
        // _profitModeDesired = what the user picked (persisted); _profitMode = what we
        // render right now. They differ only while data is still loading: a 'full'
        // desire degrades to 'total' for display without overwriting the saved choice,
        // so an early render with an empty table can't clobber the full-profit default.
        const stored = localStorage.getItem('mvp_profit_mode2');
        // Default = 'total' (price change since purchase). Coupons are shown
        // separately under the figure, not folded into "Прибыль" — mixing realized
        // coupon cash into P&L overstated it and confused users.
        window._profitModeDesired = (stored === 'day' || stored === 'total' || stored === 'full') ? stored : 'total';
        window._profitMode = window._profitModeDesired;
      })();
      function setProfitMode(m) {
        if (m !== 'day' && m !== 'total' && m !== 'full') m = 'full';
        // Persist the user's intent; degrade to total only for display when full has no data yet.
        window._profitModeDesired = m;
        try { localStorage.setItem('mvp_profit_mode2', m); } catch(e) {}
        window._profitMode = (m === 'full' && !portfolioSupportsFull()) ? 'total' : m;
        const th = document.getElementById('th-profit');
        if (!th) return;
        const lbl = th.querySelector('.th-profit-label');
        if (lbl) {
          const labels = {
            full:  _tProfit('tbl.profitFull', 'Полная'),
            total: _tProfit('tbl.profit', 'Прибыль'),
            day:   _tProfit('tbl.profitDay', 'За день'),
          };
          lbl.textContent = labels[window._profitMode] || labels.total;
        }
        const toggle = document.getElementById('th-profit-toggle');
        if (toggle) {
          // Hide toggle entirely on manual portfolios that have no full mode AND
          // are showing the default 'total' (still allow total⇄day there).
          toggle.style.display = '';
          const special = window._profitMode !== 'total';
          toggle.style.borderColor = special ? 'var(--blue-500)' : 'var(--border)';
          toggle.style.color = special ? 'var(--blue-500)' : 'var(--text-muted)';
        }
        // Mirror the current mode on the prominent header button. "total" reads as
        // "От покупки" here (the column header keeps the bare "Прибыль").
        const modeLbl = document.getElementById('btn-profit-mode-label');
        if (modeLbl) {
          const labels = {
            full:  window._lang === 'en' ? 'Full' : 'Полная',
            total: window._lang === 'en' ? 'Since buy' : 'От покупки',
            day:   window._lang === 'en' ? 'Day' : 'За день',
          };
          modeLbl.textContent = labels[window._profitMode] || labels.total;
        }
      }
      // Wrapper used by the header button: advance to the next mode and re-render.
      function cycleProfitMode() {
        setProfitMode(nextProfitMode());
        if (typeof renderTable === 'function') renderTable();
      }
      function nextProfitMode() {
        const order = portfolioSupportsFull() ? ['full', 'total', 'day'] : ['total', 'day'];
        const idx = order.indexOf(window._profitModeDesired);
        return order[(idx + 1) % order.length];
      }
      function profitValueFor(row) {
        if (window._profitMode === 'day') return row.day_profit;
        if (window._profitMode === 'full') return (row.full_profit ?? row.profit);
        return row.profit;
      }
      function getSortValue(row, key) {
        if (key === 'company_rating') { const s = ratingToScore(parseRating(row.company_rating)); return s === null ? -1 : s; }
        if (key === 'profit' && window._profitMode === 'day') {
          const v = row.day_profit;
          return (v === null || v === undefined) ? -Infinity : v;
        }
        if (key === 'profit' && window._profitMode === 'full') {
          const v = (row.full_profit ?? row.profit);
          return (v === null || v === undefined) ? -Infinity : v;
        }
        return row[key] ?? '';
      }
      function computeSortFields(row) {
        if (row.type === 'bond') {
          const coupon = Number(row.coupon || 0);
          const period = Number(row.coupon_period || 0);
          const qty = Number(row.quantity || 0);
          const freq = period > 0 ? Math.round(365 / period) : 2;
          row.coupon_yield = Number(row.coupon_rate || 0);
          row.coupon_frequency = freq;
          row.your_annual_coupon = coupon > 0 && qty > 0 ? roundTo2(coupon * qty * freq) : 0;
        } else {
          row.coupon_yield = 0;
          row.coupon_frequency = 0;
          row.your_annual_coupon = 0;
        }
      }
      function getSortedRows(rows) {
        if (!sortState.key) return rows;
        return [...rows].sort((a, b) => {
          const r = compareValues(getSortValue(a, sortState.key), getSortValue(b, sortState.key));
          return sortState.direction === 'asc' ? r : -r;
        });
      }

      // ── RECALC ───────────────────────────────────────────────────
      function recalculateWeights(rows) {
        const total = rows.reduce((s, r) => s + Number(r.current_value || 0), 0);
        rows.forEach(r => { r.weight = total > 0 ? roundTo2(Number(r.current_value || 0) / total * 100) : 0; });
      }
      function recalculateRow(row, qty, price) {
        const cp = Number(row.current_price || 0);
        const aci = Number(row.aci || 0);
        const dirty = cp + aci;
        row.quantity = qty; row.purchase_price = price;
        row.current_value = roundTo2(dirty * qty);
        row.profit = roundTo2((cp - price) * qty);
      }

      // ── SUMMARY ──────────────────────────────────────────────────
      function calculateSummary(rows) {
        const totalAssets  = rows.reduce((s, r) => s + Number(r.current_value || 0), 0);
        const totalProfit  = rows.reduce((s, r) => s + Number(r.profit || 0), 0);
        const totalInvested = rows.reduce((s, r) => s + Number(r.purchase_price || 0) * Number(r.quantity || 0), 0);
        const totalFullProfit = rows.reduce((s, r) => s + Number((r.full_profit ?? r.profit) || 0), 0);
        const totalRealizedCoupons = rows.reduce((s, r) => s + Number(r.realized_coupons || 0), 0);
        const hasFullData = rows.some(r => r.full_profit !== null && r.full_profit !== undefined);
        // Day P&L: sum across rows that have day_profit. Day base = sum of prev_close_value
        // ONLY for those same rows (so the percentage uses a comparable denominator).
        let dayProfit = 0, dayBase = 0, dayCount = 0;
        for (const r of rows) {
          if (r.day_profit !== null && r.day_profit !== undefined) {
            dayProfit += Number(r.day_profit);
            dayBase += Number(r.prev_close_value || 0);
            dayCount++;
          }
        }
        const hasDayData = dayCount > 0;
        let annualIncome = 0, weightedYield = 0, bondWeightSum = 0;
        let weightedCouponRate = 0, couponRateWeightSum = 0;
        for (const row of rows) {
          if (row.type !== 'bond') continue;
          const qty = Number(row.quantity || 0);
          const coupon = Number(row.coupon || 0), period = Number(row.coupon_period || 0);
          const freq = period > 0 ? Math.round(365 / period) : 2;
          if (coupon > 0 && qty > 0) annualIncome += coupon * qty * freq;
          const pv = Number(row.current_value || 0), my = Number(row.market_yield || 0);
          if (pv > 0 && my > 0) { weightedYield += my * pv; bondWeightSum += pv; }
          const cr = Number(row.coupon_rate || 0);
          if (pv > 0 && cr > 0) { weightedCouponRate += cr * pv; couponRateWeightSum += pv; }
        }
        const annualYield    = totalAssets > 0 ? (annualIncome / totalAssets) * 100 : 0;
        const totalBondYield = bondWeightSum > 0 ? weightedYield / bondWeightSum : 0;
        const avgCouponRate  = couponRateWeightSum > 0 ? weightedCouponRate / couponRateWeightSum : 0;
        return { totalAssets, totalProfit, totalInvested, totalFullProfit, totalRealizedCoupons,
                 hasFullData, dayProfit, dayBase, hasDayData,
                 annualIncome, annualYield, totalBondYield, avgCouponRate };
      }

      // ── UPDATE STAT CARDS ────────────────────────────────────────
      function animateCounter(el, toValue, format) {
        const duration = 550;
        const from = parseFloat(el.dataset.counterFrom || '0') || 0;
        el.dataset.counterFrom = toValue;
        if (cancelAnimationFrame) cancelAnimationFrame(el._raf);
        const startTime = performance.now();
        function step(now) {
          const t = Math.min((now - startTime) / duration, 1);
          const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
          el.textContent = format(from + (toValue - from) * eased);
          if (t < 1) { el._raf = requestAnimationFrame(step); }
          else el.textContent = format(toValue);
        }
        el._raf = requestAnimationFrame(step);
      }

      function updateStatCards(summary) {
        const sv  = document.getElementById('stat-value');
        const sp  = document.getElementById('stat-profit');
        const sc  = document.getElementById('stat-coupon');
        const svs = document.getElementById('stat-value-sub');
        const sps = document.getElementById('stat-profit-sub');
        const scs = document.getElementById('stat-coupon-sub');

        const cashRub = Number(window.cashTotalRub || 0);
        const isEmpty = tableRows.length === 0 && cashRub === 0;

        const n = tableRows.length;
        const posSuffix = n === 1 ? t('stat.sub.pos1') : n < 5 ? t('stat.sub.pos2') : t('stat.sub.pos5');

        sv.className = 'stat-value';
        const displayTotal = roundTo2(summary.totalAssets + cashRub);
        animateCounter(sv, isEmpty ? 0 : displayTotal, v => fmt(roundTo2(v)) + ' ₽');
        const posText = n + ' ' + t('stat.sub.positions') + posSuffix;
        const cashText = cashRub > 0 ? '<br><span style="color:var(--text-muted)">в т.ч. свободно: ' + fmt(roundTo2(cashRub)) + ' ₽</span>' : '';
        const totalText = (!isAllMode && portfolios.length > 1 && allPortfoliosTotalValue > 0) ? '<br><span style="color:var(--blue-500);font-weight:600">Всего: ' + fmt(roundTo2(allPortfoliosTotalValue)) + ' ₽</span>' : '';
        svs.innerHTML = isEmpty ? t('stat.sub.empty') : (posText + cashText + totalText);
        svs.style.whiteSpace = 'pre-wrap';

        // Headline follows the selected profit mode (the ⇄ toggle on the table),
        // so the big number matches the column. 'full' only if data supports it.
        const useFull = window._profitMode === 'full' && summary.hasFullData;
        const headlineProfit = useFull
          ? summary.totalFullProfit
          : (window._profitMode === 'day' ? summary.dayProfit : summary.totalProfit);
        sp.className = 'stat-value ' + (isEmpty ? '' : headlineProfit > 0 ? 'positive' : headlineProfit < 0 ? 'negative' : '');
        animateCounter(sp, isEmpty ? 0 : roundTo2(headlineProfit),
          v => (v >= 0 ? '+' : '') + fmt(roundTo2(v)) + ' ₽');
        if (isEmpty) {
          sps.textContent = t('stat.sub.addItems');
        } else {
          const pctOfPortfolio = summary.totalInvested > 0
            ? ((headlineProfit / summary.totalInvested) * 100).toFixed(2) + t('stat.sub.ofPortfolio')
            : '\u00a0';
          const realizedCoupons = useFull
            ? Number(summary.totalRealizedCoupons || 0)
            : Number((window._analyticsExtra && window._analyticsExtra.realized_coupons) || 0);
          const aciTotal = tableRows.reduce((s, r) => s + (Number(r.aci || 0) * Number(r.quantity || 0)), 0);
          const parts = [];
          if (realizedCoupons > 0) parts.push('купоны: ' + Math.round(realizedCoupons).toLocaleString('ru') + ' ₽');
          if (aciTotal > 0) parts.push('НКД: ' + Math.round(aciTotal).toLocaleString('ru') + ' ₽');
          const breakdown = parts.length
            ? '<br><span style="font-size:10px;color:var(--text-muted);">' + parts.join(' · ') + '</span>'
            : '';
          let dayLine = '';
          if (summary.hasDayData) {
            const dp = summary.dayProfit;
            const dpct = summary.dayBase > 0 ? (dp / summary.dayBase) * 100 : 0;
            const color = dp > 0 ? 'var(--green-400)' : (dp < 0 ? 'var(--red-400)' : 'var(--text-muted)');
            const sign = dp >= 0 ? '+' : '';
            const pctSign = dpct >= 0 ? '+' : '';
            dayLine = '<br><span style="font-size:11px;color:' + color + ';font-weight:500;">'
              + 'За день: ' + sign + fmt(roundTo2(dp)) + ' ₽ ('
              + pctSign + dpct.toFixed(2) + '%)</span>';
          }
          sps.innerHTML = pctOfPortfolio + breakdown + dayLine;
        }

        sc.className = 'stat-value';
        animateCounter(sc, isEmpty ? 0 : roundTo2(summary.annualIncome), v => fmt(roundTo2(v)) + ' ₽');
        if (isEmpty) {
          scs.textContent = t('stat.sub.noCoupons');
        } else {
          const grossPct = fmt(roundTo2(summary.annualYield)) + t('stat.sub.ofValue');
          const netCoupon = _applyRfTax(summary.annualIncome);
          const taxStr = netCoupon < summary.annualIncome
            ? '<br><span style="font-size:10px;color:var(--text-muted);">после налогов: ' + Math.round(netCoupon).toLocaleString('ru') + ' ₽</span>'
            : '';
          scs.innerHTML = grossPct + taxStr;
        }

        const sr  = document.getElementById('stat-risk');
        const srs = document.getElementById('stat-risk-sub');
        if (sr && srs) {
          if (isEmpty) {
            sr.textContent = '—'; sr.style.color = ''; srs.textContent = '\u00a0';
          } else {
            const riskKey = yieldToRiskKey(summary.totalBondYield);
            const rInfo = getRiskInfo(riskKey);
            sr.textContent = rInfo.label;
            sr.style.color = rInfo.color;
            const parts = [];
            if (summary.totalBondYield > 0) parts.push(summary.totalBondYield.toFixed(1) + '% ' + t('stat.sub.avgYtm'));
            const divObj = calculateDiversificationScore(tableRows);
            if (divObj && divObj.score >= 0) {
              const lbl = divObj.score >= 70 ? 'отличная' : divObj.score >= 50 ? 'хорошая' : divObj.score >= 30 ? 'средняя' : 'слабая';
              const col = divObj.score >= 70 ? 'var(--green-400)' : divObj.score >= 50 ? 'var(--text-secondary)' : divObj.score >= 30 ? '#f59e0b' : 'var(--red-400)';
              parts.push('<span style="font-size:10px;color:var(--text-muted);" title="Сводная оценка диверсификации (0-100). Учитывает разброс по эмитентам, рейтингам, срокам, валютам и типу купона.">диверсификация: <span style="color:' + col + ';">' + divObj.score + '/100</span> · ' + lbl + '</span>');
            }
            srs.innerHTML = parts.join('<br>');
          }
        }
      }

      function updateNextCouponWidget(rows) {
        const el = document.getElementById('stat-next-coupon');
        const sub = document.getElementById('stat-next-coupon-sub');
        if (!el) return;
        const today = new Date();
        let nearest = null;
        let nearestRow = null;
        rows.forEach(row => {
          if (!row.next_coupon_date) return;
          const d = new Date(row.next_coupon_date);
          if (d < today) return;
          if (!nearest || d < nearest) { nearest = d; nearestRow = row; }
        });
        if (nearest && nearestRow) {
          const diff = Math.round((nearest - today) / 864e5);
          el.textContent = nearest.toLocaleDateString('ru', {day:'numeric',month:'short'});
          el.style.color = diff <= 7 ? 'var(--green-600)' : '';
          if (sub) {
            const emitter = (nearestRow.name || nearestRow.ticker || '').replace(/\s*(АО|ПАО|ООО)\s*/gi,'').trim().substring(0, 18);
            const couponAmt = nearestRow.coupon != null && nearestRow.quantity > 0
              ? Math.round(nearestRow.coupon * nearestRow.quantity) : null;
            const amtStr = couponAmt ? `${couponAmt.toLocaleString('ru')} ₽` : null;
            if (diff <= 7) {
              sub.textContent = `через ${diff} дн.` + (emitter ? ` · ${emitter}` : '');
            } else {
              sub.textContent = [emitter, amtStr].filter(Boolean).join(' · ');
            }
          }
        } else {
          el.textContent = '—';
          if (sub) sub.textContent = '';
        }
      }

      function updateYTMWidget(rows) {
        const el = document.getElementById('stat-ytm');
        const sub = document.getElementById('stat-ytm-sub');
        if (!el) return;
        const bonds = rows.filter(r => r.type === 'bond' && r.market_yield > 0 && r.current_value > 0);
        if (!bonds.length) { el.textContent = '—'; if (sub) sub.textContent = ''; return; }
        const totalVal = bonds.reduce((s, r) => s + r.current_value, 0);
        const wtd = bonds.reduce((s, r) => s + r.market_yield * r.current_value, 0) / totalVal;
        el.textContent = `${wtd.toFixed(1)}%`;
        if (sub) {
          const keyRate = Number(window._keyRate || 0);
          const annualGross = wtd / 100 * totalVal;
          const netYtm = wtd * (1 - _effectiveTaxRate(annualGross));
          const parts = [];
          if (keyRate > 0) {
            const spread = wtd - keyRate;
            const sign = spread >= 0 ? '+' : '';
            const col = spread >= 2 ? 'var(--green-400)' : spread < 0 ? 'var(--red-400)' : 'var(--text-muted)';
            parts.push(`<span style="color:${col};">${sign}${spread.toFixed(1)} п.п.</span> к ставке ${keyRate.toFixed(1)}%`);
          } else {
            parts.push('средневзвешенная');
          }
          parts.push(`<span style="font-size:10px;color:var(--text-muted);">после налогов: ${netYtm.toFixed(1)}%</span>`);
          // Duration inline
          const now = new Date();
          const withMat = rows.filter(r => r.type === 'bond' && r.current_value > 0)
            .map(r => ({ r, yrs: _bondMaturityYears(r, now) }))
            .filter(x => x.yrs != null && x.yrs > 0);
          if (withMat.length) {
            const totVal = withMat.reduce((s, x) => s + x.r.current_value, 0);
            const mac = withMat.reduce((s, x) => s + x.yrs * x.r.current_value, 0) / totVal;
            parts.push(`<span style="font-size:10px;color:var(--text-muted);" title="Средневзвешенный срок до погашения (Macaulay duration). Чем больше — тем чувствительнее портфель к ставке ЦБ.">дюрация: ${mac.toFixed(1)} г</span>`);
          }
          sub.innerHTML = parts.join('<br>');
        }
      }

      // RF tax helper: progressive 13/15% by annual income. Threshold 5 млн ₽/год.
      function _effectiveTaxRate(annualIncome) {
        if (annualIncome <= 0) return 0;
        const threshold = 5_000_000;
        if (annualIncome <= threshold) return 0.13;
        return (threshold * 0.13 + (annualIncome - threshold) * 0.15) / annualIncome;
      }
      function _applyRfTax(annualIncome) {
        return annualIncome * (1 - _effectiveTaxRate(annualIncome));
      }

      // Diversification score (0-100) from inverse HHI across 5 dimensions
      function _hhiFromWeights(weights) {
        const total = weights.reduce((s, w) => s + w, 0);
        if (total <= 0) return 1;
        return weights.reduce((s, w) => s + Math.pow(w / total, 2), 0);
      }
      function calculateDiversificationScore(rows) {
        if (!rows.length) return 0;
        // 1. Issuers
        const byIssuer = {};
        rows.forEach(r => {
          const k = (r.name || r.ticker || '?').replace(/\s*(АО|ПАО|ООО|ОФЗ)\s*/gi, '').split(/[\s\-]/)[0].substring(0, 12);
          byIssuer[k] = (byIssuer[k] || 0) + (r.current_value || 0);
        });
        const issuerHHI = _hhiFromWeights(Object.values(byIssuer));
        // 2. Ratings (grouped to broad bucket)
        const ratingBucket = r => {
          const x = String(r || '').toUpperCase().replace(/^RU/, '');
          if (x.startsWith('AAA')) return 'AAA';
          if (x.startsWith('AA')) return 'AA';
          if (x.startsWith('A')) return 'A';
          if (x.startsWith('BBB')) return 'BBB';
          if (x.startsWith('BB')) return 'BB';
          if (x.startsWith('B')) return 'B';
          if (x.startsWith('C') || x.startsWith('D')) return 'C';
          return 'NR';
        };
        const byRating = {};
        rows.forEach(r => { const b = ratingBucket(r.company_rating); byRating[b] = (byRating[b] || 0) + (r.current_value || 0); });
        const ratingHHI = _hhiFromWeights(Object.values(byRating));
        // 3. Maturity buckets
        const now = new Date();
        const matBuckets = { lt1: 0, m1_3: 0, m3_5: 0, gt5: 0, none: 0 };
        rows.forEach(r => {
          if (r.type !== 'bond') { matBuckets.none += (r.current_value || 0); return; }
          const yrs = _bondMaturityYears(r, now);
          if (yrs == null) { matBuckets.none += (r.current_value || 0); return; }
          if (yrs < 1) matBuckets.lt1 += r.current_value;
          else if (yrs < 3) matBuckets.m1_3 += r.current_value;
          else if (yrs < 5) matBuckets.m3_5 += r.current_value;
          else matBuckets.gt5 += r.current_value;
        });
        const maturityHHI = _hhiFromWeights(Object.values(matBuckets));
        // 4. Currency
        const byCcy = {};
        rows.forEach(r => {
          let c = (r.face_unit || 'SUR').toUpperCase();
          if (c === 'SUR' || c === '') c = 'RUB';
          byCcy[c] = (byCcy[c] || 0) + (r.current_value || 0);
        });
        const currencyHHI = _hhiFromWeights(Object.values(byCcy));
        // 5. Coupon type (fix vs float)
        const couponBuckets = { fix: 0, float: 0 };
        rows.forEach(r => {
          if (r.type !== 'bond') return;
          if (r.is_floater) couponBuckets.float += (r.current_value || 0);
          else couponBuckets.fix += (r.current_value || 0);
        });
        const couponHHI = _hhiFromWeights(Object.values(couponBuckets));
        // Combine: inverse weighted. HHI=1 (fully concentrated) → 0, HHI=0 (perfect diversification) → 100.
        // Weights: issuers 35%, ratings 25%, maturity 15%, currency 10%, coupon 15%.
        const combinedHHI =
          0.35 * issuerHHI +
          0.25 * ratingHHI +
          0.15 * maturityHHI +
          0.10 * currencyHHI +
          0.15 * couponHHI;
        const score = Math.max(0, Math.min(100, Math.round((1 - combinedHHI) * 100)));
        return { score, components: { issuerHHI, ratingHHI, maturityHHI, currencyHHI, couponHHI } };
      }
      async function updateCashWidget(pid) {
        const badge = document.getElementById('autosync-badge');
        const cashRow = document.getElementById('autosync-cash');
        if (!badge) return;
        // В all-режиме свободные средства — сумма по всем счетам, её ставит
        // loadExtraAnalytics() из free_cash_rub. Здесь не трогаем cashTotalRub,
        // иначе он перетрётся остатком одного (последнего) портфеля.
        if (isAllMode) { badge.style.display = 'none'; if (cashRow) cashRow.style.display = 'none'; return; }
        const hide = () => {
          badge.style.display = 'none';
          if (cashRow) { cashRow.style.display = 'none'; cashRow.textContent = ''; }
          if (Number(window.cashTotalRub || 0) !== 0) { window.cashTotalRub = 0; renderTable(); }
        };
        if (!pid) return hide();
        try {
          const r = await apiFetch('/tbank/sync/status?portfolio_id=' + pid);
          if (!r.ok) return hide();
          const s = await r.json();
          if (!s.enabled) return hide();
          const sym = { RUB: '₽', USD: '$', EUR: '€', CNY: '¥', HKD: 'HK$', GBP: '£' };
          const cash = (s.cash || []).filter(c => c.amount > 0);
          badge.textContent = '🔄 авто синхронизация с банком';
          badge.style.display = '';
          if (cashRow) {
            if (cash.length > 0) {
              const fmt = (c) => {
                const amt = c.amount.toLocaleString('ru', { maximumFractionDigits: 2 });
                return (sym[c.currency] || c.currency) + ' ' + amt;
              };
              cashRow.textContent = 'свободно ' + cash.map(fmt).join(' · ');
              cashRow.style.display = '';
            } else {
              cashRow.style.display = 'none';
              cashRow.textContent = '';
            }
          }
          const newTotal = Number(s.cash_total_rub || 0);
          if (newTotal !== Number(window.cashTotalRub || 0)) {
            window.cashTotalRub = newTotal;
            renderTable();
          }
        } catch (_) { hide(); }
      }

      function updateLastUpdateTime() {
        const el = document.getElementById('last-update-time');
        if (el) el.textContent = `Обновлено ${new Date().toLocaleTimeString('ru', {hour:'2-digit',minute:'2-digit'})}`;
      }

      // ── ANALYTICS ────────────────────────────────────────────────

      function drawPieChart(canvasId, data, colors) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const total = data.reduce((s, d) => s + d.value, 0);
        if (total === 0) return;
        let start = -Math.PI / 2;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const cx = canvas.width / 2, cy = canvas.height / 2, r = Math.min(cx, cy) - 8;
        data.forEach((d, i) => {
          const slice = (d.value / total) * 2 * Math.PI;
          ctx.beginPath();
          ctx.moveTo(cx, cy);
          ctx.arc(cx, cy, r, start, start + slice);
          ctx.closePath();
          ctx.fillStyle = colors[i % colors.length];
          ctx.fill();
          start += slice;
        });
        // Donut hole
        ctx.beginPath();
        ctx.arc(cx, cy, r * 0.55, 0, 2 * Math.PI);
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg-card').trim() || 'rgba(2,8,23,.95)';
        ctx.fill();
      }

      function buildLegend(containerId, data, colors, total) {
        const el = document.getElementById(containerId);
        if (!el) return;
        el.innerHTML = '';
        data.forEach((d, i) => {
          const pct = total > 0 ? ((d.value / total) * 100).toFixed(1) : '0';
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;gap:6px;';
          const dot = document.createElement('span');
          dot.style.cssText = `width:10px;height:10px;border-radius:50%;background:${colors[i % colors.length]};flex-shrink:0;`;
          const txt = document.createElement('span');
          txt.style.color = 'var(--slate-600)';
          txt.textContent = `${d.label} — ${pct}%`;
          row.appendChild(dot); row.appendChild(txt);
          el.appendChild(row);
        });
      }

      function renderAnalytics(rows) {
        if (!rows || rows.length === 0) return;

        // By type (horizontal bars)
        renderTypeBars(rows);
        // By issuer top-5 (horizontal bars)
        renderIssuerBars(rows);


        // Top/loss performers
        const withProfit = rows.filter(r => typeof r.profit === 'number');
        const sorted = [...withProfit].sort((a, b) => b.profit - a.profit);
        const renderPerformers = (containerId, items) => {
          const el = document.getElementById(containerId);
          if (!el) return;
          el.innerHTML = '';
          items.slice(0, 5).forEach(r => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--table-border);transition:background .1s;';
            row.addEventListener('mouseover', () => { row.style.background = 'var(--table-row-hover)'; });
            row.addEventListener('mouseout',  () => { row.style.background = ''; });
            const left = document.createElement('div');
            const name = document.createElement('div');
            name.style.cssText = 'font-weight:500;font-size:13px;color:var(--text-primary);';
            name.textContent = r.name || r.ticker;
            const ticker = document.createElement('div');
            ticker.style.cssText = 'font-size:11px;color:var(--text-muted);margin-top:2px;';
            ticker.textContent = r.ticker;
            left.appendChild(name); left.appendChild(ticker);
            const right = document.createElement('div');
            right.style.cssText = `font-weight:600;font-size:13px;color:${r.profit >= 0 ? 'var(--green-400)' : 'var(--red-400)'};`;
            right.textContent = `${r.profit >= 0 ? '+' : ''}${Math.round(r.profit).toLocaleString('ru')} ₽`;
            row.appendChild(left); row.appendChild(right);
            el.appendChild(row);
          });
        };
        renderPerformers('analytics-top-gain', sorted);
        renderPerformers('analytics-top-loss', [...sorted].reverse());

        // Bond-specific analytics
        renderDurationCard(rows);
        renderMaturityLadder(rows);
        renderRatingsChart(rows);
        renderCurrencyChart(rows);
        renderCouponTypeBar(rows);
        renderYTMCurve(rows);
        renderStressTest(rows);
        renderSellFirst(rows);
      }

      // ── BOND ANALYTICS: Duration ─────────────────────────────────
      function _bondMaturityYears(row, now) {
        // Effective maturity = nearest of offer/buyback/maturity (что наступит первым)
        const candidates = [row.maturity_date, row.buyback_date, row.offer_date]
          .filter(Boolean)
          .map(d => parseLocalDate(d))
          .filter(d => !isNaN(d.getTime()) && d > now);
        if (!candidates.length) return null;
        const nearest = new Date(Math.min(...candidates.map(d => d.getTime())));
        return (nearest - now) / (365.25 * 24 * 3600e3);
      }

      function renderDurationCard(rows) {
        const body = document.getElementById('duration-body');
        if (!body) return;
        const now = new Date();
        const bonds = rows.filter(r => r.type === 'bond' && r.current_value > 0);
        const withMat = bonds
          .map(r => ({ r, yrs: _bondMaturityYears(r, now) }))
          .filter(x => x.yrs != null && x.yrs > 0);
        if (!withMat.length) {
          body.innerHTML = '<div style="color:var(--text-muted);font-size:13px;">Нет данных для расчёта</div>';
          return;
        }
        const totalVal = withMat.reduce((s, x) => s + x.r.current_value, 0);
        const macaulay = withMat.reduce((s, x) => s + x.yrs * x.r.current_value, 0) / totalVal;
        const withYtm = bonds.filter(r => r.market_yield != null && r.market_yield > 0 && r.current_value > 0);
        const ytmTotal = withYtm.reduce((s, r) => s + r.current_value, 0);
        const avgYtmPct = ytmTotal > 0
          ? withYtm.reduce((s, r) => s + r.market_yield * r.current_value, 0) / ytmTotal
          : 10;
        const avgYtm = avgYtmPct / 100;
        const modDur = macaulay / (1 + avgYtm);
        const portfolioVal = bonds.reduce((s, r) => s + r.current_value, 0);
        const macStr = macaulay.toFixed(2);
        const modStr = modDur.toFixed(2);

        // Coupon-vs-YTM line: средневзвешенный coupon_rate vs avgYtm
        const withCoupon = bonds.filter(r => r.coupon_rate != null && r.coupon_rate > 0 && r.current_value > 0);
        const couponTotal = withCoupon.reduce((s, r) => s + r.current_value, 0);
        const avgCouponRate = couponTotal > 0
          ? withCoupon.reduce((s, r) => s + r.coupon_rate * r.current_value, 0) / couponTotal
          : 0;

        // Reinvestment risk: бумаги с погашением <12 мес
        const soonMat = withMat.filter(x => x.yrs < 1);
        const soonValue = soonMat.reduce((s, x) => s + x.r.current_value, 0);

        const couponVsYtmStr = (avgCouponRate > 0 && avgYtmPct > 0) ? (() => {
          const diff = avgYtmPct - avgCouponRate;
          if (Math.abs(diff) < 0.3) return `Купон ≈ YTM (${avgCouponRate.toFixed(1)}%) — портфель торгуется около номинала.`;
          if (diff > 0) return `Купон ${avgCouponRate.toFixed(1)}% / YTM ${avgYtmPct.toFixed(1)}% — бумаги <b style="color:var(--green-400);">ниже номинала</b>, +${diff.toFixed(1)}% upside к погашению.`;
          return `Купон ${avgCouponRate.toFixed(1)}% / YTM ${avgYtmPct.toFixed(1)}% — бумаги <b style="color:var(--red-400);">выше номинала</b>, премия ${(-diff).toFixed(1)}%.`;
        })() : '';

        const reinvestStr = soonValue > 0 ? (() => {
          const sharePct = soonValue / portfolioVal * 100;
          return `<div style="margin-top:12px;padding:10px 12px;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);border-radius:var(--radius);font-size:11px;color:var(--text-secondary);">
            <b style="color:#f59e0b;">Реинвест-риск:</b> ${Math.round(sharePct)}% портфеля гасится в течение года (≈${Math.round(soonValue).toLocaleString('ru')} ₽). Нужен план куда перекладывать.
          </div>`;
        })() : '';

        body.innerHTML = `
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px;">
            <div style="font-size:28px;font-weight:700;color:var(--text-primary);line-height:1;">${macStr}</div>
            <div style="font-size:13px;color:var(--text-muted);" title="Средневзвешенный срок до погашения (Macaulay duration). Чем больше — тем чувствительнее портфель к изменению ставки ЦБ.">года · средний срок до погашения</div>
          </div>
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:14px;" title="Модифицированная дюрация (Modified Duration). Показывает, на сколько процентов изменится стоимость портфеля при изменении ставки на 1 п.п.">Модиф. дюрация: <b style="color:var(--text-secondary);">${modStr}</b></div>

          <div style="margin-bottom:10px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
              <span style="font-size:12px;color:var(--text-secondary);font-weight:600;">Сценарий ставки:</span>
              <span id="duration-slider-val" style="font-size:12px;font-weight:700;color:var(--blue-500);">+1.0 п.п.</span>
            </div>
            <input type="range" id="duration-slider" min="-300" max="300" value="100" step="25"
              style="width:100%;cursor:pointer;accent-color:var(--blue-500);" />
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-muted);margin-top:2px;">
              <span>−3 п.п.</span><span>0</span><span>+3 п.п.</span>
            </div>
          </div>

          <div id="duration-impact" style="display:flex;gap:10px;align-items:stretch;margin-bottom:10px;flex-wrap:wrap;">
            <!-- filled by slider handler -->
          </div>

          ${couponVsYtmStr ? `<div style="font-size:11px;color:var(--text-secondary);padding:8px 10px;background:var(--bg-card-secondary, rgba(148,163,184,.06));border-radius:var(--radius-sm);line-height:1.4;">${couponVsYtmStr}</div>` : ''}
          ${reinvestStr}

          <div style="font-size:10px;color:var(--text-muted);margin-top:10px;line-height:1.4;">Упрощённая оценка по сроку до ближайшего события (погашение/оферта). Не учитывает выпуклость.</div>
        `;

        const slider = document.getElementById('duration-slider');
        const impactEl = document.getElementById('duration-impact');
        const valEl = document.getElementById('duration-slider-val');
        const fmtRub = v => Math.round(Math.abs(v)).toLocaleString('ru');
        const updateImpact = () => {
          const bp = parseFloat(slider.value);
          const pp = bp / 100;
          valEl.textContent = (pp >= 0 ? '+' : '') + pp.toFixed(2) + ' п.п.';
          const impactPct = -modDur * pp;
          const impactRub = portfolioVal * impactPct / 100;
          const color = impactRub >= 0 ? 'var(--green-400)' : 'var(--red-400)';
          const bg = impactRub >= 0 ? 'rgba(74,222,128,.08)' : 'rgba(248,113,113,.08)';
          const border = impactRub >= 0 ? 'rgba(74,222,128,.25)' : 'rgba(248,113,113,.25)';
          const sign = impactPct >= 0 ? '+' : '';
          const newVal = portfolioVal + impactRub;
          impactEl.innerHTML = `
            <div style="flex:1 1 130px;min-width:130px;background:${bg};border:1px solid ${border};border-radius:var(--radius);padding:10px 12px;">
              <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px;">Изменение</div>
              <div style="font-size:17px;font-weight:700;color:${color};line-height:1;">${sign}${impactPct.toFixed(2)}%</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${impactRub >= 0 ? '+' : '−'}${fmtRub(impactRub)} ₽</div>
            </div>
            <div style="flex:1 1 130px;min-width:130px;background:var(--bg-card-secondary, rgba(148,163,184,.06));border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;">
              <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px;">Новая стоимость</div>
              <div style="font-size:17px;font-weight:700;color:var(--text-primary);line-height:1;">${Math.round(newVal).toLocaleString('ru')} ₽</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">было ${Math.round(portfolioVal).toLocaleString('ru')} ₽</div>
            </div>
          `;
        };
        slider.addEventListener('input', updateImpact);
        updateImpact();
      }

      // ── BOND ANALYTICS: Maturity ladder ──────────────────────────
      function renderMaturityLadder(rows) {
        const canvas = document.getElementById('maturity-ladder-chart');
        const emptyEl = document.getElementById('maturity-ladder-empty');
        if (!canvas) return;
        const now = new Date();
        const bonds = rows.filter(r => r.type === 'bond' && r.current_value > 0);
        const buckets = [
          { label: '<1 года', min: 0, max: 1, value: 0 },
          { label: '1-3 г',   min: 1, max: 3, value: 0 },
          { label: '3-5 л',   min: 3, max: 5, value: 0 },
          { label: '5+ лет',  min: 5, max: Infinity, value: 0 },
        ];
        let placed = 0;
        bonds.forEach(r => {
          const yrs = _bondMaturityYears(r, now);
          if (yrs == null || yrs <= 0) return;
          const b = buckets.find(b => yrs >= b.min && yrs < b.max);
          if (b) { b.value += r.current_value; placed++; }
        });
        if (!placed) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          return;
        }
        canvas.style.display = 'block';
        if (emptyEl) emptyEl.style.display = 'none';
        _drawHorizontalBars(canvas, buckets, '#2563eb');
      }

      // ── BOND ANALYTICS: Ratings distribution ─────────────────────
      function renderRatingsChart(rows) {
        const canvas = document.getElementById('ratings-chart');
        const emptyEl = document.getElementById('ratings-empty');
        if (!canvas) return;
        const norm = r => {
          if (!r || r === '—' || r === 'N/A') return null;
          const s = String(r).toUpperCase().replace(/^RU/, '');
          if (s.startsWith('AAA')) return 'AAA';
          if (s.startsWith('AA'))  return 'AA';
          if (s.startsWith('A'))   return 'A';
          if (s.startsWith('BBB')) return 'BBB';
          if (s.startsWith('BB'))  return 'BB';
          if (s.startsWith('B'))   return 'B';
          if (s.startsWith('C') || s.startsWith('D')) return 'C/D';
          return null;
        };
        const buckets = [
          { label: 'AAA',  value: 0, color: '#16a34a' },
          { label: 'AA',   value: 0, color: '#22c55e' },
          { label: 'A',    value: 0, color: '#84cc16' },
          { label: 'BBB',  value: 0, color: '#eab308' },
          { label: 'BB',   value: 0, color: '#f59e0b' },
          { label: 'B',    value: 0, color: '#f97316' },
          { label: 'C/D',  value: 0, color: '#dc2626' },
          { label: 'Без рейтинга', value: 0, color: '#64748b' },
        ];
        const bonds = rows.filter(r => r.type === 'bond' && r.current_value > 0);
        if (!bonds.length) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          return;
        }
        bonds.forEach(r => {
          const cat = norm(r.company_rating);
          if (cat) {
            const b = buckets.find(b => b.label === cat);
            if (b) b.value += r.current_value;
          } else {
            buckets[buckets.length - 1].value += r.current_value;
          }
        });
        const nonZero = buckets.filter(b => b.value > 0);
        if (!nonZero.length) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          return;
        }
        canvas.style.display = 'block';
        if (emptyEl) emptyEl.style.display = 'none';
        _drawHorizontalBars(canvas, nonZero, null);
      }

      // ── STRUCTURE: by currency (horizontal bars) ─────────────────
      function renderCurrencyChart(rows) {
        const canvas = document.getElementById('structure-currency-chart');
        const emptyEl = document.getElementById('structure-currency-empty');
        if (!canvas) return;
        const byCcy = {};
        rows.forEach(r => {
          let ccy = (r.face_unit || 'SUR').toUpperCase();
          if (ccy === 'SUR' || ccy === '') ccy = 'RUB';
          byCcy[ccy] = (byCcy[ccy] || 0) + (r.current_value || 0);
        });
        const cashRub = Number(window.cashTotalRub || 0);
        if (cashRub > 0) byCcy['RUB'] = (byCcy['RUB'] || 0) + cashRub;
        const ccyColors = { RUB: '#2563eb', USD: '#16a34a', EUR: '#8b5cf6', CNY: '#f59e0b', GBP: '#dc2626', CHF: '#0ea5e9', HKD: '#ec4899' };
        const buckets = Object.entries(byCcy)
          .filter(([_, v]) => v > 0)
          .map(([label, value]) => ({ label, value, color: ccyColors[label] || '#64748b' }))
          .sort((a, b) => b.value - a.value);
        if (!buckets.length) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          return;
        }
        canvas.style.display = 'block';
        if (emptyEl) emptyEl.style.display = 'none';
        _drawHorizontalBars(canvas, buckets, null);
      }

      // ── STRUCTURE: by instrument type (horizontal bars) ──────────
      function renderTypeBars(rows) {
        const canvas = document.getElementById('structure-type-chart');
        const emptyEl = document.getElementById('structure-type-empty');
        if (!canvas) return;
        const typeMeta = {
          bond:  { label: 'Облигации', color: '#2563eb' },
          stock: { label: 'Акции',     color: '#16a34a' },
          etf:   { label: 'Фонды',     color: '#f59e0b' },
        };
        const byType = {};
        rows.forEach(r => {
          const meta = typeMeta[r.type] || { label: r.type || 'Прочее', color: '#64748b' };
          if (!byType[meta.label]) byType[meta.label] = { label: meta.label, value: 0, color: meta.color };
          byType[meta.label].value += (r.current_value || 0);
        });
        const buckets = Object.values(byType).filter(b => b.value > 0).sort((a, b) => b.value - a.value);
        if (!buckets.length) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          return;
        }
        canvas.style.display = 'block';
        if (emptyEl) emptyEl.style.display = 'none';
        _drawHorizontalBars(canvas, buckets, null);
      }

      // ── STRUCTURE: by issuer top-5 + Other (horizontal bars) ─────
      function renderIssuerBars(rows) {
        const canvas = document.getElementById('structure-issuer-chart');
        const emptyEl = document.getElementById('structure-issuer-empty');
        if (!canvas) return;
        const byIssuer = {};
        rows.forEach(r => {
          const name = (r.name || r.ticker || '?').substring(0, 20);
          byIssuer[name] = (byIssuer[name] || 0) + (r.current_value || 0);
        });
        const sorted = Object.entries(byIssuer)
          .map(([label, value]) => ({ label, value }))
          .sort((a, b) => b.value - a.value);
        const palette = ['#2563eb','#0ea5e9','#16a34a','#84cc16','#f59e0b'];
        const top5 = sorted.slice(0, 5).map((d, i) => ({ ...d, color: palette[i] }));
        const otherVal = sorted.slice(5).reduce((s, d) => s + d.value, 0);
        if (otherVal > 0) top5.push({ label: 'Другие', value: otherVal, color: '#64748b' });
        if (!top5.length) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          return;
        }
        canvas.style.display = 'block';
        if (emptyEl) emptyEl.style.display = 'none';
        _drawHorizontalBars(canvas, top5, null);
      }

      // ── BOND ANALYTICS: Coupon type bar ──────────────────────────
      function renderCouponTypeBar(rows) {
        const canvas = document.getElementById('coupon-type-chart');
        const empty = document.getElementById('coupon-type-empty');
        const hint = document.getElementById('coupon-type-hint');
        if (!canvas) return;
        const bonds = rows.filter(r => r.type === 'bond' && r.current_value > 0);
        if (!bonds.length) {
          canvas.style.display = 'none';
          if (empty) empty.style.display = 'block';
          if (hint) hint.textContent = '';
          return;
        }
        const total = bonds.reduce((s, r) => s + r.current_value, 0);
        const floatVal = bonds.filter(r => r.is_floater).reduce((s, r) => s + r.current_value, 0);
        const fixedVal = total - floatVal;
        const floatPct = total > 0 ? (floatVal / total) * 100 : 0;
        const buckets = [];
        if (fixedVal > 0) buckets.push({ label: 'Фиксированный', value: fixedVal, color: '#2563eb' });
        if (floatVal > 0) buckets.push({ label: 'Плавающий', value: floatVal, color: '#f59e0b' });
        canvas.style.display = 'block';
        if (empty) empty.style.display = 'none';
        _drawHorizontalBars(canvas, buckets, null);
        if (hint) {
          if (floatPct >= 70) hint.textContent = 'В основном флоатеры — выигрывает при росте ставки, проигрывает при снижении.';
          else if (floatPct >= 30) hint.textContent = 'Смешанный профиль — баланс между фиксом и защитой от роста ставки.';
          else if (floatPct > 0) hint.textContent = 'Флоатеров мало — портфель чувствителен к снижению ставки.';
          else hint.textContent = 'Все купоны фиксированные — выиграет от снижения ставки ЦБ.';
        }
      }

      // Reset canvas inline-width so it doesn't keep its parent inflated,
      // then measure actual available width from the parent. Returns int px.
      function _measureCanvasParent(canvas, fallback) {
        canvas.style.width = '0px';
        const parent = canvas.parentElement;
        const w = parent ? parent.clientWidth : 0;
        return w > 0 ? w : (fallback || 400);
      }

      // ── Shared: horizontal bar chart helper ──────────────────────
      function _drawHorizontalBars(canvas, buckets, fixedColor) {
        const W = _measureCanvasParent(canvas, 400);
        const dpr = window.devicePixelRatio || 1;
        const H = Math.max(140, 24 * buckets.length + 30);
        canvas.style.width = W + 'px';
        canvas.style.height = H + 'px';
        canvas.width = W * dpr;
        canvas.height = H * dpr;
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        ctx.clearRect(0, 0, W, H);
        const st = getComputedStyle(document.documentElement);
        const labelClr = st.getPropertyValue('--text-secondary').trim() || '#475569';
        const mutedClr = st.getPropertyValue('--text-muted').trim() || '#64748b';
        const trackClr = st.getPropertyValue('--table-border').trim() || 'rgba(148,163,184,.15)';
        // Measure widest label dynamically — hardcoded width clipped long
        // labels like "Фиксированный". Clamp so labels don't eat the bar.
        ctx.font = '600 11px Inter, system-ui, sans-serif';
        const measured = Math.max(...buckets.map(b => ctx.measureText(b.label).width));
        const labelW = Math.min(Math.max(72, Math.ceil(measured) + 8), Math.floor(W * 0.45));
        const valW = 90;
        const padR = 8;
        const barX = labelW + 6;
        const barW = Math.max(40, W - labelW - valW - padR - 6);
        const rowH = 22;
        const total = buckets.reduce((s, b) => s + b.value, 0) || 1;
        const maxV = Math.max(...buckets.map(b => b.value), 1);
        buckets.forEach((b, i) => {
          const y = 10 + i * (rowH + 4);
          // label
          ctx.fillStyle = labelClr;
          ctx.font = '600 11px Inter, system-ui, sans-serif';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(b.label, 4, y + rowH / 2);
          // track
          ctx.fillStyle = trackClr;
          const trackR = Math.min(4, rowH / 2);
          _roundRect(ctx, barX, y, barW, rowH, trackR);
          ctx.fill();
          // fill
          const w = b.value > 0 ? (b.value / maxV) * barW : 0;
          if (w > 0) {
            ctx.fillStyle = fixedColor || b.color || '#2563eb';
            _roundRect(ctx, barX, y, w, rowH, trackR);
            ctx.fill();
          }
          // value
          const pct = ((b.value / total) * 100).toFixed(0) + '%';
          const valStr = b.value >= 1000000
            ? (b.value / 1000000).toFixed(1) + ' М ₽'
            : b.value >= 1000
              ? Math.round(b.value / 1000) + ' к ₽'
              : Math.round(b.value) + ' ₽';
          ctx.fillStyle = mutedClr;
          ctx.font = '500 11px Inter, system-ui, sans-serif';
          ctx.textAlign = 'right';
          ctx.fillText(`${valStr} · ${pct}`, W - padR, y + rowH / 2);
        });
      }

      function _roundRect(ctx, x, y, w, h, r) {
        if (w < 2 * r) r = w / 2;
        if (h < 2 * r) r = h / 2;
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y, x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r);
        ctx.arcTo(x, y, x + w, y, r);
        ctx.closePath();
      }

      // ── ANOMALIES BANNER ─────────────────────────────────────────
      function renderAnomaliesBanner(anomalies) {
        const banner = document.getElementById('anomalies-banner');
        const list = document.getElementById('anomalies-list');
        if (!banner || !list) return;
        if (!anomalies || !anomalies.length) {
          banner.style.display = 'none';
          return;
        }
        const sevOrder = { high: 0, medium: 1, low: 2 };
        const sorted = [...anomalies].sort((a, b) => (sevOrder[a.severity] || 99) - (sevOrder[b.severity] || 99));
        list.innerHTML = sorted.slice(0, 5).map(a => {
          const dot = a.severity === 'high' ? '#dc2626' : a.severity === 'medium' ? '#f59e0b' : '#64748b';
          return `<div style="display:flex;align-items:center;gap:8px;">
            <span style="width:6px;height:6px;border-radius:50%;background:${dot};flex-shrink:0;"></span>
            <span>${esc(a.text)}</span>
          </div>`;
        }).join('');
        banner.style.display = '';
      }

      // Месяц в родительном падеже для фраз «до конца …»
      const _MONTHS_GENITIVE = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
      function monthGenitive(date) { return _MONTHS_GENITIVE[date.getMonth()]; }
      // Месяц в предложном падеже для фраз «в …»
      const _MONTHS_PREPOSITIONAL = ['январе','феврале','марте','апреле','мае','июне','июле','августе','сентябре','октябре','ноябре','декабре'];
      function monthPrepositional(date) { return _MONTHS_PREPOSITIONAL[date.getMonth()]; }

      // ── CASH-TO-REINVEST CARD ────────────────────────────────────
      function renderCashReinvestCard(extra) {
        const card = document.getElementById('cash-reinvest-card');
        const body = document.getElementById('cash-reinvest-body');
        if (!card || !body) return;
        const freeCash = Number(extra?.free_cash_rub || window.cashTotalRub || 0);
        const events = extra?.events || [];
        // Horizon — конец текущего месяца. Сегодняшние купоны исключаем (уже в freeCash).
        const today = new Date();
        const todayStr = today.toISOString().slice(0, 10);
        const endOfMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0); // last day of current month
        const endStr = endOfMonth.toISOString().slice(0, 10);
        const daysLeft = Math.max(0, Math.ceil((endOfMonth - today) / 86400000));
        const futureCoupons = events.filter(e => e.type === 'coupon' && e.date > todayStr && e.date <= endStr);
        const futureMaturity = events.filter(e => e.type === 'maturity' && e.date > todayStr && e.date <= endStr);
        const incomingCoupons = futureCoupons.reduce((s, e) => s + (e.amount || 0), 0);
        const incomingMaturity = futureMaturity.reduce((s, e) => s + (e.amount || 0), 0);
        const totalAvailable = freeCash + incomingCoupons + incomingMaturity;
        const hasAnything = freeCash > 0 || incomingCoupons > 0 || incomingMaturity > 0 || events.length > 0;
        card.style.display = hasAnything ? '' : 'none';
        const fmtRub = v => Math.round(v).toLocaleString('ru');
        const monthName = monthGenitive(endOfMonth);
        const daysLeftLbl = daysLeft === 0 ? 'сегодня последний день' : daysLeft === 1 ? 'остался 1 день' : (daysLeft < 5 ? `осталось ${daysLeft} дня` : `осталось ${daysLeft} дней`);
        body.innerHTML = `
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px;flex-wrap:wrap;">
            <div style="font-size:26px;font-weight:700;color:var(--text-primary);line-height:1;" title="Свободные деньги на счёте + купоны и погашения, которые поступят до конца ${monthName} (не считая сегодняшних — они скорее всего уже зачислены)">${fmtRub(totalAvailable)} ₽</div>
            <div style="font-size:12px;color:var(--text-muted);">доступно к реинвесту</div>
          </div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:10px;line-height:1.4;">Свободно сейчас + поступления до конца ${monthName} · ${daysLeftLbl}</div>
          <div style="display:flex;flex-direction:column;gap:6px;font-size:12px;">
            <div style="display:flex;justify-content:space-between;gap:8px;">
              <span style="color:var(--text-secondary);" title="Деньги, уже зачисленные брокером на счёт (включая ранее полученные купоны)">Свободно сейчас</span>
              <span style="font-weight:600;">${fmtRub(freeCash)} ₽</span>
            </div>
            <div style="display:flex;justify-content:space-between;gap:8px;">
              <span style="color:var(--text-secondary);" title="Купоны с датой выплаты строго после сегодняшней до конца ${monthName}">Купоны впереди</span>
              <span style="font-weight:600;color:${incomingCoupons > 0 ? 'var(--green-400)' : 'var(--text-muted)'};">${incomingCoupons > 0 ? '+' : ''}${fmtRub(incomingCoupons)} ₽</span>
            </div>
            <div style="display:flex;justify-content:space-between;gap:8px;">
              <span style="color:var(--text-secondary);" title="Возврат номинала по облигациям с погашением до конца ${monthName}">Погашения впереди</span>
              <span style="font-weight:600;color:${incomingMaturity > 0 ? 'var(--green-400)' : 'var(--text-muted)'};">${incomingMaturity > 0 ? '+' : ''}${fmtRub(incomingMaturity)} ₽</span>
            </div>
          </div>
          ${totalAvailable > 1000 ? '<div style="font-size:11px;color:var(--text-muted);margin-top:10px;line-height:1.4;">Совет: проверьте раздел «Рекомендации для диверсификации» ниже — есть бумаги, которые усилят портфель.</div>' : ''}
        `;
      }

      // ── EVENTS 30 DAYS CARD ──────────────────────────────────────
      function renderEventsCard(events) {
        const body = document.getElementById('events-body');
        const titleEl = document.getElementById('events-card-title');
        if (!body) return;
        const today = new Date();
        const endOfMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0);
        const endStr = endOfMonth.toISOString().slice(0, 10);
        const endOfNextMonth = new Date(today.getFullYear(), today.getMonth() + 2, 0);
        const endNextStr = endOfNextMonth.toISOString().slice(0, 10);
        const monthName = monthGenitive(endOfMonth);
        if (titleEl) titleEl.textContent = `События до конца ${monthName}`;
        const typeIcon = { coupon: '💰', maturity: '🟢', offer: '📋', buyback: '📋' };
        const typeColor = { coupon: 'var(--blue-500)', maturity: 'var(--green-400)', offer: '#f59e0b', buyback: '#f59e0b' };
        const typeLabel = { coupon: 'купон', maturity: 'погашение', offer: 'оферта', buyback: 'buyback' };
        const todayMid = new Date(); todayMid.setHours(0,0,0,0);
        const renderRow = e => {
          const d = new Date(e.date);
          const diff = Math.round((d - todayMid) / 86400000);
          const dStr = d.toLocaleDateString('ru', { day: 'numeric', month: 'short' });
          const dayLbl = diff === 0 ? 'сегодня' : diff === 1 ? 'завтра' : `через ${diff} дн.`;
          const amtStr = e.amount > 0 ? `+${Math.round(e.amount).toLocaleString('ru')} ₽` : '';
          return `<div style="display:flex;align-items:center;gap:10px;padding:8px 16px;border-bottom:1px solid var(--table-border);">
            <span style="font-size:14px;flex-shrink:0;">${typeIcon[e.type] || '•'}</span>
            <div style="flex:1;min-width:0;">
              <div style="font-size:12px;font-weight:500;color:var(--text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(e.name || e.ticker)}</div>
              <div style="font-size:10px;color:var(--text-muted);">${typeLabel[e.type] || e.type} · ${dStr} (${dayLbl})</div>
            </div>
            ${amtStr ? `<div style="font-size:12px;font-weight:600;color:${typeColor[e.type]};white-space:nowrap;">${amtStr}</div>` : ''}
          </div>`;
        };
        const filtered = events.filter(e => e.date <= endStr);
        if (filtered.length) {
          body.innerHTML = filtered.map(renderRow).join('');
          return;
        }
        // Нет событий до конца месяца — показываем заглушку и события следующего месяца
        const nextMonthName = monthPrepositional(endOfNextMonth);
        const nextEvents = events.filter(e => e.date > endStr && e.date <= endNextStr);
        let html = `<div style="padding:14px 18px;color:var(--text-muted);font-size:13px;text-align:center;">Нет событий до конца ${monthName}</div>`;
        if (nextEvents.length) {
          html += `<div style="padding:8px 16px 4px;font-size:11px;font-weight:600;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.03em;">Дальше — в ${nextMonthName}</div>`;
          html += nextEvents.map(renderRow).join('');
        }
        body.innerHTML = html;
      }

      // ── YTM CURVE (scatter) ──────────────────────────────────────
      function renderYTMCurve(rows) {
        const canvas = document.getElementById('ytm-curve-chart');
        const emptyEl = document.getElementById('ytm-curve-empty');
        const hintEl = document.getElementById('ytm-curve-hint');
        if (!canvas) return;
        // Show the YTM/Events row whenever we have any portfolio data
        const row = document.getElementById('ytm-events-row');
        if (row) row.style.display = rows && rows.length ? '' : 'none';
        const now = new Date();
        const bonds = rows.filter(r => r.type === 'bond' && r.current_value > 0 && r.market_yield > 0);
        const points = bonds
          .map(r => ({ r, yrs: _bondMaturityYears(r, now) }))
          .filter(x => x.yrs != null && x.yrs > 0 && x.yrs <= 30);
        if (!points.length) {
          canvas.style.display = 'none';
          if (emptyEl) emptyEl.style.display = 'block';
          if (hintEl) hintEl.textContent = '';
          return;
        }
        canvas.style.display = 'block';
        if (emptyEl) emptyEl.style.display = 'none';

        const dpr = window.devicePixelRatio || 1;
        // Fixed canvas height — grid stretch syncs card outer heights, while
        // canvas itself stays at a stable visual size to keep dots round.
        const H = 240;
        // Two-step width: set 100% first so global `canvas { max-width: 100% }`
        // and parent padding don't squish us after the fact; then read the
        // real CSS width, lock it as inline px and match canvas.width to
        // that × dpr so 1 logical px = 1 CSS px in both axes.
        canvas.style.width = '100%';
        canvas.style.height = H + 'px';
        const W = Math.max(200, Math.round(canvas.getBoundingClientRect().width) || _measureCanvasParent(canvas, 400));
        canvas.style.width = W + 'px';
        canvas.width = W * dpr;
        canvas.height = H * dpr;
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        const st = getComputedStyle(document.documentElement);
        const gridClr = st.getPropertyValue('--table-border').trim() || 'rgba(148,163,184,.15)';
        const labelClr = st.getPropertyValue('--text-muted').trim() || '#64748b';
        const axisTitleClr = st.getPropertyValue('--text-secondary').trim() || '#94a3b8';

        // Color by rating bucket (greens for AAA, ambers for BB and weaker, gray for unrated)
        const ratingBucketColor = r => {
          const x = String(r || '').toUpperCase().replace(/^RU/, '');
          if (x.startsWith('AAA')) return '#16a34a';
          if (x.startsWith('AA'))  return '#22c55e';
          if (x.startsWith('A'))   return '#84cc16';
          if (x.startsWith('BBB')) return '#eab308';
          if (x.startsWith('BB'))  return '#f59e0b';
          if (x.startsWith('B'))   return '#f97316';
          if (x.startsWith('C') || x.startsWith('D')) return '#dc2626';
          return '#64748b';
        };

        const PAD_L = 52, PAD_R = 12, PAD_T = 28, PAD_B = 40;
        const chartW = W - PAD_L - PAD_R;
        const chartH = H - PAD_T - PAD_B;
        const maxYrs = Math.max(...points.map(p => p.yrs), 1);
        const maxYtm = Math.max(...points.map(p => p.r.market_yield), 1);
        const minYtm = Math.min(...points.map(p => p.r.market_yield), maxYtm);
        const keyRate = Number(window._keyRate || 0);
        let ytmLoRaw = Math.min(minYtm - 1, keyRate > 0 ? keyRate - 1 : minYtm - 1);
        let ytmHiRaw = Math.max(maxYtm + 1, keyRate > 0 ? keyRate + 1 : maxYtm + 1);
        const ytmLo = Math.max(0, Math.floor(ytmLoRaw));
        const ytmHi = Math.ceil(ytmHiRaw);
        const xrange = Math.ceil(maxYrs);
        const xticks = xrange <= 5 ? Math.max(2, xrange) : 5;
        const yticks = 4;

        const xToPx = yrs => PAD_L + chartW * (yrs / xrange);
        const yToPx = ytm => PAD_T + chartH * (1 - (ytm - ytmLo) / (ytmHi - ytmLo));

        const totalVal = points.reduce((s, p) => s + p.r.current_value, 0);
        const enriched = points.map(p => {
          const share = p.r.current_value / totalVal;
          return {
            ...p,
            share,
            radius: 5 + Math.sqrt(share * 400),
            color: ratingBucketColor(p.r.company_rating),
            x: xToPx(p.yrs),
            y: yToPx(p.r.market_yield),
          };
        });

        function drawScene(hoverIdx) {
          ctx.clearRect(0, 0, W, H);

          // Axis titles
          ctx.fillStyle = axisTitleClr;
          ctx.font = '600 10px Inter, system-ui, sans-serif';
          ctx.textAlign = 'left';
          ctx.fillText('YTM, %', 6, PAD_T - 14);
          ctx.textAlign = 'right';
          ctx.fillText('Срок до погашения/оферты, лет →', W - PAD_R, H - 6);

          // Grid lines + Y labels
          ctx.strokeStyle = gridClr;
          ctx.lineWidth = 1;
          ctx.font = '500 10px Inter, system-ui, sans-serif';
          ctx.fillStyle = labelClr;
          for (let i = 0; i <= yticks; i++) {
            const y = PAD_T + chartH * (1 - i / yticks);
            const v = ytmLo + (ytmHi - ytmLo) * i / yticks;
            ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
            ctx.textAlign = 'right';
            ctx.fillText(v.toFixed(1) + '%', PAD_L - 6, y + 3);
          }
          for (let i = 0; i <= xticks; i++) {
            const x = PAD_L + chartW * i / xticks;
            const v = xrange * i / xticks;
            // Vertical light grid line
            ctx.strokeStyle = gridClr;
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x, PAD_T); ctx.lineTo(x, PAD_T + chartH); ctx.stroke();
            ctx.textAlign = 'center';
            ctx.fillStyle = labelClr;
            ctx.fillText(v.toFixed(v < 1 ? 1 : 0) + ' г', x, H - PAD_B + 14);
          }

          // Key rate horizontal dashed line + label
          if (keyRate > 0 && keyRate >= ytmLo && keyRate <= ytmHi) {
            const yK = yToPx(keyRate);
            ctx.save();
            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([5, 4]);
            ctx.beginPath(); ctx.moveTo(PAD_L, yK); ctx.lineTo(W - PAD_R, yK); ctx.stroke();
            ctx.restore();
            // Badge background
            const txt = `Ключевая ${keyRate.toFixed(1)}%`;
            ctx.font = 'bold 10px Inter, system-ui, sans-serif';
            const tw = ctx.measureText(txt).width;
            ctx.fillStyle = '#f59e0b';
            _roundRect(ctx, W - PAD_R - tw - 12, yK - 16, tw + 10, 14, 3);
            ctx.fill();
            ctx.fillStyle = '#fff';
            ctx.textAlign = 'right';
            ctx.fillText(txt, W - PAD_R - 5, yK - 5);
          }

          // Dots
          enriched.forEach((p, i) => {
            const isHover = i === hoverIdx;
            const r = isHover ? p.radius + 2 : p.radius;
            ctx.fillStyle = p.color + (isHover ? 'ee' : 'cc');
            ctx.beginPath();
            ctx.arc(p.x, p.y, r, 0, 2 * Math.PI);
            ctx.fill();
            ctx.strokeStyle = isHover ? '#fff' : 'rgba(255,255,255,0.7)';
            ctx.lineWidth = isHover ? 2 : 1;
            ctx.stroke();
          });

          // Hover crosshair + nice label near dot
          if (hoverIdx != null && hoverIdx >= 0) {
            const p = enriched[hoverIdx];
            ctx.save();
            ctx.strokeStyle = 'rgba(148,163,184,0.35)';
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(PAD_L, p.y); ctx.lineTo(W - PAD_R, p.y);
            ctx.moveTo(p.x, PAD_T); ctx.lineTo(p.x, PAD_T + chartH);
            ctx.stroke();
            ctx.restore();
          }
        }

        drawScene(null);

        // Tooltip
        let tip = document.getElementById('_ytm_curve_tooltip');
        if (!tip) {
          tip = document.createElement('div');
          tip.id = '_ytm_curve_tooltip';
          tip.style.cssText = [
            'position:fixed;z-index:9999;pointer-events:none;',
            'background:#1e293b;border:1px solid rgba(148,163,184,.2);',
            'border-radius:8px;padding:8px 11px;',
            'box-shadow:0 8px 32px rgba(0,0,0,.55);',
            'font-family:Inter,sans-serif;font-size:12px;color:#f1f5f9;',
            'min-width:180px;max-width:240px;display:none;transition:opacity .12s;',
          ].join('');
          document.body.appendChild(tip);
        }

        if (canvas._curveAbort) canvas._curveAbort.abort();
        canvas._curveAbort = new AbortController();
        const sig = { signal: canvas._curveAbort.signal };

        const findHit = (mx, my) => {
          // Find nearest dot in pixel space within (radius + 4)
          let best = -1, bestDist = Infinity;
          enriched.forEach((p, i) => {
            const d = Math.hypot(mx - p.x, my - p.y);
            if (d <= p.radius + 4 && d < bestDist) { bestDist = d; best = i; }
          });
          return best;
        };

        canvas.addEventListener('mousemove', e => {
          const rect = canvas.getBoundingClientRect();
          const mx = e.clientX - rect.left;
          const my = e.clientY - rect.top;
          const idx = findHit(mx, my);
          drawScene(idx >= 0 ? idx : null);
          if (idx < 0) { tip.style.display = 'none'; return; }
          const p = enriched[idx];
          const r = p.r;
          const matDate = r.maturity_date ? new Date(r.maturity_date).toLocaleDateString('ru', { day:'numeric', month:'short', year:'numeric' }) : null;
          const offerDate = r.offer_date ? new Date(r.offer_date).toLocaleDateString('ru', { day:'numeric', month:'short', year:'numeric' }) : null;
          const buybackDate = r.buyback_date ? new Date(r.buyback_date).toLocaleDateString('ru', { day:'numeric', month:'short', year:'numeric' }) : null;
          const dateLine = offerDate ? `Оферта: ${offerDate}` : (buybackDate ? `Buyback: ${buybackDate}` : (matDate ? `Погашение: ${matDate}` : ''));
          const ratingLabel = r.company_rating && r.company_rating !== '—' ? r.company_rating : 'без рейтинга';
          const couponLine = r.coupon_rate ? `Купон: ${r.coupon_rate.toFixed(1)}%/год` : '';
          const sharePct = (p.share * 100).toFixed(1);
          const cv = Math.round(r.current_value).toLocaleString('ru');
          const isLight = document.documentElement.getAttribute('data-theme') === 'light';
          const primaryClr = isLight ? '#0f172a' : '#f1f5f9';
          const mutedClr   = isLight ? '#64748b' : '#94a3b8';
          const labelClrT  = isLight ? '#475569' : '#cbd5e1';
          tip.innerHTML = `
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
              <span style="width:8px;height:8px;border-radius:50%;background:${p.color};flex-shrink:0;"></span>
              <span style="font-weight:700;font-size:12px;color:${primaryClr};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(r.name || r.ticker)}">${esc((r.name || r.ticker).substring(0, 28))}</span>
            </div>
            <div style="display:grid;grid-template-columns:auto auto;gap:4px 10px;font-size:11px;color:${labelClrT};">
              <span>YTM:</span><span style="text-align:right;font-weight:700;color:${primaryClr};">${r.market_yield.toFixed(2)}%</span>
              <span>Срок:</span><span style="text-align:right;color:${primaryClr};">${p.yrs.toFixed(2)} г</span>
              <span>Доля:</span><span style="text-align:right;color:${primaryClr};">${sharePct}% (${cv} ₽)</span>
              <span>Рейтинг:</span><span style="text-align:right;color:${p.color};font-weight:600;">${ratingLabel}</span>
              ${couponLine ? `<span>Купон:</span><span style="text-align:right;color:${primaryClr};">${r.coupon_rate.toFixed(2)}%</span>` : ''}
            </div>
            ${dateLine ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(148,163,184,.18);font-size:10px;color:${mutedClr};">${dateLine}</div>` : ''}
          `;
          tip.style.display = 'block';
          tip.style.background = isLight ? '#ffffff' : '#1e293b';
          tip.style.color = primaryClr;
          tip.style.border = isLight ? '1px solid #e2e8f0' : '1px solid rgba(148,163,184,.2)';
          const TW = tip.offsetWidth || 200, TH = tip.offsetHeight || 100;
          let tx = e.clientX + 14;
          let ty = e.clientY - TH / 2;
          if (tx + TW > window.innerWidth - 8) tx = e.clientX - TW - 14;
          if (ty < 8) ty = 8;
          if (ty + TH > window.innerHeight - 8) ty = window.innerHeight - TH - 8;
          tip.style.left = tx + 'px';
          tip.style.top = ty + 'px';
        }, sig);
        canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; drawScene(null); }, sig);

        if (hintEl) {
          const rated = enriched.filter(p => p.r.company_rating && p.r.company_rating !== '—').length;
          const hintParts = [
            'X — годы до ближайшего события, Y — YTM, размер = доля в портфеле, цвет = кредитный рейтинг.',
          ];
          if (keyRate > 0) hintParts.push('Точки <b>выше</b> жёлтой пунктирной линии ключевой ставки — обгоняют депозит.');
          if (rated < enriched.length) hintParts.push(`<span style="color:var(--text-muted);">${enriched.length - rated} бумаг без рейтинга показаны серым.</span>`);
          hintEl.innerHTML = hintParts.join(' ');
        }
      }

      // ── STRESS TEST: emitter default ─────────────────────────────
      function renderStressTest(rows) {
        const body = document.getElementById('stress-test-body');
        if (!body) return;
        const bonds = rows.filter(r => r.type === 'bond' && r.current_value > 0);
        if (!bonds.length) {
          body.innerHTML = '<div style="color:var(--text-muted);font-size:13px;">Нет облигаций для расчёта</div>';
          return;
        }
        // Group by issuer
        const byIssuer = {};
        bonds.forEach(r => {
          const key = (r.name || r.ticker || '?')
            .replace(/\s*(АО|ПАО|ООО|ОФЗ)\s*/gi, '').split(/[\s\-]/)[0].substring(0, 14);
          if (!byIssuer[key]) byIssuer[key] = { name: key, value: 0, tickers: [] };
          byIssuer[key].value += r.current_value;
          byIssuer[key].tickers.push(r.ticker);
        });
        const totalVal = bonds.reduce((s, r) => s + r.current_value, 0);
        const top3 = Object.values(byIssuer).sort((a, b) => b.value - a.value).slice(0, 3);
        // Recovery scenarios
        const scenarios = [
          { label: 'Пессимистично', rec: 0.30, color: '#dc2626' },
          { label: 'Базовый', rec: 0.50, color: '#f59e0b' },
          { label: 'Оптимистично', rec: 0.70, color: '#16a34a' },
        ];
        const fmtRub = v => Math.round(v).toLocaleString('ru');
        const rowsHtml = top3.map(em => {
          const sharePct = (em.value / totalVal * 100).toFixed(1);
          const losses = scenarios.map(sc => {
            const loss = em.value * (1 - sc.rec);
            const lossPct = (loss / totalVal * 100).toFixed(1);
            return `<div style="flex:1;text-align:center;">
              <div style="font-size:11px;color:${sc.color};font-weight:600;">${sc.label}</div>
              <div style="font-size:11px;color:var(--text-secondary);">−${fmtRub(loss)} ₽</div>
              <div style="font-size:10px;color:var(--text-muted);">(−${lossPct}%)</div>
            </div>`;
          }).join('');
          return `<div style="padding:10px 0;border-bottom:1px solid var(--table-border);">
            <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px;">
              <span style="font-size:13px;font-weight:600;color:var(--text-primary);">${esc(em.name)}</span>
              <span style="font-size:11px;color:var(--text-muted);">${sharePct}% портфеля · ${fmtRub(em.value)} ₽</span>
            </div>
            <div style="display:flex;gap:8px;">${losses}</div>
          </div>`;
        }).join('');
        body.innerHTML = `
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px;line-height:1.4;">
            Что потеряете если эмитент обанкротится. Сценарии восстановления: 30% / 50% / 70%.
          </div>
          ${rowsHtml}
        `;
      }

      // ── "What to sell first" ─────────────────────────────────────
      function renderSellFirst(rows) {
        const el = document.getElementById('analytics-sell-first');
        if (!el) return;
        const now = new Date();
        const keyRate = Number(window._keyRate || 0);
        // Cache rating history hits from anomalies (only those in window._analyticsExtra)
        const downgradeSet = new Set();
        (window._analyticsExtra?.anomalies || []).forEach(a => {
          if (a.type === 'rating_downgrade') downgradeSet.add(a.ticker);
        });
        const scored = rows.map(r => {
          const breakdown = []; // { label, points, reasonShort }
          const cost = (r.purchase_price || 0) * (r.quantity || 0);
          // 1. Loss
          if (cost > 0) {
            const pnlPct = (r.profit || 0) / cost * 100;
            if (pnlPct < -5) {
              const pts = Math.min(5, -pnlPct / 5);
              breakdown.push({ label: `Убыток ${pnlPct.toFixed(1)}%`, points: pts, reasonShort: 'убыток' });
            }
          }
          // 2. Imminent offer / buyback (<60 days)
          for (const fld of ['offer_date', 'buyback_date']) {
            const d = r[fld] ? new Date(r[fld]) : null;
            if (d && d > now) {
              const days = Math.round((d - now) / 86400000);
              if (days < 60) {
                breakdown.push({
                  label: `${fld === 'offer_date' ? 'Оферта' : 'Buyback'} через ${days} дн.`,
                  points: 2,
                  reasonShort: fld === 'offer_date' ? 'скоро оферта' : 'скоро buyback',
                });
              }
            }
          }
          // 3. Downgrade in last 14 days
          if (downgradeSet.has(r.ticker)) {
            breakdown.push({ label: 'Понижен рейтинг за 14 дн.', points: 3, reasonShort: 'downgrade' });
          }
          // 4. Low YTM vs key rate
          if (r.type === 'bond' && keyRate > 0 && r.market_yield > 0 && r.market_yield < keyRate - 1) {
            breakdown.push({
              label: `YTM ${r.market_yield.toFixed(1)}% ниже ставки ЦБ (${keyRate.toFixed(1)}%)`,
              points: 1,
              reasonShort: 'низкий YTM',
            });
          }
          const score = breakdown.reduce((s, b) => s + b.points, 0);
          return { r, score, breakdown };
        }).filter(x => x.score > 0).sort((a, b) => b.score - a.score);
        if (!scored.length) {
          el.innerHTML = '<div style="padding:14px 16px;color:var(--text-muted);font-size:13px;">Все позиции в порядке — продавать ничего не нужно.</div>';
          return;
        }
        // Severity threshold (visual intensity of the score badge)
        const sevColor = score =>
          score >= 5 ? 'var(--red-400)' :
          score >= 3 ? '#f97316' :          /* orange */
          score >= 1.5 ? '#f59e0b' :        /* amber */
          'var(--text-muted)';
        el.innerHTML = '';
        scored.slice(0, 5).forEach(({ r, score, breakdown }) => {
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--table-border);';
          const reasons = breakdown.map(b => b.reasonShort);
          // Build native title tooltip with per-factor breakdown
          const tooltip = [
            `${r.name || r.ticker} — оценка ${score.toFixed(1)}`,
            '',
            ...breakdown.map(b => `+${b.points.toFixed(1)}  ${b.label}`),
            '',
            'Чем выше оценка, тем больше факторов против бумаги.',
          ].join('\n');
          const clr = sevColor(score);
          row.innerHTML = `<div style="min-width:0;flex:1;">
              <div style="font-size:13px;font-weight:500;color:var(--text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(r.name || r.ticker)}</div>
              <div style="font-size:10px;color:var(--text-muted);margin-top:2px;">${reasons.join(' · ')}</div>
            </div>
            <div title="${esc(tooltip)}" style="font-size:13px;font-weight:700;color:${clr};margin-left:8px;flex-shrink:0;cursor:help;padding:2px 8px;border-radius:10px;background:rgba(148,163,184,.08);">${score.toFixed(1)}</div>`;
          el.appendChild(row);
        });
        // Footer explanation
        const footer = document.createElement('div');
        footer.style.cssText = 'padding:8px 16px 10px;font-size:10px;color:var(--text-muted);line-height:1.4;border-top:1px solid var(--table-border);';
        footer.innerHTML = 'Сводная оценка (чем выше — тем выше приоритет продажи): убыток &gt;5% (до +5), оферта &lt;60 дн (+2), понижение рейтинга (+3), YTM ниже ставки ЦБ (+1). Наведите на цифру для разбивки.';
        el.appendChild(footer);
      }

      async function loadAnalyticsHistory(days = 90) {
        // Update active button
        document.querySelectorAll('.analytics-period-btn').forEach(btn => {
          btn.classList.toggle('active', parseInt(btn.dataset.days) === days);
        });
        const canvas = document.getElementById('analytics-history-chart');
        try {
          let snapshots;
          if (shareToken) {
            const headers = {};
            if (sharePassword) headers['X-Share-Password'] = sharePassword;
            const r = await fetch(`/share/${shareToken}/snapshots?days=${days}`, { headers });
            if (!r.ok) throw new Error('fetch failed');
            snapshots = await r.json();
          } else if (isAllMode) {
            const r = await apiFetch(`/portfolios/all/snapshots?days=${days}`);
            snapshots = await r.json();
          } else if (portfolioId) {
            const r = await apiFetch(`/portfolios/${portfolioId}/snapshots?days=${days}`);
            snapshots = await r.json();
          } else {
            return;
          }
          const meta = document.getElementById('analytics-history-meta');
          const retBox = document.getElementById('analytics-history-return');
          const retAbs = document.getElementById('analytics-history-return-abs');
          const retPct = document.getElementById('analytics-history-return-pct');
          if (!snapshots || snapshots.length < 2) {
            if (meta) meta.textContent = snapshots && snapshots.length === 1
              ? `1 точка данных · ${snapshots[0].date}`
              : 'Нет данных · обновляется каждые 15 мин';
            if (retBox) retBox.style.display = 'none';
            if (canvas) { canvas._snapshots = null; const es = snapshots && snapshots.length === 1 ? snapshots[0] : null; canvas._emptySnap = es; drawHistoryChartEmpty(canvas, es); }
            return;
          }
          if (meta) {
            const first = snapshots[0].date, last = snapshots[snapshots.length-1].date;
            const lastVal = snapshots[snapshots.length-1].total_value;
            const fmt = lastVal >= 1000 ? (lastVal/1000).toFixed(1) + ' тыс. ₽' : lastVal.toFixed(0) + ' ₽';
            meta.textContent = `${snapshots.length} точек · ${first} — ${last} · последняя: ${fmt}`;
          }
          if (retBox && retAbs && retPct) {
            const firstVal = Number(snapshots[0].total_value || 0);
            const lastVal  = Number(snapshots[snapshots.length-1].total_value || 0);
            const diff = lastVal - firstVal;
            const pct  = firstVal > 0 ? (diff / firstVal) * 100 : 0;
            const sign = diff >= 0 ? '+' : '−';
            const absDiff = Math.abs(diff);
            const color = diff > 0 ? 'var(--green-400)' : diff < 0 ? 'var(--red-400)' : 'var(--text-primary)';
            retAbs.style.color = color;
            retAbs.textContent = `${sign}${Math.round(absDiff).toLocaleString('ru')} ₽`;
            retPct.textContent = `изменение стоимости ${diff >= 0 ? '+' : ''}${pct.toFixed(2)}% за ${days} дн.`;
            retBox.style.display = '';
          }
          if (canvas) { canvas._snapshots = snapshots; canvas._lastDays = days; }
          // Load RGBI in parallel (only when not in share mode and toggle is on)
          const rgbiToggle = document.getElementById('rgbi-toggle');
          if (canvas) canvas._rgbi = null;
          if (!shareToken && rgbiToggle && rgbiToggle.checked) {
            try {
              const r = await apiFetch(`/portfolios/benchmarks/rgbi?days=${days}`);
              if (r.ok) {
                const rgbi = await r.json();
                if (canvas) canvas._rgbi = rgbi;
              }
            } catch (_) {}
          }
          drawHistoryChart(canvas, snapshots);
        } catch(e) {
          if (canvas) drawHistoryChartEmpty(canvas, null);
        }
      }

      function drawHistoryChartEmpty(canvas, oneSnapshot) {
        if (!canvas) return;
        const W = _measureCanvasParent(canvas, 600);
        const H = 200;
        canvas.width = W;
        canvas.height = H;
        canvas.style.width = '100%';
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, W, H);
        const pad = { t: 16, r: 16, b: 48, l: 56 };
        const chartW = W - pad.l - pad.r;
        const chartH = H - pad.t - pad.b;
        const st = getComputedStyle(document.documentElement);
        const gridClr  = st.getPropertyValue('--table-border').trim() || 'rgba(148,163,184,.1)';
        const labelClr = st.getPropertyValue('--text-muted').trim() || '#64748b';
        const lineClr  = st.getPropertyValue('--border').trim() || 'rgba(148,163,184,.25)';

        // Grid lines
        ctx.strokeStyle = gridClr; ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
          const y = pad.t + (chartH / 4) * i;
          ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
        }

        // Zero line at bottom of chart area
        const zeroY = pad.t + chartH;
        ctx.strokeStyle = lineClr; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]);
        ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W - pad.r, zeroY); ctx.stroke();
        ctx.setLineDash([]);

        // Y label "0"
        ctx.fillStyle = labelClr; ctx.font = '10px Inter,sans-serif';
        ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
        ctx.fillText('0', pad.l - 4, zeroY);

        // Caption
        ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
        if (oneSnapshot) {
          // 1 точка есть — показываем стоимость и когда следующая точка появится
          const val = oneSnapshot.total_value;
          const fmt = val >= 1000 ? (val/1000).toFixed(1) + ' тыс. ₽' : val.toFixed(0) + ' ₽';
          ctx.font = '600 12px Inter,sans-serif';
          ctx.fillStyle = labelClr;
          ctx.fillText('Первая точка: ' + fmt + ' на ' + oneSnapshot.date, W / 2, H - 26);
          ctx.font = '11px Inter,sans-serif';
          ctx.fillStyle = lineClr;
          ctx.fillText('График появится после следующего обновления кэша (~15 мин)', W / 2, H - 10);
        } else {
          ctx.font = '600 12px Inter,sans-serif';
          ctx.fillStyle = labelClr;
          ctx.fillText('История ещё не накоплена', W / 2, H - 26);
          ctx.font = '11px Inter,sans-serif';
          ctx.fillStyle = lineClr;
          ctx.fillText('Данные появятся после первого обновления кэша (~15 мин)', W / 2, H - 10);
        }
      }

      function drawHistoryChart(canvas, snapshots, skipAnimation) {
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const W = _measureCanvasParent(canvas, 600);
        canvas.width = W;
        canvas.style.width = '100%';
        const H = canvas.height || 220;
        const values = snapshots.map(s => s.total_value);
        const costs  = snapshots.map(s => s.total_cost);
        // Securities-only line (value minus cash). Present only on snapshots taken
        // after the column was added; null elsewhere → drawn as a gap.
        const securities = snapshots.map(s => (s.securities_value != null ? s.securities_value : null));
        const hasSecurities = securities.some(v => v != null);
        // Pre-compute normalized RGBI series (if present) so we can include
        // it in min/max range — otherwise its line could fall outside the
        // visible chart area and the toggle would appear to "do nothing".
        let _rgbiNorm = null;
        const _rgbi = canvas._rgbi;
        if (_rgbi && _rgbi.length >= 2) {
          const rgbiByDate = {};
          _rgbi.forEach(p => { rgbiByDate[p.date] = p.value; });
          const rgbiSorted = [..._rgbi].sort((a, b) => a.date.localeCompare(b.date));
          const aligned = snapshots.map(s => {
            if (rgbiByDate[s.date] != null) return rgbiByDate[s.date];
            let candidate = null;
            for (const p of rgbiSorted) {
              if (p.date <= s.date) candidate = p.value; else break;
            }
            return candidate;
          });
          const firstIdx = aligned.findIndex(v => v != null);
          if (firstIdx >= 0 && values[firstIdx] > 0) {
            const baseRgbi = aligned[firstIdx];
            const baseVal = values[firstIdx];
            _rgbiNorm = aligned.map(v => v != null ? (v / baseRgbi) * baseVal : null);
          }
        }
        const rgbiOnly = _rgbiNorm ? _rgbiNorm.filter(v => v != null) : [];
        const securitiesOnly = securities.filter(v => v != null);
        const allVals = [...values, ...costs, ...rgbiOnly, ...securitiesOnly];
        const maxV = Math.max(...allVals);
        const minV = Math.min(...allVals) * 0.98;
        const pad = { t: 16, r: 16, b: 32, l: 56 };
        const chartW = W - pad.l - pad.r;
        const chartH = H - pad.t - pad.b;
        const xScale = (i) => pad.l + (i / Math.max(snapshots.length - 1, 1)) * chartW;
        const yScale = (v) => pad.t + chartH - ((v - minV) / (maxV - minV || 1)) * chartH;

        const st = getComputedStyle(document.documentElement);
        const gridClr  = st.getPropertyValue('--table-border').trim() || 'rgba(148,163,184,.1)';
        const labelClr = st.getPropertyValue('--text-muted').trim() || '#64748b';
        const lineClr  = st.getPropertyValue('--blue-500').trim() || '#3b82f6';
        const dashClr  = st.getPropertyValue('--border').trim() || 'rgba(148,163,184,.15)';
        const bgClr    = st.getPropertyValue('--modal-bg').trim() || '#0d1829';
        const textClr  = st.getPropertyValue('--text-primary').trim() || '#f1f5f9';

        // Interpolate point along polyline at fractional index
        function interpPoint(arr, fIdx) {
          if (fIdx <= 0) return arr[0];
          if (fIdx >= arr.length - 1) return arr[arr.length - 1];
          const lo = Math.floor(fIdx), hi = lo + 1, t = fIdx - lo;
          return arr[lo] + (arr[hi] - arr[lo]) * t;
        }

        function drawBase(hoverIdx, progress) {
          if (progress === undefined) progress = 1;
          ctx.clearRect(0, 0, W, H);
          // How many points to draw (fractional)
          const maxIdx = (values.length - 1) * progress;

          // Grid lines + Y labels
          ctx.strokeStyle = gridClr; ctx.lineWidth = 1;
          for (let i = 0; i <= 4; i++) {
            const y = pad.t + (chartH / 4) * i;
            ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
            const val = maxV - ((maxV - minV) / 4) * i;
            ctx.fillStyle = labelClr; ctx.font = '10px Inter,sans-serif'; ctx.textAlign = 'right';
            ctx.fillText(Math.round(val / 1000) + 'k', pad.l - 4, y + 3);
          }

          // Cost baseline (dashed) — animated
          ctx.strokeStyle = dashClr; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
          ctx.beginPath();
          for (let i = 0; i <= Math.floor(maxIdx); i++) {
            i === 0 ? ctx.moveTo(xScale(i), yScale(costs[i])) : ctx.lineTo(xScale(i), yScale(costs[i]));
          }
          if (maxIdx > Math.floor(maxIdx)) {
            ctx.lineTo(xScale(maxIdx), yScale(interpPoint(costs, maxIdx)));
          }
          ctx.stroke(); ctx.setLineDash([]);

          // Value fill — animated
          ctx.beginPath();
          for (let i = 0; i <= Math.floor(maxIdx); i++) {
            i === 0 ? ctx.moveTo(xScale(i), yScale(values[i])) : ctx.lineTo(xScale(i), yScale(values[i]));
          }
          if (maxIdx > Math.floor(maxIdx)) {
            ctx.lineTo(xScale(maxIdx), yScale(interpPoint(values, maxIdx)));
          }
          ctx.lineTo(xScale(maxIdx), H - pad.b);
          ctx.lineTo(xScale(0), H - pad.b);
          ctx.closePath();
          const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
          grad.addColorStop(0, 'rgba(59,130,246,0.18)'); grad.addColorStop(1, 'rgba(59,130,246,0)');
          ctx.fillStyle = grad; ctx.fill();

          // Value line — animated
          ctx.strokeStyle = lineClr; ctx.lineWidth = 2.5;
          ctx.beginPath();
          for (let i = 0; i <= Math.floor(maxIdx); i++) {
            i === 0 ? ctx.moveTo(xScale(i), yScale(values[i])) : ctx.lineTo(xScale(i), yScale(values[i]));
          }
          if (maxIdx > Math.floor(maxIdx)) {
            ctx.lineTo(xScale(maxIdx), yScale(interpPoint(values, maxIdx)));
          }
          ctx.stroke();

          // Securities-only line (value − cash): solid green, drawn with gaps
          // where the data is missing (older snapshots).
          if (hasSecurities) {
            ctx.save();
            ctx.strokeStyle = st.getPropertyValue('--green-600').trim() || '#16a34a';
            ctx.lineWidth = 2;
            ctx.beginPath();
            let started = false;
            for (let i = 0; i <= Math.floor(maxIdx); i++) {
              const v = securities[i];
              if (v == null) { started = false; continue; }
              const x = xScale(i), y = yScale(v);
              if (!started) { ctx.moveTo(x, y); started = true; }
              else ctx.lineTo(x, y);
            }
            ctx.stroke();
            ctx.restore();
          }

          // RGBI overlay (already normalized above) — second dashed line
          if (_rgbiNorm) {
            ctx.save();
            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 2;
            ctx.setLineDash([6, 4]);
            ctx.beginPath();
            let started = false;
            for (let i = 0; i <= Math.floor(maxIdx); i++) {
              const v = _rgbiNorm[i];
              if (v == null) continue;
              const x = xScale(i), y = yScale(v);
              if (!started) { ctx.moveTo(x, y); started = true; }
              else ctx.lineTo(x, y);
            }
            ctx.stroke();
            ctx.restore();
          }

          // Unified legend — stacked rows, no overlap. Only lines actually drawn.
          {
            const greenClr = st.getPropertyValue('--green-600').trim() || '#16a34a';
            const legend = [];
            legend.push([hasSecurities ? 'Стоимость портфеля (с кэшем)' : 'Стоимость портфеля', lineClr]);
            if (hasSecurities) legend.push(['Только бумаги', greenClr]);
            if (_rgbiNorm) legend.push(['Индекс ОФЗ (норм.)', '#f59e0b']);
            ctx.font = 'bold 9px Inter, sans-serif';
            ctx.textAlign = 'left';
            legend.forEach((item, i) => {
              ctx.fillStyle = item[1];
              ctx.fillText('— ' + item[0], pad.l + 4, pad.t + 10 + i * 12);
            });
          }

          // X labels — show all (static)
          const step = Math.max(1, Math.floor(snapshots.length / 6));
          ctx.fillStyle = labelClr; ctx.font = '10px Inter,sans-serif'; ctx.textAlign = 'center';
          snapshots.forEach((s, i) => {
            if (i % step === 0 || i === snapshots.length - 1) {
              ctx.fillText(s.date.substring(5), xScale(i), H - pad.b + 14);
            }
          });

          // Hover overlay
          if (hoverIdx !== null && hoverIdx >= 0 && hoverIdx < snapshots.length) {
            const x  = xScale(hoverIdx);
            const yV = yScale(values[hoverIdx]);
            const yC = yScale(costs[hoverIdx]);
            const s  = snapshots[hoverIdx];

            // Vertical hairline
            ctx.strokeStyle = 'rgba(148,163,184,.35)'; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, H - pad.b); ctx.stroke();

            // Dot on value line
            ctx.fillStyle = lineClr;
            ctx.beginPath(); ctx.arc(x, yV, 4, 0, Math.PI * 2); ctx.fill();
            ctx.strokeStyle = bgClr; ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.arc(x, yV, 4, 0, Math.PI * 2); ctx.stroke();

            // Tooltip
            const profit = s.total_value - s.total_cost;
            const pct    = s.total_cost > 0 ? (profit / s.total_cost * 100) : 0;
            const fmtV   = new Intl.NumberFormat('ru-RU').format(Math.round(s.total_value));
            const fmtP   = (profit >= 0 ? '+' : '') + new Intl.NumberFormat('ru-RU').format(Math.round(profit));
            const fmtPct = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
            const line1  = s.date;
            const line2  = fmtV + ' ₽';
            const line3  = fmtP + ' ₽  (' + fmtPct + ')';

            ctx.font = 'bold 11px Inter,sans-serif';
            const tw2 = ctx.measureText(line2).width;
            ctx.font = '10px Inter,sans-serif';
            const tw1 = ctx.measureText(line1).width;
            const tw3 = ctx.measureText(line3).width;
            const tw  = Math.max(tw1, tw2, tw3);
            const th  = 52, tpad = 8, gap = 6;
            let tx = x + gap;
            if (tx + tw + tpad * 2 > W - pad.r) tx = x - tw - tpad * 2 - gap;

            const ty = Math.max(pad.t, Math.min(yV - th / 2, H - pad.b - th));
            ctx.fillStyle = bgClr;
            ctx.beginPath();
            if (ctx.roundRect) {
              ctx.roundRect(tx, ty, tw + tpad * 2, th, 5);
            } else {
              ctx.rect(tx, ty, tw + tpad * 2, th);
            }
            ctx.fill();
            ctx.strokeStyle = 'rgba(148,163,184,.2)'; ctx.lineWidth = 1;
            ctx.stroke();

            ctx.fillStyle = labelClr; ctx.font = '10px Inter,sans-serif'; ctx.textAlign = 'left';
            ctx.fillText(line1, tx + tpad, ty + 14);
            ctx.fillStyle = textClr; ctx.font = 'bold 12px Inter,sans-serif';
            ctx.fillText(line2, tx + tpad, ty + 30);
            ctx.fillStyle = profit >= 0 ? '#4ade80' : '#f87171'; ctx.font = '10px Inter,sans-serif';
            ctx.fillText(line3, tx + tpad, ty + 46);
          }
        }

        // Cancel previous animation if running
        if (canvas._histAnimId) { cancelAnimationFrame(canvas._histAnimId); canvas._histAnimId = null; }

        if (skipAnimation) {
          drawBase(null, 1);
        } else {
          // Animate line drawing over 600ms with ease-out
          var animStart = null;
          var ANIM_DURATION = 600;
          function animStep(ts) {
            if (!animStart) animStart = ts;
            var elapsed = ts - animStart;
            var t = Math.min(elapsed / ANIM_DURATION, 1);
            // ease-out cubic
            var progress = 1 - Math.pow(1 - t, 3);
            drawBase(null, progress);
            if (t < 1) { canvas._histAnimId = requestAnimationFrame(animStep); }
            else { canvas._histAnimId = null; }
          }
          canvas._histAnimId = requestAnimationFrame(animStep);
        }

        // Hover interaction — replace listener each redraw
        const oldHandler = canvas._historyMouseMove;
        const oldLeave   = canvas._historyMouseLeave;
        if (oldHandler) canvas.removeEventListener('mousemove', oldHandler);
        if (oldLeave)   canvas.removeEventListener('mouseleave', oldLeave);

        canvas._historyMouseMove = (e) => {
          if (canvas._histAnimId) return; // Don't hover during animation
          const rect = canvas.getBoundingClientRect();
          const mx   = (e.clientX - rect.left) * (W / rect.width);
          if (mx < pad.l || mx > W - pad.r) { drawBase(null, 1); return; }
          const idx = Math.round((mx - pad.l) / chartW * (snapshots.length - 1));
          drawBase(Math.max(0, Math.min(idx, snapshots.length - 1)), 1);
        };
        canvas._historyMouseLeave = () => { if (!canvas._histAnimId) drawBase(null, 1); };

        canvas.addEventListener('mousemove', canvas._historyMouseMove);
        canvas.addEventListener('mouseleave', canvas._historyMouseLeave);
      }

      async function manualRefresh() {
        const btn = document.getElementById('btn-refresh');
        const icon = document.getElementById('refresh-icon');
        if (btn) btn.disabled = true;
        if (icon) icon.style.animation = 'spin .6s linear infinite';
        try { await syncTableFromServer(); } finally {
          if (btn) btn.disabled = false;
          if (icon) icon.style.animation = '';
        }
      }

      // ── MONTHLY CHART ─────────────────────────────────────────────
      function parseLocalDate(dateStr) {
        // Parse YYYY-MM-DD as local date, not UTC
        const [year, month, day] = dateStr.split('-').map(Number);
        return new Date(year, month - 1, day);
      }

      function generatePaymentDates(nextCouponIso, periodDays, rangeStart, rangeEnd) {
        const dates = [];
        if (!nextCouponIso || periodDays <= 0) return dates;
        const anchor = parseLocalDate(nextCouponIso);
        if (isNaN(anchor.getTime())) return dates;
        let d = new Date(anchor);
        while (d > rangeStart) d = new Date(d.getTime() - periodDays * 86400000);
        if (d < rangeStart) d = new Date(d.getTime() + periodDays * 86400000);
        const seenMonths = new Set();
        while (d < rangeEnd) {
          const key = `${d.getFullYear()}-${d.getMonth()}`;
          if (!seenMonths.has(key)) {
            seenMonths.add(key);
            dates.push(new Date(d));
          }
          d = new Date(d.getTime() + periodDays * 86400000);
        }
        return dates;
      }

      // couponPeriodMode: 'year-ahead' (default) | 2026 | 2027 | 2028 ...
      var _couponPeriodMode = 'year-ahead';

      function _couponRange() {
        const now = new Date();
        if (_couponPeriodMode === 'year-ahead') {
          return {
            start: new Date(now.getFullYear(), now.getMonth(), 1),
            months: 12
          };
        }
        const yr = Number(_couponPeriodMode);
        return {
          start: new Date(yr, 0, 1),
          months: 12
        };
      }

      function calculateMonthlyIncome(rows) {
        const { start, months } = _couponRange();
        const rangeEnd = new Date(start.getFullYear(), start.getMonth() + months + 1, 1);
        const slots = [];
        for (let i = 0; i < months; i++) {
          const d = new Date(start.getFullYear(), start.getMonth() + i, 1);
          slots.push({ year: d.getFullYear(), month: d.getMonth(), income: 0, principal: 0, hasOffer: false, bonds: [] });
        }
        for (const row of rows) {
          if (row.type !== 'bond') continue;
          const coupon  = Number(row.coupon || 0);
          const qty     = Number(row.quantity || 0);
          const period  = Number(row.coupon_period || 0);

          // Дата погашения — после неё купоны не платятся
          let matDate = null;
          if (row.maturity_date) {
            matDate = parseLocalDate(row.maturity_date);
            if (isNaN(matDate.getTime())) matDate = null;
          }

          if (row.next_coupon_date && period > 0 && coupon > 0 && qty > 0) {
            const payDates = generatePaymentDates(row.next_coupon_date, period, start, rangeEnd);
            for (const pd of payDates) {
              if (matDate && pd >= matDate) continue;
              for (const slot of slots) {
                if (pd.getFullYear() === slot.year && pd.getMonth() === slot.month) {
                  const amount = coupon * qty;
                  slot.income += amount;
                  slot.bonds.push({ name: row.name || row.ticker, ticker: row.ticker, coupon, qty, amount });
                  break;
                }
              }
            }
          }
          // Оферта — отмечаем месяц
          if (row.offer_date) {
            const od = parseLocalDate(row.offer_date);
            if (!isNaN(od.getTime())) {
              for (const slot of slots) {
                if (od.getFullYear() === slot.year && od.getMonth() === slot.month) {
                  slot.hasOffer = true; break;
                }
              }
            }
          }
          // Погашение — отмечаем месяц + сумма возврата номинала (RUB)
          if (matDate) {
            for (const slot of slots) {
              if (matDate.getFullYear() === slot.year && matDate.getMonth() === slot.month) {
                slot.hasMaturity = true;
                if (!slot.maturingBonds) slot.maturingBonds = [];
                const cv = Number(row.current_value || 0);
                const aciVal = Number(row.aci || 0) * qty;
                const principalRub = Math.max(0, cv - aciVal);
                slot.principal += principalRub;
                slot.maturingBonds.push({
                  name: row.name || row.ticker,
                  ticker: row.ticker,
                  principal: principalRub,
                });
                break;
              }
            }
          }
        }
        return slots;
      }

      function drawMonthlyChart(rows) {
        const canvas  = document.getElementById('monthly-chart');
        const emptyEl = document.getElementById('chart-empty');
        if (!canvas) return;
        const data    = calculateMonthlyIncome(rows);
        // Toggle: hide principal (maturity payouts) when checkbox is off
        const principalCb = document.getElementById('cashflow-show-principal');
        const showPrincipal = !principalCb || principalCb.checked;
        if (!showPrincipal) {
          data.forEach(d => { d.principal = 0; d.hasMaturity = false; d.maturingBonds = []; });
        }
        const hasData = data.some(d => d.income > 0 || d.principal > 0);
        canvas.style.display = hasData ? 'block' : 'none';
        emptyEl.style.display = hasData ? 'none' : 'block';
        if (!hasData) return;

        const W    = _measureCanvasParent(canvas, 800);
        const dpr  = window.devicePixelRatio || 1;
        const H    = 260;
        canvas.style.width  = W + 'px';
        canvas.style.height = H + 'px';
        canvas.width  = W * dpr;
        canvas.height = H * dpr;

        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        // PAD_T increased to fit "оферта" label above bars; PAD_B for month + income labels
        const approxSlotW = (W - 64 - 12) / data.length;
        const rotateLabels = approxSlotW < 42;
        const PAD_L = 64, PAD_R = 12, PAD_T = 44, PAD_B = rotateLabels ? 60 : 28;
        const chartW = W - PAD_L - PAD_R;
        const chartH = H - PAD_T - PAD_B;
        const slotW  = chartW / data.length;
        const barW   = slotW * 0.58;
        const maxInc = Math.max(...data.map(d => (d.income || 0) + (d.principal || 0)), 1);

        const stM = getComputedStyle(document.documentElement);
        const mGridClr  = stM.getPropertyValue('--table-border').trim() || 'rgba(148,163,184,.1)';
        const mLabelClr = stM.getPropertyValue('--text-muted').trim() || '#64748b';
        const mBgClr    = stM.getPropertyValue('--bg-card').trim() || 'rgba(255,255,255,.03)';
        const mBarClr   = stM.getPropertyValue('--blue-400').trim() || '#60a5fa';
        const mPrincClr = stM.getPropertyValue('--green-400').trim() || '#4ade80';
        const mYAxisClr = stM.getPropertyValue('--border').trim() || 'rgba(148,163,184,.15)';

        // Background
        ctx.fillStyle = mBgClr;
        ctx.fillRect(0, 0, W, H);

        // Grid lines + Y labels
        const gridCount = 4;
        ctx.strokeStyle = mGridClr;
        ctx.lineWidth   = 1;
        ctx.font        = '500 9px Inter, system-ui, sans-serif';
        ctx.fillStyle   = mLabelClr;
        ctx.textAlign   = 'right';
        for (let i = 0; i <= gridCount; i++) {
          const y   = PAD_T + chartH * (1 - i / gridCount);
          const val = maxInc * i / gridCount;
          ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
          ctx.fillText(fmt(roundTo2(val)), PAD_L - 6, y + 3);
        }

        // Abbreviated income formatter for bar labels
        const fmtBarVal = v => {
          if (v >= 1000000) return (v / 1000000).toFixed(1).replace('.0', '') + ' М';
          if (v >= 1000)    return Math.round(v / 1000) + ' к';
          return Math.round(v) + ' ₽';
        };

        // Bars (stacked: coupons bottom, principal top)
        const bottomBase = H - PAD_B; // baseline for text rows at bottom
        data.forEach((d, i) => {
          const x    = PAD_L + i * slotW + (slotW - barW) / 2;
          const couponH = d.income > 0 ? (d.income / maxInc) * chartH : 0;
          const princH  = d.principal > 0 ? (d.principal / maxInc) * chartH : 0;
          const barH   = couponH + princH;
          const y      = PAD_T + chartH - barH;
          const cx     = x + barW / 2;

          if (barH > 0) {
            // Coupon part (bottom) — blue (or orange if offer)
            if (couponH > 0) {
              ctx.fillStyle = d.hasOffer ? '#f59e0b' : mBarClr;
              const couponY = PAD_T + chartH - couponH;
              if (princH > 0) {
                // square top corners when principal stacks on top
                ctx.fillRect(x, couponY, barW, couponH);
              } else {
                const radius = Math.min(4, couponH / 2);
                ctx.beginPath();
                ctx.moveTo(x + radius, couponY);
                ctx.lineTo(x + barW - radius, couponY);
                ctx.quadraticCurveTo(x + barW, couponY, x + barW, couponY + radius);
                ctx.lineTo(x + barW, couponY + couponH);
                ctx.lineTo(x, couponY + couponH);
                ctx.lineTo(x, couponY + radius);
                ctx.quadraticCurveTo(x, couponY, x + radius, couponY);
                ctx.closePath();
                ctx.fill();
              }
            }
            // Principal part (top) — green, rounded top
            if (princH > 0) {
              ctx.fillStyle = mPrincClr;
              const radius = Math.min(4, princH / 2);
              ctx.beginPath();
              ctx.moveTo(x + radius, y);
              ctx.lineTo(x + barW - radius, y);
              ctx.quadraticCurveTo(x + barW, y, x + barW, y + radius);
              ctx.lineTo(x + barW, y + princH);
              ctx.lineTo(x, y + princH);
              ctx.lineTo(x, y + radius);
              ctx.quadraticCurveTo(x, y, x + radius, y);
              ctx.closePath();
              ctx.fill();
            }

            // Total label above bar
            const totalAmt = d.income + d.principal;
            const totLabel = fmtBarVal(totalAmt);
            if (barH >= 18) {
              // Inside bar — white text
              ctx.fillStyle = 'rgba(255,255,255,0.92)';
              ctx.font      = 'bold 9px Inter, system-ui, sans-serif';
              if (rotateLabels && barH >= 36) {
                // Rotated 90° inside bar for narrow slots
                ctx.save();
                ctx.translate(cx, y + barH / 2);
                ctx.rotate(-Math.PI / 2);
                ctx.textAlign = 'center';
                ctx.fillText(totLabel, 0, 3);
                ctx.restore();
              } else {
                ctx.textAlign = 'center';
                ctx.fillText(totLabel, cx, y + Math.min(barH / 2 + 4, barH - 4));
              }
            } else {
              // Above bar — colored text
              ctx.fillStyle = d.principal > 0 ? '#16a34a' : (d.hasOffer ? '#b45309' : '#2563eb');
              ctx.font      = 'bold 8.5px Inter, system-ui, sans-serif';
              ctx.textAlign = 'center';
              ctx.fillText(totLabel, cx, y - 4);
            }
          }

          // "Оферта/Offer" label above bar
          if (d.hasOffer) {
            const labelY = barH > 0 ? y - (barH >= 18 ? 18 : 16) : PAD_T - 5;
            ctx.fillStyle  = '#d97706';
            ctx.font       = 'bold 8px Inter, system-ui, sans-serif';
            ctx.textAlign  = 'center';
            ctx.fillText(t('chart.offer'), cx, labelY);
          }

          // "Погашение/Maturity" marker — same green as principal segment
          if (d.hasMaturity) {
            const mLabelY = barH > 0 && !d.hasOffer ? y - (barH >= 18 ? 18 : 16) : PAD_T - (d.hasOffer ? 14 : 5);
            ctx.fillStyle  = '#16a34a';
            ctx.font       = 'bold 8px Inter, system-ui, sans-serif';
            ctx.textAlign  = 'center';
            ctx.fillText(t('chart.maturity'), cx, mLabelY);
          }

          // Month name at bottom
          ctx.fillStyle = mLabelClr;
          ctx.font      = '9px Inter, system-ui, sans-serif';
          const now = new Date();
          const lbl = getMonthNames()[d.month] + (d.year !== now.getFullYear() ? ' \'' + String(d.year).slice(2) : '');
          if (rotateLabels) {
            ctx.save();
            ctx.translate(cx, bottomBase + 8);
            ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'right';
            ctx.fillText(lbl, 0, 0);
            ctx.restore();
          } else {
            ctx.textAlign = 'center';
            ctx.fillText(lbl, cx, bottomBase + 13);
          }
        });

        // Y-axis label
        ctx.save();
        ctx.translate(10, H / 2);
        ctx.rotate(-Math.PI / 2);
        ctx.fillStyle  = mYAxisClr;
        ctx.font       = '9px Inter, system-ui, sans-serif';
        ctx.textAlign  = 'center';
        ctx.fillText(t('chart.yLabel'), 0, 0);
        ctx.restore();

        // ── Tooltip on mousemove ──────────────────────────────────
        let _chartTooltip = document.getElementById('_monthly_chart_tooltip');
        if (!_chartTooltip) {
          _chartTooltip = document.createElement('div');
          _chartTooltip.id = '_monthly_chart_tooltip';
          _chartTooltip.style.cssText = [
            'position:fixed;z-index:9999;pointer-events:none;',
            'background:#1e293b;border:1px solid rgba(148,163,184,.2);',
            'border-radius:8px;padding:10px 13px;',
            'box-shadow:0 8px 32px rgba(0,0,0,.6),0 2px 8px rgba(0,0,0,.4);',
            'font-family:Inter,sans-serif;font-size:12px;color:#f1f5f9;',
            'min-width:180px;max-width:260px;display:none;backdrop-filter:none;',
          ].join('');
          document.body.appendChild(_chartTooltip);
        }

        // Attach tooltip listeners (remove old ones first)
        if (canvas._tooltipAbort) canvas._tooltipAbort.abort();
        canvas._tooltipAbort = new AbortController();
        const _sig = { signal: canvas._tooltipAbort.signal };
        canvas.addEventListener('mousemove', (e) => {
          const rect = canvas.getBoundingClientRect();
          const mx = e.clientX - rect.left;
          const my = e.clientY - rect.top;
          // Find which bar slot
          let found = null;
          data.forEach((d, i) => {
            const x = PAD_L + i * slotW + (slotW - barW) / 2;
            const totH = ((d.income || 0) + (d.principal || 0)) > 0 ? (((d.income || 0) + (d.principal || 0)) / maxInc) * chartH : 0;
            const y2 = PAD_T + chartH - totH;
            if (mx >= x - 4 && mx <= x + barW + 4 && my >= PAD_T && my <= H - PAD_B) {
              found = { d, x, barY: y2, cx: x + barW / 2 };
            }
          });
          if (!found || (!found.d.income && !found.d.principal && !found.d.hasMaturity && !found.d.hasOffer)) {
            _chartTooltip.style.display = 'none';
            return;
          }
          const { d } = found;
          const monthName = getMonthNames()[d.month] + (d.year !== new Date().getFullYear() ? ' ' + d.year : '');
          const totalAll = (d.income || 0) + (d.principal || 0);
          const sorted = [...d.bonds].sort((a, b) => b.amount - a.amount);
          let rows_html = sorted.map(b => {
            const shortName = b.name.length > 22 ? b.name.substring(0, 21) + '…' : b.name;
            return `<div style="display:flex;justify-content:space-between;gap:12px;padding:3px 0;border-bottom:1px solid var(--border,rgba(148,163,184,.08));">
              <span style="color:var(--text-secondary,#94a3b8);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(b.name)}">${esc(shortName)}</span>
              <span style="font-weight:600;white-space:nowrap;color:var(--green-400,#4ade80);">+${Math.round(b.amount).toLocaleString('ru')} ₽</span>
            </div>`;
          }).join('');
          let matHtml = '';
          if (d.hasMaturity && d.maturingBonds && d.maturingBonds.length) {
            const matLabel = window._lang === 'en' ? 'Maturing' : 'Погашение';
            matHtml = `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(34,197,94,.25);">
              <div style="font-weight:600;font-size:11px;color:#16a34a;margin-bottom:3px;">${matLabel}: +${Math.round(d.principal || 0).toLocaleString('ru')} ₽</div>
              ${d.maturingBonds.map(b => `<div style="display:flex;justify-content:space-between;gap:10px;font-size:11px;color:var(--text-secondary,#94a3b8);padding:1px 0;">
                <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(b.name || b.ticker)}</span>
                ${b.principal > 0 ? `<span style="font-weight:600;color:#16a34a;white-space:nowrap;">+${Math.round(b.principal).toLocaleString('ru')} ₽</span>` : ''}
              </div>`).join('')}
            </div>`;
          }
          _chartTooltip.innerHTML = `
            <div style="font-weight:700;font-size:13px;margin-bottom:7px;color:var(--text-primary,#f1f5f9);">${monthName}</div>
            ${rows_html}
            ${matHtml}
            ${totalAll > 0 ? `<div style="display:flex;justify-content:space-between;gap:12px;padding-top:6px;margin-top:6px;border-top:1px solid var(--border,rgba(148,163,184,.15));font-weight:700;">
              <span>${window._lang === 'en' ? 'Total' : 'Итого'}</span>
              <span style="color:var(--green-400,#4ade80);">+${Math.round(totalAll).toLocaleString('ru')} ₽</span>
            </div>` : ''}`;
          // Position tooltip near cursor, keep within viewport
          const TW = 264, TH = _chartTooltip.offsetHeight || 120;
          let tx = e.clientX + 14;
          let ty = e.clientY - TH / 2;
          if (tx + TW > window.innerWidth - 8) tx = e.clientX - TW - 14;
          if (ty < 8) ty = 8;
          if (ty + TH > window.innerHeight - 8) ty = window.innerHeight - TH - 8;
          _chartTooltip.style.left = tx + 'px';
          _chartTooltip.style.top  = ty + 'px';
          // Solid background depending on theme
          const isLight = document.documentElement.getAttribute('data-theme') === 'light';
          _chartTooltip.style.background = isLight ? '#ffffff' : '#1e293b';
          _chartTooltip.style.color      = isLight ? '#0f172a' : '#f1f5f9';
          _chartTooltip.style.border     = isLight ? '1px solid #e2e8f0' : '1px solid rgba(148,163,184,.2)';
          _chartTooltip.style.display = 'block';
        }, _sig);

        canvas.addEventListener('mouseleave', () => {
          _chartTooltip.style.display = 'none';
        }, _sig);
      }

      function initCouponPeriodBtns() {
        const container = document.getElementById('coupon-period-btns');
        if (!container) return;
        const now = new Date();
        const curYear = now.getFullYear();
        const buttons = [
          { label: t('chart.yearAhead', 'На год вперёд'), value: 'year-ahead' },
          { label: String(curYear), value: String(curYear) },
          { label: String(curYear + 1), value: String(curYear + 1) },
          { label: String(curYear + 2), value: String(curYear + 2) },
        ];
        container.innerHTML = '';
        buttons.forEach(b => {
          const btn = document.createElement('button');
          btn.className = 'analytics-period-btn' + (b.value === _couponPeriodMode ? ' active' : '');
          btn.textContent = b.label;
          btn.onclick = function() {
            _couponPeriodMode = b.value;
            container.querySelectorAll('.analytics-period-btn').forEach(el => el.classList.remove('active'));
            btn.classList.add('active');
            if (typeof tableRows !== 'undefined') drawMonthlyChart(tableRows);
          };
          container.appendChild(btn);
        });
      }

      // ── EDITABLE CELLS ───────────────────────────────────────────
      const tableStatusEl = document.getElementById('table-status');

      async function saveEditableRow(id, qty, price) {
        const r = await apiFetch(`/portfolios/${portfolioId}/instruments/${id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ quantity: qty, purchase_price: price }),
        });
        const p = await r.json();
        if (!r.ok) throw new Error(p.detail || 'Не удалось обновить строку.');
      }
      async function saveCouponRow(id, coupon) {
        const r = await apiFetch(`/portfolios/${portfolioId}/instruments/${id}/coupon`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ coupon }),
        });
        const p = await r.json();
        if (!r.ok) throw new Error(p.detail || 'Не удалось обновить купон.');
        return p;
      }
      async function saveCouponRateRow(id, coupon_rate) {
        const r = await apiFetch(`/portfolios/${portfolioId}/instruments/${id}/coupon-rate`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ coupon_rate }),
        });
        const p = await r.json();
        if (!r.ok) throw new Error(p.detail || 'Не удалось обновить ставку купона.');
        return p;
      }

      // A bond with no purchase date and no known coupon history: its realized
      // coupons cannot be computed, so they are missing from the full profit.
      // realized_coupons == null means "unknown" — 0 is a legitimate value (the
      // first coupon may simply not be due yet), and T-Bank positions covered by
      // the operations journal already carry a number, so both are excluded:
      // their profit does not depend on this date.
      function bondNeedsPurchaseDate(row) {
        return row.type === 'bond' && !row.purchase_date && row.realized_coupons == null;
      }

      const _DATE_HINT_TITLE = 'Не указана дата покупки — полученные купоны не учитываются в полной прибыли. Нажмите, чтобы указать.';

      function createDateHintButton(row) {
        const hint = document.createElement('button');
        hint.type = 'button';
        hint.className = 'date-hint-btn';
        // Inline SVG rather than an emoji: renders identically everywhere,
        // including systems without an emoji font.
        hint.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4">'
          + '<rect x="1.7" y="3" width="12.6" height="11.3" rx="1.6"/><path d="M1.7 6.6h12.6M5.2 1.7v2.6M10.8 1.7v2.6"/></svg>';
        hint.title = _DATE_HINT_TITLE;
        hint.setAttribute('aria-label', 'Указать дату покупки');
        hint.addEventListener('click', (e) => { e.stopPropagation(); openEditInstrumentModal(row); });
        return hint;
      }

      function createEditableCell(row, field, extraClass) {
        const td = document.createElement('td');
        // Show the purchase date as a tooltip on the buy-price cell when known.
        if (field === 'purchase_price' && row.purchase_date) {
          const d = String(row.purchase_date).substring(0, 10);
          const [y, m, day] = d.split('-');
          td.title = `Куплено ${day}.${m}.${y}`;
        }
        const needsDate = field === 'purchase_price' && bondNeedsPurchaseDate(row);
        if (isReadOnly) {
          td.textContent = fmt(row[field]);
          return td;
        }
        const input = document.createElement('input');
        input.type = 'number'; input.className = 'editable-input' + (extraClass ? ' ' + extraClass : '');
        input.min = '0.01'; input.step = '0.01'; input.value = String(row[field]);
        const commit = async () => {
          const oQty   = Number(row.quantity), oPrice = Number(row.purchase_price);
          const pQty   = Number(field === 'quantity'       ? input.value : row.quantity);
          const pPrice = Number(field === 'purchase_price' ? input.value : row.purchase_price);
          if (!Number.isFinite(pQty) || !Number.isFinite(pPrice) || pQty <= 0 || pPrice <= 0) {
            setStatus(tableStatusEl, 'Введите корректные положительные значения.', true);
            input.value = String(row[field]); return;
          }
          try {
            recalculateRow(row, pQty, pPrice); recalculateWeights(tableRows);
            persistTableCache(); renderTable();
            await saveEditableRow(row.id, pQty, pPrice);
            setStatus(tableStatusEl, 'Строка обновлена.');
          } catch (err) {
            recalculateRow(row, oQty, oPrice); recalculateWeights(tableRows);
            persistTableCache(); renderTable();
            setStatus(tableStatusEl, err.message || 'Ошибка обновления.', true);
          }
        };
        input.addEventListener('blur', commit);
        input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
        input.addEventListener('wheel', e => e.preventDefault(), { passive: false });
        td.appendChild(input);
        if (needsDate) td.appendChild(createDateHintButton(row));
        return td;
      }

      function createCouponRateCell(row) {
        const td = document.createElement('td');
        if (row.type !== 'bond') { td.textContent = '—'; return td; }
        const cv = Number(row.coupon_rate);
        const floaterHasValue = row.is_floater && Number.isFinite(cv) && cv > 0;
        // Флоатер с автоданными — всегда read-only (не редактировать даже если manual_coupon_rate_set=true)
        const isEditable = !isReadOnly && !floaterHasValue && (
          row.manual_coupon_rate_set === true ||
          !(Number.isFinite(cv) && cv > 0)
        );
        if (!isEditable) {
          const txt = fmt(roundTo2(cv)) + '%';
          if (row.is_floater && !row.manual_coupon_rate_set) {
            const tip = floaterHasValue ? 'Плавающий купон — по последнему известному' : 'Плавающий купон — ещё не объявлен';
            const label = floaterHasValue ? `${txt}<sup style="color:var(--blue-500);font-size:9px;margin-left:1px;">~</sup>` : `—<sup style="color:var(--blue-500);font-size:9px;margin-left:1px;">~</sup>`;
            td.innerHTML = `<span title="${tip}" style="cursor:default;">${label}</span>`;
          } else {
            td.textContent = txt;
          }
          return td;
        }
        const input = document.createElement('input');
        input.type = 'number'; input.className = 'editable-input coupon-input';
        input.min = '0'; input.step = '0.01'; input.value = Number.isFinite(cv) && cv > 0 ? String(cv) : '';
        input.placeholder = '%';
        const needsHighlight = !(Number.isFinite(cv) && cv > 0) && !row.manual_coupon_rate_set;
        if (needsHighlight) td.style.cssText = 'background:rgba(251,191,36,.15);outline:1px solid rgba(251,191,36,.5);';
        if (row.is_floater && !row.manual_coupon_rate_set) input.title = 'Плавающий купон — введите вручную пока не объявлен';
        const commit = async () => {
          const orig   = Number.isFinite(Number(row.coupon_rate)) ? Number(row.coupon_rate) : 0;
          const parsed = Number(input.value);
          if (input.value === '') return;
          if (!Number.isFinite(parsed) || parsed < 0) {
            setStatus(tableStatusEl, 'Введите корректную ставку купона.', true);
            input.value = orig > 0 ? String(orig) : ''; return;
          }
          try {
            row.coupon_rate = parsed; row.manual_coupon_rate_set = true;
            persistTableCache(); renderTable();
            const upd = await saveCouponRateRow(row.id, parsed);
            row.coupon_rate = Number(upd.coupon_rate); row.manual_coupon_rate_set = upd.manual_coupon_rate_set === true;
            persistTableCache(); renderTable();
            setStatus(tableStatusEl, 'Ставка купона установлена.');
          } catch (err) {
            row.coupon_rate = orig; persistTableCache(); renderTable();
            setStatus(tableStatusEl, err.message || 'Ошибка обновления ставки купона.', true);
          }
        };
        input.addEventListener('blur', commit);
        input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
        input.addEventListener('wheel', e => e.preventDefault(), { passive: false });
        td.appendChild(input); return td;
      }

      function createCouponCell(row) {
        const td = document.createElement('td');
        if (row.type !== 'bond') { td.textContent = '—'; return td; }
        const cv = Number(row.coupon);
        const floaterCouponHasValue = row.is_floater && Number.isFinite(cv) && cv > 0;
        // Флоатер с автоданными — всегда read-only
        const isEditable = !isReadOnly && !floaterCouponHasValue && (
          row.manual_coupon_set === true ||
          !(Number.isFinite(cv) && cv > 0)
        );
        if (!isEditable) {
          if (row.is_floater && !row.manual_coupon_set) {
            const tip = floaterCouponHasValue ? 'Плавающий купон — по последнему известному' : 'Плавающий купон — ещё не объявлен';
            const label = floaterCouponHasValue ? `${fmt(row.coupon)}<sup style="color:var(--blue-500);font-size:9px;margin-left:1px;">~</sup>` : `—<sup style="color:var(--blue-500);font-size:9px;margin-left:1px;">~</sup>`;
            td.innerHTML = `<span title="${tip}" style="cursor:default;">${label}</span>`;
          } else {
            td.textContent = fmt(row.coupon);
          }
          return td;
        }
        const input = document.createElement('input');
        input.type = 'number'; input.className = 'editable-input coupon-input';
        input.min = '0'; input.step = '0.01'; input.value = Number.isFinite(cv) && cv > 0 ? String(cv) : '';
        input.placeholder = '₽';
        const needsHighlight = !(Number.isFinite(cv) && cv > 0) && !row.manual_coupon_set;
        if (needsHighlight) td.style.cssText = 'background:rgba(251,191,36,.15);outline:1px solid rgba(251,191,36,.5);';
        if (row.is_floater && !row.manual_coupon_set) input.title = 'Плавающий купон — введите вручную пока не объявлен';
        const commit = async () => {
          const orig   = Number.isFinite(Number(row.coupon)) ? Number(row.coupon) : 0;
          const parsed = Number(input.value);
          if (input.value === '') return;
          if (!Number.isFinite(parsed) || parsed < 0) {
            setStatus(tableStatusEl, 'Введите корректный купон.', true);
            input.value = String(orig); return;
          }
          try {
            row.coupon = parsed; row.manual_coupon_set = true;
            persistTableCache(); renderTable();
            const upd = await saveCouponRow(row.id, parsed);
            row.coupon = Number(upd.coupon); row.manual_coupon_set = upd.manual_coupon_set === true;
            persistTableCache(); renderTable();
            setStatus(tableStatusEl, 'Купон установлен.');
          } catch (err) {
            row.coupon = orig; persistTableCache(); renderTable();
            setStatus(tableStatusEl, err.message || 'Ошибка обновления купона.', true);
          }
        };
        input.addEventListener('blur', commit);
        input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
        input.addEventListener('wheel', e => e.preventDefault(), { passive: false });
        td.appendChild(input); return td;
      }

      // ── PORTFOLIO WIZARD ─────────────────────────────────────────

      const RISK_LABELS = {
        ultra_low:    { label: 'Минимальный',   desc: 'ОФЗ — государственные облигации', color: '#22c55e', yieldMin: 5, yieldMax: 10 },
        conservative: { label: 'Консервативный',desc: 'Купон < 12% — ОФЗ и 1-й эшелон', color: '#22c55e', yieldMin: 5, yieldMax: 12 },
        low:          { label: 'Низкий',        desc: 'Рейтинг AAA–AA, крупнейшие эмитенты', color: '#84cc16', yieldMin: 5, yieldMax: 13 },
        moderate:     { label: 'Умеренный',     desc: 'Рейтинг A–BBB, надёжный корпоратив', color: '#eab308', yieldMin: 8, yieldMax: 18 },
        elevated:     { label: 'Повышенный',    desc: 'Рейтинг BBB–BB+, средний бизнес', color: '#f97316', yieldMin: 12, yieldMax: 22 },
        high:         { label: 'Высокий',       desc: 'Рейтинг BB+, высокодоходные', color: '#f87171', yieldMin: 23, yieldMax: 27 },
        aggressive:   { label: 'Агрессивный',   desc: 'Купон > 22% — ВДО и высокодоходные', color: '#ef4444', yieldMin: 22, yieldMax: 30 },
      };
      const RISK_LABELS_EN = {
        ultra_low:    { label: 'Minimal',       desc: 'OFZ — government bonds' },
        conservative: { label: 'Conservative',  desc: 'Coupon < 12% — OFZ & tier-1' },
        low:          { label: 'Low',           desc: 'AAA–AA rating, largest issuers' },
        moderate:     { label: 'Moderate',      desc: 'A–BBB rating, reliable corporates' },
        elevated:     { label: 'Elevated',      desc: 'BBB–BB+ rating, mid-size business' },
        high:         { label: 'High',          desc: 'BB+ rating, high-yield bonds' },
        aggressive:   { label: 'Aggressive',    desc: 'Coupon > 22% — high-yield / VDO' },
      };
      function getRiskInfo(key) {
        const base = RISK_LABELS[key];
        if (!base) return { label: window._lang === 'en' ? 'Undefined' : 'Не определён', desc: '', color: '#94a3b8', yieldMin: 5, yieldMax: 30 };
        if (window._lang === 'en') {
          const en = RISK_LABELS_EN[key] || {};
          return { ...base, label: en.label || base.label, desc: en.desc || base.desc };
        }
        return base;
      }
      const RISK_KEYS = Object.keys(RISK_LABELS);

      function yieldToRiskKey(yv) {
        let best = 'moderate', bestDist = Infinity;
        for (const key of RISK_KEYS) {
          const info = RISK_LABELS[key];
          const dist = Math.abs((info.yieldMin + info.yieldMax) / 2 - yv);
          if (dist < bestDist) { bestDist = dist; best = key; }
        }
        return best;
      }

      let _suggestAbortCtrl = null;
      async function loadAddSuggestions() {
        const section = document.getElementById('add-suggestions-section');
        const grid = document.getElementById('add-suggestions-grid');
        const riskBadge = document.getElementById('add-suggestions-risk');
        if (!section || !grid) return;

        // Don't show if portfolio is empty
        if (!tableRows || tableRows.length === 0) {
          section.style.display = 'none';
          return;
        }

        // Calculate avg yield from portfolio
        const yields = tableRows.map(r => Number(r.market_yield || r.coupon_percent || 0)).filter(v => v > 0);
        if (yields.length === 0) { section.style.display = 'none'; return; }
        const avgYield = yields.reduce((a, b) => a + b, 0) / yields.length;
        const riskKey = yieldToRiskKey(avgYield);
        const riskInfo = getRiskInfo(riskKey);

        // Cancel previous request
        if (_suggestAbortCtrl) _suggestAbortCtrl.abort();
        _suggestAbortCtrl = new AbortController();

        section.style.display = 'block';
        riskBadge.textContent = riskInfo.label;
        riskBadge.style.cssText = `font-size:12px;padding:2px 8px;border-radius:999px;font-weight:600;background:${riskInfo.color}22;color:${riskInfo.color};border:1px solid ${riskInfo.color}44;`;
        grid.innerHTML = `<div style="color:var(--text-muted);font-size:13px;padding:8px 0;">${t('add.suggestLoading')}</div>`;

        try {
          const res = await apiFetch(`/bonds/suggest?amount=1000000&yield=${avgYield.toFixed(1)}&risk=${riskKey}`);
          if (!res.ok) throw new Error();
          const data = await res.json();
          const bonds = (data.bonds || []).slice(0, 12);
          if (bonds.length === 0) { section.style.display = 'none'; return; }

          // Filter out bonds already in portfolio
          const existingTickers = new Set(tableRows.map(r => r.ticker));
          const filtered = bonds.filter(b => !existingTickers.has(b.ticker));
          if (filtered.length === 0) { section.style.display = 'none'; return; }

          grid.innerHTML = '';
          filtered.slice(0, 12).forEach(b => {
            const maturity = b.maturity ? b.maturity.substring(0, 7) : '—';
            const offerNote = b.offer_date ? ` · ${window._lang === 'en' ? 'offer' : 'оферта'} ${b.offer_date.substring(0,7)}` : '';
            const ratingHtml = b.rating ? `<span style="font-size:10px;font-weight:700;padding:1px 5px;border-radius:4px;background:var(--bg-elevated);color:var(--text-muted);margin-right:6px;">${esc(b.rating)}</span>` : '';
            const card = document.createElement('div');
            card.className = 'suggest-card';
            card.innerHTML = `
              <div class="suggest-card-body">
                <div class="suggest-card-name">${ratingHtml}${esc(b.name)}</div>
                <div class="suggest-card-meta">${esc(b.ticker)} · ${window._lang === 'en' ? 'maturity' : 'погашение'} ${esc(maturity)}${esc(offerNote)}</div>
              </div>
              <div class="suggest-card-yield">${esc(String(b.coupon_percent))}%</div>
              <button class="suggest-card-add" title="${t('add.btn')}">+</button>
            `;
            const addBtn = card.querySelector('.suggest-card-add');
            addBtn.addEventListener('click', async () => {
              addBtn.disabled = true;
              addBtn.textContent = '…';
              try {
                const r = await apiFetch(`/portfolios/${portfolioId}/instruments`, {
                  method: 'POST', headers: {'Content-Type':'application/json'},
                  body: JSON.stringify({ ticker: b.ticker, quantity: 1, purchase_price: b.purchase_price || b.current_price || 1000 })
                });
                if (!r.ok) throw new Error();
                addBtn.textContent = '✓';
                addBtn.style.background = 'var(--green-600)';
                await syncTableFromServer();
              } catch {
                addBtn.disabled = false;
                addBtn.textContent = '+';
              }
            });
            grid.appendChild(card);
          });
        } catch {
          section.style.display = 'none';
        }
      }

      function fmtAmount(v) {
        if (v >= 1_000_000) return (v / 1_000_000).toFixed(v % 1_000_000 === 0 ? 0 : 1) + ' ' + t('wizard.amountM');
        if (v >= 1_000)    return (v / 1_000).toFixed(0) + ' ' + t('wizard.amountK');
        return v + ' ₽';
      }

      // Logarithmic slider: 10k..100M mapped to 0..100
      function sliderToAmount(s) {
        const min = Math.log10(10_000), max = Math.log10(100_000_000);
        return Math.round(Math.pow(10, min + (s / 100) * (max - min)));
      }
      function amountToSlider(a) {
        const min = Math.log10(10_000), max = Math.log10(100_000_000);
        return Math.round(((Math.log10(a) - min) / (max - min)) * 100);
      }

      function buildWizardHTML() {
        return `
        <div id="pw-wizard" style="padding:40px 24px 60px;font-family:inherit;background-image:linear-gradient(rgba(148,163,184,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.06) 1px,transparent 1px);background-size:44px 44px;border-radius:var(--radius,8px);width:100%;box-sizing:border-box;text-align:left;">
          <div style="display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:32px;">
            <div style="font-size:28px;font-weight:800;color:var(--slate-100);letter-spacing:-.5px;margin-bottom:8px;" data-i18n="wizard.title">
              Подберём портфель за вас
            </div>
            <div style="font-size:14px;color:var(--slate-400);max-width:480px;width:100%;margin:0 auto;line-height:1.6;" data-i18n="wizard.desc">
              Укажите сумму, желаемую доходность и уровень риска — система подберёт до 10 облигаций с MOEX.
            </div>
          </div>

          <div class="pw-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;max-width:900px;margin:0 auto 28px;">

            <!-- Slider 1: Amount -->
            <div class="pw-card">
              <div class="pw-card-label" data-i18n="wizard.amount">Сумма портфеля</div>
              <div id="pw-amount-val" class="pw-card-value">500 тыс ₽</div>
              <input id="pw-amount" type="range" min="0" max="100" value="59" class="pw-slider">
              <div class="pw-slider-hints"><span data-i18n="wizard.amountMin">10 тыс</span><span data-i18n="wizard.amountMax">100 млн</span></div>
            </div>

            <!-- Slider 2: Yield -->
            <div class="pw-card">
              <div class="pw-card-label" data-i18n="wizard.yield">Ожидаемая доходность</div>
              <div id="pw-yield-val" class="pw-card-value">12%</div>
              <input id="pw-yield" type="range" min="5" max="30" step="0.5" value="12" class="pw-slider">
              <div class="pw-slider-hints"><span id="pw-yield-min">5%</span><span id="pw-yield-max">30%</span></div>
              <div id="pw-yield-range-note" style="font-size:11px;color:var(--slate-500);margin-top:6px;min-height:14px;"></div>
            </div>

            <!-- Slider 3: Risk -->
            <div class="pw-card">
              <div class="pw-card-label" data-i18n="wizard.risk">Уровень риска</div>
              <div id="pw-risk-val" class="pw-card-value" style="color:#eab308;">Умеренный</div>
              <input id="pw-risk" type="range" min="0" max="4" step="1" value="2" class="pw-slider">
              <div class="pw-slider-hints"><span data-i18n="wizard.riskMin">Мин.</span><span data-i18n="wizard.riskHigh">Высокий</span></div>
              <div id="pw-risk-desc" style="font-size:11px;color:var(--slate-500);margin-top:6px;">Рейтинг A–BBB, надёжный корпоратив</div>
            </div>
          </div>

          <div style="text-align:center;margin-bottom:32px;">
            <button id="pw-submit" class="btn" style="background:linear-gradient(135deg,#2563eb,#4f46e5);color:#fff;padding:12px 36px;font-size:14px;font-weight:600;border-radius:8px;min-width:200px;" data-i18n="wizard.submit">
              Подобрать портфель
            </button>
            <div style="margin-top:12px;">
              <button onclick="openAddInstrumentModal()" style="background:none;border:none;color:var(--slate-500);font-size:12px;cursor:pointer;text-decoration:underline;font-family:inherit;" data-i18n="wizard.addManual">
                Добавить бумагу вручную
              </button>
            </div>
          </div>

          <div id="pw-result" style="display:none;max-width:900px;margin:0 auto;"></div>

          <style>
            .pw-card {
              background: var(--slate-800,#1e293b);
              border: 1px solid var(--slate-700,#334155);
              border-radius: 12px;
              padding: 20px 20px 16px;
            }
            .pw-card-label {
              font-size: 11px; font-weight: 600; text-transform: uppercase;
              letter-spacing: .5px; color: var(--slate-400,#94a3b8); margin-bottom: 8px;
            }
            .pw-card-value {
              font-size: 26px; font-weight: 700; color: var(--slate-100,#f1f5f9);
              letter-spacing: -.5px; margin-bottom: 14px;
            }
            .pw-slider {
              -webkit-appearance: none; appearance: none;
              width: 100%; height: 4px;
              background: var(--slate-600,#475569);
              border-radius: 2px; outline: none; cursor: pointer;
            }
            .pw-slider::-webkit-slider-thumb {
              -webkit-appearance: none; appearance: none;
              width: 18px; height: 18px; border-radius: 50%;
              background: #3b82f6;
              box-shadow: 0 0 0 3px rgba(59,130,246,.25);
              cursor: pointer; transition: box-shadow .15s;
            }
            .pw-slider::-webkit-slider-thumb:hover {
              box-shadow: 0 0 0 5px rgba(59,130,246,.3);
            }
            .pw-slider::-moz-range-thumb {
              width: 18px; height: 18px; border-radius: 50%;
              background: #3b82f6; border: none; cursor: pointer;
            }
            .pw-slider-hints {
              display: flex; justify-content: space-between;
              font-size: 10px; color: var(--slate-500,#64748b); margin-top: 6px;
            }
            body.mobile-mode .pw-grid {
              grid-template-columns: 1fr !important;
            }
            body.mobile-mode .pw-results-grid {
              grid-template-columns: 1fr !important;
            }
            body.mobile-mode .pw-result-stats {
              flex-direction: column; align-items: flex-start; gap: 8px;
            }
            body.mobile-mode .pw-result-stats-nums {
              flex-wrap: wrap; gap: 16px;
            }
            .pw-bond-card {
              background: var(--slate-800,#1e293b);
              border: 1px solid var(--slate-700,#334155);
              border-radius: 10px; padding: 14px 16px;
              display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
              transition: border-color .15s;
            }
            .pw-bond-card:hover { border-color: #3b82f6; }
            .pw-dismiss-btn {
              position: absolute; top: 3px; right: 6px;
              background: none; border: none; cursor: pointer;
              color: var(--slate-500); font-size: 15px; line-height: 1;
              padding: 2px 5px; border-radius: 4px; font-family: inherit;
              transition: color .15s, background .15s;
            }
            .pw-dismiss-btn:hover { color: #f87171; background: rgba(248,113,113,.1); }
            .pw-rating-badge {
              flex-shrink: 0; min-width: 38px; padding: 3px 7px;
              border-radius: 5px; font-size: 11px; font-weight: 700;
              text-align: center; background: rgba(59,130,246,.15); color: #60a5fa;
              border: 1px solid rgba(59,130,246,.25);
            }
          </style>
        </div>`;
      }

      function initWizard() {
        const amountSlider = document.getElementById('pw-amount');
        const yieldSlider  = document.getElementById('pw-yield');
        const riskSlider   = document.getElementById('pw-risk');
        const amountVal    = document.getElementById('pw-amount-val');
        const yieldVal     = document.getElementById('pw-yield-val');
        const riskVal      = document.getElementById('pw-risk-val');
        const riskDesc     = document.getElementById('pw-risk-desc');
        const submitBtn    = document.getElementById('pw-submit');
        const resultEl     = document.getElementById('pw-result');

        if (!amountSlider) return;

        const yieldRangeNote = document.getElementById('pw-yield-range-note');

        const updateAmount = () => {
          amountVal.textContent = fmtAmount(sliderToAmount(+amountSlider.value));
        };

        const updateYieldDisplay = (v = +yieldSlider.value) => {
          yieldVal.textContent = v.toFixed(1).replace('.0', '') + '%';
        };

        // Closest risk level by midpoint distance
        const yieldToRiskIndex = (yv) => {
          let bestIdx = 0, bestDist = Infinity;
          RISK_KEYS.forEach((key, idx) => {
            const { yieldMin, yieldMax } = RISK_LABELS[key];
            const dist = Math.abs((yieldMin + yieldMax) / 2 - yv);
            if (dist < bestDist) { bestDist = dist; bestIdx = idx; }
          });
          return bestIdx;
        };

        // Update risk label/desc/note to match current yield (no slider animation)
        let _syncingRisk = false;
        const syncRiskFromYield = () => {
          const idx  = yieldToRiskIndex(+yieldSlider.value);
          const key  = RISK_KEYS[idx];
          const info = getRiskInfo(key);
          riskVal.textContent = info.label;
          riskVal.style.color  = info.color;
          riskDesc.textContent = info.desc;
          yieldRangeNote.textContent = t('wizard.yieldNote').replace('{label}', info.label.toLowerCase()).replace('{min}', info.yieldMin).replace('{max}', info.yieldMax);
          if (+riskSlider.value !== idx) {
            _syncingRisk = true;
            riskSlider.value = idx;
            _syncingRisk = false;
          }
        };

        // Smoothly animate yield slider to target value
        let _yieldAnimId = null;
        const animateYieldTo = (target) => {
          if (_yieldAnimId) cancelAnimationFrame(_yieldAnimId);
          const start = +yieldSlider.value;
          if (Math.abs(start - target) < 0.05) return;
          const duration = 380;
          const t0 = performance.now();
          const step = (now) => {
            const p    = Math.min(1, (now - t0) / duration);
            const ease = 1 - Math.pow(1 - p, 3); // cubic ease-out
            const v    = start + (target - start) * ease;
            yieldSlider.value = v;
            updateYieldDisplay(v);
            syncRiskFromYield();
            if (p < 1) _yieldAnimId = requestAnimationFrame(step);
          };
          _yieldAnimId = requestAnimationFrame(step);
        };

        // Yield drag → risk label follows in real-time
        const updateYield = () => {
          if (_yieldAnimId) { cancelAnimationFrame(_yieldAnimId); _yieldAnimId = null; }
          updateYieldDisplay();
          syncRiskFromYield();
        };

        // Risk click → yield animates to midpoint of selected range
        const updateRisk = () => {
          if (_syncingRisk) return;
          const key  = RISK_KEYS[+riskSlider.value];
          const info = getRiskInfo(key);
          riskVal.textContent = info.label;
          riskVal.style.color  = info.color;
          riskDesc.textContent = info.desc;
          const mid = (info.yieldMin + info.yieldMax) / 2;
          animateYieldTo(mid);
        };

        amountSlider.addEventListener('input', updateAmount);
        yieldSlider.addEventListener('input', updateYield);
        riskSlider.addEventListener('input', updateRisk);

        updateAmount(); updateYieldDisplay(); syncRiskFromYield();

        submitBtn.addEventListener('click', async () => {
          const amount = sliderToAmount(+amountSlider.value);
          const yieldT = +yieldSlider.value;
          const riskKey = RISK_KEYS[+riskSlider.value];

          window._wizardBusy = true;
          submitBtn.disabled = true;
          submitBtn.innerHTML = '<span style="width:13px;height:13px;border-radius:50%;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;animation:spinBtn .6s linear infinite;display:inline-block;margin-right:6px;vertical-align:middle;"></span>' + t('wizard.selecting');
          resultEl.style.display = 'none';

          try {
            const res = await apiFetch(`/bonds/suggest?amount=${amount}&yield=${yieldT}&risk=${riskKey}`);
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || t('wizard.selectError'));
            renderWizardResult(data, resultEl, amount);
          } catch(err) {
            resultEl.style.display = 'block';
            const errDiv = document.createElement('div');
            errDiv.style.cssText = 'text-align:center;color:#f87171;padding:16px;';
            errDiv.textContent = err.message;
            resultEl.innerHTML = '';
            resultEl.appendChild(errDiv);
          } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = t('wizard.submit');
            // _wizardBusy остаётся true пока результаты видны (сбрасывается при добавлении или "Изменить параметры")
          }
        });
      }

      function renderWizardResult(data, container, budget) {
        const { bonds, summary } = data;
        const riskInfo = getRiskInfo(summary.risk) || getRiskInfo(summary.original_risk) || { label: summary.risk, color: '#94a3b8' };
        const fmt = v => new Intl.NumberFormat('ru-RU').format(Math.round(v));

        const freqLabel = f => `${f}×${t('wizard.freqYear')}`;

        const skipped = new Set();

        let cardsHtml = bonds.map((b, i) => {
          const ratingValue = b.rating || b.company_rating || null;
          let ratingHtml;
          if (ratingValue) {
            // Colour by rating tier
            const rv = ratingValue.toUpperCase().split('(')[0].trim();
            const isTop = ['AAA','AA+','AA','AA-'].includes(rv);
            const isMid = ['A+','A','A-','BBB+','BBB','BBB-'].includes(rv);
            const rBg = isTop ? 'rgba(34,197,94,.15)' : isMid ? 'rgba(59,130,246,.15)' : 'rgba(251,191,36,.15)';
            const rColor = isTop ? '#4ade80' : isMid ? '#60a5fa' : '#fbbf24';
            const rBorder = isTop ? 'rgba(34,197,94,.3)' : isMid ? 'rgba(59,130,246,.25)' : 'rgba(251,191,36,.3)';
            ratingHtml = `<span class="pw-rating-badge" style="background:${rBg};color:${rColor};border-color:${rBorder};">${ratingValue}</span>`;
          } else if (b.listlevel) {
            const lvlLabel = ['', 'Ур.1', 'Ур.2', 'Ур.3'][b.listlevel] || `Ур.${b.listlevel}`;
            const lvlColor = b.listlevel === 1 ? '#4ade80' : b.listlevel === 2 ? '#60a5fa' : '#94a3b8';
            const lvlBg = b.listlevel === 1 ? 'rgba(34,197,94,.1)' : b.listlevel === 2 ? 'rgba(59,130,246,.1)' : 'rgba(148,163,184,.1)';
            const lvlBorder = b.listlevel === 1 ? 'rgba(34,197,94,.2)' : b.listlevel === 2 ? 'rgba(59,130,246,.2)' : 'rgba(148,163,184,.2)';
            ratingHtml = `<span class="pw-rating-badge" title="Уровень листинга MOEX" style="background:${lvlBg};color:${lvlColor};border-color:${lvlBorder};">${lvlLabel}</span>`;
          } else {
            ratingHtml = `<span class="pw-rating-badge" style="background:rgba(100,116,139,.15);color:#64748b;border-color:rgba(100,116,139,.2);">—</span>`;
          }
          const maturity = b.maturity ? b.maturity.substring(0, 7) : '—';
          const offerNote = b.offer_date ? ` · ${t('wizard.offer')} ${b.offer_date.substring(0,7)}` : '';
          const freq = freqLabel(b.coupon_frequency || 2);
          return `
          <div class="pw-bond-card" data-bond-idx="${i}" style="position:relative;">
            <button class="pw-dismiss-btn" data-idx="${i}" title="Пропустить">×</button>
            ${ratingHtml}
            <div style="flex:1;min-width:0;">
              <div style="font-weight:600;font-size:13px;color:var(--slate-100);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(b.name)}</div>
              <div style="font-size:11px;color:var(--slate-400);margin-top:2px;">${esc(b.ticker)} · ${t('wizard.maturity')} ${esc(maturity)}${esc(offerNote)}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;">
              <div style="font-size:13px;font-weight:700;color:#4ade80;">${esc(String(b.coupon_percent))}% <span style="font-size:10px;font-weight:400;color:var(--slate-400);">${esc(freq)}</span></div>
              <div style="font-size:11px;color:var(--slate-400);">${esc(String(b.lots))} ${t('wizard.lotX')} ${fmt(b.purchase_price)} ₽</div>
              <div style="font-size:12px;color:var(--slate-300);font-weight:600;">${fmt(b.total_cost)} ₽</div>
            </div>
          </div>`;
        }).join('');

        const leftover = budget - summary.total_cost;

        container.style.display = 'block';
        container.innerHTML = `
          <div class="pw-result-stats" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:20px;">
            <div>
              <div style="font-size:17px;font-weight:700;color:var(--slate-100);">${t('wizard.result.title')}</div>
              <div style="font-size:12px;color:var(--slate-400);margin-top:3px;">
                ${summary.bonds_count} ${t('wizard.result.bondsRisk')} <span style="color:${riskInfo.color};">${riskInfo.label}</span>
              </div>
            </div>
            <div class="pw-result-stats-nums" style="display:flex;gap:24px;">
              <div style="text-align:center;">
                <div style="font-size:20px;font-weight:700;color:var(--slate-100);">${fmt(summary.total_cost)} ₽</div>
                <div style="font-size:11px;color:var(--slate-400);">${t('wizard.result.total')}</div>
              </div>
              <div style="text-align:center;">
                <div style="font-size:20px;font-weight:700;color:#4ade80;">${summary.avg_yield.toFixed(1)}%</div>
                <div style="font-size:11px;color:var(--slate-400);">${t('wizard.result.avgCoupon')}</div>
              </div>
              ${leftover > 0 ? `<div style="text-align:center;">
                <div style="font-size:20px;font-weight:700;color:var(--slate-400);">${fmt(leftover)} ₽</div>
                <div style="font-size:11px;color:var(--slate-400);">${t('wizard.result.leftover')}</div>
              </div>` : ''}
            </div>
          </div>
          <div class="pw-results-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:10px;margin-bottom:24px;">
            ${cardsHtml}
          </div>
          <div style="text-align:center;">
            <button id="pw-add-all" class="btn" style="background:linear-gradient(135deg,#16a34a,#15803d);color:#fff;padding:11px 32px;font-size:14px;font-weight:600;border-radius:8px;">
              ${t('wizard.result.addAll')}
            </button>
            <button onclick="document.getElementById('pw-result').style.display='none';window._wizardBusy=false;" style="background:none;border:none;color:var(--slate-500);font-size:12px;cursor:pointer;text-decoration:underline;font-family:inherit;margin-left:16px;">
              ${t('wizard.result.changeParams')}
            </button>
          </div>
        `;

        // Dismiss buttons
        container.querySelectorAll('.pw-dismiss-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const idx = Number(btn.dataset.idx);
            skipped.add(idx);
            const card = container.querySelector(`.pw-bond-card[data-bond-idx="${idx}"]`);
            if (card) {
              card.style.transition = 'opacity .2s,transform .2s';
              card.style.opacity = '0';
              card.style.transform = 'scale(.95)';
              setTimeout(() => card.remove(), 200);
            }
          });
        });

        const doBulkAdd = async (bondsToAdd) => {
          const addBtn = document.getElementById('pw-add-all');
          addBtn.disabled = true;
          addBtn.textContent = 'Добавляем...';

          const payload = bondsToAdd.map(b => ({ ticker: b.ticker, quantity: b.lots * b.lot_size }));
          let addedTickers = [], failedTickers = [];
          try {
            const res = await apiFetch(`/portfolios/${portfolioId}/instruments/bulk`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
            addedTickers = Array.isArray(res.added) ? res.added : bondsToAdd.map(b => b.ticker);
            failedTickers = Array.isArray(res.failed) ? res.failed : [];
          } catch(e) {
            console.warn('Bulk add failed', e);
            failedTickers = bondsToAdd.map(b => b.ticker);
          }

          window._wizardBusy = false;

          if (failedTickers.length === 0) {
            addBtn.textContent = `✓ Добавлено ${addedTickers.length} бумаг`;
            addBtn.style.background = 'linear-gradient(135deg,#16a34a,#15803d)';
          } else {
            addBtn.textContent = addedTickers.length > 0
              ? `✓ ${addedTickers.length} добавлено, ${failedTickers.length} не удалось`
              : `✗ Не удалось добавить ${failedTickers.length} бумаг`;
            addBtn.style.background = addedTickers.length > 0
              ? 'linear-gradient(135deg,#d97706,#b45309)'
              : 'linear-gradient(135deg,#dc2626,#b91c1c)';

            // Показываем кнопку повтора для упавших
            const retryBonds = bondsToAdd.filter(b => failedTickers.includes(b.ticker));
            let retryBtn = document.getElementById('pw-retry-btn');
            if (!retryBtn) {
              retryBtn = document.createElement('button');
              retryBtn.id = 'pw-retry-btn';
              retryBtn.style.cssText = 'background:none;border:1px solid #f87171;color:#f87171;font-size:12px;cursor:pointer;font-family:inherit;margin-left:12px;padding:6px 14px;border-radius:6px;transition:background .15s';
              retryBtn.onmouseenter = () => retryBtn.style.background = 'rgba(248,113,113,.1)';
              retryBtn.onmouseleave = () => retryBtn.style.background = 'none';
              addBtn.parentNode.appendChild(retryBtn);
            }
            retryBtn.textContent = `Повторить (${failedTickers.length})`;
            retryBtn.onclick = () => {
              retryBtn.remove();
              doBulkAdd(retryBonds);
            };
          }

          await syncTableFromServer();
        };

        document.getElementById('pw-add-all').addEventListener('click', () => {
          const toAdd = bonds.filter((_, i) => !skipped.has(i));
          doBulkAdd(toAdd);
        });

        container.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }

      // ── RENDER TABLE ─────────────────────────────────────────────
      const tableBody = document.getElementById('table-body');

      const SKEL_WIDTHS = ['30%','70%','55%','45%','35%','45%','50%','50%','35%','40%','40%','40%','50%','40%','50%','50%','30%'];
      function showSkeletonRows(count = 7) {
        tableBody.innerHTML = '';
        for (let i = 0; i < count; i++) {
          const tr = document.createElement('tr');
          tr.className = 'skeleton-row';
          SKEL_WIDTHS.forEach((w, ci) => {
            const td = document.createElement('td');
            const s  = document.createElement('span');
            s.className = 'skel-cell';
            s.style.width = w;
            s.style.animationDelay = `${i * 60 + ci * 10}ms`;
            td.appendChild(s);
            tr.appendChild(td);
          });
          tableBody.appendChild(tr);
        }
      }

      function addCell(tr, val) {
        const td = document.createElement('td'); td.textContent = val; tr.appendChild(td); return td;
      }

      function setEmptyState(isEmpty) {
        const statGrid    = document.querySelector('.stat-grid');
        const chartCard   = document.getElementById('chart-card');
        const tableStatus = document.getElementById('table-status')?.parentElement;
        const thead       = document.querySelector('#panel-table table thead');
        const tableCard   = document.querySelector('.table-wrap')?.closest('.card');
        [statGrid, chartCard, tableStatus].forEach(el => {
          if (el) el.style.display = isEmpty ? 'none' : '';
        });
        if (thead) thead.style.display = isEmpty ? 'none' : '';
        // Убираем белый фон и бордер карточки таблицы когда показывается wizard
        if (tableCard) {
          tableCard.style.background = isEmpty ? 'transparent' : '';
          tableCard.style.border     = isEmpty ? 'none' : '';
          tableCard.style.overflow   = isEmpty ? 'visible' : '';
        }
      }

      function showToast(msg, duration = 1800) {
        let toast = document.getElementById('_toast_notification');
        if (!toast) {
          toast = document.createElement('div');
          toast.id = '_toast_notification';
          toast.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(8px);background:var(--bg-elevated);color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius);padding:8px 18px;font-size:13px;font-weight:500;box-shadow:0 4px 16px rgba(0,0,0,.25);z-index:9999;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;white-space:nowrap;';
          document.body.appendChild(toast);
        }
        toast.textContent = msg;
        clearTimeout(toast._timer);
        requestAnimationFrame(() => {
          toast.style.opacity = '1';
          toast.style.transform = 'translateX(-50%) translateY(0)';
        });
        toast._timer = setTimeout(() => {
          toast.style.opacity = '0';
          toast.style.transform = 'translateX(-50%) translateY(8px)';
        }, duration);
      }

      function renderTable() {
        // Re-validate profit mode against current data (full mode needs T-Bank rows).
        // Pass the DESIRED mode, not the (possibly degraded) effective one — otherwise a
        // 'full'→'total' degradation during loading would persist as the saved choice.
        if (typeof setProfitMode === 'function') setProfitMode(window._profitModeDesired);
        tableRows.forEach(r => computeSortFields(r));
        const rows = getSortedRows(tableRows);
        tableBody.innerHTML = '';

        // Show portfolio wizard if empty
        if (rows.length === 0 && !isReadOnly) {
          setEmptyState(true);
          const tr = document.createElement('tr');
          const td = document.createElement('td');
          td.colSpan = 20;
          td.style.cssText = 'padding: 0; border: none; white-space: normal; text-align: center;';
          td.innerHTML = buildWizardHTML();
          tr.appendChild(td);
          tableBody.appendChild(tr);
          initWizard();
          applyLang(window._lang);
          return;
        }

        setEmptyState(false);

        for (let idx = 0; idx < rows.length; idx++) {
          const row = rows[idx];
          const tr  = document.createElement('tr');
          tr.style.animation = `rowFadeIn .18s ease-out ${Math.min(idx * 25, 400)}ms both`;

          addCell(tr, String(idx + 1));

          // Name with link
          const nameTd = document.createElement('td');
          nameTd.className = 'col-name';
          const link = document.createElement('a');
          link.href    = row.type === 'stock'
            ? 'https://smart-lab.ru/forum/' + encodeURIComponent(row.ticker)
            : 'https://smart-lab.ru/q/bonds/' + encodeURIComponent(row.ticker) + '/';
          link.target  = '_blank'; link.rel = 'noopener noreferrer';
          link.textContent = fmt(row.name); link.title = fmt(row.name);
          nameTd.appendChild(link);
          // Ticker badge — click to copy
          if (row.ticker) {
            const tickerBadge = document.createElement('span');
            tickerBadge.textContent = row.ticker;
            tickerBadge.title = 'Скопировать тикер';
            tickerBadge.style.cssText = 'display:block;font-size:10px;color:var(--text-muted);cursor:pointer;width:fit-content;';
            tickerBadge.onclick = (e) => {
              e.stopPropagation();
              navigator.clipboard.writeText(row.ticker).then(() => showToast(`Скопировано: ${row.ticker}`));
            };
            nameTd.appendChild(tickerBadge);
          }
          // /all: show which broker(s)/portfolio(s) hold this security.
          if (isAllMode && row.brokers && row.brokers.length) {
            const bb = document.createElement('span');
            const names = row.brokers.map(x => `${x.name}: ${Math.round(x.quantity)} шт`).join('\n');
            if (row.brokers.length > 1) {
              bb.textContent = `🔗 ${row.brokers.length} брокера`;
              bb.style.cssText = 'display:inline-block;margin-top:3px;font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;background:rgba(34,197,94,.12);color:var(--green-400);border:1px solid rgba(34,197,94,.3);cursor:help;';
            } else {
              bb.textContent = row.brokers[0].name;
              bb.style.cssText = 'display:inline-block;margin-top:3px;font-size:9px;color:var(--text-muted);cursor:help;';
            }
            bb.title = names;
            nameTd.appendChild(bb);
          } else if (isAllMode && row.portfolio_name) {
            const pb = document.createElement('span');
            pb.textContent = row.portfolio_name;
            pb.style.cssText = 'display:block;margin-top:2px;font-size:9px;color:var(--text-muted);';
            nameTd.appendChild(pb);
          }
          if (row.face_unit && row.face_unit !== 'SUR') {
            const fxBadge = document.createElement('span');
            fxBadge.textContent = ({CNY:'¥ CNY', USD:'$ USD', EUR:'€ EUR', CHF:'₣ CHF'})[row.face_unit] || row.face_unit;
            // Tooltip explains the RUB figures: numbers in the table are the
            // currency nominal converted at the live FX rate.
            const fxSym = ({CNY:'¥', USD:'$', EUR:'€', CHF:'₣'})[row.face_unit] || '';
            if (row.fx_rate && row.fx_rate > 1 && row.nominal) {
              const nomCcy = row.nominal / row.fx_rate;
              fxBadge.title = 'Номинал ' + nomCcy.toLocaleString('ru', {maximumFractionDigits: 0}) + ' ' + fxSym
                + ' × ' + row.fx_rate.toFixed(2) + ' ₽ = ' + Math.round(row.nominal).toLocaleString('ru') + ' ₽'
                + '\nЦифры в таблице — в рублях по текущему курсу.';
            } else {
              fxBadge.title = 'Номинал в ' + row.face_unit;
            }
            fxBadge.style.cssText = 'display:inline-block;margin-top:3px;font-size:9px;font-weight:600;padding:1px 4px;border-radius:3px;background:rgba(59,130,246,.12);color:#2563eb;border:1px solid rgba(59,130,246,.25);letter-spacing:.3px;';
            nameTd.appendChild(fxBadge);
          }
          if (row.is_qual) {
            const qualBadge = document.createElement('span');
            qualBadge.textContent = 'КИ';
            qualBadge.title = 'Только для квалифицированных инвесторов';
            qualBadge.style.cssText = 'display:inline-block;margin-top:3px;font-size:9px;font-weight:600;padding:1px 4px;border-radius:3px;background:rgba(234,179,8,.15);color:#ca8a04;border:1px solid rgba(234,179,8,.3);letter-spacing:.3px;';
            nameTd.appendChild(qualBadge);
          }
          // The same hint also rides in the name cell, which is the only column
          // visible on a phone without scrolling the table sideways — the buy-price
          // column sits well past the right edge at 360-390px. CSS shows exactly
          // one of the two depending on body.mobile-mode.
          if (!isReadOnly && bondNeedsPurchaseDate(row)) {
            const mHint = createDateHintButton(row);
            mHint.classList.add('date-hint-mobile');
            nameTd.appendChild(mHint);
          }
          tr.appendChild(nameTd);

          tr.appendChild(createRatingCell(row.company_rating));
          // Котировки нет (бумаги нет на MOEX — например, иностранная из
          // синка брокера). Показываем прочерк: ноль читался бы как реальная
          // цена, обнулившаяся до нуля.
          const noData = row.no_market_data === true;
          const priceTd = addCell(tr, noData ? '—' : fmt(row.current_price));
          if (noData) {
            priceTd.classList.add('no-market-data');
            priceTd.title = t('table.noMarketData', 'Нет данных: бумага не торгуется на MOEX');
          }
          tr.appendChild(createEditableCell(row, 'quantity', ''));
          tr.appendChild(createEditableCell(row, 'purchase_price', ''));
          const valueTd = addCell(tr, noData ? '—' : fmt(row.current_value));
          if (noData) valueTd.classList.add('no-market-data');

          // Profit cell — total or day P&L depending on global mode
          const profitTotal = Number(row.profit || 0);
          const rawProfitVal = profitValueFor(row);
          const profitVal = (rawProfitVal === null || rawProfitVal === undefined)
            ? null : Number(rawProfitVal);
          const profitTd = document.createElement('td');
          // Treat values that round to 0 as "no change" — avoids "+0"/"-0".
          if (profitVal === null || Math.round(profitVal) === 0) {
            profitTd.textContent = '—';
            profitTd.style.color = 'var(--text-muted)';
            // Без пояснения прочерк читается как «прибыль не изменилась»,
            // тогда как здесь она просто не вычислима.
            if (noData) {
              profitTd.classList.add('no-market-data');
              profitTd.title = t('table.noMarketData', 'Нет данных: бумага не торгуется на MOEX');
            }
          } else {
            profitTd.textContent = (profitVal > 0 ? '+' : '−') + fmt(Math.abs(profitVal));
            if (profitVal > 0) profitTd.classList.add('profit-positive');
            else profitTd.classList.add('profit-negative');
          }
          // Breakdown tooltip so the figure isn't a black box (esp. "Полная").
          if (profitVal !== null && row.type === 'bond') {
            const _q = Number(row.quantity || 0);
            const body = Number(row.profit || 0);                 // переоценка тела
            const aciT = Number(row.aci || 0) * _q;               // накопленный НКД
            const coup = Number(row.realized_coupons || 0);       // полученные купоны
            const fmtSigned = (v) => (v >= 0 ? '+' : '−') + fmt(Math.abs(Math.round(v))) + ' ₽';
            if (window._profitMode === 'full' && row.full_profit != null) {
              profitTd.title =
                'Полная прибыль = переоценка тела + НКД + полученные купоны\n' +
                'Переоценка тела:  ' + fmtSigned(body) + '\n' +
                'Накопленный НКД:  ' + fmtSigned(aciT) + '\n' +
                'Купоны получены:  ' + fmtSigned(coup) + '\n' +
                '─────────────\n' +
                'Итого:  ' + fmtSigned(Number(row.full_profit));
            } else if (window._profitMode === 'day') {
              profitTd.title = 'Изменение цены тела за последнюю торговую сессию × количество';
            } else {
              profitTd.title = 'Прибыль от покупки = (текущая чистая цена − цена покупки) × количество';
            }
          }
          tr.appendChild(profitTd);
          // Цветовая индикация строки по прибыли (всегда по total — стабильнее)
          if (profitTotal > 0) tr.style.setProperty('--row-profit-color', 'rgba(22,163,74,0.04)');
          else if (profitTotal < 0) tr.style.setProperty('--row-profit-color', 'rgba(220,38,38,0.04)');

          addCell(tr, fmt(row.weight) + '%');
          tr.appendChild(createCouponCell(row));

          if (row.type === 'bond') {
            const qty       = Number(row.quantity || 0);
            const couponRub = Number(row.coupon || 0);
            const period    = Number(row.coupon_period || 0);
            const freq      = period > 0 ? Math.round(365 / period) : 2;
            tr.appendChild(createCouponRateCell(row));                             // % купона
            addCell(tr, String(freq));                                              // Частота
            addCell(tr, fmt(roundTo2(couponRub * qty * freq)));                    // Годовой купон
            addCell(tr, row.market_yield != null ? fmt(row.market_yield) : '—');  // Рын.доход.
          } else {
            addCell(tr, '—'); addCell(tr, '—'); addCell(tr, '—'); addCell(tr, '—');
          }

          addCell(tr, row.type === 'bond' ? fmt(row.maturity_date) : '—');  // Погашение
          { const td = document.createElement('td'); td.className = 'col-offer'; td.textContent = row.type === 'bond' && row.offer_date ? fmt(row.offer_date) : ''; tr.appendChild(td); }  // Оферта

          // Actions (only in edit mode; never in /all — rows there are aggregated/virtual)
          if (!isReadOnly && !isAllMode) {
            const actionTd = document.createElement('td');
            actionTd.style.cssText = 'white-space:nowrap; text-align:right;';

            // Move button — shown only when user has more than one portfolio
            if (portfolios.length > 1) {
              const moveBtn = document.createElement('button');
              moveBtn.className = 'btn-sm-move';
              moveBtn.title = t('action.moveTooltip');
              moveBtn.textContent = '⇄';
              moveBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                const pop = document.getElementById('move-popover');
                // Toggle: if already open for same item, close it
                if (pop.style.display !== 'none' && _movePopoverItemId === row.id) {
                  hideMovePopover();
                } else {
                  showMovePopover(moveBtn, row.id);
                }
              });
              actionTd.appendChild(moveBtn);
            }

            // Alert (bell) button
            const alertBtn = document.createElement('button');
            alertBtn.className = 'btn-sm';
            alertBtn.style.marginLeft = '4px';
            alertBtn.title = 'Ценовые алерты';
            alertBtn.textContent = '\uD83D\uDD14';
            alertBtn.addEventListener('click', (e) => {
              e.stopPropagation();
              openPriceAlerts(row.id, portfolioId);
            });
            actionTd.appendChild(alertBtn);

            // Edit button (price / quantity)
            const editBtn = document.createElement('button');
            editBtn.className = 'btn-sm';
            editBtn.style.marginLeft = '4px';
            editBtn.title = 'Редактировать';
            editBtn.textContent = '✎';
            editBtn.addEventListener('click', (e) => {
              e.stopPropagation();
              openEditInstrumentModal(row);
            });
            actionTd.appendChild(editBtn);

            // Delete button
            const delBtn = document.createElement('button');
            delBtn.className = 'btn-sm';
            delBtn.style.marginLeft = '4px';
            delBtn.title = t('action.deleteTooltip');
            delBtn.textContent = '✕';
            delBtn.addEventListener('click', async () => {
              if (!confirm(t('action.confirmDelete'))) return;
              try {
                const resp = await apiFetch(`/portfolios/${portfolioId}/instruments/${row.id}`, { method: 'DELETE' });
                const body = await resp.json();
                if (!resp.ok) throw new Error(body.detail || 'Не удалось удалить.');
                tableRows = tableRows.filter(r => r.id !== row.id);
                recalculateWeights(tableRows); persistTableCache(); renderTable();
                setStatus(tableStatusEl, t('action.deleted'));
              } catch (err) { setStatus(tableStatusEl, err.message || t('action.deleteError'), true); }
            });
            actionTd.appendChild(delBtn);

            tr.appendChild(actionTd);
          }
          tableBody.appendChild(tr);
        }

        // Show offer column only when at least one bond has an offer date
        const hasOfferDate = rows.some(r => r.type === 'bond' && r.offer_date);
        const thOffer = document.getElementById('th-offer');
        if (thOffer) thOffer.style.display = hasOfferDate ? '' : 'none';
        document.querySelectorAll('.col-offer').forEach(el => { el.style.display = hasOfferDate ? '' : 'none'; });

        // Cash + autosync badge — independent of rows count
        updateCashWidget(portfolioId);

        if (rows.length === 0) { setEmptyState(true); drawMonthlyChart(tableRows); return; }

        // ── Summary row ──
        const summary = calculateSummary(rows);
        updateStatCards(summary);
        updateNextCouponWidget(tableRows);
        updateYTMWidget(tableRows);
        updateLastUpdateTime();

        const sumRow = document.createElement('tr');
        sumRow.className = 'summary-row';

        // col 1-6 (colspan=6): label
        const lbl = document.createElement('td'); lbl.colSpan = 6; lbl.textContent = t('table.total'); sumRow.appendChild(lbl);
        // col 7: Стоимость
        addCell(sumRow, fmt(roundTo2(summary.totalAssets)) + ' ₽');
        // col 8: Прибыль — follows the active profit-column mode (full / day / total)
        const sumProfit = window._profitMode === 'full' ? summary.totalFullProfit
                        : window._profitMode === 'day'  ? summary.dayProfit
                        : summary.totalProfit;
        const p2 = document.createElement('td');
        p2.textContent = (sumProfit >= 0 ? '+' : '') + fmt(roundTo2(sumProfit)) + ' ₽';
        p2.className   = sumProfit > 0 ? 'sum-positive' : sumProfit < 0 ? 'sum-negative' : '';
        sumRow.appendChild(p2);
        // cols 9-10: Доля + Купон (empty)
        const c1 = document.createElement('td'); c1.colSpan = 2; c1.textContent = ''; sumRow.appendChild(c1);
        // col 11: % купона (средневзвешенная ставка купона)
        addCell(sumRow, summary.avgCouponRate > 0 ? fmt(roundTo2(summary.avgCouponRate)) + '%' : '');
        // col 12: Частота (empty)
        addCell(sumRow, '');
        // col 13: Годовой купон (сумма)
        addCell(sumRow, fmt(roundTo2(summary.annualIncome)) + ' ₽');
        // col 14: Рын.доход. (средневзвешенная YTM)
        addCell(sumRow, fmt(roundTo2(summary.totalBondYield)) + '%');
        // cols 15-17: Погашение, Оферта, Действия (empty)
        const c5 = document.createElement('td'); c5.colSpan = 3; c5.textContent = ''; sumRow.appendChild(c5);
        tableBody.appendChild(sumRow);

        initCouponPeriodBtns();
        drawMonthlyChart(tableRows);
      }

      // ── SORT HEADERS ─────────────────────────────────────────────
      // Profit-mode toggle (⇄ icon in "Прибыль" header)
      const profitToggleBtn = document.getElementById('th-profit-toggle');
      if (profitToggleBtn) {
        profitToggleBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          setProfitMode(nextProfitMode());
          renderTable();
          if (typeof updateStatCards === 'function' && typeof calculateSummary === 'function') {
            try { updateStatCards(calculateSummary(tableRows)); } catch(err) {}
          }
        });
      }
      // Apply initial mode label/color
      setProfitMode(window._profitModeDesired);
      document.querySelectorAll('th[data-sort-key]').forEach(th => {
        th.addEventListener('click', (e) => {
          if (e.target && e.target.id === 'th-profit-toggle') return;
          const key = th.dataset.sortKey;
          if (sortState.key === key) sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
          else { sortState.key = key; sortState.direction = 'asc'; }
          document.querySelectorAll('th[data-sort-key]').forEach(h => {
            const arrow = h.dataset.sortKey === key ? (sortState.direction === 'asc' ? ' ↑' : ' ↓') : '';
            // Special-case for the "Прибыль" header: it has complex inner HTML
            // (label span + toggle button), so we only refresh the arrow inside
            // a dedicated suffix span and never rewrite the whole th.
            if (h.id === 'th-profit') {
              let arrowEl = h.querySelector('.th-sort-arrow');
              if (!arrowEl) {
                arrowEl = document.createElement('span');
                arrowEl.className = 'th-sort-arrow';
                arrowEl.style.fontStyle = 'normal';
                h.appendChild(arrowEl);
              }
              arrowEl.textContent = arrow;
              return;
            }
            // Always use current-language label for translated headers
            const i18nHtmlKey = h.dataset.i18nHtml;
            if (i18nHtmlKey) {
              const base = TRANSLATIONS[window._lang]?.[i18nHtmlKey] || h.dataset.origLabel || '';
              if (!h.dataset.origLabel) h.dataset.origLabel = base;
              h.innerHTML = base + (arrow ? `<span style="font-style:normal">${arrow}</span>` : '');
            } else {
              const base = h.dataset.i18n
                ? (TRANSLATIONS[window._lang]?.[h.dataset.i18n] || h.textContent.replace(/\s[↑↓]$/, '').trim())
                : (h.dataset.origLabel || (() => { h.dataset.origLabel = h.textContent.replace(/\s[↑↓]$/, '').trim(); return h.dataset.origLabel; })());
              h.textContent = base + arrow;
            }
          });
          renderTable();
        });
      });

      // ── RESIZE: redraw all canvas-based analytics ─────────────────
      // Debounced redraw — multiple panes resize in quick succession,
      // so we batch into a single rAF tick to avoid spam.
      let _redrawScheduled = false;
      const _redrawBondAnalytics = () => {
        if (_redrawScheduled) return;
        _redrawScheduled = true;
        // Double rAF: gives the browser one frame to finish CSS-driven layout
        // (e.g., mobile-mode class toggle, grid → 1col collapse) before we
        // measure parent widths.
        requestAnimationFrame(() => requestAnimationFrame(() => {
          _redrawScheduled = false;
          if (typeof tableRows === 'undefined' || !tableRows.length) return;
          renderIssuerBars(tableRows);
          renderMaturityLadder(tableRows);
          renderCurrencyChart(tableRows);
          renderTypeBars(tableRows);
          renderRatingsChart(tableRows);
          renderCouponTypeBar(tableRows);
          renderYTMCurve(tableRows);
        }));
      };

      // Full analytics refresh — used after mobile-mode toggle. Includes
      // HTML-based panels (events / cash-to-reinvest / anomalies / top
      // performers / sell-first), not just canvases.
      const _redrawAllAnalytics = () => {
        if (typeof tableRows === 'undefined' || !tableRows.length) return;
        // HTML-based: re-render performers, sell-first
        try { renderAnalytics(tableRows); } catch (_) {}
        // HTML-based: extras (events 30d, cash-to-reinvest, anomalies)
        const extra = window._analyticsExtra;
        if (extra) {
          try { renderAnomaliesBanner(extra.anomalies || []); } catch (_) {}
          try { renderCashReinvestCard(extra); } catch (_) {}
          try { renderEventsCard(extra.events || []); } catch (_) {}
        }
        // Canvas: forces redraw (also covers _redrawBondAnalytics targets)
        _redrawBondAnalytics();
      };

      if (typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(_redrawBondAnalytics);
        ['structure-card', 'ytm-curve-card'].forEach(id => {
          const el = document.getElementById(id);
          if (el) ro.observe(el);
        });
        const chartCard = document.getElementById('chart-card');
        if (chartCard) new ResizeObserver(() => drawMonthlyChart(tableRows)).observe(chartCard);
        const historyCard = document.getElementById('analytics-history-card');
        if (historyCard) new ResizeObserver(() => {
          const c = document.getElementById('analytics-history-chart');
          if (c && c._snapshots) drawHistoryChart(c, c._snapshots, true);
          else if (c) drawHistoryChartEmpty(c, c._emptySnap || null);
        }).observe(historyCard);
      } else {
        window.addEventListener('resize', () => {
          drawMonthlyChart(tableRows);
          const c = document.getElementById('analytics-history-chart');
          if (c && c._snapshots) drawHistoryChart(c, c._snapshots, true);
          _redrawBondAnalytics();
        });
      }

      // ── Mobile-mode toggle hook: refresh everything (canvases + HTML) ─
      // ResizeObserver catches canvas-card width changes, but HTML lists
      // (events, top performers, sell-first) don't reflow on body class
      // toggle — re-render them explicitly.
      if (typeof MutationObserver !== 'undefined') {
        let _lastMobile = document.body.classList.contains('mobile-mode');
        new MutationObserver(() => {
          const isMobile = document.body.classList.contains('mobile-mode');
          if (isMobile === _lastMobile) return;
          _lastMobile = isMobile;
          // 2 rAFs to let CSS-driven layout settle before re-measuring
          requestAnimationFrame(() => requestAnimationFrame(() => _redrawAllAnalytics()));
        }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
      }

      // ── SYNC ─────────────────────────────────────────────────────
      function setAllGrouped(on) {
        window._allGrouped = !!on;
        syncTableFromServer().catch(() => {});
      }

      async function refreshRatings() {
        if (isReadOnly || isAllMode || !portfolioId) return;
        const btn = document.getElementById('btn-refresh-ratings');
        if (btn && btn.disabled) return;
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '↻ Обновляю…'; }
        try {
          const resp = await apiFetch(`/portfolios/${portfolioId}/refresh-ratings`, { method: 'POST' });
          if (resp.status === 429) {
            toast(t('ratings.tooOften'), 'warn');
            return;
          }
          if (!resp.ok) { toast(t('ratings.error'), 'error'); return; }
          const data = await resp.json().catch(() => ({}));
          await syncTableFromServer();
          toast(t('ratings.updated').replace('{n}', data.updated ?? 0), 'success');
        } catch (e) {
          toast(t('ratings.error'), 'error');
        } finally {
          if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
      }

      async function syncTableFromServer() {
        showSkeletonRows();
        if (_soldBanner) _soldBanner.style.display = 'none';
        let url;
        if (isReadOnly && shareToken) {
          url = `/share/${shareToken}/table?ts=${Date.now()}`;
        } else if (isAllMode) {
          const grp = window._allGrouped ? '&group=ticker' : '';
          url = `/portfolios/all/table?ts=${Date.now()}${grp}`;
        } else {
          if (!portfolioId) throw new Error('Портфель не выбран.');
          url = `/portfolios/${portfolioId}/table?ts=${Date.now()}`;
        }
        const resp = await apiFetch(url, { cache: 'no-store' });
        if (resp.status === 403) {
          // Password required - show modal instead of prompt
          return new Promise((resolve, reject) => {
            // Store the resolve/reject for later use in password modal
            window._passwordModalResolve = () => {
              const password = document.getElementById('password-modal-input').value;
              if (!password) {
                document.getElementById('password-modal-status').textContent = 'Введите пароль';
                return;
              }
              sharePassword = password;
              // Store password in sessionStorage for this share token
              if (shareToken) {
                sessionStorage.setItem(`mvp_share_password_${shareToken}`, password);
              }
              closePasswordModal();
              syncTableFromServer().then(resolve).catch(reject);
            };
            window._passwordModalReject = reject;
            showPasswordModal();
          });
        }
        if (!resp.ok) throw new Error('Не удалось загрузить таблицу портфеля.');
        const payload = await resp.json();
        // Keep custom (off-exchange) items: they carry is_traded=false by design,
        // but must still appear — only hide genuinely delisted exchange instruments.
        tableRows = (payload.items || []).filter(r => r.is_traded !== false || r.source === 'custom');
        if (!isReadOnly) { try { persistTableCache(); } catch(e) {} }
        renderTable();
        scheduleAnalytics();
        // Update total portfolios value after sync
        if (!isReadOnly && !isAllMode) {
          calculateAllPortfoliosTotal().then(val => {
            allPortfoliosTotalValue = val;
            const summary = calculateSummary(tableRows);
            updateStatCards(summary);
          }).catch(err => console.error('Failed to update totals:', err));
          // Check for sold positions that need user action
          checkPendingRemovalBanner(portfolioId).catch(() => {});
        }
      }

      // ── SOLD-POSITION BANNER ──────────────────────────────────────
      const _soldBanner     = document.getElementById('sold-banner');
      const _soldBannerList = document.getElementById('sold-banner-list');

      async function checkPendingRemovalBanner(pid) {
        if (!pid || isReadOnly) { _soldBanner.style.display = 'none'; return; }
        try {
          const r = await apiFetch(`/tbank/sync/status?portfolio_id=${pid}`);
          if (!r.ok) { _soldBanner.style.display = 'none'; return; }
          const s = await r.json();
          if (!s.enabled || !s.pending_removal || s.pending_removal.length === 0) {
            _soldBanner.style.display = 'none';
            return;
          }
          _soldBanner._pid     = pid;
          _soldBanner._tickers = s.pending_removal;
          _soldBannerList.textContent = 'Исчезли из брокера: ' + s.pending_removal.join(', ');
          _soldBanner.style.display = '';
        } catch (_) {
          _soldBanner.style.display = 'none';
        }
      }

      document.getElementById('sold-banner-sync-btn').addEventListener('click', async () => {
        const pid     = _soldBanner._pid;
        const tickers = _soldBanner._tickers || [];
        if (!pid || !tickers.length) return;
        const btn = document.getElementById('sold-banner-sync-btn');
        btn.disabled = true;
        btn.textContent = 'Удаляю…';
        try {
          const r = await apiFetch('/tbank/sync/confirm-removal', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ portfolio_id: pid, tickers, confirm: true }),
          });
          const res = await r.json();
          if (r.ok) {
            setStatus(tableStatusEl, 'Удалено ' + res.removed + ' позиций из портфеля');
            _soldBanner.style.display = 'none';
            syncTableFromServer().catch(() => {});
          } else {
            btn.disabled = false; btn.textContent = 'Привести к виду брокера';
          }
        } catch (_) {
          btn.disabled = false; btn.textContent = 'Привести к виду брокера';
        }
      });

      document.getElementById('sold-banner-keep-btn').addEventListener('click', async () => {
        const pid     = _soldBanner._pid;
        const tickers = _soldBanner._tickers || [];
        _soldBanner.style.display = 'none';
        if (!pid || !tickers.length) return;
        try {
          await apiFetch('/tbank/sync/confirm-removal', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ portfolio_id: pid, tickers, confirm: false }),
          });
        } catch (_) {}
      });

      // ── ADD FORM (legacy stubs — форма перенесена в #add-instrument-modal) ───────

      // ── NOTIFICATIONS ─────────────────────────────────────────────
      const notifStatusEl = document.getElementById('notif-status');
      const tgTokenInput  = document.getElementById('tg-token');
      const tgChatInput   = document.getElementById('tg-chat');
      const tgThreshInput = document.getElementById('tg-threshold');
      const tgLangSelect  = document.getElementById('tg-lang');
      async function loadNotifSettings() {
        if (!_currentUserIsAdmin) return;
        try {
          const r = await apiFetch('/settings/notifications');
          if (!r.ok) return;
          const s = await r.json();
          tgTokenInput.value  = s.tg_bot_token || '';
          tgChatInput.value   = s.tg_chat_id   || '';
          tgThreshInput.value = s.price_drop_threshold ?? 5;
          if (tgLangSelect && s.tg_lang) tgLangSelect.value = s.tg_lang;
        } catch {}
      }
      document.getElementById('notif-save-btn').addEventListener('click', async () => {
        setStatus(notifStatusEl, t('settings.notif.saving'));
        try {
          const r = await apiFetch('/settings/notifications', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tg_bot_token: tgTokenInput.value.trim(), tg_chat_id: tgChatInput.value.trim(), price_drop_threshold: Number(tgThreshInput.value) || 5, tg_lang: tgLangSelect?.value || 'ru' }),
          });
          if (!r.ok) throw new Error(t('settings.notif.saveError'));
          setStatus(notifStatusEl, t('settings.notif.saved'));
        } catch (err) { setStatus(notifStatusEl, err.message, true); }
      });
      document.getElementById('notif-test-btn').addEventListener('click', async () => {
        setStatus(notifStatusEl, t('settings.notif.sending'));
        try {
          const r = await apiFetch('/settings/notifications/test', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tg_bot_token: tgTokenInput.value.trim(), tg_chat_id: tgChatInput.value.trim(), price_drop_threshold: Number(tgThreshInput.value) || 5, tg_lang: tgLangSelect?.value || 'ru' }),
          });
          const res = await r.json();
          if (!r.ok || !res.success) throw new Error(t('settings.notif.sendError'));
          setStatus(notifStatusEl, t('settings.notif.sent'));
        } catch (err) { setStatus(notifStatusEl, err.message, true); }
      });

      // ── COUPON NOTIFICATIONS (personal) ──────────────────────────
      async function initCouponNotifications() {
        const enabledChk = document.getElementById('coupon-notif-enabled');
        const daysRow    = document.getElementById('coupon-notif-days-row');
        const daysInput  = document.getElementById('coupon-notif-days');
        const saveBtn    = document.getElementById('btn-save-coupon-notif');
        const statusEl   = document.getElementById('coupon-notif-status');
        if (!enabledChk) return;

        // Toggle days row on checkbox change
        enabledChk.addEventListener('change', () => {
          daysRow.style.display = enabledChk.checked ? 'flex' : 'none';
        });

        // Load current settings
        try {
          const r = await apiFetch('/settings/notifications/personal');
          if (r.ok) {
            const s = await r.json();
            enabledChk.checked = !!s.coupon_notif_enabled;
            daysInput.value    = s.coupon_notif_days ?? 3;
            daysRow.style.display = enabledChk.checked ? 'flex' : 'none';
          }
        } catch {}

        // Save button
        saveBtn && saveBtn.addEventListener('click', async () => {
          setStatus(statusEl, t('settings.notif.saving'));
          try {
            const r = await apiFetch('/settings/notifications/personal', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                coupon_notif_enabled: enabledChk.checked,
                coupon_notif_days: parseInt(daysInput.value, 10) || 3,
              }),
            });
            if (!r.ok) throw new Error(t('settings.notif.saveError'));
            setStatus(statusEl, t('settings.notif.saved'));
          } catch (err) { setStatus(statusEl, err.message, true); }
        });
      }

      initCouponNotifications();

      // ── IMPORT ───────────────────────────────────────────────────
      const ioExportStatusEl = document.getElementById('io-export-status');
      const ioImportStatusEl = document.getElementById('io-import-status');
      const importFileInp = document.getElementById('import-file');
      document.getElementById('export-btn').addEventListener('click', async () => {
        if (!portfolioId) { setStatus(ioExportStatusEl, 'Портфель не выбран.', true); return; }
        setStatus(ioExportStatusEl, 'Экспорт...');
        try {
          const r = await apiFetch(`/portfolios/${portfolioId}/export`);
          if (!r.ok) throw new Error('Ошибка экспорта.');
          const blob = await r.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = 'portfolio.csv';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
          setStatus(ioExportStatusEl, t('io.exportDone'));
        } catch (err) { setStatus(ioExportStatusEl, err.message, true); }
      });

      document.getElementById('export-pdf-btn').addEventListener('click', async () => {
        if (!portfolioId) { setStatus(ioExportStatusEl, 'Портфель не выбран.', true); return; }
        setStatus(ioExportStatusEl, 'Экспорт PDF...');
        try {
          const pdfLang = (window._lang === 'en') ? 'en' : 'ru';
          const resp = await apiFetch(`/portfolios/${portfolioId}/report.pdf?lang=${pdfLang}`);
          if (!resp.ok) { setStatus(ioExportStatusEl, 'Ошибка экспорта PDF.', true); return; }
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = `portfolio_${portfolioId}.pdf`;
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          URL.revokeObjectURL(url);
          setStatus(ioExportStatusEl, t('io.exportDone'));
        } catch (e) {
          setStatus(ioExportStatusEl, 'Ошибка экспорта PDF: ' + e.message, true);
        }
      });

      document.getElementById('import-btn').addEventListener('click', async () => {
        const importPortfolioSel = document.getElementById('import-portfolio');
        const selectedPortfolioId = importPortfolioSel.value;
        if (!selectedPortfolioId) { setStatus(ioImportStatusEl, t('io.importPortfolio') + '.', true); return; }
        const file = importFileInp.files[0];
        if (!file) { setStatus(ioImportStatusEl, t('io.chooseFilePlaceholder'), true); return; }
        setStatus(ioImportStatusEl, t('io.importing'));
        const fd = new FormData(); fd.append('file', file);
        try {
          const r = await apiFetch(`/portfolios/${selectedPortfolioId}/import`, { method: 'POST', body: fd });
          const res = await r.json();
          if (!r.ok) throw new Error(res.detail || t('io.importError'));
          let msg = t('io.importedN').replace('{n}', res.added);
          if (res.errors > 0) msg += ' ' + t('io.importErrors').replace('{n}', res.errors);
          setStatus(ioImportStatusEl, msg, res.errors > 0);
          if (res.added > 0) {
            // Reload current portfolio view if we imported to it
            if (Number(selectedPortfolioId) === portfolioId) {
              syncTableFromServer().catch(() => {});
            }
          }
          importFileInp.value = '';
        } catch (err) { setStatus(ioImportStatusEl, err.message, true); }
      });

      // ── EXPORT / IMPORT ALL PORTFOLIOS ─────────────────────────────
      const ioExportAllStatusEl = document.getElementById('io-export-all-status');
      const ioImportAllStatusEl = document.getElementById('io-import-all-status');

      document.getElementById('export-all-btn').addEventListener('click', async () => {
        setStatus(ioExportAllStatusEl, t('io.exporting', 'Экспорт...'));
        try {
          const r = await apiFetch('/portfolios/export-all');
          if (!r.ok) throw new Error(t('io.exportError', 'Ошибка экспорта'));
          const blob = await r.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url; a.download = 'all_portfolios.csv'; a.click();
          URL.revokeObjectURL(url);
          setStatus(ioExportAllStatusEl, t('io.exportDone'));
        } catch (err) { setStatus(ioExportAllStatusEl, err.message, true); }
      });

      document.getElementById('import-all-btn').addEventListener('click', async () => {
        const file = document.getElementById('import-all-file').files[0];
        if (!file) { setStatus(ioImportAllStatusEl, t('io.chooseFilePlaceholder'), true); return; }
        setStatus(ioImportAllStatusEl, t('io.importing'));
        const fd = new FormData(); fd.append('file', file);
        try {
          const r = await apiFetch('/portfolios/import-all', { method: 'POST', body: fd });
          const res = await r.json();
          if (!r.ok) throw new Error(res.detail || t('io.importError'));
          let msg = t('io.importedN').replace('{n}', res.added);
          if (res.errors > 0) msg += ' ' + t('io.importErrors').replace('{n}', res.errors);
          setStatus(ioImportAllStatusEl, msg, res.errors > 0);
          if (res.added > 0) {
            await loadPortfolios();
            updatePortfolioSelector();
            syncTableFromServer().catch(() => {});
          }
          document.getElementById('import-all-file').value = '';
        } catch (err) { setStatus(ioImportAllStatusEl, err.message, true); }
      });

      // ── T-BANK IMPORT & AUTO-SYNC ─────────────────────────────────
      (function() {
        const tbankTokenInp = document.getElementById('tbank-token');
        const tbankAccountField = document.getElementById('tbank-account-field');
        const tbankAccountSel = document.getElementById('tbank-account-sel');
        const tbankLoadBtn = document.getElementById('tbank-load-btn');
        const tbankBondsBtn = document.getElementById('tbank-bonds-btn');
        const tbankAllBtn = document.getElementById('tbank-all-btn');
        const tbankPreviewBox = document.getElementById('tbank-preview-box');
        const tbankStatusEl = document.getElementById('io-tbank-status');
        const tbankAutosyncCb = document.getElementById('tbank-autosync-cb');
        const tbankSyncStatusBox = document.getElementById('tbank-sync-status-box');
        const tbankSyncNowBtn = document.getElementById('tbank-sync-now-btn');
        const tbankRemovalModal = document.getElementById('tbank-removal-modal');
        const tbankRemovalList = document.getElementById('tbank-removal-list');
        const tbankRemovalKeepBtn = document.getElementById('tbank-removal-keep-btn');
        const tbankRemovalDeleteBtn = document.getElementById('tbank-removal-delete-btn');

        let _currentSyncPortfolioId = null;
        let _pendingRemovalTickers = [];

        function _relativeTime(isoStr) {
          if (!isoStr) return '';
          const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
          if (diff < 60) return 'только что';
          if (diff < 3600) return Math.floor(diff / 60) + ' мин назад';
          if (diff < 86400) return Math.floor(diff / 3600) + ' ч назад';
          return Math.floor(diff / 86400) + ' д назад';
        }

        function _showRemovalModal(tickers, portfolioId) {
          if (!tickers || tickers.length === 0) { tbankRemovalModal.style.display = 'none'; return; }
          _pendingRemovalTickers = tickers;
          _currentSyncPortfolioId = portfolioId;
          tbankRemovalList.textContent = 'Исчезли из брокера: ' + tickers.join(', ');
          tbankRemovalModal.style.display = '';
        }

        async function loadTbankSyncStatus(portfolioId) {
          if (!portfolioId) { tbankSyncStatusBox.style.display = 'none'; tbankSyncNowBtn.style.display = 'none'; return; }
          _currentSyncPortfolioId = portfolioId;
          try {
            const r = await apiFetch('/tbank/sync/status?portfolio_id=' + portfolioId);
            if (!r.ok) { tbankSyncStatusBox.style.display = 'none'; return; }
            const s = await r.json();
            if (!s.enabled) {
              tbankAutosyncCb.checked = false;
              tbankSyncStatusBox.style.display = 'none';
              tbankSyncNowBtn.style.display = 'none';
              tbankRemovalModal.style.display = 'none';
              return;
            }
            tbankAutosyncCb.checked = true;
            let statusText = 'Токен: ' + (s.masked_token || '?') + ' · Счёт: ' + (s.account_id || '?');
            if (s.last_sync_at) statusText += ' · Синхр. ' + _relativeTime(s.last_sync_at);
            if (s.last_sync_error) statusText += ' · ⚠ ' + s.last_sync_error;
            tbankSyncStatusBox.textContent = statusText;
            tbankSyncStatusBox.style.display = '';
            tbankSyncNowBtn.style.display = '';
            if (s.pending_removal && s.pending_removal.length > 0) {
              _showRemovalModal(s.pending_removal, portfolioId);
            } else {
              tbankRemovalModal.style.display = 'none';
            }
          } catch (_) {
            tbankSyncStatusBox.style.display = 'none';
          }
        }

        function resetTbankUI() {
          tbankTokenInp.value = '';
          tbankAccountField.style.display = 'none';
          tbankPreviewBox.style.display = 'none';
          tbankBondsBtn.style.display = 'none';
          tbankAllBtn.style.display = 'none';
          tbankAutosyncCb.checked = false;
          tbankSyncStatusBox.style.display = 'none';
          tbankRemovalModal.style.display = 'none';
        }

        tbankLoadBtn.addEventListener('click', async () => {
          const token = tbankTokenInp.value.trim();
          if (!token) { setStatus(tbankStatusEl, t('io.tbankEnterToken'), true); return; }
          setStatus(tbankStatusEl, t('io.tbankLoadingAccounts'));
          tbankLoadBtn.disabled = true;
          tbankPreviewBox.style.display = 'none';
          tbankBondsBtn.style.display = 'none';
          tbankAllBtn.style.display = 'none';
          try {
            const r = await apiFetch('/tbank/accounts', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ token }),
            });
            const res = await r.json();
            if (!r.ok) throw new Error(res.detail || t('io.tbankAccountsError'));
            const accounts = res.accounts || [];
            if (accounts.length === 0) { setStatus(tbankStatusEl, t('io.tbankNoAccounts'), true); return; }
            tbankAccountSel.innerHTML = '';
            accounts.forEach(a => {
              const opt = document.createElement('option');
              opt.value = a.id;
              opt.textContent = a.name || a.id;
              tbankAccountSel.appendChild(opt);
            });
            tbankAccountField.style.display = '';
            setStatus(tbankStatusEl, t('io.tbankAccountsLoaded').replace('{n}', accounts.length));
            // Auto-preview first account
            await loadPreview();
          } catch (err) {
            setStatus(tbankStatusEl, err.message, true);
          } finally {
            tbankLoadBtn.disabled = false;
          }
        });

        tbankAccountSel.addEventListener('change', () => { loadPreview(); });

        async function loadPreview() {
          const token = tbankTokenInp.value.trim();
          const account_id = tbankAccountSel.value;
          if (!token || !account_id) return;
          setStatus(tbankStatusEl, t('io.tbankLoadingPreview'));
          tbankPreviewBox.style.display = 'none';
          tbankBondsBtn.style.display = 'none';
          tbankAllBtn.style.display = 'none';
          try {
            const r = await apiFetch('/tbank/preview', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ token, account_id }),
            });
            const res = await r.json();
            if (!r.ok) throw new Error(res.detail || t('io.tbankImportError'));
            if (res.total === 0) { setStatus(tbankStatusEl, t('io.tbankNoPositions'), true); return; }
            tbankPreviewBox.innerHTML = t('io.tbankPreview')
              .replace('{total}', res.total)
              .replace('{bonds}', res.bonds)
              .replace('{stocks}', res.stocks);
            tbankPreviewBox.style.display = '';
            if (res.bonds > 0) tbankBondsBtn.style.display = '';
            if (res.total > 0) tbankAllBtn.style.display = '';
            setStatus(tbankStatusEl, '');
          } catch (err) {
            setStatus(tbankStatusEl, err.message, true);
          }
        }

        async function doImport(bondsOnly) {
          const token = tbankTokenInp.value.trim();
          const account_id = tbankAccountSel.value;
          if (!token || !account_id) return;
          setStatus(tbankStatusEl, t('io.tbankImporting'));
          tbankBondsBtn.disabled = true;
          tbankAllBtn.disabled = true;
          try {
            const r = await apiFetch('/tbank/import', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ token, account_id, bonds_only: bondsOnly }),
            });
            const res = await r.json();
            if (!r.ok) throw new Error(res.detail || t('io.tbankImportError'));
            let msg = t('io.tbankImportSuccess').replace('{name}', res.portfolio_name).replace('{n}', res.added);
            if (res.errors > 0) msg += ' ' + t('io.importErrors').replace('{n}', res.errors);

            // If autosync is checked, enable sync on the new portfolio
            if (tbankAutosyncCb.checked) {
              try {
                const sr = await apiFetch('/tbank/sync/enable', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ portfolio_id: res.portfolio_id, token, account_id, bonds_only: bondsOnly }),
                });
                if (sr.ok) {
                  msg += ' · Автосинхронизация включена';
                }
              } catch (_) {}
            }

            setStatus(tbankStatusEl, msg, res.errors > 0);
            resetTbankUI();
            await loadPortfolios();
            updatePortfolioSelector();
            settingsLoadPortfolios();
          } catch (err) {
            setStatus(tbankStatusEl, err.message, true);
          } finally {
            tbankBondsBtn.disabled = false;
            tbankAllBtn.disabled = false;
          }
        }

        // Disable sync when checkbox unchecked (only if already configured)
        tbankAutosyncCb.addEventListener('change', async () => {
          if (!tbankAutosyncCb.checked && _currentSyncPortfolioId) {
            try {
              await apiFetch('/tbank/sync/disable', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ portfolio_id: _currentSyncPortfolioId }),
              });
              tbankSyncStatusBox.style.display = 'none';
              tbankSyncNowBtn.style.display = 'none';
            } catch (_) {}
          }
        });

        // Sync now button
        tbankSyncNowBtn.addEventListener('click', async () => {
          if (!_currentSyncPortfolioId) return;
          tbankSyncNowBtn.disabled = true;
          setStatus(tbankStatusEl, 'Синхронизация...');
          try {
            const r = await apiFetch('/tbank/sync/now', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ portfolio_id: _currentSyncPortfolioId }),
            });
            const res = await r.json();
            if (!r.ok) throw new Error(res.detail || 'Ошибка синхронизации');
            let msg = 'Синхронизировано: +' + res.added + ' новых, ' + res.updated + ' обновлено';
            if (res.skipped) msg = 'Синхронизация уже выполняется';
            setStatus(tbankStatusEl, msg);
            await loadTbankSyncStatus(_currentSyncPortfolioId);
            if (res.removed_candidates && res.removed_candidates.length > 0) {
              _showRemovalModal(res.removed_candidates, _currentSyncPortfolioId);
            }
            // Refresh table
            syncTableFromServer().catch(() => {});
          } catch (err) {
            setStatus(tbankStatusEl, err.message, true);
          } finally {
            tbankSyncNowBtn.disabled = false;
          }
        });

        // Removal modal buttons
        tbankRemovalKeepBtn.addEventListener('click', async () => {
          if (!_currentSyncPortfolioId) return;
          try {
            await apiFetch('/tbank/sync/confirm-removal', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ portfolio_id: _currentSyncPortfolioId, tickers: _pendingRemovalTickers, confirm: false }),
            });
          } catch (_) {}
          tbankRemovalModal.style.display = 'none';
          _pendingRemovalTickers = [];
        });

        tbankRemovalDeleteBtn.addEventListener('click', async () => {
          if (!_currentSyncPortfolioId) return;
          try {
            const r = await apiFetch('/tbank/sync/confirm-removal', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ portfolio_id: _currentSyncPortfolioId, tickers: _pendingRemovalTickers, confirm: true }),
            });
            const res = await r.json();
            if (r.ok) {
              setStatus(tbankStatusEl, 'Удалено ' + res.removed + ' позиций из портфеля');
              syncTableFromServer().catch(() => {});
            }
          } catch (_) {}
          tbankRemovalModal.style.display = 'none';
          _pendingRemovalTickers = [];
        });

        // Expose loadTbankSyncStatus globally for portfolio switch
        window._loadTbankSyncStatus = loadTbankSyncStatus;

        tbankBondsBtn.addEventListener('click', () => doImport(true));
        tbankAllBtn.addEventListener('click', () => doImport(false));
      })();

      // ── AUTH & PORTFOLIO MANAGEMENT ───────────────────────────────

      // Check authentication and render app
      // Advanced analytics are Pro-only. For non-Pro users, blur these cards
      // and overlay an upsell. Pro users see them untouched.
      const PRO_WIDGETS = [
        { id: 'stress-test-card', title: 'Стресс-тест по ставке ЦБ' },
        { id: 'ytm-curve-card', title: 'Рыночная доходность и YTM-кривая' },
        { id: 'swot-card', title: 'SWOT-анализ портфеля' },
        { id: 'events-card', title: 'События портфеля (30 дней)' },
        { id: 'cash-reinvest-card', title: 'Свободные средства и реинвест', forceShow: true },
      ];
      function applyProGates() {
        if (window._isPro) return;  // Pro sees everything
        PRO_WIDGETS.forEach(w => {
          const card = document.getElementById(w.id);
          if (!card || card.classList.contains('pro-locked')) return;
          if (w.forceShow) card.style.display = '';  // show (blurred) as an upsell
          card.classList.add('pro-locked');
          const ov = document.createElement('div');
          ov.className = 'pro-overlay';
          ov.innerHTML =
            '<span class="pro-badge">PRO</span>' +
            '<div class="pro-title">' + w.title + '</div>' +
            '<div class="pro-desc">Доступно в тарифе Pro — разбор рисков и доходности портфеля.</div>' +
            '<button class="btn btn-primary" style="margin-top:6px;height:32px;padding:0 16px;font-size:13px;" onclick="openProModal()">Оформить Pro</button>';
          card.appendChild(ov);
        });
      }

      // ── Pro billing (YooKassa) ───────────────────────────────────
      let _billingEnabled = false;
      async function initBilling() {
        try {
          const r = await fetch('/billing/config');
          if (!r.ok) return;
          const d = await r.json();
          _billingEnabled = !!d.enabled;
          const pm = document.getElementById('pro-price-month');
          const py = document.getElementById('pro-price-year');
          if (pm && d.price_month) pm.textContent = d.price_month + ' ₽';
          if (py && d.price_year) py.textContent = Number(d.price_year).toLocaleString('ru-RU') + ' ₽';
        } catch (e) { /* billing optional */ }
        // Handle return from payment / deep-link to buy.
        const params = new URLSearchParams(location.search);
        if (params.get('pro') === 'success') {
          toast('Оплата прошла — Pro активирован 🎉', 'success');
          history.replaceState(null, '', '/app');
          setTimeout(() => location.reload(), 1500);
        } else if (params.get('pro') === '1' && !window._isPro) {
          history.replaceState(null, '', '/app');
          openProModal();
        }
      }
      function openProModal() {
        if (!_billingEnabled) { toast('Оплата скоро будет доступна', 'warn'); return; }
        document.getElementById('pro-modal').style.display = 'flex';
      }
      function closeProModal() {
        document.getElementById('pro-modal').style.display = 'none';
      }
      async function buyPro(plan, btn) {
        const status = document.getElementById('pro-modal-status');
        if (btn) btn.disabled = true;
        if (status) status.textContent = 'Создаём платёж…';
        try {
          const r = await apiFetch('/billing/create', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ plan }),
          });
          const d = await r.json();
          if (!r.ok || !d.confirmation_url) throw new Error(d.detail || 'Ошибка');
          location.href = d.confirmation_url;  // redirect to YooKassa
        } catch (e) {
          if (status) status.textContent = 'Не удалось создать платёж. Попробуйте позже.';
          if (btn) btn.disabled = false;
        }
      }

      async function loadDonateLink() {
        try {
          const r = await fetch('/donate-info');
          if (!r.ok) return;
          const d = await r.json();
          if (!d.enabled) return;
          const link = document.getElementById('donate-link');
          const sep = document.getElementById('donate-sep');
          if (link) {
            link.href = d.url || d.sbp_url;
            link.style.display = '';
            if (sep) sep.style.display = '';
          }
        } catch(e) { /* donate is optional */ }
      }

      async function initAuth() {
        const token = localStorage.getItem('mvp_auth_token');
        const username = localStorage.getItem('mvp_username');
        const savedPortfolioId = localStorage.getItem('mvp_active_portfolio_id');

        if (!token) {
          showLoginScreen();
          return;
        }

        // Try to verify token and load app
        try {
          const res = await apiFetch('/auth/me');
          if (res.status === 401 || res.status === 403 || !res.ok) {
            clearUserState();
            showLoginScreen();
            return;
          }
          const meData = await res.json();
          setupAdminNav(meData.is_admin || false);
          window._isPro = !!meData.is_pro;
          if (window._isPro) {
            // AI-анализ портфеля временно скрыт на фронте (раскомментировать для возврата):
            // const aiCard = document.getElementById('ai-analysis-card');
            // if (aiCard) aiCard.style.display = '';
            const taxCard = document.getElementById('tax-card');
            if (taxCard) taxCard.style.display = '';
            // «Bond AI Pro» badge replaces the version chip for Pro users.
            const proBadge = document.getElementById('brand-pro-badge');
            const verBadge = document.getElementById('brand-version-badge');
            if (proBadge) {
              proBadge.style.display = '';
              proBadge.style.cursor = 'help';
              if (meData.pro_until) {
                const d = String(meData.pro_until).substring(0, 10);
                const [y, m, day] = d.split('-');
                proBadge.title = `Pro активен до ${day}.${m}.${y}`;
              } else {
                proBadge.title = 'Pro активен бессрочно';
              }
            }
            if (verBadge) verBadge.style.display = 'none';
          }
          applyProGates();
          initBilling();
          loadDonateLink();
          showDashboard();
          await loadPortfolios();
          if (window.checkTgResetBanner) window.checkTgResetBanner();
          // Calculate total value of all portfolios (non-blocking)
          if (portfolios.length > 1) {
            calculateAllPortfoliosTotal().then(val => {
              allPortfoliosTotalValue = val;
              const summary = calculateSummary(tableRows);
              updateStatCards(summary);
            }).catch(() => {});
          }
          // Set portfolioId from saved value or first portfolio
          if (savedPortfolioId && portfolios.some(p => p.id === Number(savedPortfolioId))) {
            portfolioId = Number(savedPortfolioId);
          } else if (portfolios.length > 0) {
            portfolioId = portfolios[0].id;
            localStorage.setItem('mvp_active_portfolio_id', String(portfolioId));
          }
          if (isAllMode) {
            updatePortfolioSelector();
            const titleEl = document.getElementById('topbar-portfolio-name');
            if (titleEl) { titleEl.textContent = 'Все портфели'; titleEl.style.display = ''; }
            // Aggregate same securities across brokers by default; show the toggle.
            window._allGrouped = true;
            const grpToggle = document.getElementById('all-group-toggle');
            if (grpToggle) grpToggle.style.display = 'inline-flex';
            const ptCard = document.getElementById('portfolio-table-card');
            if (ptCard) { const tt = ptCard.querySelector('.card-title span'); if (tt) tt.textContent = 'Бумаги (все портфели)'; }
            await syncTableFromServer();
          } else if (portfolioId) {
            updatePortfolioSelector();
            await syncTableFromServer();
            // Показать онбординг для новых пользователей
            setTimeout(() => initOnboarding(), 1500);
          } else {
            // No portfolio yet (e.g. freshly registered user) — still run the
            // onboarding wizard so they can create their first portfolio.
            updatePortfolioSelector();
            setTimeout(() => initOnboarding(), 400);
          }
        } catch (err) {
          showLoginScreen();
        }
      }

      // Clear per-user state so switching accounts in the same browser doesn't
      // carry over the previous user's active portfolio / mode. Device-level
      // prefs (theme, lang, mobile mode, last username) are intentionally kept.
      function clearUserState() {
        ['mvp_auth_token', 'mvp_username', 'mvp_active_portfolio',
         'mvp_active_portfolio_id', 'mvp_profit_mode2', 'mvp_onboarding_done',
         'mvp_start_onboarding'].forEach(k => localStorage.removeItem(k));
      }

      function showLoginScreen() {
        document.querySelector('.container').style.display = 'none';
        let loginHtml = document.getElementById('login-screen-template');
        if (!loginHtml) {
          const div = document.createElement('div');
          div.id = 'login-screen-template';
          div.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;width:100%;height:100%;overflow-y:auto;';

          const shakeCard = () => {
            const card = document.getElementById('auth-card');
            if (!card) return;
            card.classList.remove('l-shake');
            void card.offsetWidth;
            card.classList.add('l-shake');
          };

          const setAuthMsg = (text, type) => {
            const el = document.getElementById('auth-message');
            if (!el) return;
            el.className = 'auth-msg' + (type ? ' ' + type : '');
            el.textContent = text;
          };

          const handleLogin = async () => {
            const username = document.getElementById('auth-username').value.trim();
            const password = document.getElementById('auth-password').value.trim();
            const loginBtn = document.getElementById('btn-login');
            if (!username || !password) { shakeCard(); setAuthMsg(t('auth.enterCreds'), 'error'); return; }
            loginBtn.disabled = true;
            loginBtn.innerHTML = '<span class="btn-spinner"></span>' + t('auth.checking');
            setAuthMsg('', '');
            try {
              const res = await fetch('/auth/login', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
              });
              const data = await res.json();
              if (res.ok) {
                clearUserState();  // drop any previous user's portfolio/mode
                localStorage.setItem('mvp_auth_token', data.access_token);
                localStorage.setItem('mvp_username', data.username);
                localStorage.setItem('mvp_last_username', username);
                loginBtn.innerHTML = '<span class="btn-check">✓</span>' + t('auth.signing');
                loginBtn.style.background = 'linear-gradient(135deg,#16a34a,#15803d)';
                setAuthMsg(t('auth.success'), 'success');
                setTimeout(() => location.reload(), 700);
              } else {
                shakeCard();
                setAuthMsg(data.detail || t('auth.loginError'), 'error');
                loginBtn.disabled = false;
                loginBtn.innerHTML = t('login.loginBtn');
              }
            } catch (err) {
              shakeCard();
              setAuthMsg(t('auth.networkError') + ': ' + (err.message || ''), 'error');
              loginBtn.disabled = false;
              loginBtn.innerHTML = t('login.loginBtn');
            }
          };

          const handleRegister = async () => {
            const username = document.getElementById('auth-username').value.trim();
            const password = document.getElementById('auth-password').value.trim();
            const regBtn = document.getElementById('btn-register');
            if (!username || !password) { shakeCard(); setAuthMsg(t('auth.enterCreds'), 'error'); return; }
            if (username.length < 3) { shakeCard(); setAuthMsg(t('auth.minLogin'), 'error'); return; }
            if (password.length < 6) { shakeCard(); setAuthMsg(t('auth.minPassword'), 'error'); return; }
            regBtn.disabled = true;
            regBtn.textContent = t('auth.registering');
            setAuthMsg('', '');
            try {
              const res = await fetch('/auth/register', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
              });
              const data = await res.json();
              if (res.ok) {
                clearUserState();  // a brand-new account must not inherit prior state
                localStorage.setItem('mvp_auth_token', data.access_token);
                localStorage.setItem('mvp_username', data.username);
                localStorage.setItem('mvp_last_username', username);
                localStorage.setItem('mvp_start_onboarding', '1');
                regBtn.textContent = t('auth.registered');
                setAuthMsg(t('auth.accountCreated'), 'success');
                setTimeout(() => location.reload(), 700);
              } else {
                shakeCard();
                setAuthMsg((typeof data === 'object' && data.detail) ? String(data.detail) : t('auth.registerError'), 'error');
                regBtn.disabled = false;
                regBtn.textContent = t('login.registerBtn');
              }
            } catch (err) {
              shakeCard();
              setAuthMsg(t('auth.networkError') + ': ' + (err.message || ''), 'error');
              regBtn.disabled = false;
              regBtn.textContent = t('login.registerBtn');
            }
          };

          const savedUsername = localStorage.getItem('mvp_last_username') || '';

          div.innerHTML = `
            <style>
              @property --ba { syntax: '<angle>'; initial-value: 0deg; inherits: false; }
              @keyframes orbDrift1 { 0%,100%{transform:translate(0,0) scale(1)} 33%{transform:translate(60px,-40px) scale(1.08)} 66%{transform:translate(-30px,50px) scale(.95)} }
              @keyframes orbDrift2 { 0%,100%{transform:translate(0,0) scale(1)} 33%{transform:translate(-50px,60px) scale(1.06)} 66%{transform:translate(40px,-30px) scale(.97)} }
              @keyframes orbDrift3 { 0%,100%{transform:translate(0,0) scale(1)} 50%{transform:translate(30px,40px) scale(1.04)} }
              @keyframes orbDrift4 { 0%,100%{transform:translate(0,0) scale(1)} 50%{transform:translate(-40px,-20px) scale(1.05)} }
              @keyframes borderSpin { to { --ba: 360deg; } }
              @keyframes cardIn { from{opacity:0;transform:translateY(20px) scale(.97)} to{opacity:1;transform:none} }
              @keyframes slideInLeft { from{opacity:0;transform:translateX(-24px)} to{opacity:1;transform:none} }
              @keyframes lShake { 0%,100%{transform:translateX(0)} 20%{transform:translateX(-6px)} 40%{transform:translateX(6px)} 60%{transform:translateX(-4px)} 80%{transform:translateX(4px)} }
              @keyframes successPop { 0%{transform:scale(1)} 40%{transform:scale(1.06)} 100%{transform:scale(1)} }
              @keyframes lSpin { to{transform:rotate(360deg)} }
              .l-bg {
                position: fixed; inset: 0; overflow: hidden;
                background: radial-gradient(ellipse 80% 60% at 50% -10%, #0f2256 0%, #020817 60%);
              }
              .l-orb {
                position: absolute; border-radius: 50%;
                filter: blur(50px); pointer-events: none; will-change: transform;
              }
              .l-orb-1 { width:600px;height:600px;top:-150px;left:-120px;background:radial-gradient(circle,rgba(37,99,235,.7) 0%,rgba(37,99,235,.25) 45%,transparent 70%);animation:orbDrift1 11s ease-in-out infinite; }
              .l-orb-2 { width:500px;height:500px;bottom:-100px;right:-100px;background:radial-gradient(circle,rgba(99,102,241,.65) 0%,rgba(99,102,241,.18) 45%,transparent 70%);animation:orbDrift2 14s ease-in-out infinite; }
              .l-orb-3 { width:360px;height:360px;top:35%;left:50%;background:radial-gradient(circle,rgba(14,165,233,.55) 0%,rgba(14,165,233,.15) 45%,transparent 70%);animation:orbDrift3 9s ease-in-out infinite; }
              .l-orb-4 { width:320px;height:320px;top:15%;right:20%;background:radial-gradient(circle,rgba(168,85,247,.5) 0%,rgba(168,85,247,.12) 45%,transparent 70%);animation:orbDrift4 12s ease-in-out infinite; }
              .l-grid {
                position: fixed; inset: 0; pointer-events: none;
                background-image: linear-gradient(rgba(148,163,184,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.04) 1px,transparent 1px);
                background-size: 44px 44px;
              }
              .l-wrap {
                position: relative; min-height: 100vh;
                display: flex; flex-direction: column;
                font-family: Inter, system-ui, sans-serif;
              }
              .l-hero {
                flex: 1; display: flex; align-items: center; justify-content: center;
                gap: 72px; padding: 60px 40px; flex-wrap: wrap;
              }
              .l-left { max-width: 460px; }
              .l-brand {
                display: flex; align-items: center; gap: 10px;
                margin-bottom: 44px;
                animation: slideInLeft .5s ease-out both;
              }
              .l-brand-mark {
                width: 34px; height: 34px; background: linear-gradient(135deg,#2563eb,#4f46e5);
                border-radius: 8px; display:flex;align-items:center;justify-content:center;
                font-weight: 800; font-size: 16px; color: #fff;
                box-shadow: 0 4px 12px rgba(37,99,235,.4);
              }
              .l-brand-name { font-weight: 700; font-size: 15px; color: #f1f5f9; letter-spacing:-.3px; }
              .l-brand-badge {
                background: rgba(30,58,138,.7); color: #93c5fd;
                font-size: 10px; font-weight: 600; padding: 2px 7px;
                border-radius: 4px; border: 1px solid rgba(59,130,246,.25);
                backdrop-filter: blur(4px);
              }
              .l-h1 {
                font-size: clamp(28px,4vw,46px); font-weight: 800;
                color: #f8fafc; line-height: 1.12; letter-spacing: -1.5px;
                margin: 0 0 18px 0;
                animation: slideInLeft .5s ease-out .08s both;
              }
              .l-h1 .grad {
                background: linear-gradient(135deg,#60a5fa 0%,#818cf8 50%,#a78bfa 100%);
                -webkit-background-clip: text; -webkit-text-fill-color: transparent;
              }
              .l-sub {
                font-size: 15px; color: #94a3b8; line-height: 1.7;
                margin: 0 0 36px 0;
                animation: slideInLeft .5s ease-out .16s both;
              }
              .l-feats {
                list-style:none;margin:0;padding:0;
                display:flex;flex-direction:column;gap:12px;
              }
              .l-feats li {
                display:flex;align-items:flex-start;gap:12px;
                font-size:13.5px;color:#cbd5e1;
                animation: slideInLeft .5s ease-out both;
              }
              .l-feats li:nth-child(1){animation-delay:.22s}
              .l-feats li:nth-child(2){animation-delay:.28s}
              .l-feats li:nth-child(3){animation-delay:.34s}
              .l-feats li:nth-child(4){animation-delay:.40s}
              .l-feats li:nth-child(5){animation-delay:.46s}
              .l-feat-icon {
                flex-shrink:0;width:22px;height:22px;
                background:rgba(30,58,138,.6);border:1px solid rgba(59,130,246,.2);
                border-radius:6px;display:flex;align-items:center;justify-content:center;
                font-size:11px;margin-top:1px;
              }
              .l-right { flex-shrink: 0; animation: cardIn .5s cubic-bezier(.34,1.2,.64,1) .1s both; }
              .auth-card-wrap {
                position: relative; padding: 2px; border-radius: 18px;
                background: conic-gradient(from var(--ba), #334155 0%, #3b82f6 20%, #818cf8 40%, #334155 60%);
                animation: borderSpin 4s linear infinite;
                box-shadow: 0 0 40px rgba(37,99,235,.18), 0 25px 50px rgba(0,0,0,.5);
              }
              #auth-card {
                background: rgba(15,23,42,.92);
                backdrop-filter: blur(20px);
                border-radius: 16px;
                padding: 36px 32px;
                width: 300px;
              }
              #auth-card.l-shake { animation: lShake .4s ease-out; }
              .auth-title {
                font-size: 18px; font-weight: 700; color: #f1f5f9;
                margin: 0 0 4px 0; text-align: center;
              }
              .auth-desc {
                font-size: 12.5px; color: #64748b;
                text-align: center; margin: 0 0 22px 0;
              }
              .fl-wrap { position: relative; }
              .fl-input {
                width: 100%; padding: 20px 12px 7px;
                background: rgba(30,41,59,.8); border: 1px solid #334155;
                border-radius: 8px; color: #f1f5f9;
                font-size: 13px; font-family: inherit;
                outline: none; box-sizing: border-box;
                transition: border-color .15s, background .15s;
              }
              .fl-input:focus { border-color: #3b82f6; background: rgba(15,23,42,.9); }
              .fl-label {
                position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
                font-size: 13px; color: #64748b; pointer-events: none;
                transition: top .15s, font-size .15s, color .15s, transform .15s;
                background: transparent;
              }
              .fl-input:focus + .fl-label,
              .fl-input.has-val + .fl-label {
                top: 8px; transform: none; font-size: 10px; color: #3b82f6;
              }
              .auth-btn-primary {
                width: 100%; padding: 11px;
                background: linear-gradient(135deg,#2563eb,#4f46e5);
                color: #fff; border: none; border-radius: 8px; cursor: pointer;
                font-weight: 600; font-size: 14px; font-family: inherit;
                transition: opacity .15s, transform .1s;
                position: relative; overflow: hidden;
                display: flex; align-items: center; justify-content: center; gap: 6px;
              }
              .auth-btn-primary:hover:not(:disabled) { opacity: .9; transform: translateY(-1px); }
              .auth-btn-primary:disabled { opacity: .7; cursor: default; transform: none; }
              .auth-divider {
                display:flex;align-items:center;gap:12px;
                color:#475569;font-size:11.5px;
              }
              .auth-divider::before,.auth-divider::after { content:'';flex:1;height:1px;background:#1e293b; }
              .auth-btn-secondary {
                width:100%;padding:11px;
                background:transparent;color:#94a3b8;
                border:1px solid #334155;border-radius:8px;cursor:pointer;
                font-weight:500;font-size:13.5px;font-family:inherit;
                transition:border-color .15s,color .15s,background .15s;
              }
              .auth-btn-secondary:hover:not(:disabled) { border-color:#3b82f6;color:#f1f5f9;background:rgba(59,130,246,.06); }
              .auth-msg {
                text-align:center;font-size:12px;min-height:18px;margin:12px 0 0;
                color:#64748b;transition:color .15s;
              }
              .auth-msg.error { color:#f87171; }
              .auth-msg.success { color:#4ade80; }
              .btn-spinner {
                width:13px;height:13px;border-radius:50%;
                border:2px solid rgba(255,255,255,.25);border-top-color:#fff;
                animation:lSpin .6s linear infinite;display:inline-block;flex-shrink:0;
              }
              .btn-check { font-size:14px; }
              .l-footer {
                border-top: 1px solid rgba(30,41,59,.8);
                padding: 18px 40px;
                display:flex;align-items:center;justify-content:space-between;
                flex-wrap:wrap;gap:10px;
                backdrop-filter: blur(4px);
              }
              .l-footer p { font-size:12px;color:#475569;margin:0; }
              .l-footer a { color:#3b82f6;text-decoration:none; }
              .l-footer a:hover { text-decoration:underline; }
            </style>
            <!-- Subtle language switcher -->
            <div style="position:fixed;top:14px;right:18px;z-index:10002;display:flex;gap:4px;">
              <button class="login-lang-btn" data-lang="ru" style="font-size:11px;font-weight:600;padding:3px 8px;border:1px solid rgba(148,163,184,.25);border-radius:4px;background:rgba(30,41,59,.6);color:rgba(241,245,249,.5);cursor:pointer;backdrop-filter:blur(4px);transition:.15s;" onmouseover="this.style.color='#f1f5f9'" onmouseout="this.style.color=this.dataset.lang===document.documentElement.getAttribute('data-lang')?'#f1f5f9':'rgba(241,245,249,.5)'">RU</button>
              <button class="login-lang-btn" data-lang="en" style="font-size:11px;font-weight:600;padding:3px 8px;border:1px solid rgba(148,163,184,.25);border-radius:4px;background:rgba(30,41,59,.6);color:rgba(241,245,249,.5);cursor:pointer;backdrop-filter:blur(4px);transition:.15s;" onmouseover="this.style.color='#f1f5f9'" onmouseout="this.style.color=this.dataset.lang===document.documentElement.getAttribute('data-lang')?'#f1f5f9':'rgba(241,245,249,.5)'">EN</button>
            </div>
            <div class="l-bg">
              <div class="l-orb l-orb-1"></div>
              <div class="l-orb l-orb-2"></div>
              <div class="l-orb l-orb-3"></div>
              <div class="l-orb l-orb-4"></div>
            </div>
            <div class="l-grid"></div>
            <div class="l-wrap">
              <div class="l-hero">
                <div class="l-left">
                  <div class="l-brand">
                    <div class="l-brand-mark">B</div>
                    <span class="l-brand-name">Bond AI</span><span style="font-size:9px;font-weight:600;padding:1px 5px;margin-left:6px;border-radius:4px;background:rgba(59,130,246,.12);color:#2563eb;border:1px solid rgba(59,130,246,.25);letter-spacing:.3px;vertical-align:middle;">v3.0</span>
                  </div>
                  <h1 class="l-h1" data-i18n-html="login.hero">Умный подбор<br><span class="grad">облигаций</span><br>с помощью ИИ</h1>
                  <p class="l-sub" data-i18n="login.sub">Отслеживайте акции и облигации в одном месте. ИИ подберёт облигации под ваш риск-профиль и желаемую доходность.</p>
                  <ul class="l-feats">
                    <li><span class="l-feat-icon">🤖</span><span data-i18n="login.feat1">ИИ-подбор облигаций по доходности и уровню риска</span></li>
                    <li><span class="l-feat-icon">📊</span><span data-i18n="login.feat2">Актуальные цены и доходность по данным MOEX</span></li>
                    <li><span class="l-feat-icon">💰</span><span data-i18n="login.feat3">Расчёт купонного дохода с учётом праздников и оферт</span></li>
                    <li><span class="l-feat-icon">🏅</span><span data-i18n="login.feat4">Кредитные рейтинги эмитентов облигаций</span></li>
                    <li><span class="l-feat-icon">🔗</span><span data-i18n="login.feat5">Публичные ссылки для совместного просмотра портфеля</span></li>
                  </ul>
                </div>
                <div class="l-right">
                  <div class="auth-card-wrap">
                    <div id="auth-card">
                      <p class="auth-title" data-i18n="login.welcome">Добро пожаловать</p>
                      <p class="auth-desc" data-i18n="login.desc">Войдите или создайте аккаунт</p>
                      <div style="display:flex;flex-direction:column;gap:18px;">
                        <div class="fl-wrap">
                          <input type="text" id="auth-username" class="fl-input${savedUsername ? ' has-val' : ''}" value="${savedUsername}" autocomplete="username">
                          <label class="fl-label" for="auth-username" data-i18n="auth.username">Имя пользователя</label>
                        </div>
                        <div class="fl-wrap">
                          <input type="password" id="auth-password" class="fl-input" autocomplete="current-password">
                          <label class="fl-label" for="auth-password" data-i18n="login.password">Пароль</label>
                        </div>
                        <button id="btn-login" class="auth-btn-primary" data-i18n="login.loginBtn">Войти</button>
                        <div class="auth-divider" data-i18n="login.or">или</div>
                        <button id="btn-register" class="auth-btn-secondary" data-i18n="login.registerBtn">Зарегистрироваться</button>
                        <button id="btn-forgot" class="auth-btn-secondary" style="margin-top:4px;font-size:12px;opacity:.8;" data-i18n="login.forgotBtn">Забыли пароль?</button>
                      </div>
                      <p id="auth-message" class="auth-msg"></p>
                      <!-- Forgot password form (hidden by default) -->
                      <div id="forgot-form" style="display:none;margin-top:16px;display:none;">
                        <div class="auth-divider" style="margin-bottom:12px;" data-i18n="auth.forgotTitle">Восстановление пароля</div>
                        <div class="fl-wrap" style="margin-bottom:10px;">
                          <input type="text" id="forgot-username" class="fl-input" autocomplete="username">
                          <label class="fl-label" for="forgot-username" data-i18n="auth.username">Имя пользователя</label>
                        </div>
                        <button id="btn-send-code" class="auth-btn-primary" style="margin-bottom:10px;" data-i18n="auth.sendCode">Отправить код</button>
                        <div id="forgot-code-wrap" style="display:none;">
                          <div class="fl-wrap" style="margin-bottom:10px;">
                            <input type="text" id="forgot-code" class="fl-input" maxlength="6" inputmode="numeric" autocomplete="one-time-code">
                            <label class="fl-label" for="forgot-code" data-i18n="auth.codeLabel">Код из сообщения</label>
                          </div>
                          <div class="fl-wrap" style="margin-bottom:10px;">
                            <input type="password" id="forgot-newpw" class="fl-input">
                            <label class="fl-label" for="forgot-newpw" data-i18n="auth.newPassword">Новый пароль</label>
                          </div>
                          <button id="btn-do-reset" class="auth-btn-primary" data-i18n="auth.doReset">Сменить пароль</button>
                        </div>
                        <p id="forgot-message" class="auth-msg"></p>
                        <button id="btn-back-login" class="auth-btn-secondary" style="margin-top:8px;font-size:12px;" data-i18n="auth.backToLogin">← Вернуться ко входу</button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              <div class="l-footer">
                <p>© ${new Date().getFullYear()} Bond AI · <span data-i18n="footer.disclaimer">Не является инвестиционной рекомендацией</span></p>
                <p><a href="https://tbank.ru/baf/47BkLWQ33GF" target="_blank" rel="noopener" data-i18n="footer.broker">Откройте счёт и получите акции на 2000 ₽ →</a></p>
              </div>
            </div>
          `;
          document.body.insertBefore(div, document.body.firstChild);
          loginHtml = div;

          setTimeout(() => {
            const btn1 = document.getElementById('btn-login');
            const btn2 = document.getElementById('btn-register');
            const usernameInput = document.getElementById('auth-username');
            const passwordInput = document.getElementById('auth-password');
            if (btn1) btn1.onclick = handleLogin;
            if (btn2) btn2.onclick = handleRegister;

            // Auto-focus register form if ?auth=register in URL
            const _urlAuth = new URLSearchParams(window.location.search).get('auth');
            if (_urlAuth === 'register') {
              // Highlight register button and show hint
              if (btn2) {
                btn2.style.outline = '2px solid #3b82f6';
                btn2.style.outlineOffset = '2px';
                setTimeout(() => { if (btn2) { btn2.style.outline = ''; btn2.style.outlineOffset = ''; } }, 2000);
              }
              setAuthMsg(t('auth.createAccountHint', 'Введите логин и пароль, затем нажмите «Зарегистрироваться»'), 'success');
              if (usernameInput) usernameInput.focus();
            } else if (usernameInput) usernameInput.focus();
            // Float label logic
            [usernameInput, passwordInput].forEach(inp => {
              if (!inp) return;
              const update = () => inp.classList.toggle('has-val', !!inp.value);
              inp.addEventListener('input', update);
              inp.addEventListener('change', update);
              update();
            });
            if (passwordInput) {
              passwordInput.onkeydown = (e) => {
                if (e.key === 'Enter') handleLogin();
              };
            }

            // Float labels for forgot form
            [document.getElementById('forgot-username'), document.getElementById('forgot-code'), document.getElementById('forgot-newpw')].forEach(inp => {
              if (!inp) return;
              const upd = () => inp.classList.toggle('has-val', !!inp.value);
              inp.addEventListener('input', upd); inp.addEventListener('change', upd); upd();
            });

            // Forgot password toggle
            const btnForgot = document.getElementById('btn-forgot');
            const forgotForm = document.getElementById('forgot-form');
            const authMainBtns = document.querySelector('#auth-card > div');
            const btnBackLogin = document.getElementById('btn-back-login');
            if (btnForgot) btnForgot.onclick = () => {
              authMainBtns.style.display = 'none';
              btnForgot.style.display = 'none';
              forgotForm.style.display = 'block';
            };
            if (btnBackLogin) btnBackLogin.onclick = () => {
              authMainBtns.style.display = '';
              btnForgot.style.display = '';
              forgotForm.style.display = 'none';
              document.getElementById('forgot-code-wrap').style.display = 'none';
              document.getElementById('forgot-message').textContent = '';
            };

            // Send reset code
            const btnSendCode = document.getElementById('btn-send-code');
            if (btnSendCode) btnSendCode.onclick = async () => {
              const username = document.getElementById('forgot-username').value.trim();
              const msg = document.getElementById('forgot-message');
              if (!username) { msg.textContent = t('auth.enterUsername'); msg.className = 'auth-msg error'; return; }
              btnSendCode.disabled = true;
              btnSendCode.textContent = t('auth.sending');
              try {
                const r = await fetch('/auth/forgot-password', {
                  method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({ username, lang: window._lang || 'ru' })
                });
                await r.json().catch(() => ({}));
                msg.textContent = window._lang === 'en'
                  ? 'If an account with that username exists and has a recovery channel (Telegram / Email), a code has been sent. Enter it below.'
                  : 'Если пользователь существует и у него есть канал восстановления (Telegram / Email), код отправлен. Введите его ниже.';
                msg.className = 'auth-msg success';
                document.getElementById('forgot-code-wrap').style.display = 'block';
              } catch(e) {
                msg.textContent = 'Ошибка соединения';
                msg.className = 'auth-msg error';
              }
              btnSendCode.disabled = false;
              btnSendCode.textContent = t('auth.sendCode');
            };

            // Apply reset code
            const btnDoReset = document.getElementById('btn-do-reset');
            if (btnDoReset) btnDoReset.onclick = async () => {
              const code = document.getElementById('forgot-code').value.trim();
              const pw = document.getElementById('forgot-newpw').value;
              const msg = document.getElementById('forgot-message');
              if (code.length !== 6 || !/^\d+$/.test(code)) { msg.textContent = t('auth.codeDigits'); msg.className = 'auth-msg error'; return; }
              if (pw.length < 6) { msg.textContent = t('auth.minPassword'); msg.className = 'auth-msg error'; return; }
              btnDoReset.disabled = true;
              try {
                const r = await fetch('/auth/reset-password', {
                  method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({ code, new_password: pw })
                });
                if (r.status === 204) {
                  msg.textContent = t('auth.passwordChanged');
                  msg.className = 'auth-msg success';
                  document.getElementById('forgot-code-wrap').style.display = 'none';
                  setTimeout(() => btnBackLogin.onclick(), 2500);
                } else {
                  const d = await r.json().catch(() => ({}));
                  msg.textContent = d.detail || t('auth.invalidCode');
                  msg.className = 'auth-msg error';
                }
              } catch(e) { msg.textContent = t('auth.networkError'); msg.className = 'auth-msg error'; }
              btnDoReset.disabled = false;
            };

            // Apply language (safe here — TRANSLATIONS is initialized by now)
            if (typeof window._lang !== 'undefined') applyLang(window._lang);

            // Lang switcher buttons on login page
            document.querySelectorAll('.login-lang-btn').forEach(b => {
              b.onclick = () => { setLang(b.dataset.lang); };
            });
          }, 10);
        }
        loginHtml.style.display = 'block';
      }

      function showDashboard() {
        document.querySelector('.container').style.display = 'block';
        const loginScreen = document.getElementById('login-screen-template');
        if (loginScreen) loginScreen.style.display = 'none';

        // Setup portfolio selector
        setupPortfolioSelector();

        // Add share, lang switcher and logout buttons if not in shared view
        if (!isReadOnly && !document.getElementById('btn-logout')) {
          const topbarNav = document.querySelector('.topbar-nav');
          const topbarRight = document.getElementById('topbar-right');

          // Share button (in nav)
          if (topbarNav && !document.getElementById('btn-share-portfolio')) {
            const shareBtn = document.createElement('button');
            shareBtn.id = 'btn-share-portfolio';
            shareBtn.className = 'nav-btn';
            shareBtn.setAttribute('data-i18n', 'nav.share');
            shareBtn.textContent = 'Поделиться';
            shareBtn.onclick = openShareModal;
            topbarNav.appendChild(shareBtn);
          }

          if (topbarRight) {
            // Guide (?) button
            const guideBtn = document.createElement('button');
            guideBtn.id = 'btn-guide';
            guideBtn.title = 'Справочник инвестора';
            guideBtn.style.cssText = 'width:32px;height:32px;display:flex;align-items:center;justify-content:center;background:transparent;border:1px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;color:var(--blue-400);transition:.15s;padding:0;font-size:15px;font-weight:700;';
            guideBtn.innerHTML = '?';
            guideBtn.onmouseover = () => { guideBtn.style.borderColor='var(--blue-500)'; guideBtn.style.background='rgba(96,165,250,.08)'; };
            guideBtn.onmouseout  = () => { guideBtn.style.borderColor='var(--border)'; guideBtn.style.background='transparent'; };
            guideBtn.onclick = () => openGuideModal();
            topbarRight.appendChild(guideBtn);

            // Theme toggle button
            const themeBtn = document.createElement('button');
            themeBtn.id = 'btn-theme-toggle';
            themeBtn.title = 'Светлая тема';
            themeBtn.style.cssText = 'width:32px;height:32px;display:flex;align-items:center;justify-content:center;background:transparent;border:1px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;color:var(--text-secondary);transition:.15s;padding:0;';
            themeBtn.onmouseover = () => themeBtn.style.borderColor = 'var(--border-strong)';
            themeBtn.onmouseout  = () => themeBtn.style.borderColor = 'var(--border)';
            themeBtn.onclick = toggleTheme;
            topbarRight.appendChild(themeBtn);
            updateThemeBtn();

            // Language switcher buttons
            const langWrap = document.createElement('div');
            langWrap.style.display = 'flex';
            langWrap.style.gap = '4px';
            langWrap.innerHTML = `
              <button class="lang-btn${window._lang==='ru'?' active':''}" onclick="setLang('ru')">RU</button>
              <button class="lang-btn${window._lang==='en'?' active':''}" onclick="setLang('en')">EN</button>
            `;
            topbarRight.appendChild(langWrap);

            // Logout button
            const btn = document.createElement('button');
            btn.id = 'btn-logout';
            btn.setAttribute('data-i18n', 'nav.logout');
            btn.textContent = 'Выйти';
            btn.style.cssText = 'padding:4px 12px;background:#dc2626;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;';
            btn.onclick = () => {
              clearUserState();
              location.reload();
            };
            topbarRight.appendChild(btn);
          }
        }

        // Init mobile mode
        initMobileMode();
        // Re-apply current language (in case DOM was updated)
        // Guard: TRANSLATIONS may not be defined yet if called from share IIFE before line 4534
        try { applyLang(window._lang); } catch(e) { /* TRANSLATIONS not yet initialized */ }
      }

      async function loadPortfolios() {
        try {
          const res = await apiFetch('/portfolios');
          const data = await res.json();
          portfolios = data.portfolios || [];
        } catch (err) {
          console.error('Failed to load portfolios:', err);
        }
      }

      // Cached & coalesced: one /all/totals call replaces N parallel /table calls.
      // TTL keeps the stat-card "Все портфели" fresh enough without hammering the API
      // when the user switches panels or syncs back-to-back.
      let _allTotalsCache = { value: null, at: 0, inflight: null };
      const ALL_TOTALS_TTL_MS = 30 * 1000;

      async function calculateAllPortfoliosTotal() {
        const now = Date.now();
        if (_allTotalsCache.value !== null && now - _allTotalsCache.at < ALL_TOTALS_TTL_MS) {
          return _allTotalsCache.value;
        }
        if (_allTotalsCache.inflight) return _allTotalsCache.inflight;
        _allTotalsCache.inflight = (async () => {
          try {
            const res = await apiFetch('/portfolios/all/totals');
            if (!res.ok) return _allTotalsCache.value ?? 0;
            const data = await res.json();
            const v = Number(data.grand_total_value || 0);
            _allTotalsCache = { value: v, at: Date.now(), inflight: null };
            return v;
          } catch (err) {
            console.error('Failed to load /all/totals:', err);
            _allTotalsCache.inflight = null;
            return _allTotalsCache.value ?? 0;
          }
        })();
        return _allTotalsCache.inflight;
      }

      function updatePortfolioSelector() {
        const selector = document.getElementById('portfolio-selector');
        if (!selector) return;
        selector.innerHTML = '';
        if (portfolios.length > 1 || isAllMode) {
          const allOpt = document.createElement('option');
          allOpt.value = '__all__';
          allOpt.textContent = '📊 Все портфели';
          allOpt.selected = isAllMode;
          selector.appendChild(allOpt);
        }
        for (const p of portfolios) {
          const option = document.createElement('option');
          option.value = String(p.id);
          option.textContent = p.name;
          option.selected = !isAllMode && p.id === portfolioId;
          selector.appendChild(option);
        }
        // «+ Добавить портфель» в конце списка
        const addOpt = document.createElement('option');
        addOpt.value = '__new__';
        addOpt.textContent = '+ Добавить портфель';
        addOpt.style.color = 'var(--blue-500, #3b82f6)';
        addOpt.style.fontStyle = 'italic';
        selector.appendChild(addOpt);
        // Update portfolio name in topbar
        const nameEl = document.getElementById('topbar-portfolio-name');
        const pfActionsEl = document.getElementById('topbar-portfolio-actions');
        let showPfActions = false;
        if (nameEl) {
          if (isAllMode) {
            nameEl.textContent = 'Все портфели';
            nameEl.style.display = '';
          } else {
            const active = portfolios.find(p => p.id === portfolioId);
            nameEl.textContent = active ? active.name : '';
            nameEl.style.display = active ? '' : 'none';
            showPfActions = !isReadOnly && !!active;
          }
        }
        // Rename/delete moved to the table header next to the «Портфель» title —
        // keep the topbar clean (the name still shows there).
        if (pfActionsEl) pfActionsEl.style.display = 'none';
        // Size to longest name
        if (portfolios.length) {
          const longest = portfolios.reduce((a, b) => a.name.length > b.name.length ? a : b).name;
          const canvas = document.createElement('canvas');
          const ctx = canvas.getContext('2d');
          const cs = window.getComputedStyle(selector);
          ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
          selector.style.width = Math.ceil(ctx.measureText(longest).width + 40) + 'px';
        }

        // Also update mobile selector
        const mobileSel = document.getElementById('mobile-portfolio-selector');
        const mobileWrap = document.getElementById('mobile-portfolio-wrap');
        if (mobileSel && mobileWrap) {
          mobileSel.innerHTML = '';
          if (portfolios.length > 1 || isAllMode) {
            const allOpt = document.createElement('option');
            allOpt.value = '__all__';
            allOpt.textContent = '📊 Все портфели';
            allOpt.selected = isAllMode;
            mobileSel.appendChild(allOpt);
          }
          for (const p of portfolios) {
            const opt = document.createElement('option');
            opt.value = String(p.id);
            opt.textContent = p.name;
            opt.selected = !isAllMode && p.id === portfolioId;
            mobileSel.appendChild(opt);
          }
          const mobileAddOpt = document.createElement('option');
          mobileAddOpt.value = '__new__';
          mobileAddOpt.textContent = '+ Добавить портфель';
          mobileSel.appendChild(mobileAddOpt);
          mobileWrap.style.display = 'block';
        }

        // Also update import portfolio selector
        const importSelector = document.getElementById('import-portfolio');
        if (importSelector) {
          const currentValue = importSelector.value;
          importSelector.innerHTML = `<option value="" data-i18n="io.importPortfolioPlaceholder">${t('io.importPortfolioPlaceholder')}</option>`;
          for (const p of portfolios) {
            const option = document.createElement('option');
            option.value = String(p.id);
            option.textContent = p.name;
            option.selected = String(p.id) === currentValue;
            importSelector.appendChild(option);
          }
        }

        // Multi-broker hint: show when juggling >1 portfolio outside all-mode
        const mbHint = document.getElementById('multibroker-hint');
        if (mbHint) {
          mbHint.style.display = (!isReadOnly && !isAllMode && portfolios.length > 1) ? 'flex' : 'none';
        }
        // Refresh-ratings button: per-portfolio only (hidden in all-mode / share)
        const rrBtn = document.getElementById('btn-refresh-ratings');
        if (rrBtn) {
          rrBtn.style.display = (!isReadOnly && !isAllMode && portfolioId) ? '' : 'none';
        }
        // Portfolio rename/delete icons next to the "Портфель" title
        const pfTbl = document.getElementById('pf-table-actions');
        if (pfTbl) {
          pfTbl.style.display = (!isReadOnly && !isAllMode && portfolioId) ? 'inline-flex' : 'none';
        }
        // Prominent profit-mode button (shown only when full mode is available)
        const pmBtn = document.getElementById('btn-profit-mode');
        if (pmBtn) {
          pmBtn.style.display = (!isReadOnly && !isAllMode && portfolioId) ? '' : 'none';
        }
      }

      function setupPortfolioSelector() {
        // Don't show portfolio selector in read-only shared view
        if (isReadOnly) return;

        // Mobile selector change handler
        const mobileSel = document.getElementById('mobile-portfolio-selector');
        if (mobileSel) {
          mobileSel.addEventListener('change', (e) => {
            if (e.target.value === '__new__') {
              e.target.value = isAllMode ? '__all__' : String(portfolioId);
              openCreatePortfolioModal();
              return;
            }
            if (e.target.value === '__all__') {
              if (!isAllMode) window.location.href = '/all';
              return;
            }
            if (isAllMode) {
              localStorage.setItem('mvp_active_portfolio_id', String(Number(e.target.value)));
              window.location.href = '/app';
              return;
            }
            const newId = Number(e.target.value);
            if (newId !== portfolioId) {
              portfolioId = newId;
              _analyticsLoaded = false;
              localStorage.setItem('mvp_active_portfolio_id', String(portfolioId));
              showPanel('panel-table');
              if (loadTableCache()) renderTable();
              syncTableFromServer().catch(err => console.error('Failed to sync:', err));
            }
          });
        }

        const selector = document.getElementById('portfolio-selector');
        if (!selector) {
          // Create selector if doesn't exist
          const topbarNav = document.querySelector('.topbar-nav');
          if (topbarNav) {
            const container = document.createElement('div');
            container.style.display = 'flex';
            container.style.alignItems = 'center';
            container.style.gap = '8px';
            const sel = document.createElement('select');
            sel.id = 'portfolio-selector';
            sel.style.padding = '4px 8px';
            sel.style.borderRadius = '4px';
            sel.style.border = '1px solid var(--border)';
            sel.style.background = 'var(--input-bg)';
            sel.style.color = 'var(--text-primary)';
            sel.style.fontSize = '12px';
            sel.style.lineHeight = '1';
            sel.style.height = '26px';
            sel.addEventListener('change', (e) => {
              if (e.target.value === '__new__') {
                // Сбросить выбор обратно на активный портфель
                e.target.value = isAllMode ? '__all__' : String(portfolioId);
                openCreatePortfolioModal();
                return;
              }
              if (e.target.value === '__all__') {
                if (!isAllMode) window.location.href = '/all';
                return;
              }
              if (isAllMode) {
                localStorage.setItem('mvp_active_portfolio_id', String(Number(e.target.value)));
                window.location.href = '/app';
                return;
              }
              const newId = Number(e.target.value);
              if (newId !== portfolioId) {
                portfolioId = newId;
                _analyticsLoaded = false;
                localStorage.setItem('mvp_active_portfolio_id', String(portfolioId));
                showPanel('panel-table');
                if (loadTableCache()) renderTable();
                syncTableFromServer().catch(err => console.error('Failed to sync:', err));
                // Recalculate total portfolios value
                calculateAllPortfoliosTotal().then(val => {
                  allPortfoliosTotalValue = val;
                  const summary = calculateSummary(tableRows);
                  updateStatCards(summary);
                }).catch(err => console.error('Failed to recalculate totals:', err));
              }
            });
            container.appendChild(sel);
            topbarNav.insertBefore(container, topbarNav.firstChild);
          }
        }

        // Add portfolio name + update time + refresh button to topbar-right (once)
        const topbarRight = document.getElementById('topbar-right');
        if (topbarRight && !document.getElementById('topbar-portfolio-name')) {
          // Имя портфеля
          const nameSpan = document.createElement('span');
          nameSpan.id = 'topbar-portfolio-name';
          nameSpan.style.cssText = 'font-size:12px;font-weight:500;color:var(--text-secondary);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';

          // Иконки переименования/удаления активного портфеля
          const pfActions = document.createElement('span');
          pfActions.id = 'topbar-portfolio-actions';
          pfActions.style.cssText = 'display:none;align-items:center;gap:2px;padding-right:10px;border-right:1px solid var(--border);';
          const renameBtn = document.createElement('button');
          renameBtn.id = 'btn-pf-rename';
          renameBtn.title = t('portfolio.rename');
          renameBtn.onclick = topbarRenamePortfolio;
          renameBtn.style.cssText = 'background:none;border:none;cursor:pointer;color:var(--text-muted);font-size:13px;line-height:1;padding:2px 4px;';
          renameBtn.textContent = '✎';
          const delBtn = document.createElement('button');
          delBtn.id = 'btn-pf-delete';
          delBtn.title = t('settings.portfolios.deleteTitle');
          delBtn.onclick = topbarDeletePortfolio;
          delBtn.style.cssText = 'background:none;border:none;cursor:pointer;color:var(--text-muted);font-size:13px;line-height:1;padding:2px 4px;';
          delBtn.textContent = '🗑';
          pfActions.appendChild(renameBtn);
          pfActions.appendChild(delBtn);

          // Обновлено ЧЧ:ММ
          const timeSpan = document.createElement('span');
          timeSpan.id = 'last-update-time';
          timeSpan.style.cssText = 'font-size:11px;color:var(--text-muted);white-space:nowrap;';

          // Кнопка Обновить
          const refreshBtn = document.createElement('button');
          refreshBtn.id = 'btn-refresh';
          refreshBtn.title = 'Обновить данные (R)';
          refreshBtn.onclick = manualRefresh;
          refreshBtn.style.cssText = 'display:flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid var(--btn-sec-border);border-radius:6px;background:var(--btn-sec-bg);cursor:pointer;font-size:12px;color:var(--text-secondary);transition:all .15s;white-space:nowrap;';
          refreshBtn.innerHTML = '<svg id="refresh-icon" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.5 2A5 5 0 1 0 11 6.5"/><polyline points="10.5 1 10.5 3.5 8 3.5"/></svg>Обновить';

          // Вставить в начало topbar-right (перед темой/языком/выходом)
          topbarRight.insertBefore(refreshBtn, topbarRight.firstChild);
          topbarRight.insertBefore(timeSpan, topbarRight.firstChild);
          topbarRight.insertBefore(pfActions, topbarRight.firstChild);
          topbarRight.insertBefore(nameSpan, topbarRight.firstChild);
        }

        updatePortfolioSelector();
      }

      // ── CONFIRM MODAL ────────────────────────────────────────────
      let _confirmCallback = null;
      (function initConfirmModal() {
        const overlay = document.getElementById('confirm-modal-overlay');
        const okBtn = document.getElementById('confirm-modal-ok');
        const cancelBtn = document.getElementById('confirm-modal-cancel');
        function closeConfirm() { overlay.style.display = 'none'; _confirmCallback = null; }
        okBtn.addEventListener('click', () => { const cb = _confirmCallback; closeConfirm(); if (cb) cb(); });
        cancelBtn.addEventListener('click', closeConfirm);
        overlay.addEventListener('click', e => { if (e.target === overlay) closeConfirm(); });
      })();

      function showConfirm(title, message, onConfirm) {
        document.getElementById('confirm-modal-title').textContent = title;
        document.getElementById('confirm-modal-message').textContent = message;
        _confirmCallback = onConfirm;
        document.getElementById('confirm-modal-overlay').style.display = 'flex';
      }

      // Styled replacement for window.prompt. Resolves to the entered string,
      // or null on cancel. opts: {value, placeholder, okText, mustMatch, matchError}.
      let _promptResolve = null;
      (function initPromptModal() {
        const overlay = document.getElementById('prompt-modal-overlay');
        const okBtn = document.getElementById('prompt-modal-ok');
        const cancelBtn = document.getElementById('prompt-modal-cancel');
        const input = document.getElementById('prompt-modal-input');
        const errEl = document.getElementById('prompt-modal-error');
        function close(result) {
          overlay.style.display = 'none';
          const r = _promptResolve; _promptResolve = null;
          overlay._mustMatch = undefined; overlay._matchError = '';
          if (r) r(result);
        }
        function submit() {
          const val = input.value.trim();
          if (overlay._mustMatch !== undefined && val !== overlay._mustMatch) {
            errEl.textContent = overlay._matchError || 'Значение не совпадает';
            return;
          }
          close(val);
        }
        okBtn.addEventListener('click', submit);
        cancelBtn.addEventListener('click', () => close(null));
        overlay.addEventListener('click', e => { if (e.target === overlay) close(null); });
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); submit(); }
          if (e.key === 'Escape') close(null);
        });
        input.addEventListener('input', () => { errEl.textContent = ''; });
      })();

      function showPrompt(title, message, opts) {
        opts = opts || {};
        document.getElementById('prompt-modal-title').textContent = title;
        document.getElementById('prompt-modal-message').textContent = message || '';
        const overlay = document.getElementById('prompt-modal-overlay');
        const input = document.getElementById('prompt-modal-input');
        const okBtn = document.getElementById('prompt-modal-ok');
        const errEl = document.getElementById('prompt-modal-error');
        input.value = opts.value || '';
        input.placeholder = opts.placeholder || '';
        okBtn.textContent = opts.okText || t('confirm.ok');
        errEl.textContent = '';
        overlay._mustMatch = opts.mustMatch;
        overlay._matchError = opts.matchError || '';
        overlay.style.display = 'flex';
        setTimeout(() => { input.focus(); input.select(); }, 50);
        return new Promise(resolve => { _promptResolve = resolve; });
      }

      // ── SHARE PORTFOLIO ──────────────────────────────────────────
      function openShareModal() {
        const overlay = document.getElementById('share-modal-overlay');
        if (overlay) overlay.style.display = 'flex';
        loadShareStatus();
      }

      let _shareModalPrevPortfolioId = null;
      function openShareModalFor(id) {
        _shareModalPrevPortfolioId = portfolioId;
        portfolioId = id;
        openShareModal();
      }

      function closeShareModal() {
        const overlay = document.getElementById('share-modal-overlay');
        if (overlay) overlay.style.display = 'none';
        // Restore portfolioId if it was changed for share modal
        if (_shareModalPrevPortfolioId !== null) {
          portfolioId = _shareModalPrevPortfolioId;
          _shareModalPrevPortfolioId = null;
        }
      }

      function showPasswordModal() {
        const overlay = document.getElementById('password-modal-overlay');
        if (overlay) {
          overlay.style.display = 'flex';
          document.getElementById('password-modal-input').value = '';
          document.getElementById('password-modal-status').textContent = '';
          setTimeout(() => document.getElementById('password-modal-input').focus(), 100);
        }
      }

      function closePasswordModal() {
        const overlay = document.getElementById('password-modal-overlay');
        if (overlay) overlay.style.display = 'none';
      }

      function submitPasswordModal() {
        if (window._passwordModalResolve) {
          window._passwordModalResolve();
        }
      }

      // Handle Enter key in password modal
      document.addEventListener('DOMContentLoaded', () => {
        const passInput = document.getElementById('password-modal-input');
        if (passInput) {
          passInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') submitPasswordModal();
          });
        }
        // Handle Enter key in create portfolio modal
        const createInput = document.getElementById('create-portfolio-name');
        if (createInput) {
          createInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') submitCreatePortfolio();
          });
        }
      });

      function openCreatePortfolioModal() {
        const overlay = document.getElementById('create-portfolio-modal-overlay');
        if (overlay) {
          overlay.style.display = 'flex';
          document.getElementById('create-portfolio-name').value = '';
          document.getElementById('create-portfolio-status').textContent = '';
          setTimeout(() => document.getElementById('create-portfolio-name').focus(), 100);
        }
      }

      function closeCreatePortfolioModal() {
        const overlay = document.getElementById('create-portfolio-modal-overlay');
        if (overlay) overlay.style.display = 'none';
      }

      async function submitCreatePortfolio() {
        const nameInput = document.getElementById('create-portfolio-name');
        const statusEl = document.getElementById('create-portfolio-status');
        const name = nameInput.value.trim();

        if (!name) {
          statusEl.textContent = t('modal.createPortfolio.nameRequired');
          return;
        }

        statusEl.textContent = t('modal.createPortfolio.creating');
        try {
          const res = await apiFetch('/portfolios', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
          });
          if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || t('modal.createPortfolio.createError'));
          }
          await loadPortfolios();
          const newP = portfolios[portfolios.length - 1];
          if (newP) {
            portfolioId = newP.id;
            localStorage.setItem('mvp_active_portfolio_id', String(portfolioId));
            updatePortfolioSelector();
            await syncTableFromServer();
          }
          closeCreatePortfolioModal();
        } catch (err) {
          statusEl.textContent = t('error.prefix') + (err.message || t('modal.createPortfolio.createError'));
        }
      }

      // ── MOVE POPOVER ─────────────────────────────────────────────
      let _movePopoverItemId = null;

      function showMovePopover(triggerBtn, itemId) {
        const popover = document.getElementById('move-popover');
        const sel     = document.getElementById('move-popover-select');

        sel.innerHTML = '';
        portfolios.filter(p => p.id !== portfolioId).forEach(p => {
          const opt = document.createElement('option');
          opt.value = String(p.id);
          opt.textContent = p.name;
          sel.appendChild(opt);
        });

        _movePopoverItemId = itemId;

        // Position near button, keep within viewport
        const rect  = triggerBtn.getBoundingClientRect();
        const popW  = 240;
        const popH  = 130;
        let left = rect.left;
        let top  = rect.bottom + 6;
        if (left + popW > window.innerWidth - 8)  left = window.innerWidth - popW - 8;
        if (top  + popH > window.innerHeight - 8) top  = rect.top - popH - 6;

        popover.style.left    = left + 'px';
        popover.style.top     = top  + 'px';
        popover.style.display = 'block';
      }

      function hideMovePopover() {
        document.getElementById('move-popover').style.display = 'none';
        _movePopoverItemId = null;
      }

      // Setup popover buttons once
      document.getElementById('move-popover-cancel').addEventListener('click', hideMovePopover);
      document.addEventListener('keydown', e => { if (e.key === 'Escape') hideMovePopover(); });
      document.addEventListener('click', e => {
        const pop = document.getElementById('move-popover');
        if (pop.style.display !== 'none' && !pop.contains(e.target) && !e.target.classList.contains('btn-sm-move')) {
          hideMovePopover();
        }
      });
      document.getElementById('move-popover-confirm').addEventListener('click', async () => {
        const targetId   = Number(document.getElementById('move-popover-select').value);
        const itemId     = _movePopoverItemId;
        if (!itemId || !targetId) return;
        hideMovePopover();
        try {
          const resp = await apiFetch(`/portfolios/${portfolioId}/instruments/${itemId}/move`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_portfolio_id: targetId })
          });
          if (!resp.ok) throw new Error((await resp.json()).detail || 'Ошибка');
          tableRows = tableRows.filter(r => r.id !== itemId);
          recalculateWeights(tableRows); persistTableCache(); renderTable();
          const targetName = portfolios.find(p => p.id === targetId)?.name || '';
          setStatus(tableStatusEl, window._lang === 'en' ? `Moved to "${targetName}"` : `Перенесено в «${targetName}»`);
        } catch (err) { setStatus(tableStatusEl, err.message, true); }
      });

      function openDeletePortfolioModal() {
        const overlay = document.getElementById('delete-portfolio-modal-overlay');
        if (overlay) {
          const currentPortfolio = portfolios.find(p => p.id === portfolioId);
          if (currentPortfolio) {
            document.getElementById('delete-portfolio-name').textContent = currentPortfolio.name;
          }
          document.getElementById('delete-portfolio-status').textContent = '';
          overlay.style.display = 'flex';
        }
      }

      function closeDeletePortfolioModal() {
        const overlay = document.getElementById('delete-portfolio-modal-overlay');
        if (overlay) overlay.style.display = 'none';
      }

      // ── Price alerts ────────────────────────────────────────────
      let _alertItemId = null, _alertPortfolioId = null;

      async function openPriceAlerts(itemId, pid) {
        _alertItemId = itemId;
        _alertPortfolioId = pid;
        const modal = document.getElementById('price-alert-modal');
        if (modal) modal.style.display = 'flex';
        await loadPriceAlerts();
      }

      function closePriceAlertModal() {
        const modal = document.getElementById('price-alert-modal');
        if (modal) modal.style.display = 'none';
        _alertItemId = null;
        _alertPortfolioId = null;
      }

      async function loadPriceAlerts() {
        if (!_alertItemId || !_alertPortfolioId) return;
        const list = document.getElementById('alert-list');
        const statusEl = document.getElementById('alert-modal-status');
        if (!list) return;
        list.textContent = t('alert.loading');
        try {
          const r = await apiFetch(`/portfolios/${_alertPortfolioId}/instruments/${_alertItemId}/alerts`);
          if (!r.ok) throw new Error(t('alert.loadError'));
          const data = await r.json();
          list.innerHTML = '';
          if (!data.alerts || data.alerts.length === 0) {
            list.textContent = t('alert.empty');
            return;
          }
          data.alerts.forEach(a => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--slate-100);';
            const label = document.createElement('span');
            label.textContent = (a.alert_type === 'above' ? '▲ выше ' : '▼ ниже ') + a.target_price + (a.triggered ? ' ✓' : '');
            label.style.cssText = 'font-size:13px;color:var(--slate-700);';
            const delBtn = document.createElement('button');
            delBtn.textContent = '✕';
            delBtn.className = 'btn-sm';
            delBtn.style.cssText = 'margin-left:8px;font-size:11px;';
            delBtn.addEventListener('click', () => deletePriceAlert(a.id));
            row.appendChild(label);
            row.appendChild(delBtn);
            list.appendChild(row);
          });
        } catch (e) {
          list.textContent = t('alert.loadError');
          if (statusEl) { statusEl.textContent = e.message; statusEl.className = 'status error'; }
        }
      }

      async function createPriceAlert() {
        if (!_alertItemId || !_alertPortfolioId) return;
        const alertType = document.getElementById('alert-type').value;
        const priceVal = parseFloat(document.getElementById('alert-price').value);
        const statusEl = document.getElementById('alert-modal-status');
        if (!priceVal || priceVal <= 0) {
          if (statusEl) { statusEl.textContent = t('alert.priceInvalid'); statusEl.className = 'status error'; }
          return;
        }
        try {
          const r = await apiFetch(`/portfolios/${_alertPortfolioId}/instruments/${_alertItemId}/alerts`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({alert_type: alertType, target_price: priceVal})
          });
          if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || t('error.prefix'));
          }
          document.getElementById('alert-price').value = '';
          if (statusEl) { statusEl.textContent = t('alert.created'); statusEl.className = 'status'; }
          await loadPriceAlerts();
        } catch (e) {
          if (statusEl) { statusEl.textContent = e.message; statusEl.className = 'status error'; }
        }
      }

      async function deletePriceAlert(alertId) {
        if (!_alertItemId || !_alertPortfolioId) return;
        try {
          const r = await apiFetch(`/portfolios/${_alertPortfolioId}/instruments/${_alertItemId}/alerts/${alertId}`, {method: 'DELETE'});
          if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || 'Не удалось удалить алерт');
          }
          await loadPriceAlerts();
        } catch (e) {
          const statusEl = document.getElementById('alert-modal-status');
          if (statusEl) { statusEl.textContent = e.message; statusEl.className = 'status error'; }
        }
      }

      async function submitDeletePortfolio() {
        if (!portfolioId) {
          toast(t('modal.deletePortfolio.noPortfolio'), 'warn');
          return;
        }

        const statusEl = document.getElementById('delete-portfolio-status');
        statusEl.textContent = t('modal.deletePortfolio.deleting');

        try {
          const res = await apiFetch(`/portfolios/${portfolioId}`, {
            method: 'DELETE'
          });
          if (!res.ok) {
            const text = await res.text();
            let msg = t('modal.deletePortfolio.deleteError');
            try {
              const err = JSON.parse(text);
              msg = err.detail || msg;
            } catch {}
            throw new Error(msg);
          }
          await loadPortfolios();
          // Switch to first available portfolio or clear if none left
          if (portfolios.length > 0) {
            portfolioId = portfolios[0].id;
            localStorage.setItem('mvp_active_portfolio_id', String(portfolioId));
            updatePortfolioSelector();
            await syncTableFromServer();
          } else {
            portfolioId = null;
            localStorage.removeItem('mvp_active_portfolio_id');
            updatePortfolioSelector();
          }
          closeDeletePortfolioModal();
        } catch (err) {
          statusEl.textContent = t('error.prefix') + (err.message || t('modal.deletePortfolio.deleteError'));
        }
      }

      async function loadShareStatus() {
        try {
          const res = await apiFetch(`/portfolios/${portfolioId}`);
          const portfolio = await res.json();
          const urlTextarea = document.getElementById('share-url-textarea');
          const passwordInput = document.getElementById('share-password-input');
          const revokeBtn = document.getElementById('share-revoke-btn');
          if (portfolio.share_token) {
            const shareUrl = `${window.location.origin}/share/${portfolio.share_token}`;
            urlTextarea.value = shareUrl;
            revokeBtn.style.display = 'block';
          } else {
            urlTextarea.value = t('share.notPublished');
            revokeBtn.style.display = 'none';
          }
          passwordInput.value = '';
          document.getElementById('share-status').textContent = '';
        } catch (err) {
          document.getElementById('share-status').textContent = t('share.loading') + ': ' + err.message;
        }
      }

      async function applySharePassword() {
        const passwordInput = document.getElementById('share-password-input');
        const statusEl = document.getElementById('share-status');
        statusEl.textContent = t('share.saving');
        try {
          const payload = { password: passwordInput.value.trim() || null };
          const res = await apiFetch(`/portfolios/${portfolioId}/share`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || t('error.prefix'));
          }
          const data = await res.json();
          const urlTextarea = document.getElementById('share-url-textarea');
          urlTextarea.value = `${window.location.origin}${data.share_url}`;
          document.getElementById('share-revoke-btn').style.display = 'block';
          statusEl.textContent = t('share.created');
          setTimeout(() => { statusEl.textContent = ''; }, 3000);
        } catch (err) {
          statusEl.textContent = t('error.prefix') + (err.message || '');
          statusEl.style.color = 'var(--red-400)';
        }
      }

      async function revokeShare() {
        if (!confirm(t('share.revokeConfirm'))) return;
        const statusEl = document.getElementById('share-status');
        statusEl.textContent = t('share.revoking');
        try {
          const res = await apiFetch(`/portfolios/${portfolioId}/share`, { method: 'DELETE' });
          if (!res.ok) throw new Error(t('error.prefix'));
          const urlTextarea = document.getElementById('share-url-textarea');
          const passwordInput = document.getElementById('share-password-input');
          const revokeBtn = document.getElementById('share-revoke-btn');
          urlTextarea.value = t('share.notPublishedArea');
          passwordInput.value = '';
          revokeBtn.style.display = 'none';
          statusEl.textContent = t('share.revoked');
          statusEl.style.color = 'var(--green-600)';
          setTimeout(() => { statusEl.textContent = ''; }, 3000);
        } catch (err) {
          statusEl.textContent = t('error.prefix') + (err.message || '');
          statusEl.style.color = 'var(--red-400)';
        }
      }


      // ── INIT ─────────────────────────────────────────────────────
      (async () => {
        if (isAllMode) {
          document.body.classList.add('all-mode');
        }
        if (parseShareUrl()) {
          // Read-only shared view
          isReadOnly = true;
          document.body.classList.add('read-only-mode');
          try { showDashboard(); } catch(e) {
            // showDashboard may throw if called before TRANSLATIONS is initialized
            // Still ensure container is visible
            document.querySelector('.container').style.display = 'block';
          }
          document.querySelector('.topbar-nav').style.display = 'none';
          const shareBtn = document.getElementById('btn-share-portfolio');
          if (shareBtn) shareBtn.style.display = 'none';
          const logoutBtn = document.getElementById('btn-logout');
          if (logoutBtn) logoutBtn.style.display = 'none';
          const navBtns = document.querySelectorAll('.nav-btn:not([data-panel="panel-table"])');
          navBtns.forEach(btn => btn.style.display = 'none');
          // Hide RGBI toggle and actionable row in share mode
          const rgbiLbl = document.getElementById('rgbi-toggle-label');
          if (rgbiLbl) rgbiLbl.style.display = 'none';
          // hide events card and cash-reinvest card in share mode (their data
          // comes from the private analytics-extra endpoint) and reflow the
          // grids so neighbours fill the freed space — no empty cells.
          const eventsCard = document.getElementById('events-card');
          if (eventsCard) eventsCard.style.display = 'none';
          const ytmEventsRow = document.getElementById('ytm-events-row');
          if (ytmEventsRow) ytmEventsRow.style.gridTemplateColumns = '1fr';
          const cashCard = document.getElementById('cash-reinvest-card');
          if (cashCard) cashCard.style.display = 'none';
          const performersRow = document.getElementById('performers-row');
          if (performersRow) performersRow.style.gridTemplateColumns = 'repeat(3,1fr)';

          // Put «Динамика стоимости» and «Рыночная доходность» (YTM) side by
          // side (50/50) in share view; стекаются в столбик на ≤1024px.
          const histCard = document.getElementById('analytics-history-card');
          if (histCard && ytmEventsRow && histCard.parentNode) {
            const dynRow = document.createElement('div');
            dynRow.className = 'panel-cols';
            dynRow.id = 'share-dynamics-row';
            histCard.parentNode.insertBefore(dynRow, histCard);
            dynRow.appendChild(histCard);
            dynRow.appendChild(ytmEventsRow);
          }

          // Make brand logo link to bondai.ru in share view
          const brand = document.querySelector('.topbar-brand');
          if (brand) {
            brand.onclick = () => { window.open('https://bondai.ru', '_blank', 'noopener'); };
          }

          // Add "Create your own portfolio" button in topbar
          const topbarInner = document.querySelector('.topbar-inner');
          if (topbarInner && !document.getElementById('btn-create-portfolio')) {
            const createBtn = document.createElement('button');
            createBtn.id = 'btn-create-portfolio';
            createBtn.setAttribute('data-i18n', 'portfolio.createBtn');
            createBtn.textContent = '+ Создать портфель';
            createBtn.style.marginLeft = 'auto';
            createBtn.style.padding = '8px 16px';
            createBtn.style.background = '#3b82f6';
            createBtn.style.color = '#fff';
            createBtn.style.border = 'none';
            createBtn.style.borderRadius = '4px';
            createBtn.style.cursor = 'pointer';
            createBtn.style.fontSize = '13px';
            createBtn.style.fontWeight = '500';
            createBtn.style.height = 'auto';
            createBtn.style.alignSelf = 'center';
            createBtn.onclick = () => {
              window.location.href = '/app?auth=register';
            };
            topbarInner.appendChild(createBtn);
            // Localize the freshly-added button to the current language.
            try { if (typeof applyLang === 'function') applyLang(window._lang || 'ru'); } catch (e) {}
          }

          // Don't load cache for shared views - always fetch from server
          // This ensures password-protected portfolios don't show data in background
          try {
            await syncTableFromServer();
          } catch (err) {
            setStatus(tableStatusEl, err.message || 'Ошибка загрузки портфеля.', true);
          }
          setInterval(() => {
            if (window._wizardBusy) return;
            syncTableFromServer().catch(() => {});
          }, SYNC_INTERVAL_MS);
        } else {
          // Authenticated view — load cache first for instant display, then fetch fresh
          if (loadTableCache()) renderTable();
          initAuth().then(() => {
            loadNotifSettings();
            setInterval(() => {
              if (!isAllMode && !portfolioId) return;
              if (window._wizardBusy) return;
              syncTableFromServer().catch(() => {});
            }, SYNC_INTERVAL_MS);
          });
        }

      })();

      // ── ADMIN PANEL ──────────────────────────────────────────────
      let _admPwdUserId = null;

      // Lazy loaders per tab — run once when the tab is first opened.
      const _ADM_TAB_LOADERS = {
        users:      () => adminLoadUsers(),
        money:      () => { adminLoadRevenue(30).catch(() => {}); adminLoadPayments().catch(() => {}); },
        portfolios: () => adminLoadPortfolios(),
        system:     () => { adminLoadSources().catch(() => {}); adminLoadBackups().catch(() => {}); adminLoadAuditLog().catch(() => {}); },
        promo:      () => adminLoadPromo(),
      };
      const _admTabLoaded = {};

      function adminShowTab(name, btn) {
        if (history.replaceState) history.replaceState(null, '', '#' + name);
        document.querySelectorAll('.adm-pane').forEach(p => {
          p.classList.toggle('active', p.dataset.admTab === name);
        });
        document.querySelectorAll('.adm-tab').forEach(b => {
          b.classList.toggle('active', b.dataset.tab === name);
        });
        if (!btn) {
          const tabBtn = document.querySelector(`.adm-tab[data-tab="${name}"]`);
          if (tabBtn) tabBtn.classList.add('active');
        }
        if (!_admTabLoaded[name] && _ADM_TAB_LOADERS[name]) {
          _admTabLoaded[name] = true;
          try { _ADM_TAB_LOADERS[name](); } catch (e) { /* lazy load best-effort */ }
        }
      }

      async function adminInit() {
        await adminLoadStats();
        // Deep-link via hash (/admin#money) or legacy path (/admin/promo).
        // Hash is used so tab names don't collide with /admin/* API routes.
        const hash = (location.hash.match(/^#(users|money|portfolios|system|promo)$/) || [])[1];
        const legacy = location.pathname === '/admin/promo' ? 'promo' : null;
        adminShowTab(hash || legacy || 'users');
      }

      // ── Data Sources ─────────────────────────────────────────────────────
      function _fmtAgo(seconds) {
        if (seconds === null || seconds === undefined) return '—';
        if (seconds < 60) return `${seconds}с назад`;
        if (seconds < 3600) return `${Math.floor(seconds/60)}м назад`;
        if (seconds < 86400) return `${Math.floor(seconds/3600)}ч назад`;
        return `${Math.floor(seconds/86400)}д назад`;
      }

      function _sourceStatusColor(s) {
        if (!s.enabled) return 'var(--text-muted)';
        if (s.errors > 0 && s.hits === 0) return 'var(--red-400)';
        if (s.blocked > 0) return 'var(--amber-400)';
        if (s.last_error_code) return 'var(--amber-400)';
        if (s.hits > 0) return 'var(--green-400)';
        return 'var(--text-muted)';
      }

      function _sourceStatusLabel(s) {
        if (!s.enabled) return 'Отключён';
        if (s.blocked > 0 && s.hits === 0) return 'Заблокирован';
        if (s.errors > 0 && s.hits === 0) return 'Ошибка';
        if (s.blocked > 0) return 'Частичная блокировка';
        if (s.requests === 0) return 'Нет запросов';
        if (s.hits > 0) return 'Работает';
        return 'Нет данных';
      }

      async function adminLoadSources() {
        const body = document.getElementById('adm-sources-body');
        if (!body) return;
        try {
          const r = await apiFetch('/admin/data-sources');
          if (!r.ok) throw new Error();
          const sources = await r.json();
          body.innerHTML = '';
          sources.forEach(s => {
            const color = _sourceStatusColor(s);
            const label = _sourceStatusLabel(s);
            const hitRate = s.hit_rate !== null ? `${s.hit_rate}%` : '—';

            const card = document.createElement('div');
            card.style.cssText = `background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;`;

            card.innerHTML = `
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                <div style="display:flex;align-items:center;gap:8px;">
                  <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0;"></span>
                  <span style="font-weight:600;font-size:13px;color:var(--text-primary);">${s.label}</span>
                </div>
                <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px;color:var(--text-muted);">
                  <input type="checkbox" id="src-toggle-${s.name}" ${s.enabled ? 'checked' : ''} style="cursor:pointer;" onchange="adminToggleSource('${s.name}', this.checked)">
                  Включён
                </label>
              </div>
              <div style="font-size:11px;color:${color};font-weight:600;margin-bottom:8px;">${label}</div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:11px;color:var(--text-secondary);">
                <div>Запросов: <b style="color:var(--text-primary);">${s.requests}</b></div>
                <div>Успешных: <b style="color:var(--green-400);">${s.hits}</b></div>
                <div>Ошибок: <b style="color:${s.errors > 0 ? 'var(--red-400)' : 'var(--text-primary)'};">${s.errors}</b></div>
                <div>Блокировок: <b style="color:${s.blocked > 0 ? 'var(--amber-400)' : 'var(--text-primary)'};">${s.blocked}</b></div>
                <div>Hit rate: <b style="color:var(--text-primary);">${hitRate}</b></div>
                <div>Последний: <b style="color:var(--text-primary);">${_fmtAgo(s.last_attempt_ago)}</b></div>
              </div>
              ${s.last_error_code ? `<div style="margin-top:8px;font-size:10px;color:var(--red-400);background:rgba(248,113,113,.08);border-radius:4px;padding:4px 8px;">
                HTTP ${s.last_error_code}${s.last_error_msg ? ': ' + s.last_error_msg : ''}
              </div>` : ''}
            `;
            body.appendChild(card);
          });
        } catch(e) {
          body.innerHTML = '<div style="color:var(--text-muted);font-size:12px;">Не удалось загрузить данные</div>';
        }
      }

      async function adminLoadRevenue(days, btn) {
        // toggle active button state
        if (btn) {
          document.querySelectorAll('.adm-rev-btn').forEach(b => {
            b.classList.remove('btn-primary'); b.classList.add('btn-secondary');
          });
          btn.classList.remove('btn-secondary'); btn.classList.add('btn-primary');
        }
        try {
          const r = await apiFetch('/admin/revenue?days=' + days);
          if (!r.ok) throw new Error();
          const d = await r.json();
          const fmt = n => Number(n).toLocaleString('ru-RU') + ' ₽';
          document.getElementById('adm-rev-total').textContent = fmt(d.total || 0);
          document.getElementById('adm-rev-count').textContent = d.count || 0;
          document.getElementById('adm-rev-avg').textContent = d.count ? fmt(Math.round(d.total / d.count)) : '—';
          _revHoverIdx = -1;
          _drawRevenueChart(d.series || []);
          const canvas = document.getElementById('adm-rev-canvas');
          if (canvas && !canvas._revBound) {
            canvas._revBound = true;
            canvas.addEventListener('mousemove', _revOnMove);
            canvas.addEventListener('mouseleave', _revOnLeave);
            let rt;
            window.addEventListener('resize', () => {
              clearTimeout(rt);
              rt = setTimeout(() => { if (_revSeries.length) _drawRevenueChart(_revSeries); }, 150);
            });
          }
        } catch (e) { /* admin chart optional */ }
      }

      let _revBars = [];        // {x,y,w,h,point} geometry for hover hit-testing
      let _revHoverIdx = -1;
      let _revSeries = [];

      function _revFmtMoney(n) {
        return Number(n).toLocaleString('ru-RU') + ' ₽';
      }
      function _revFmtAxis(n) {
        if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + 'М';
        if (n >= 1000) return (n / 1000).toFixed(n % 1000 ? 1 : 0) + 'к';
        return String(Math.round(n));
      }
      function _revFmtDate(s, withYear) {
        const [y, m, day] = s.split('-');
        return day + '.' + m + (withYear ? '.' + y : '');
      }
      function _revNiceMax(v) {
        // round the axis max up to a clean number for readable gridlines
        if (v <= 0) return 1;
        const mag = Math.pow(10, Math.floor(Math.log10(v)));
        const norm = v / mag;
        const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
        return nice * mag;
      }

      function _drawRevenueChart(series) {
        const canvas = document.getElementById('adm-rev-canvas');
        const empty = document.getElementById('adm-rev-empty');
        if (!canvas) return;
        _revSeries = series;
        const hasData = series.some(p => p.amount > 0);
        canvas.style.display = hasData ? 'block' : 'none';
        if (empty) empty.style.display = hasData ? 'none' : '';
        if (!hasData) { _revBars = []; return; }

        const parent = canvas.parentElement;
        const cssW = (parent ? parent.clientWidth : 600) - 32; // minus card padding
        const cssH = 220;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
        canvas.style.width = cssW + 'px'; canvas.style.height = cssH + 'px';
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cssW, cssH);

        const css = getComputedStyle(document.documentElement);
        const green = (css.getPropertyValue('--green-600') || '#16a34a').trim();
        const greenLight = (css.getPropertyValue('--green-400') || '#4ade80').trim();
        const muted = (css.getPropertyValue('--text-muted') || '#94a3b8').trim();
        const grid = (css.getPropertyValue('--border') || 'rgba(148,163,184,.15)').trim();

        const padL = 38, padR = 10, padT = 12, padB = 24;
        const plotW = cssW - padL - padR, plotH = cssH - padT - padB;
        const rawMax = Math.max(...series.map(p => p.amount), 1);
        const maxV = _revNiceMax(rawMax);
        const n = series.length;
        const gap = n > 45 ? 1 : n > 14 ? 2 : 4;
        const barW = Math.max(1, (plotW - gap * (n - 1)) / n);

        // ── horizontal gridlines + Y-axis labels ──
        ctx.font = '10px Inter, sans-serif';
        ctx.textBaseline = 'middle';
        const ticks = 4;
        for (let t = 0; t <= ticks; t++) {
          const val = (maxV / ticks) * t;
          const y = padT + plotH - (val / maxV) * plotH;
          ctx.strokeStyle = grid;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(padL, y + 0.5);
          ctx.lineTo(cssW - padR, y + 0.5);
          ctx.stroke();
          ctx.fillStyle = muted;
          ctx.textAlign = 'right';
          ctx.fillText(_revFmtAxis(val), padL - 6, y);
        }

        // ── bars: rounded top + vertical gradient ──
        _revBars = [];
        series.forEach((p, i) => {
          const h = (p.amount / maxV) * plotH;
          const x = padL + i * (barW + gap);
          const y = padT + plotH - h;
          _revBars.push({ x, y, w: barW, h, point: p, index: i });
          if (p.amount <= 0) return;
          const hovered = i === _revHoverIdx;
          const grad = ctx.createLinearGradient(0, y, 0, y + h);
          grad.addColorStop(0, hovered ? greenLight : green);
          grad.addColorStop(1, hovered ? green : greenLight);
          ctx.fillStyle = grad;
          const r = Math.min(barW / 2, 4, h);
          ctx.beginPath();
          ctx.moveTo(x, y + h);
          ctx.lineTo(x, y + r);
          ctx.quadraticCurveTo(x, y, x + r, y);
          ctx.lineTo(x + barW - r, y);
          ctx.quadraticCurveTo(x + barW, y, x + barW, y + r);
          ctx.lineTo(x + barW, y + h);
          ctx.closePath();
          ctx.fill();
        });

        // ── X-axis labels: ~6 evenly spaced dates ──
        ctx.fillStyle = muted;
        ctx.font = '10px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'alphabetic';
        const step = Math.max(1, Math.ceil(n / 6));
        for (let i = 0; i < n; i += step) {
          const b = _revBars[i];
          ctx.fillText(_revFmtDate(series[i].date, false), b.x + b.w / 2, cssH - 7);
        }
      }

      function _revOnMove(e) {
        const canvas = document.getElementById('adm-rev-canvas');
        const tip = document.getElementById('adm-rev-tooltip');
        if (!canvas || !tip || !_revBars.length) return;
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        // nearest bar by x
        let idx = -1, best = Infinity;
        _revBars.forEach((b, i) => {
          const cx = b.x + b.w / 2;
          const dist = Math.abs(mx - cx);
          if (dist < best && dist < (b.w / 2 + 6)) { best = dist; idx = i; }
        });
        if (idx !== _revHoverIdx) { _revHoverIdx = idx; _drawRevenueChart(_revSeries); }
        if (idx < 0) { tip.style.opacity = '0'; return; }
        const p = _revBars[idx].point;
        const plural = p.count === 1 ? 'платёж' : (p.count >= 2 && p.count <= 4 ? 'платежа' : 'платежей');
        tip.innerHTML = '<div style="font-weight:700;margin-bottom:2px;">' + _revFmtDate(p.date, true) + '</div>'
          + '<div style="color:var(--green-400);font-weight:700;">' + _revFmtMoney(p.amount) + '</div>'
          + '<div style="color:var(--text-muted);font-size:11px;">' + p.count + ' ' + plural + '</div>';
        // position tooltip near the bar, kept inside the canvas box
        const parent = canvas.parentElement;
        tip.style.opacity = '1';
        const tw = tip.offsetWidth, th = tip.offsetHeight;
        let left = _revBars[idx].x + _revBars[idx].w / 2 - tw / 2;
        left = Math.max(4, Math.min(left, parent.clientWidth - tw - 4));
        let top = _revBars[idx].y - th - 8;
        if (top < 0) top = _revBars[idx].y + 8;
        tip.style.left = left + 'px';
        tip.style.top = top + 'px';
      }
      function _revOnLeave() {
        const tip = document.getElementById('adm-rev-tooltip');
        if (tip) tip.style.opacity = '0';
        if (_revHoverIdx !== -1) { _revHoverIdx = -1; _drawRevenueChart(_revSeries); }
      }

      async function adminLoadPayments() {
        const body = document.getElementById('adm-payments-body');
        if (!body) return;
        body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:12px;">Загрузка…</td></tr>';
        try {
          const r = await apiFetch('/admin/payments');
          if (!r.ok) throw new Error();
          const rows = await r.json();
          if (!rows.length) {
            body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:12px;">Платежей пока нет</td></tr>';
            return;
          }
          body.innerHTML = '';
          rows.forEach(p => {
            const tr = document.createElement('tr');
            const date = (p.created_at || '').substring(0, 10);
            const planTxt = p.plan === 'year' ? 'Год' : 'Месяц';
            const tdDate = document.createElement('td'); tdDate.textContent = date;
            const tdUser = document.createElement('td'); tdUser.textContent = (p.username || '') + ' #' + (p.user_id || '');
            const tdPlan = document.createElement('td'); tdPlan.textContent = planTxt;
            const tdAmount = document.createElement('td'); tdAmount.textContent = Math.round(p.amount || 0) + ' ₽';
            const tdReceipt = document.createElement('td'); tdReceipt.style.textAlign = 'center';
            const cb = document.createElement('input');
            cb.type = 'checkbox'; cb.checked = !!p.receipt_done; cb.style.cursor = 'pointer';
            cb.onchange = () => adminToggleReceipt(p.payment_id, cb.checked, cb);
            tdReceipt.appendChild(cb);
            tr.append(tdDate, tdUser, tdPlan, tdAmount, tdReceipt);
            body.appendChild(tr);
          });
        } catch(e) {
          body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:12px;">Не удалось загрузить</td></tr>';
        }
      }
      async function adminToggleReceipt(paymentId, done, cb) {
        try {
          const r = await apiFetch(`/admin/payments/${encodeURIComponent(paymentId)}/receipt`, {
            method: 'PATCH', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ done }),
          });
          if (!r.ok) throw new Error();
        } catch(e) {
          if (cb) cb.checked = !done;
          toast('Ошибка сохранения', 'error');
        }
      }

      let _promoMaterials = [];
      function _escHtml(s) {
        const d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
      }
      async function adminLoadPromo() {
        const body = document.getElementById('adm-promo-body');
        if (!body) return;
        body.innerHTML = '<div style="color:var(--text-muted);font-size:12px;">Загрузка…</div>';
        try {
          const r = await apiFetch('/admin/promo-materials');
          if (!r.ok) throw new Error();
          _promoMaterials = await r.json();
          body.innerHTML = '';
          _promoMaterials.forEach((m, i) => {
            const card = document.createElement('div');
            card.style.cssText = 'background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;';
            const preview = (m.body || '').slice(0, 600);
            const truncated = (m.body || '').length > 600;
            card.innerHTML =
              '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:8px;">' +
                '<div>' +
                  '<div style="font-weight:600;font-size:13px;color:var(--text-primary);margin-bottom:3px;">' + _escHtml(m.title) + '</div>' +
                  '<div style="display:flex;gap:6px;flex-wrap:wrap;">' +
                    '<span style="font-size:10px;color:var(--blue-400);background:rgba(96,165,250,.1);border-radius:4px;padding:2px 7px;">' + _escHtml(m.platform) + '</span>' +
                    '<span style="font-size:10px;color:var(--text-muted);background:var(--bg-subtle,rgba(148,163,184,.1));border-radius:4px;padding:2px 7px;">' + _escHtml(m.tone) + '</span>' +
                    '<span style="font-size:10px;color:var(--text-muted);background:var(--bg-subtle,rgba(148,163,184,.1));border-radius:4px;padding:2px 7px;">' + _escHtml(m.format) + '</span>' +
                  '</div>' +
                '</div>' +
                '<button class="btn btn-primary" style="height:30px;font-size:12px;padding:0 12px;white-space:nowrap;flex-shrink:0;" onclick="adminCopyPromo(' + i + ', this)">Копировать</button>' +
              '</div>' +
              '<pre style="margin:8px 0 0;white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:11px;line-height:1.55;color:var(--text-secondary);max-height:180px;overflow:auto;background:var(--bg-subtle,rgba(148,163,184,.06));border-radius:6px;padding:10px 12px;">' +
                _escHtml(preview) + (truncated ? '\n…' : '') +
              '</pre>';
            body.appendChild(card);
          });
        } catch(e) {
          body.innerHTML = '<div style="color:var(--text-muted);font-size:12px;">Не удалось загрузить материалы</div>';
        }
      }

      async function adminCopyPromo(i, btn) {
        const m = _promoMaterials[i];
        if (!m) return;
        try {
          await navigator.clipboard.writeText(m.body || '');
          const orig = btn.textContent;
          btn.textContent = '✓ Скопировано';
          setTimeout(() => { btn.textContent = orig; }, 1600);
        } catch(e) {
          toast('Не удалось скопировать — выделите текст вручную', 'error');
        }
      }

      async function adminToggleSource(name, enabled) {
        try {
          const r = await apiFetch(`/admin/data-sources/${name}/toggle`, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ enabled }),
          });
          if (!r.ok) throw new Error();
        } catch(e) {
          // Revert checkbox on error
          const cb = document.getElementById(`src-toggle-${name}`);
          if (cb) cb.checked = !enabled;
          toast('Ошибка изменения настройки', 'error');
        }
      }

      async function adminClearRatingCache() {
        try {
          const r = await apiFetch('/admin/data-sources/ratings/clear-cache', { method: 'POST' });
          const d = await r.json();
          const statusEl = document.getElementById('adm-status');
          statusEl.textContent = `Кэш рейтингов сброшен (${d.cleared} записей). При следующем обновлении портфеля рейтинги запросятся повторно.`;
          statusEl.className = 'status ok';
          setTimeout(() => { statusEl.textContent = ''; statusEl.className = 'status'; }, 5000);
        } catch(e) {
          const statusEl = document.getElementById('adm-status');
          statusEl.textContent = 'Ошибка сброса кэша';
          statusEl.className = 'status error';
        }
      }

      async function adminLoadStats() {
        try {
          const r = await apiFetch('/admin/stats');
          const s = await r.json();
          document.getElementById('adm-stat-users').textContent = s.users;
          document.getElementById('adm-stat-portfolios').textContent = s.portfolios;
          document.getElementById('adm-stat-shared').textContent = s.shared_links;
          document.getElementById('adm-stat-instruments').textContent = s.total_instruments;
          document.getElementById('adm-stat-active-today').textContent = s.active_today;
          document.getElementById('adm-stat-autosync').textContent = s.autosync_count;
          document.getElementById('adm-stat-tg-notif').textContent = s.tg_notif_count;
          document.getElementById('adm-stat-backups').textContent = s.backup_count;
        } catch(e) {}
      }

      function _mkBtn(text, style, onClick) {
        const btn = document.createElement('button');
        btn.textContent = text;
        btn.style.cssText = style + ';padding:3px 8px;font-size:11px;border:none;border-radius:4px;cursor:pointer;';
        btn.addEventListener('click', onClick);
        return btn;
      }

      function _mkIconBtn(icon, title, colorStyle, onClick) {
        const btn = document.createElement('button');
        btn.textContent = icon;
        btn.title = title;
        btn.dataset.colorStyle = colorStyle;
        btn.style.cssText = 'width:26px;height:26px;font-size:12px;border-radius:var(--radius-sm);cursor:pointer;padding:0;line-height:1;font-family:inherit;transition:background .15s,color .15s,border-color .15s;' + colorStyle;
        btn.addEventListener('click', onClick);
        return btn;
      }

      function _featureBadge(active) {
        const span = document.createElement('span');
        if (active) {
          span.style.cssText = 'display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:rgba(22,163,74,.12);color:var(--green-600);font-weight:500;';
          span.textContent = 'Да';
        } else {
          span.style.cssText = 'color:var(--slate-400);font-size:12px;';
          span.textContent = '—';
        }
        return span;
      }

      async function adminLoadUsers() {
        const tbody = document.getElementById('adm-users-body');
        tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--slate-400);padding:16px;">Загрузка...</td></tr>';
        try {
          const r = await apiFetch('/admin/users');
          const users = await r.json();
          tbody.innerHTML = '';
          users.forEach(u => {
            const isPrimary = u.id === 1;
            const tr = document.createElement('tr');

            const tdId = document.createElement('td');
            tdId.textContent = u.id;

            const tdName = document.createElement('td');
            tdName.style.fontWeight = '600';
            tdName.textContent = u.username;
            if (isPrimary) {
              const star = document.createElement('span');
              star.textContent = ' ★';
              star.style.cssText = 'font-size:10px;color:#f59e0b;';
              tdName.appendChild(star);
            }

            const tdCount = document.createElement('td');
            tdCount.textContent = u.portfolio_count;

            const tdDate = document.createElement('td');
            tdDate.textContent = u.created_at ? u.created_at.substring(0, 10) : '—';

            const tdLastLogin = document.createElement('td');
            if (u.last_login) {
              const d = new Date(u.last_login);
              const daysAgo = Math.floor((Date.now() - d.getTime()) / 86400000);
              tdLastLogin.title = u.last_login.substring(0, 19).replace('T', ' ') + ' UTC';
              tdLastLogin.style.cssText = 'color:var(--text-secondary);font-size:11px;white-space:nowrap;';
              tdLastLogin.textContent = daysAgo === 0 ? 'Сегодня' : daysAgo === 1 ? 'Вчера' : `${daysAgo}д назад`;
            } else {
              tdLastLogin.style.color = 'var(--slate-400)';
              tdLastLogin.textContent = '—';
            }

            const tdSync = document.createElement('td');
            tdSync.style.textAlign = 'center';
            tdSync.appendChild(_featureBadge(u.has_autosync));

            const tdTg = document.createElement('td');
            tdTg.style.textAlign = 'center';
            tdTg.appendChild(_featureBadge(u.has_tg));

            const tdCoupon = document.createElement('td');
            tdCoupon.style.textAlign = 'center';
            tdCoupon.appendChild(_featureBadge(u.has_coupon_notif));

            const tdShare = document.createElement('td');
            tdShare.style.textAlign = 'center';
            tdShare.appendChild(_featureBadge(u.has_sharing));

            const tdRole = document.createElement('td');
            const roleSpan = document.createElement('span');
            if (u.is_admin) {
              roleSpan.textContent = t('adm.roleAdmin');
              roleSpan.style.cssText = 'color:#f59e0b;font-weight:600;';
            } else {
              roleSpan.textContent = t('adm.roleUser');
              roleSpan.style.color = 'var(--slate-400)';
            }
            tdRole.appendChild(roleSpan);

            // Pro status + expiry
            const tdPro = document.createElement('td');
            tdPro.style.textAlign = 'center';
            tdPro.style.whiteSpace = 'nowrap';
            if (u.is_pro) {
              const badge = document.createElement('span');
              badge.textContent = 'PRO';
              badge.style.cssText = 'font-size:10px;font-weight:700;color:var(--green-400);background:rgba(74,222,128,.12);border-radius:4px;padding:2px 6px;';
              tdPro.appendChild(badge);
              if (u.pro_until) {
                const until = document.createElement('div');
                until.textContent = 'до ' + String(u.pro_until).substring(0, 10);
                until.style.cssText = 'font-size:10px;color:var(--text-muted);margin-top:2px;';
                tdPro.appendChild(until);
              } else {
                const inf = document.createElement('div');
                inf.textContent = 'бессрочно';
                inf.style.cssText = 'font-size:10px;color:var(--text-muted);margin-top:2px;';
                tdPro.appendChild(inf);
              }
            } else {
              tdPro.textContent = '—';
              tdPro.style.color = 'var(--slate-400)';
            }

            const tdActions = document.createElement('td');
            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:2px;justify-content:center;';

            const pwdBtn = _mkIconBtn('🔑', t('adm.btnPassword'), 'background:rgba(59,130,246,.12);color:var(--blue-600);border:1px solid rgba(59,130,246,.25);',
              () => adminOpenPwdModal(u.id, u.username));
            div.appendChild(pwdBtn);

            // Grant / revoke Pro
            const proBtn = _mkIconBtn(u.is_pro ? '★' : '☆', u.is_pro ? 'Снять Pro' : 'Выдать Pro',
              u.is_pro ? 'background:rgba(74,222,128,.12);color:var(--green-400);border:1px solid rgba(74,222,128,.3);'
                       : 'background:rgba(148,163,184,.1);color:var(--text-muted);border:1px solid var(--border);',
              function() { adminSetPro(u.id, !u.is_pro, this); });
            div.appendChild(proBtn);

            if (u.is_admin && !isPrimary) {
              const demoteBtn = _mkIconBtn('⬇', t('adm.btnDemote'), 'background:rgba(217,119,6,.12);color:#d97706;border:1px solid rgba(217,119,6,.25);',
                function() { adminSetRole(u.id, false, this); });
              div.appendChild(demoteBtn);
            } else if (!u.is_admin) {
              const promoteBtn = _mkIconBtn('⬆', t('adm.btnPromote'), 'background:rgba(217,119,6,.12);color:#d97706;border:1px solid rgba(217,119,6,.25);',
                function() { adminSetRole(u.id, true, this); });
              div.appendChild(promoteBtn);
              const delBtn = _mkIconBtn('🗑', t('adm.btnDelete'), 'background:rgba(220,38,38,.1);color:var(--red-400);border:1px solid rgba(220,38,38,.25);',
                () => adminDeleteUser(u.id, u.username));
              div.appendChild(delBtn);
            }

            tdActions.appendChild(div);
            tr.append(tdId, tdName, tdCount, tdDate, tdLastLogin, tdSync, tdTg, tdCoupon, tdShare, tdRole, tdPro, tdActions);
            tbody.appendChild(tr);
          });
        } catch(e) {
          tbody.innerHTML = `<tr><td colspan="12" style="color:var(--red-400);text-align:center;">${t('adm.loadError')}</td></tr>`;
        }
      }

      async function adminSetPro(userId, makePro, btn) {
        if (btn) btn.disabled = true;
        try {
          const r = await apiFetch(`/admin/users/${userId}/pro`, {
            method: 'PATCH',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ is_pro: makePro }),
          });
          if (!r.ok) throw new Error();
          toast(makePro ? 'Pro выдан' : 'Pro снят', 'success');
          adminLoadUsers();
        } catch(e) {
          toast('Ошибка изменения Pro', 'error');
          if (btn) btn.disabled = false;
        }
      }

      async function adminLoadPortfolios() {
        const tbody = document.getElementById('adm-portfolios-body');
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--slate-400);padding:16px;">${t('adm.loading')}</td></tr>`;
        try {
          const r = await apiFetch('/admin/portfolios');
          const portfolios = await r.json();
          tbody.innerHTML = '';
          portfolios.forEach(p => {
            const tr = document.createElement('tr');

            const tdId = document.createElement('td');
            tdId.textContent = p.id;

            const tdName = document.createElement('td');
            tdName.style.fontWeight = '500';
            tdName.textContent = p.name;

            const tdUser = document.createElement('td');
            tdUser.textContent = p.username;
            const userIdSpan = document.createElement('span');
            userIdSpan.textContent = ` #${p.user_id}`;
            userIdSpan.style.cssText = 'color:var(--slate-400);font-size:10px;';
            tdUser.appendChild(userIdSpan);

            const tdCount = document.createElement('td');
            tdCount.textContent = p.item_count;

            const tdShared = document.createElement('td');
            if (p.share_token) {
              const link = document.createElement('a');
              link.href = `/share/${encodeURIComponent(p.share_token)}`;
              link.target = '_blank';
              link.style.cssText = 'color:var(--blue-600);font-size:11px;';
              link.textContent = t('adm.openLink');
              tdShared.appendChild(link);
            } else {
              const dash = document.createElement('span');
              dash.textContent = '—';
              dash.style.color = 'var(--slate-400)';
              tdShared.appendChild(dash);
            }

            const tdDate = document.createElement('td');
            tdDate.textContent = p.created_at ? p.created_at.substring(0, 10) : '—';

            const tdActions = document.createElement('td');
            const delBtn = _mkIconBtn('🗑', t('adm.btnDelete'), 'background:rgba(220,38,38,.1);color:var(--red-400);border:1px solid rgba(220,38,38,.25);',
              () => adminDeletePortfolio(p.id, p.name));
            tdActions.appendChild(delBtn);

            tr.append(tdId, tdName, tdUser, tdCount, tdShared, tdDate, tdActions);
            tbody.appendChild(tr);
          });
        } catch(e) {
          tbody.innerHTML = `<tr><td colspan="7" style="color:var(--red-400);text-align:center;">${t('adm.loadError')}</td></tr>`;
        }
      }

      function adminOpenPwdModal(userId, username) {
        _admPwdUserId = userId;
        document.getElementById('adm-pwd-modal').style.display = 'flex';
        document.getElementById('adm-pwd-input').value = '';
        document.getElementById('adm-pwd-status').textContent = '';
        document.getElementById('adm-pwd-modal').querySelector('.modal-title').textContent =
          t('adm.passwordLabel') + username;
        setTimeout(() => document.getElementById('adm-pwd-input').focus(), 50);
      }

      async function adminSubmitPassword() {
        const pwd = document.getElementById('adm-pwd-input').value.trim();
        const status = document.getElementById('adm-pwd-status');
        if (pwd.length < 6) { status.textContent = t('adm.pwMinLength'); return; }
        try {
          const r = await apiFetch(`/admin/users/${_admPwdUserId}/password`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: pwd}),
          });
          if (r.ok) {
            document.getElementById('adm-pwd-modal').style.display = 'none';
            document.getElementById('adm-status').textContent = t('adm.pwChanged');
            document.getElementById('adm-status').style.color = 'var(--green-600)';
          } else {
            const e = await r.json();
            status.textContent = e.detail || 'Ошибка';
          }
        } catch(e) { status.textContent = 'Ошибка сети'; }
      }

      async function adminSetRole(userId, makeAdmin, btn) {
        const action = makeAdmin ? t('adm.btnPromote') : t('adm.btnDemote');
        if (!confirm(t('adm.confirmAction').replace('{action}', action))) return;
        btn.disabled = true;
        try {
          const r = await apiFetch(`/admin/users/${userId}/role`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({is_admin: makeAdmin}),
          });
          if (r.ok) {
            await adminLoadUsers();
          } else {
            const e = await r.json();
            toast(e.detail || 'Ошибка', 'error');
            btn.disabled = false;
          }
        } catch(e) { toast(t('err.network', 'Ошибка сети'), 'error'); btn.disabled = false; }
      }

      async function adminDeleteUser(userId, username) {
        if (!confirm(t('adm.confirmDelete').replace('{username}', username))) return;
        try {
          const r = await apiFetch(`/admin/users/${userId}`, {method: 'DELETE'});
          if (r.ok) {
            await adminLoadUsers();
            await adminLoadPortfolios();
            await adminLoadStats();
          } else {
            const e = await r.json();
            toast(e.detail || 'Ошибка', 'error');
          }
        } catch(e) { toast(t('err.network', 'Ошибка сети'), 'error'); }
      }

      async function adminDeletePortfolio(portfolioId, name) {
        if (!confirm(t('adm.confirmDeletePort').replace('{name}', name))) return;
        try {
          const r = await apiFetch(`/admin/portfolios/${portfolioId}`, {method: 'DELETE'});
          if (r.ok) {
            await adminLoadPortfolios();
            await adminLoadStats();
          } else {
            const e = await r.json();
            toast(e.detail || 'Ошибка', 'error');
          }
        } catch(e) { toast(t('err.network', 'Ошибка сети'), 'error'); }
      }

      async function adminLoadBackups() {
        const tbody = document.getElementById('adm-backup-body');
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--slate-400);padding:16px;">Загрузка...</td></tr>';
        try {
          const r = await apiFetch('/admin/backups');
          if (!r.ok) throw new Error();
          const data = await r.json();
          document.getElementById('adm-backup-keep').value = data.keep_count;
          document.getElementById('adm-backup-hour').value = (data.daily_hour + 10) % 24;
          if (!data.backups.length) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--slate-400);padding:16px;">Нет резервных копий</td></tr>';
            return;
          }
          tbody.innerHTML = '';
          data.backups.forEach(b => {
            const tr = document.createElement('tr');
            const parts = b.filename.replace('.db','').split('_');
            const dateStr = parts.length >= 3 ? `${parts[1].substring(0,4)}-${parts[1].substring(4,6)}-${parts[1].substring(6,8)} ${parts[2].substring(0,2)}:${parts[2].substring(2,4)}` : '—';
            const label = parts[3] || 'startup';
            const sizeKb = (b.size / 1024).toFixed(0) + ' KB';
            const labelBadge = label === 'manual'
              ? '<span style="background:rgba(59,130,246,.12);color:var(--blue-600);padding:1px 6px;border-radius:8px;font-size:10px;">ручной</span>'
              : label === 'auto'
              ? '<span style="background:rgba(22,163,74,.12);color:var(--green-600);padding:1px 6px;border-radius:8px;font-size:10px;">авто</span>'
              : '<span style="color:var(--slate-400);font-size:10px;">запуск</span>';

            const tdName = document.createElement('td');
            tdName.style.fontSize = '11px';
            tdName.textContent = b.filename;

            const tdSize = document.createElement('td');
            tdSize.style.cssText = 'font-size:11px;color:var(--text-secondary);';
            tdSize.textContent = sizeKb;

            const tdDate = document.createElement('td');
            tdDate.style.cssText = 'font-size:11px;color:var(--text-secondary);white-space:nowrap;';
            tdDate.textContent = dateStr;

            const tdLabel = document.createElement('td');
            tdLabel.innerHTML = labelBadge;

            const tdAct = document.createElement('td');
            const actDiv = document.createElement('div');
            actDiv.style.cssText = 'display:flex;gap:4px;';
            const dlBtn = _mkIconBtn('⬇', 'Скачать', 'background:rgba(59,130,246,.12);color:var(--blue-600);border:1px solid rgba(59,130,246,.25);',
              () => window.open(`/admin/backups/${encodeURIComponent(b.filename)}`, '_blank'));
            const delBtn = _mkIconBtn('🗑', 'Удалить', 'background:rgba(220,38,38,.1);color:var(--red-400);border:1px solid rgba(220,38,38,.25);',
              () => adminDeleteBackup(b.filename));
            actDiv.append(dlBtn, delBtn);
            tdAct.appendChild(actDiv);

            tr.append(tdName, tdSize, tdDate, tdLabel, tdAct);
            tbody.appendChild(tr);
          });
        } catch(e) {
          tbody.innerHTML = '<tr><td colspan="5" style="color:var(--red-400);text-align:center;">Ошибка загрузки</td></tr>';
        }
      }

      async function adminCreateBackup() {
        const btn = event.target;
        btn.disabled = true;
        btn.textContent = 'Создание...';
        try {
          const r = await apiFetch('/admin/backups', {method: 'POST'});
          if (r.ok) {
            await adminLoadBackups();
            toast('Резервная копия создана', 'success');
          } else {
            toast('Ошибка создания бэкапа', 'error');
          }
        } catch(e) { toast('Ошибка сети', 'error'); }
        finally { btn.disabled = false; btn.textContent = '+ Создать сейчас'; }
      }

      async function adminDeleteBackup(filename) {
        if (!confirm(`Удалить резервную копию ${filename}?`)) return;
        try {
          const r = await apiFetch(`/admin/backups/${encodeURIComponent(filename)}`, {method: 'DELETE'});
          if (r.ok) { await adminLoadBackups(); }
          else { toast('Ошибка удаления', 'error'); }
        } catch(e) { toast('Ошибка сети', 'error'); }
      }

      async function adminSaveBackupSettings() {
        const keep = parseInt(document.getElementById('adm-backup-keep').value);
        const hourVld = parseInt(document.getElementById('adm-backup-hour').value);
        const hour = (hourVld - 10 + 24) % 24;
        if (isNaN(keep) || keep < 1 || isNaN(hourVld) || hourVld < 0 || hourVld > 23) {
          toast('Некорректные значения', 'error'); return;
        }
        try {
          const r = await apiFetch('/admin/backup-settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({backup_keep_count: keep, backup_daily_hour: hour}),
          });
          if (r.ok) {
            const s = document.getElementById('adm-backup-settings-status');
            s.textContent = '✓ Сохранено';
            setTimeout(() => s.textContent = '', 2000);
          } else { toast('Ошибка сохранения', 'error'); }
        } catch(e) { toast('Ошибка сети', 'error'); }
      }

      async function adminLoadAuditLog() {
        const tbody = document.getElementById('adm-audit-body');
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--slate-400);padding:16px;">Загрузка...</td></tr>';
        try {
          const r = await apiFetch('/admin/audit-log?limit=100&offset=0');
          if (!r.ok) throw new Error();
          const entries = await r.json();
          if (!entries.length) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--slate-400);padding:16px;">Нет записей</td></tr>';
            return;
          }
          tbody.innerHTML = '';
          entries.forEach(e => {
            const tr = document.createElement('tr');
            let detailsText = '—';
            if (e.details) {
              try { detailsText = JSON.stringify(JSON.parse(e.details)).substring(0, 80); }
              catch { detailsText = String(e.details).substring(0, 80); }
            }
            const cells = [
              e.id,
              e.admin_username || e.admin_user_id,
              e.action,
              e.target_type || '—',
              e.target_id !== null && e.target_id !== undefined ? e.target_id : '—',
              detailsText,
              e.ip_address || '—',
              e.created_at ? e.created_at.substring(0, 19).replace('T', ' ') : '—',
            ];
            cells.forEach(text => {
              const td = document.createElement('td');
              td.textContent = text;
              td.style.fontSize = '11px';
              tr.appendChild(td);
            });
            tbody.appendChild(tr);
          });
        } catch(err) {
          tbody.innerHTML = '<tr><td colspan="8" style="color:var(--red-400);text-align:center;">Ошибка загрузки</td></tr>';
        }
      }

      // Show admin panel tab if user is admin
      let _currentUserIsAdmin = false;
      function setupAdminNav(isAdmin) {
        _currentUserIsAdmin = isAdmin;
        const btn = document.getElementById('nav-admin');
        if (btn && isAdmin) btn.style.display = '';
        // Deep-link: /admin/promo (legacy path) or /admin#<tab> opens the admin
        // panel on that sub-section. adminInit() picks the tab from hash/path.
        if (isAdmin && window.location.pathname === '/admin/promo') {
          showPanel('panel-admin');
          adminInit().catch(() => {});
        }
      }

      // Lazy-load admin data when tab is clicked
      document.getElementById('nav-admin').addEventListener('click', () => {
        setTimeout(() => adminInit(), 0);
      });

      // ── RIPPLE ON .btn ────────────────────────────────────────────
      document.addEventListener('pointerdown', (e) => {
        const btn = e.target.closest('.btn');
        if (!btn || btn.disabled) return;
        const rect = btn.getBoundingClientRect();
        const size = Math.max(rect.width, rect.height);
        const wave = document.createElement('span');
        wave.className = 'ripple-wave';
        wave.style.cssText = `width:${size}px;height:${size}px;`
          + `left:${e.clientX - rect.left - size / 2}px;`
          + `top:${e.clientY - rect.top - size / 2}px`;
        btn.appendChild(wave);
        wave.addEventListener('animationend', () => wave.remove(), { once: true });
      });

      // ── SETTINGS TABS ─────────────────────────────────────────────
      document.querySelectorAll('.stab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          document.querySelectorAll('.stab-btn').forEach(b => b.classList.remove('stab-active'));
          document.querySelectorAll('.stab-content').forEach(c => c.style.display = 'none');
          btn.classList.add('stab-active');
          const target = document.getElementById(btn.dataset.stab);
          if (target) target.style.display = 'block';
          if (btn.dataset.stab === 'stab-portfolios') settingsLoadPortfolios();
          if (btn.dataset.stab === 'stab-account') settingsLoadAccount();
        });
      });

      // Load account info
      async function settingsLoadAccount() {
        // Fill current username
        const unCurrent = document.getElementById('acc-username-current');
        if (unCurrent) unCurrent.value = localStorage.getItem('mvp_username') || '';
        try {
          const r = await apiFetch('/auth/me/portfolios-stats');
          if (!r.ok) return;
          const d = await r.json();
          const emailInput = document.getElementById('acc-email');
          if (emailInput && d.email) emailInput.value = d.email;
          const tgInput = document.getElementById('acc-tg-chat');
          if (tgInput && d.tg_chat_id) tgInput.value = d.tg_chat_id;
        } catch(e) {}
      }

      // Load portfolio stats
      async function settingsLoadPortfolios() {
        const tbody = document.getElementById('settings-portfolios-body');
        if (!tbody) return;
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--slate-400);padding:16px;">Загрузка...</td></tr>';
        try {
          const r = await apiFetch('/auth/me/portfolios-stats');
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const d = await r.json();
          tbody.innerHTML = '';
          const allPortfolios = d.portfolios || [];
          allPortfolios.forEach(p => {
            const tr = document.createElement('tr');
            tr.id = `sp-row-${p.id}`;
            const riskKey = p.risk || 'unknown';
            const riskInfo = getRiskInfo(riskKey);
            const riskLabel = riskInfo ? riskInfo.label : riskKey;
            const riskColor = riskInfo ? riskInfo.color : '#94a3b8';
            const sumFormatted = p.total_cost > 0 ? fmtAmount(p.total_cost) : '—';

            // td: name
            const tdName = document.createElement('td');
            const nameSpan = document.createElement('span');
            nameSpan.id = `sp-name-${p.id}`;
            nameSpan.textContent = p.name;
            const nameInput = document.createElement('input');
            nameInput.id = `sp-input-${p.id}`;
            nameInput.className = 'input';
            nameInput.style.cssText = 'display:none;max-width:180px;padding:4px 8px;font-size:12px;';
            nameInput.value = p.name;
            tdName.appendChild(nameSpan);
            tdName.appendChild(nameInput);

            // td: item count
            const tdCount = document.createElement('td');
            tdCount.textContent = p.item_count;

            // td: sum
            const tdSum = document.createElement('td');
            tdSum.style.whiteSpace = 'nowrap';
            tdSum.textContent = sumFormatted;

            // td: risk
            const tdRisk = document.createElement('td');
            const riskSpan = document.createElement('span');
            riskSpan.style.cssText = `color:${riskColor};font-weight:500;font-size:12px;`;
            riskSpan.textContent = riskLabel;
            tdRisk.appendChild(riskSpan);

            // td: actions
            const tdActions = document.createElement('td');
            tdActions.style.cssText = 'white-space:nowrap;text-align:right;';
            const actionsDiv = document.createElement('div');
            actionsDiv.style.cssText = 'display:flex;gap:4px;align-items:center;flex-wrap:nowrap;justify-content:flex-end;';

            // merge select + button
            const others = allPortfolios.filter(x => x.id !== p.id);
            if (others.length > 0) {
              const sel = document.createElement('select');
              sel.id = `sp-merge-sel-${p.id}`;
              sel.className = 'select-input';
              sel.style.cssText = 'height:22px;font-size:10px;padding:0 4px;width:auto;';
              others.forEach(x => {
                const opt = document.createElement('option');
                opt.value = x.id;
                opt.textContent = x.name;
                sel.appendChild(opt);
              });
              const mergeBtn = document.createElement('button');
              mergeBtn.className = 'btn-sm-move';
              mergeBtn.title = t('settings.portfolios.mergeTitle');
              mergeBtn.textContent = '⇄';
              mergeBtn.addEventListener('click', () => settingsMergePortfolio(p.id, p.name));
              actionsDiv.appendChild(sel);
              actionsDiv.appendChild(mergeBtn);
            }

            // edit btn
            const editBtn = document.createElement('button');
            editBtn.className = 'btn-sm';
            editBtn.id = `sp-edit-${p.id}`;
            editBtn.style.cssText = 'color:var(--text-secondary);background:var(--bg-elevated);border-color:var(--border);';
            editBtn.textContent = t('portfolio.rename');
            editBtn.addEventListener('click', () => settingsEditPortfolio(p.id));

            // save btn
            const saveBtn = document.createElement('button');
            saveBtn.className = 'btn-sm';
            saveBtn.id = `sp-save-${p.id}`;
            saveBtn.style.cssText = 'display:none;background:var(--blue-600);color:#fff;border-color:var(--blue-600);';
            saveBtn.textContent = t('portfolio.save');
            saveBtn.addEventListener('click', () => settingsSavePortfolio(p.id));

            // share btn
            const shareBtn = document.createElement('button');
            shareBtn.className = 'btn-sm';
            shareBtn.style.cssText = 'color:var(--text-secondary);background:var(--bg-elevated);border-color:var(--border);';
            shareBtn.title = t('nav.share');
            shareBtn.textContent = t('settings.portfolios.shareBtn');
            shareBtn.addEventListener('click', () => openShareModalFor(p.id));

            // delete btn
            const delBtn = document.createElement('button');
            delBtn.className = 'btn-sm';
            delBtn.style.cssText = 'color:var(--red-400);background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.25);';
            delBtn.title = t('settings.portfolios.deleteTitle');
            delBtn.textContent = t('settings.portfolios.deleteBtn');
            delBtn.addEventListener('click', () => settingsDeletePortfolio(p.id, p.name));

            actionsDiv.appendChild(editBtn);
            actionsDiv.appendChild(saveBtn);
            actionsDiv.appendChild(shareBtn);
            actionsDiv.appendChild(delBtn);
            tdActions.appendChild(actionsDiv);

            tr.appendChild(tdName);
            tr.appendChild(tdCount);
            tr.appendChild(tdSum);
            tr.appendChild(tdRisk);
            tr.appendChild(tdActions);
            tbody.appendChild(tr);
          });
        } catch(e) {
          console.error('settingsLoadPortfolios error:', e);
          tbody.innerHTML = '<tr><td colspan="6" style="color:var(--red-400);text-align:center;">Ошибка загрузки</td></tr>';
        }
      }

      async function settingsMergePortfolio(sourceId, sourceName) {
        const sel = document.getElementById(`sp-merge-sel-${sourceId}`);
        if (!sel) return;
        const targetId = parseInt(sel.value);
        if (!targetId) return;
        const targetName = sel.options[sel.selectedIndex].text;
        const msg = t('settings.portfolios.mergeConfirm').replace('{source}', sourceName).replace('{target}', targetName);
        showConfirm(t('settings.portfolios.mergeTitle'), msg, async () => {
          try {
            const r = await apiFetch(`/portfolios/${sourceId}/merge-into/${targetId}`, { method: 'POST' });
            if (!r.ok) { toast(t('settings.portfolios.mergeError'), 'error'); return; }
            await r.json();
            document.getElementById(`sp-row-${sourceId}`)?.remove();
            await loadPortfolios();
            updatePortfolioSelector();
            settingsLoadPortfolios();
            if (portfolioId === sourceId) { portfolioId = targetId; renderTable(); }
          } catch(e) { toast(t('settings.portfolios.mergeError'), 'error'); }
        });
      }

      function settingsEditPortfolio(id) {
        document.getElementById(`sp-name-${id}`).style.display = 'none';
        document.getElementById(`sp-input-${id}`).style.display = '';
        document.getElementById(`sp-edit-${id}`).style.display = 'none';
        document.getElementById(`sp-save-${id}`).style.display = '';
        document.getElementById(`sp-input-${id}`).focus();
      }

      async function settingsSavePortfolio(id) {
        const newName = document.getElementById(`sp-input-${id}`).value.trim();
        if (!newName) return;
        try {
          const r = await apiFetch(`/portfolios/${id}`, { method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name: newName }) });
          if (!r.ok) { toast(t('err.saveFailed', 'Ошибка сохранения'), 'error'); return; }
          document.getElementById(`sp-name-${id}`).textContent = newName;
          document.getElementById(`sp-name-${id}`).style.display = '';
          document.getElementById(`sp-input-${id}`).style.display = 'none';
          document.getElementById(`sp-edit-${id}`).style.display = '';
          document.getElementById(`sp-save-${id}`).style.display = 'none';
          // Update portfolio selector
          await loadPortfolios();
          updatePortfolioSelector();
        } catch(e) { toast(t('err.saveFailed', 'Ошибка сохранения'), 'error'); }
      }

      async function settingsDeletePortfolio(id, name) {
        const msg = t('settings.portfolios.deleteConfirm').replace('{name}', name);
        showConfirm(t('settings.portfolios.deleteTitle'), msg, async () => {
          try {
            const r = await apiFetch(`/portfolios/${id}`, { method: 'DELETE' });
            if (!r.ok) { toast(t('settings.portfolios.deleteError'), 'error'); return; }
            document.getElementById(`sp-row-${id}`)?.remove();
            await loadPortfolios();
            updatePortfolioSelector();
            settingsLoadPortfolios();
            if (portfolioId === id) { portfolioId = portfolios[0]?.id || null; renderTable(); }
          } catch(e) { toast(t('settings.portfolios.deleteError'), 'error'); }
        });
      }

      // ── TOPBAR: rename / delete active portfolio ─────────────────
      async function topbarRenamePortfolio() {
        if (isReadOnly || isAllMode || !portfolioId) return;
        const active = portfolios.find(p => p.id === portfolioId);
        const cur = active ? active.name : '';
        const res = await showPrompt(t('portfolio.rename'), '', { value: cur, okText: t('confirm.ok') });
        if (res === null) return;
        const newName = res.trim();
        if (!newName || newName === cur) return;
        try {
          const r = await apiFetch(`/portfolios/${portfolioId}`, {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name: newName }),
          });
          if (!r.ok) { toast(t('err.saveFailed', 'Ошибка сохранения'), 'error'); return; }
          await loadPortfolios();
          updatePortfolioSelector();
        } catch(e) { toast(t('err.saveFailed', 'Ошибка сохранения'), 'error'); }
      }

      async function topbarDeletePortfolio() {
        if (isReadOnly || isAllMode || !portfolioId) return;
        const active = portfolios.find(p => p.id === portfolioId);
        const name = active ? active.name : '';
        // Deleting a non-empty portfolio is irreversible (cascades positions,
        // snapshots, coupons). Require typing the name to confirm — guards against
        // an accidental click on the header trash icon.
        const hasData = Array.isArray(tableRows) && tableRows.length > 0;
        if (hasData) {
          const typed = await showPrompt(
            'Удалить портфель?',
            'Удаление «' + name + '» необратимо — будут удалены все ' +
            tableRows.length + ' бумаг(и), история и купоны.\nЧтобы подтвердить, введите название портфеля:',
            { placeholder: name, okText: 'Удалить', mustMatch: name,
              matchError: 'Название не совпадает с «' + name + '»' });
          if (typed === null) return;  // cancelled or didn't match
          try {
            const r = await apiFetch(`/portfolios/${portfolioId}`, { method: 'DELETE' });
            if (!r.ok) { toast(t('settings.portfolios.deleteError'), 'error'); return; }
            await loadPortfolios();
            updatePortfolioSelector();
            if (portfolioId === active.id) { portfolioId = portfolios[0]?.id || null; }
            if (portfolioId) { syncTableFromServer().catch(() => {}); } else { renderTable(); }
            toast('Портфель удалён', 'success');
          } catch (e) { toast(t('settings.portfolios.deleteError'), 'error'); }
          return;
        }
        settingsDeletePortfolio(portfolioId, name);
      }

      // Change username
      document.getElementById('acc-username-btn').addEventListener('click', async () => {
        const newUsername = document.getElementById('acc-username-new').value.trim();
        const status = document.getElementById('acc-username-status');
        if (newUsername.length < 3) { status.textContent = t('settings.account.usernameMinLength'); status.className = 'status error'; return; }
        try {
          const r = await apiFetch('/auth/me/username', { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ new_username: newUsername }) });
          if (r.status === 204) {
            localStorage.setItem('mvp_username', newUsername);
            document.getElementById('acc-username-current').value = newUsername;
            document.getElementById('acc-username-new').value = '';
            status.textContent = t('settings.account.usernameChanged');
            status.className = 'status ok';
          } else {
            const d = await r.json().catch(() => ({}));
            status.textContent = d.detail || t('settings.account.usernameTaken');
            status.className = 'status error';
          }
        } catch(e) { status.textContent = t('settings.account.pwConnError'); status.className = 'status error'; }
      });

      // Change password
      document.getElementById('acc-pw-btn').addEventListener('click', async () => {
        const oldPw = document.getElementById('acc-old-pw').value;
        const newPw = document.getElementById('acc-new-pw').value;
        const newPw2 = document.getElementById('acc-new-pw2').value;
        const status = document.getElementById('acc-pw-status');
        if (newPw !== newPw2) { status.textContent = t('settings.account.pwMismatch'); status.className = 'status error'; return; }
        if (newPw.length < 6) { status.textContent = t('settings.account.pwMinLength'); status.className = 'status error'; return; }
        try {
          const r = await apiFetch('/auth/change-password', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ old_password: oldPw, new_password: newPw }) });
          if (r.status === 204) {
            status.textContent = t('settings.account.pwChanged');
            status.className = 'status ok';
            document.getElementById('acc-old-pw').value = '';
            document.getElementById('acc-new-pw').value = '';
            document.getElementById('acc-new-pw2').value = '';
          } else {
            const d = await r.json().catch(() => ({}));
            status.textContent = d.detail || t('settings.account.pwError');
            status.className = 'status error';
          }
        } catch(e) { status.textContent = t('settings.account.pwConnError'); status.className = 'status error'; }
      });

      // Change email
      document.getElementById('acc-email-btn').addEventListener('click', async () => {
        const email = document.getElementById('acc-email').value.trim();
        const status = document.getElementById('acc-email-status');
        if (!email) { status.textContent = t('settings.account.emailRequired'); status.className = 'status error'; return; }
        try {
          const r = await apiFetch('/auth/change-email', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ email }) });
          if (r.ok) {
            const d = await r.json();
            if (d.smtp_available) {
              status.textContent = t('settings.account.emailSavedSmtp');
            } else {
              status.textContent = t('settings.account.emailSavedNoSmtp');
            }
            status.className = 'status ok';
          } else {
            status.textContent = t('settings.account.emailError');
            status.className = 'status error';
          }
        } catch(e) { status.textContent = t('settings.account.emailConnError'); status.className = 'status error'; }
      });

      // Save Telegram Chat ID
      document.getElementById('acc-tg-btn').addEventListener('click', async () => {
        const tgId = document.getElementById('acc-tg-chat').value.trim();
        const st = document.getElementById('acc-tg-status');
        if (!tgId) { st.textContent = t('settings.account.tgRequired'); st.className = 'status error'; return; }
        // @username Bot API не принимает: sendMessage вернёт «chat not found».
        if (!/^-?\d{1,32}$/.test(tgId)) {
          st.textContent = t('settings.account.tgNotNumeric', 'Chat ID — это число, а не @username');
          st.className = 'status error';
          return;
        }
        try {
          const r = await apiFetch('/auth/me/telegram', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ tg_chat_id: tgId }) });
          if (r.status === 204) { st.textContent = t('settings.account.tgSaved'); st.className = 'status ok'; }
          else { st.textContent = t('settings.account.tgError'); st.className = 'status error'; }
        } catch(e) { st.textContent = t('settings.account.tgConnError'); st.className = 'status error'; }
      });

      // Load account on settings tab open
      document.querySelector('[data-panel="panel-settings"]')?.addEventListener('click', () => {
        setTimeout(settingsLoadAccount, 100);
      });

      // ── БАННЕР «Telegram отвязан» ────────────────────────────────
      // Показывается тем, у кого в поле лежал @username: Bot API его не
      // резолвит, адрес обнулили за них. Скрытие запоминаем локально —
      // серверный флаг гаснет, только когда введён рабочий ID.
      const TG_RESET_DISMISS_KEY = 'mvp_tg_reset_dismissed';

      window.checkTgResetBanner = checkTgResetBanner;
      async function checkTgResetBanner() {
        const banner = document.getElementById('tg-reset-banner');
        if (!banner) return;
        if (localStorage.getItem(TG_RESET_DISMISS_KEY) === '1') return;
        try {
          const r = await apiFetch('/auth/me/portfolios-stats');
          if (!r.ok) return;
          const d = await r.json();
          if (d.tg_chat_id_reset) banner.style.display = '';
        } catch (e) {}
      }

      document.getElementById('tg-reset-fix-btn')?.addEventListener('click', () => {
        document.getElementById('tg-reset-banner').style.display = 'none';
        showPanel('panel-settings');
        document.querySelector('.stab-btn[data-stab="stab-account"]')?.click();
        setTimeout(() => {
          const inp = document.getElementById('acc-tg-chat');
          if (inp) { inp.focus(); inp.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
        }, 150);
      });

      document.getElementById('tg-reset-dismiss-btn')?.addEventListener('click', () => {
        localStorage.setItem(TG_RESET_DISMISS_KEY, '1');
        document.getElementById('tg-reset-banner').style.display = 'none';
      });

      // ── WATCHLIST ─────────────────────────────────────────────────
      // ── WATCHLIST STUBS (panel removed) ─────────────────────────
      async function loadWatchlist() {}
      async function addToWatchlist() {}
      async function removeFromWatchlist() {}

      // ── ANALYTICS LAZY LOAD ──────────────────────────────────────
      let _analyticsLoaded = false;
      function scheduleAnalytics() {
        _analyticsLoaded = false;
        const targetIds = ['analytics-history-card','swot-card','recommendations-card'];
        const obs = new IntersectionObserver(entries => {
          if (entries.some(e => e.isIntersecting) && !_analyticsLoaded && tableRows.length) {
            _analyticsLoaded = true;
            loadExtraAnalytics();  // CBR key rate + anomalies + events + realized + cash
            renderAnalytics(tableRows);
            loadAnalyticsHistory(30);
            renderSwotAnalysis(tableRows);
            loadPortfolioRecommendations();
          }
        }, { threshold: 0.05 });
        targetIds.forEach(id => {
          const el = document.getElementById(id);
          if (el) obs.observe(el);
        });
      }

      async function loadExtraAnalytics() {
        if (shareToken) return; // share-mode не имеет этих endpoint-ов
        if (!isAllMode && !portfolioId) return;
        try {
          const extraUrl = isAllMode
            ? '/portfolios/all/analytics-extra'
            : `/portfolios/${portfolioId}/analytics-extra`;
          const [keyRateR, extraR] = await Promise.all([
            apiFetch('/portfolios/meta/key-rate').then(r => r.ok ? r.json() : null).catch(() => null),
            apiFetch(extraUrl).then(r => r.ok ? r.json() : null).catch(() => null),
          ]);
          if (keyRateR && keyRateR.key_rate) window._keyRate = keyRateR.key_rate;
          if (extraR) {
            window._analyticsExtra = extraR;
            // В all-режиме «свободно» = сумма по всем счетам (free_cash_rub с бэка),
            // а не остаток последнего просмотренного портфеля.
            if (isAllMode) {
              const allCash = Number(extraR.free_cash_rub || 0);
              if (allCash !== Number(window.cashTotalRub || 0)) {
                window.cashTotalRub = allCash;
                renderTable();
              }
            }
            if (typeof updateStatCards === 'function' && tableRows.length) {
              updateStatCards(calculateSummary(tableRows));
              updateYTMWidget(tableRows);
            }
            renderAnomaliesBanner(extraR.anomalies || []);
            renderCashReinvestCard(extraR);
            renderEventsCard(extraR.events || []);
          }
          // Re-render YTM curve with key-rate line (sell-first too — its scoring
          // uses keyRate). Both rendered initially before extra arrived.
          if (tableRows.length) {
            if (typeof renderYTMCurve === 'function') renderYTMCurve(tableRows);
            if (typeof renderSellFirst === 'function') renderSellFirst(tableRows);
          }
        } catch (e) { /* silent */ }
      }

      // ── SWOT ANALYSIS ────────────────────────────────────────────
      function _swotPickDiversified(tagged, maxCount) {
        const result = [], usedCats = new Set();
        for (const item of tagged) {
          if (result.length >= maxCount) break;
          if (!usedCats.has(item.cat)) { result.push(item.text); usedCats.add(item.cat); }
        }
        for (const item of tagged) {
          if (result.length >= maxCount) break;
          if (!result.includes(item.text)) result.push(item.text);
        }
        return result;
      }

      function renderSwotAnalysis(rows) {
        const body = document.getElementById('swot-body');
        if (!body) return;
        if (!rows || rows.length === 0) {
          body.innerHTML = `<p style="color:var(--text-muted);font-size:13px;grid-column:1/-1;padding:8px 0;">${t('swot.noData')}</p>`;
          return;
        }
        // Each fact: { cat, score, text }
        // score — приоритет (выше = важнее показывать первым)
        const facts_S = [];
        const facts_W = [];
        const S = (cat, score, text) => facts_S.push({ cat, score, text });
        const W = (cat, score, text) => facts_W.push({ cat, score, text });
        const now = new Date();
        const bonds = rows.filter(r => r.type === 'bond' || !r.type);

        // ── 1. YTM (доходность к погашению) ──────────────────────────
        const withYtm = bonds.filter(r => r.market_yield != null && r.market_yield > 0);
        if (withYtm.length) {
          const tw = withYtm.reduce((s,r) => s + (r.weight||0), 0) || 1;
          const avgYtm = withYtm.reduce((s,r) => s + (r.market_yield||0)*(r.weight||0)/tw, 0);
          if (avgYtm >= 20)      S('ytm', 90, `YTM ${avgYtm.toFixed(1)}% — существенно выше рынка, высокий доход с риском`);
          else if (avgYtm >= 16) S('ytm', 80, `YTM ${avgYtm.toFixed(1)}% — выше ключевой ставки`);
          else if (avgYtm >= 12) S('ytm', 50, `YTM ${avgYtm.toFixed(1)}% — соответствует рыночному уровню`);
          else if (avgYtm >= 8)  W('ytm', 70, `YTM ${avgYtm.toFixed(1)}% — ниже ключевой ставки, реальная доходность отрицательная`);
          else                   W('ytm', 90, `YTM ${avgYtm.toFixed(1)}% — портфель существенно проигрывает депозиту`);
        }

        // ── 2. Купонный денежный поток ────────────────────────────────
        const withCoupon = bonds.filter(r => r.coupon > 0 && r.quantity > 0 && r.current_value > 0);
        if (withCoupon.length) {
          const totalVal = withCoupon.reduce((s,r) => s + r.current_value, 0);
          const annualCoupon = withCoupon.reduce((s,r) => {
            const freq = r.coupon_period > 0 ? Math.round(365 / r.coupon_period) : 2;
            return s + r.coupon * r.quantity * freq;
          }, 0);
          const couponYield = totalVal > 0 ? annualCoupon / totalVal * 100 : 0;
          if (couponYield >= 16)     S('cashflow', 85, `Купонный доход ${couponYield.toFixed(1)}%/год — высокий денежный поток`);
          else if (couponYield >= 11) S('cashflow', 55, `Купонный доход ${couponYield.toFixed(1)}%/год — стабильные выплаты`);
          else if (couponYield < 6 && couponYield > 0) W('cashflow', 60, `Купонный доход лишь ${couponYield.toFixed(1)}%/год — слабый денежный поток`);
        }

        // ── 3. Диверсификация — единый блок (эмитенты + концентрация) ─
        // Собираем по эмитентам: один тикер = один эмитент (серии объединяем)
        const issuerWeights = {};
        rows.forEach(r => {
          const key = (r.name||r.ticker||'?')
            .replace(/\s*(АО|ПАО|ООО|ОФЗ)\s*/gi, '')
            .split(/[\s\-]/)[0].substring(0, 12);
          issuerWeights[key] = (issuerWeights[key]||0) + (r.weight||0);
        });
        const issuerCount = Object.keys(issuerWeights).length;
        const maxIssuerW = Math.max(0, ...Object.values(issuerWeights));
        const maxItemW   = rows.length ? Math.max(...rows.map(r => r.weight||0)) : 0;
        const topIssuerName = Object.entries(issuerWeights).sort((a,b)=>b[1]-a[1])[0]?.[0] || '?';

        if (maxIssuerW > 40) {
          W('diversification', 95, `Один эмитент (${topIssuerName}) — ${maxIssuerW.toFixed(0)}% портфеля: критический кредитный риск`);
        } else if (maxIssuerW > 25) {
          W('diversification', 75, `Один эмитент (${topIssuerName}) занимает ${maxIssuerW.toFixed(0)}% — снизить для безопасности`);
        } else if (maxItemW > 30) {
          W('diversification', 65, `Одна бумага занимает ${maxItemW.toFixed(0)}% портфеля — высокая концентрация`);
        } else if (issuerCount >= 8 && maxIssuerW < 20) {
          S('diversification', 80, `${issuerCount} эмитентов, максимальная доля ${maxIssuerW.toFixed(0)}% — отличная диверсификация`);
        } else if (issuerCount >= 5 && maxIssuerW < 30) {
          S('diversification', 55, `${issuerCount} эмитентов — хороший уровень диверсификации`);
        } else if (issuerCount <= 2) {
          W('diversification', 85, `Только ${issuerCount} эмитента — высокая концентрация кредитного риска`);
        }

        // ── 4. Срочная структура (лестница погашений) ─────────────────
        const yearMap = {};
        const shortBonds = [], longBonds = [];
        bonds.forEach(r => {
          if (!r.maturity_date) return;
          const mat = new Date(r.maturity_date);
          yearMap[mat.getFullYear()] = (yearMap[mat.getFullYear()]||0) + 1;
          const yrs = (mat - now) / (365.25*24*3600e3);
          if (yrs < 1) shortBonds.push(r);
          if (yrs > 5) longBonds.push(r);
        });
        const clusteredYears = Object.values(yearMap).filter(v => v >= 3).length;
        const spreadYears = Object.keys(yearMap).length;
        if (shortBonds.length >= 2) {
          const sw = shortBonds.reduce((s,r)=>s+(r.weight||0),0);
          W('maturity', 80, `${shortBonds.length} бумаги (${sw.toFixed(0)}%) гасятся в течение года — нужен план реинвестирования`);
        } else if (clusteredYears > 0) {
          W('maturity', 55, `Кластеризация: ${clusteredYears} года с 3+ погашениями — реинвестиционный риск`);
        } else if (longBonds.length >= 3) {
          const lw = longBonds.reduce((s,r)=>s+(r.weight||0),0);
          W('maturity', 50, `${longBonds.length} бумаги (${lw.toFixed(0)}%) со сроком 5+ лет — высокий процентный риск`);
        } else if (spreadYears >= 4 && !clusteredYears) {
          S('maturity', 65, `Лестница погашений по ${spreadYears} годам — снижен реинвестиционный риск`);
        }

        // ── 5. ОФЗ / государственная защита ──────────────────────────
        const ofzWeight = rows.filter(r => r.ticker && /^SU\d/.test(r.ticker))
          .reduce((s,r) => s + (r.weight||0), 0);
        if (ofzWeight >= 30) S('ofz', 70, `ОФЗ: ${ofzWeight.toFixed(0)}% портфеля — надёжная государственная защита`);
        else if (ofzWeight >= 15) S('ofz', 40, `ОФЗ ${ofzWeight.toFixed(0)}% — есть государственная защита`);
        else if (ofzWeight < 5 && rows.length >= 4) W('ofz', 40, 'Нет ОФЗ — портфель полностью в корпоративном кредитном риске');

        // ── 6. Кредитные рейтинги ─────────────────────────────────────
        const isRated = r => r.company_rating && r.company_rating !== 'N/A' && r.company_rating !== '—';
        const highRated = rows.filter(r => isRated(r) && /^(AAA|AA[^-]|ruAAA|ruAA[^-])/i.test(r.company_rating));
        const unrated   = rows.filter(r => !isRated(r));
        if (highRated.length >= 3) S('rating', 75, `${highRated.length} бумаги с рейтингом AA+/AAA — высококачественный кредитный профиль`);
        else if (highRated.length >= 1) S('rating', 40, `${highRated.length} бумаги с рейтингом AA/AAA присутствуют в портфеле`);
        if (unrated.length >= Math.ceil(rows.length / 2)) W('rating', 70, `${unrated.length} из ${rows.length} бумаг без рейтинга — кредитный риск не определён`);
        else if (unrated.length >= 3) W('rating', 50, `${unrated.length} бумаг без рейтинга — рекомендуется проверить эмитентов`);

        // ── 7. P&L и убытки ───────────────────────────────────────────
        const totalProfit = rows.reduce((s,r)=>s+(r.profit||0),0);
        const totalCost   = rows.reduce((s,r)=>s+(r.purchase_price||0)*(r.quantity||0),0);
        const lossRows    = rows.filter(r => (r.profit||0) < -100); // > 100 ₽ убытка
        if (totalCost > 0) {
          const pnlPct = totalProfit / totalCost * 100;
          if (pnlPct >= 15)      S('pnl', 85, `Портфель +${pnlPct.toFixed(1)}% к цене покупки — отличный результат`);
          else if (pnlPct >= 5)  S('pnl', 60, `Портфель в плюсе: +${pnlPct.toFixed(1)}% к цене покупки`);
          else if (pnlPct < -10) W('pnl', 85, `Портфель −${Math.abs(pnlPct).toFixed(1)}% — значительный убыток, рассмотрите ребалансировку`);
          else if (pnlPct < -3)  W('pnl', 60, `Портфель в минусе: ${pnlPct.toFixed(1)}% к цене покупки`);
        }
        if (lossRows.length >= 3 && lossRows.length > rows.length * 0.4) {
          const totalLoss = lossRows.reduce((s,r)=>s+(r.profit||0),0);
          W('losses', 65, `${lossRows.length} убыточных позиций на ${Math.abs(totalLoss).toLocaleString('ru')} ₽`);
        }

        // ── 8. Оферты ────────────────────────────────────────────────
        const nearOffer = bonds.filter(r => {
          const d = r.buyback_date || r.offer_date;
          if (!d) return false;
          const days = (new Date(d) - now) / (24*3600e3);
          return days > 0 && days < 180;
        });
        if (nearOffer.length) {
          W('offer', 85, `Оферта через <180 дней: ${nearOffer.map(r=>r.ticker).join(', ')} — риск досрочного погашения`);
        }

        // Сортируем по score убыванию, выбираем 2 из разных категорий
        facts_S.sort((a,b) => b.score - a.score);
        facts_W.sort((a,b) => b.score - a.score);
        const strengths  = _swotPickDiversified(facts_S, 2);
        const weaknesses = _swotPickDiversified(facts_W, 2);

        body.innerHTML = '';
        const mkSection = (title, items, color, accent, arrow) => {
          const div = document.createElement('div');
          div.style.cssText = `background:${color};border:1px solid ${color.replace('.08','0.18')};border-radius:var(--radius-lg);padding:14px 16px;`;
          div.innerHTML = `<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:${accent};margin-bottom:10px;">${arrow} ${title}</div>`;
          if (!items.length) {
            div.innerHTML += `<p style="font-size:12px;color:var(--text-muted);margin:0;">${t('swot.noIssues')}</p>`;
          } else {
            items.forEach(item => {
              const p = document.createElement('p');
              p.style.cssText = 'margin:0 0 7px;font-size:12px;color:var(--text-secondary);display:flex;gap:6px;align-items:flex-start;line-height:1.45;';
              p.innerHTML = `<span style="flex-shrink:0;color:${accent};margin-top:1px;">${arrow}</span><span>${esc(item)}</span>`;
              div.appendChild(p);
            });
          }
          return div;
        };
        body.appendChild(mkSection(t('swot.strengths'), strengths, 'rgba(74,222,128,.08)', 'var(--green-400)', '↑'));
        body.appendChild(mkSection(t('swot.risks'), weaknesses, 'rgba(248,113,113,.08)', 'var(--red-400)', '↓'));
      }

      // ── PORTFOLIO RECOMMENDATIONS ────────────────────────────────
      async function loadAiAnalysis() {
        const body = document.getElementById('ai-analysis-body');
        if (!body || !portfolioId) return;
        body.innerHTML = '<span style="color:var(--text-muted);">Анализирую портфель…</span>';
        try {
          const r = await apiFetch(`/portfolios/${portfolioId}/ai-analysis`);
          if (r.status === 403) {
            body.innerHTML = '<span style="color:var(--text-muted);">Доступно в тарифе Pro.</span>';
            return;
          }
          if (!r.ok) throw new Error();
          const d = await r.json();
          if (d.available === false) {
            body.innerHTML = '<span style="color:var(--text-muted);">' + _escHtml(d.message || 'AI-анализ скоро будет доступен.') + '</span>';
            return;
          }
          let html = '';
          if (d.summary) html += '<div style="margin-bottom:10px;font-weight:600;color:var(--text-primary);">' + _escHtml(d.summary) + '</div>';
          if (Array.isArray(d.points) && d.points.length) {
            html += '<ul style="margin:0;padding-left:18px;">' +
              d.points.map(p => '<li style="margin-bottom:5px;">' + _escHtml(p) + '</li>').join('') +
              '</ul>';
          }
          html += '<div style="margin-top:12px;font-size:11px;color:var(--text-muted);">AI-анализ на основе данных портфеля. Не индивидуальная инвестиционная рекомендация.</div>';
          body.innerHTML = html || '<span style="color:var(--text-muted);">Нет данных для анализа.</span>';
        } catch(e) {
          body.innerHTML = '<span style="color:var(--text-muted);">Не удалось выполнить анализ. Попробуйте позже.</span>';
        }
      }

      function _fmtRub(v) {
        return (Math.round((v || 0) * 100) / 100).toLocaleString('ru-RU') + ' ₽';
      }
      async function loadTaxReport() {
        const body = document.getElementById('tax-body');
        if (!body || !portfolioId) return;
        body.innerHTML = '<span style="color:var(--text-muted);">Считаю…</span>';
        try {
          const r = await apiFetch(`/portfolios/${portfolioId}/tax-report`);
          if (r.status === 403) { body.innerHTML = '<span style="color:var(--text-muted);">Доступно в тарифе Pro.</span>'; return; }
          if (!r.ok) throw new Error();
          const d = await r.json();
          const s = d.summary;
          body.innerHTML =
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:12px;">' +
              _taxStat('Купонный доход', s.coupon_income, 'налог ' + _fmtRub(s.coupon_tax)) +
              _taxStat('Финрезультат (прогноз)', s.financial_result, 'налог ' + _fmtRub(s.result_tax)) +
              _taxStat('Итого налог (оценка)', s.total_tax, 'база ' + _fmtRub(s.total_base), true) +
            '</div>' +
            '<div style="font-size:11px;color:var(--text-muted);">' + _escHtml(d.disclaimer || '') + '</div>';
        } catch(e) {
          body.innerHTML = '<span style="color:var(--text-muted);">Не удалось рассчитать. Попробуйте позже.</span>';
        }
      }
      function _taxStat(label, value, sub, hl) {
        return '<div style="background:var(--bg-subtle,rgba(148,163,184,.06));border-radius:8px;padding:10px 12px;">' +
          '<div style="font-size:11px;color:var(--text-muted);margin-bottom:3px;">' + _escHtml(label) + '</div>' +
          '<div style="font-size:16px;font-weight:700;color:' + (hl ? 'var(--blue-400)' : 'var(--text-primary)') + ';">' + _fmtRub(value) + '</div>' +
          '<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">' + _escHtml(sub) + '</div>' +
        '</div>';
      }
      async function downloadTaxCsv() {
        if (!portfolioId) return;
        try {
          const r = await apiFetch(`/portfolios/${portfolioId}/tax-report.csv`);
          if (!r.ok) { toast('Не удалось выгрузить', 'error'); return; }
          const blob = await r.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url; a.download = `tax-report-${portfolioId}.csv`;
          document.body.appendChild(a); a.click(); a.remove();
          URL.revokeObjectURL(url);
        } catch(e) { toast('Не удалось выгрузить', 'error'); }
      }

      async function loadPortfolioRecommendations() {
        const grid = document.getElementById('recommendations-grid');
        if (!grid) return;
        if (!tableRows.length) {
          grid.innerHTML = `<p style="color:var(--text-muted);font-size:13px;padding:8px 0;">${t('rec.empty')}</p>`;
          return;
        }
        grid.innerHTML = `<p style="color:var(--text-muted);font-size:13px;padding:8px 0;">${t('rec.loading')}</p>`;

        // Analyze portfolio
        const existingTickers = new Set(tableRows.map(r => r.ticker));
        const withYtm = tableRows.filter(r => r.ytm != null);
        const totalWeight = withYtm.reduce((s,r) => s + (r.weight||0), 0) || 1;
        const avgYtm = withYtm.length ? withYtm.reduce((s,r) => s + (r.ytm||0)*(r.weight||0)/totalWeight, 0) : 13;
        const hasOFZ = tableRows.some(r => r.ticker && /^SU\d/.test(r.ticker));
        const issuerMap = {};
        tableRows.forEach(r => {
          const key = (r.name||r.ticker||'Unknown').split(' ')[0].substring(0,8);
          issuerMap[key] = (issuerMap[key]||0) + (r.weight||0);
        });
        const maxConcentration = Math.max(0, ...Object.values(issuerMap));

        // Build fetch buckets — 4 штуки с уникальными risk-уровнями
        const usedRisks = new Set();
        const addBucket = (risk, yld, rationale) => {
          if (!usedRisks.has(risk)) { usedRisks.add(risk); return { risk, yield: yld, rationale }; }
          return null;
        };
        const buckets = [
          !hasOFZ
            ? addBucket('ultra_low', 8, 'Добавить ОФЗ для защиты от дефолтов')
            : addBucket('ultra_low', 9, 'ОФЗ-флоатер для защиты от ставки'),
          maxConcentration > 25
            ? addBucket('low', 11, 'Снизить концентрацию — добавить качественный эмитент')
            : addBucket('low', 12, 'Качественные корпораты для баланса'),
          addBucket('moderate', Math.max(13, Math.round(avgYtm + 1)), 'Повысить доходность портфеля'),
          avgYtm < 14
            ? addBucket('elevated', 16, 'Высокодоходная альтернатива')
            : addBucket('high', 20, 'Агрессивный высокодоходный сегмент'),
        ].filter(Boolean);
        const top4 = buckets.slice(0, 4);

        // Parallel fetch
        const results = await Promise.allSettled(
          top4.map(b => apiFetch(`/bonds/suggest?risk=${b.risk}&yield=${b.yield}&amount=100000`).then(r=>r.json()))
        );

        // One card per issuer — avoid recommending 10 series of same company
        function _issuerKey(bond) {
          const t = (bond.ticker || '').toUpperCase();
          if (/^RU\d{9,}/.test(t)) {
            return (bond.name || bond.short_name || t).split(' ').slice(0, 2).join(' ').toUpperCase().substring(0, 16);
          }
          return t.replace(/[-_]?\d+[A-Z]*$/, '').substring(0, 8);
        }

        const cards = [];
        const shownIssuers = new Set();
        // Soft-block issuers already in portfolio (used only when alternatives exist)
        const portfolioIssuers = new Set();
        tableRows.forEach(r => portfolioIssuers.add(_issuerKey(r)));

        results.forEach((res, i) => {
          if (res.status !== 'fulfilled') return;
          const items = (res.value.bonds || res.value.suggestions || []).filter(s => !existingTickers.has(s.ticker));
          if (!items.length) return;
          // 1st choice: not in portfolio AND not already shown in another card
          let item = items.find(s => !portfolioIssuers.has(_issuerKey(s)) && !shownIssuers.has(_issuerKey(s)));
          // 2nd: not in portfolio (but may duplicate another card's issuer)
          if (!item) item = items.find(s => !portfolioIssuers.has(_issuerKey(s)));
          // 3rd: not shown yet (may be portfolio issuer — different bond at least)
          if (!item) item = items.find(s => !shownIssuers.has(_issuerKey(s)));
          // 4th: any remaining bond
          if (!item) item = items[0];
          if (!item) return;
          shownIssuers.add(_issuerKey(item));
          cards.push({ ...item, rationale: top4[i].rationale, risk: top4[i].risk });
        });

        if (!cards.length) {
          grid.innerHTML = `<p style="color:var(--text-muted);font-size:13px;padding:8px 0;">${t('rec.noDiversify')}</p>`;
          return;
        }

        const riskLabel = { ultra_low:'ОФЗ', low:'Низкий', moderate:'Умеренный', elevated:'Выше среднего' };
        const riskColor = { ultra_low:'var(--green-400)', low:'var(--cyan-400)', moderate:'var(--amber-400)', elevated:'var(--rose-400)' };

        grid.innerHTML = '';
        cards.forEach(c => {
          const div = document.createElement('div');
          div.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;cursor:pointer;transition:border-color .15s;';
          div.onmouseover = () => div.style.borderColor = 'var(--blue-500)';
          div.onmouseout  = () => div.style.borderColor = 'var(--border)';
          const riskClr = riskColor[c.risk] || 'var(--text-muted)';
          const riskLbl = riskLabel[c.risk] || c.risk;
          const coupon  = c.coupon_percent != null ? Number(c.coupon_percent).toFixed(1)+'%' : '—';
          const ytm     = c.market_yield != null ? Number(c.market_yield).toFixed(1)+'%' : '—';
          div.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
              <span style="font-size:11px;font-weight:700;letter-spacing:.5px;color:${riskClr};text-transform:uppercase;">${riskLbl}</span>
              <span style="font-size:11px;font-weight:600;color:var(--text-muted);">${c.ticker||''}</span>
            </div>
            <div style="font-size:13px;font-weight:600;color:var(--text-primary);margin-bottom:8px;line-height:1.3;">${c.name||c.ticker||'—'}</div>
            <div style="display:flex;gap:12px;margin-bottom:10px;">
              <span style="font-size:11px;color:var(--text-muted);">Купон <strong style="color:var(--text-secondary);">${coupon}</strong></span>
              <span style="font-size:11px;color:var(--text-muted);">YTM <strong style="color:var(--text-secondary);">${ytm}</strong></span>
            </div>
            <div style="font-size:11px;color:var(--blue-400);background:rgba(59,130,246,.08);border-radius:6px;padding:5px 8px;margin-bottom:10px;">${esc(c.rationale)}</div>
            <button onclick="addRecommendedBond('${esc(c.ticker||'')}',${c.purchase_price||100})"
              style="width:100%;height:28px;font-size:12px;font-weight:600;background:var(--blue-600);color:#fff;border:none;border-radius:var(--radius-sm);cursor:pointer;transition:.15s;"
              onmouseover="this.style.background='var(--blue-700)'" onmouseout="this.style.background='var(--blue-600)'">
              ${t('rec.add')}
            </button>`;
          grid.appendChild(div);
        });
      }

      async function addRecommendedBond(ticker, price) {
        if (!portfolioId) return;
        const modal = document.getElementById('add-instrument-modal');
        if (modal) {
          document.getElementById('add-modal-ticker').value = ticker;
          document.getElementById('add-modal-price').value = price;
          document.getElementById('add-modal-qty').value = 1;
          document.getElementById('add-modal-status').textContent = '';
          modal.style.display = 'flex';
        }
      }

      // ── ADD INSTRUMENT MODAL ─────────────────────────────────────
      function toggleCustomMode(on) {
        // Custom = off-exchange item: hide MOEX search/suggest, show manual fields.
        document.getElementById('add-custom-fields').style.display = on ? 'block' : 'none';
        const suggest = document.getElementById('add-suggest-list');
        if (suggest) suggest.style.display = on ? 'none' : 'grid';
        const lbl = document.getElementById('add-modal-ticker-label');
        if (lbl) lbl.textContent = on ? 'Название бумаги' : 'Тикер или название';
        const ti = document.getElementById('add-modal-ticker');
        if (ti) ti.placeholder = on ? 'Напр.: Облигация СПБ ХХХ' : 'Например: SU26238RMFS4 или Сбербанк';
        const priceLbl = document.querySelector('#add-modal-price')?.previousElementSibling;
        if (priceLbl) priceLbl.textContent = on ? 'Цена покупки (₽)' : 'Цена покупки (₽, необяз.)';
        if (on) document.getElementById('add-modal-autocomplete').style.display = 'none';
      }

      function openAddInstrumentModal() {
        // No early-return on missing portfolioId: the modal opens anyway and
        // submitAddInstrumentModal() creates a default portfolio on first add.
        const modal = document.getElementById('add-instrument-modal');
        if (!modal) return;
        const tickerInput = document.getElementById('add-modal-ticker');
        tickerInput.value = '';
        tickerInput.style.textTransform = '';
        document.getElementById('add-modal-price').value = '';
        document.getElementById('add-modal-qty').value = 1;
        const addDateEl = document.getElementById('add-modal-date');
        if (addDateEl) addDateEl.value = '';
        // reset custom mode
        const customCb = document.getElementById('add-modal-custom');
        if (customCb) { customCb.checked = false; toggleCustomMode(false); }
        document.getElementById('add-modal-custom-price').value = '';
        document.getElementById('add-modal-custom-coupon').value = '';
        document.getElementById('add-modal-custom-nominal').value = '';
        document.getElementById('add-modal-custom-freq').value = '2';
        document.getElementById('add-modal-custom-maturity').value = '';
        document.getElementById('add-modal-status').textContent = '';
        document.getElementById('add-modal-autocomplete').style.display = 'none';
        // Load suggest list
        loadAddSuggestForModal();
        modal.style.display = 'flex';
        // Init autocomplete once
        _initAddModalAutocomplete();
        setTimeout(() => tickerInput.focus(), 80);
      }

      let _autocompleteInited = false;
      function _initAddModalAutocomplete() {
        if (_autocompleteInited) return;
        _autocompleteInited = true;
        const input = document.getElementById('add-modal-ticker');
        const drop  = document.getElementById('add-modal-autocomplete');
        let _acTimer = null;

        input.addEventListener('input', () => {
          clearTimeout(_acTimer);
          const q = input.value.trim();
          if (q.length < 2) { drop.style.display = 'none'; return; }
          _acTimer = setTimeout(() => _doAutocomplete(q), 220);
        });

        input.addEventListener('keydown', (e) => {
          const items = drop.querySelectorAll('[data-ac-item]');
          const active = drop.querySelector('[data-ac-item].ac-active');
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            const next = active ? active.nextElementSibling : items[0];
            if (next) { active && active.classList.remove('ac-active'); next.classList.add('ac-active'); next.scrollIntoView({block:'nearest'}); }
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            const prev = active ? active.previousElementSibling : items[items.length-1];
            if (prev) { active && active.classList.remove('ac-active'); prev.classList.add('ac-active'); prev.scrollIntoView({block:'nearest'}); }
          } else if (e.key === 'Enter' && active) {
            e.preventDefault();
            active.click();
          } else if (e.key === 'Escape') {
            drop.style.display = 'none';
          }
        });

        document.addEventListener('mousedown', (e) => {
          if (!drop.contains(e.target) && e.target !== input) drop.style.display = 'none';
        });
      }

      async function _doAutocomplete(q) {
        const drop = document.getElementById('add-modal-autocomplete');
        try {
          const res = await apiFetch(`/bonds/search?q=${encodeURIComponent(q)}&limit=8`);
          if (!res.ok) return;
          const items = await res.json();
          if (!items.length) { drop.style.display = 'none'; return; }
          drop.innerHTML = '';
          items.forEach(b => {
            const row = document.createElement('div');
            row.setAttribute('data-ac-item', '1');
            row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer;transition:background .1s;border-bottom:1px solid rgba(148,163,184,.08);';
            row.onmouseover = () => { drop.querySelectorAll('[data-ac-item]').forEach(r => r.classList.remove('ac-active')); row.classList.add('ac-active'); };
            const coupon = b.coupon_percent != null ? `${Number(b.coupon_percent).toFixed(1)}%` : '';
            const mat = b.maturity ? b.maturity.substring(0,7) : '';
            const rating = b.rating ? `<span style="font-size:10px;padding:1px 5px;border-radius:3px;background:rgba(59,130,246,.15);color:#60a5fa;border:1px solid rgba(59,130,246,.2);">${esc(b.rating)}</span>` : '';
            const qualBadge = b.is_qual ? `<span title="Только для квалифицированных инвесторов" style="font-size:10px;padding:1px 5px;border-radius:3px;background:rgba(234,179,8,.15);color:#ca8a04;border:1px solid rgba(234,179,8,.3);">КИ</span>` : '';
            row.innerHTML = `
              <div style="min-width:0;flex:1;">
                <div style="font-size:12px;font-weight:600;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(b.name)}</div>
                <div style="font-size:10px;color:var(--text-muted);margin-top:1px;">${esc(b.ticker)}${mat ? ' · ' + esc(mat) : ''}${coupon ? ' · ' + esc(coupon) : ''}</div>
              </div>
              ${rating}${qualBadge}`;
            row.addEventListener('click', () => {
              const tickerInput = document.getElementById('add-modal-ticker');
              tickerInput.value = b.ticker;
              tickerInput.style.textTransform = 'uppercase';
              document.getElementById('add-modal-price').value = '';  // auto-fetch current price
              drop.style.display = 'none';
            });
            drop.appendChild(row);
          });
          // Style active item
          const style = document.getElementById('_ac_style');
          if (!style) {
            const s = document.createElement('style');
            s.id = '_ac_style';
            s.textContent = '[data-ac-item].ac-active{background:rgba(148,163,184,.12)!important;}';
            document.head.appendChild(s);
          }
          drop.style.display = 'block';
        } catch(e) {}
      }

      function closeAddInstrumentModal() {
        const modal = document.getElementById('add-instrument-modal');
        if (modal) modal.style.display = 'none';
      }

      async function loadAddSuggestForModal() {
        const list = document.getElementById('add-suggest-list');
        if (!list) return;
        list.innerHTML = '';
        try {
          const resp = await apiFetch('/bonds/suggest?risk=moderate&yield=12&amount=100000');
          const data = await resp.json();
          const items = (data.bonds || data.suggestions || []).slice(0,4);
          items.forEach(s => {
            const btn = document.createElement('button');
            btn.style.cssText = 'background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);padding:6px 8px;text-align:left;cursor:pointer;transition:border-color .15s;font-family:inherit;';
            btn.onmouseover = () => btn.style.borderColor = 'var(--blue-500)';
            btn.onmouseout  = () => btn.style.borderColor = 'var(--border)';
            const coupon = s.coupon_percent != null ? Number(s.coupon_percent).toFixed(1)+'%' : '';
            const shortName = (s.name || s.ticker || '').substring(0, 22);
            btn.innerHTML = `<div style="font-size:11px;font-weight:600;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(shortName)}</div><div style="font-size:10px;color:var(--text-muted);">${esc(s.ticker)}${coupon ? ' · ' + esc(coupon) : ''}</div>`;
            btn.onclick = () => {
              const inp = document.getElementById('add-modal-ticker');
              inp.value = s.ticker;
              inp.style.textTransform = 'uppercase';
              document.getElementById('add-modal-price').value = '';  // auto-fetch current price
              document.getElementById('add-modal-autocomplete').style.display = 'none';
            };
            list.appendChild(btn);
          });
        } catch(e) {}
      }

      let _editItemId = null;
      let _editIsCustom = false;
      function openEditInstrumentModal(row) {
        _editItemId = row.id;
        _editIsCustom = row.source === 'custom';
        const modal = document.getElementById('edit-instrument-modal');
        if (!modal) return;
        document.getElementById('edit-modal-name').textContent = row.name || row.ticker || '';
        document.getElementById('edit-modal-qty').value = row.quantity != null ? row.quantity : 1;
        document.getElementById('edit-modal-price').value =
          row.purchase_price != null ? row.purchase_price : (row.current_price != null ? row.current_price : '');
        document.getElementById('edit-modal-date').value = row.purchase_date ? String(row.purchase_date).substring(0, 10) : '';
        // Show the live-price field only for off-exchange items.
        const cpWrap = document.getElementById('edit-modal-curprice-wrap');
        cpWrap.style.display = _editIsCustom ? 'block' : 'none';
        if (_editIsCustom) document.getElementById('edit-modal-curprice').value = row.current_price != null ? row.current_price : '';
        // Custom-bond fields (nominal / freq / maturity / coupon rate).
        const cfWrap = document.getElementById('edit-custom-fields');
        cfWrap.style.display = _editIsCustom ? 'block' : 'none';
        if (_editIsCustom) {
          document.getElementById('edit-modal-custom-coupon').value = row.coupon_rate != null ? row.coupon_rate : '';
          document.getElementById('edit-modal-custom-nominal').value = row.nominal != null ? row.nominal : '';
          const freq = row.coupon_period ? Math.round(365 / row.coupon_period) : 2;
          document.getElementById('edit-modal-custom-freq').value = String([2, 4, 12].includes(freq) ? freq : 2);
          document.getElementById('edit-modal-custom-maturity').value = row.maturity_date ? String(row.maturity_date).substring(0, 10) : '';
        }
        // Date hint: purchase date affects full profit only for non-T-Bank items.
        const dateHint = document.getElementById('edit-modal-date-hint');
        if (dateHint) {
          dateHint.textContent = row.source === 'tbank'
            ? 'Купоны для бумаг из Т-Банка берутся из журнала операций — дата покупки на полную прибыль не влияет.'
            : 'Учитывает полученные купоны в полной прибыли.';
        }
        document.getElementById('edit-modal-rating').value = row.company_rating != null ? row.company_rating : '';
        document.getElementById('edit-modal-status').textContent = '';
        modal.style.display = 'flex';
        setTimeout(() => document.getElementById('edit-modal-price').focus(), 80);
      }
      function closeEditInstrumentModal() {
        const modal = document.getElementById('edit-instrument-modal');
        if (modal) modal.style.display = 'none';
        _editItemId = null;
      }
      async function submitEditInstrumentModal() {
        const statusEl = document.getElementById('edit-modal-status');
        if (!_editItemId || !portfolioId) { closeEditInstrumentModal(); return; }
        const qty = parseFloat(document.getElementById('edit-modal-qty').value);
        const price = parseFloat(document.getElementById('edit-modal-price').value);
        const dateVal = document.getElementById('edit-modal-date').value || null;
        if (!qty || qty <= 0) { statusEl.textContent = 'Укажите количество'; return; }
        if (!price || price <= 0) { statusEl.textContent = 'Укажите цену покупки'; return; }
        statusEl.textContent = 'Сохранение...';
        statusEl.style.color = 'var(--text-muted)';
        try {
          const payload = { quantity: qty, purchase_price: price };
          if (dateVal) payload.purchase_date = dateVal;
          payload.manual_rating = (document.getElementById('edit-modal-rating').value || '').trim();
          if (_editIsCustom) {
            const cp = parseFloat(document.getElementById('edit-modal-curprice').value);
            if (cp > 0) payload.current_price = cp;
            const cr = parseFloat(document.getElementById('edit-modal-custom-coupon').value);
            if (cr >= 0 && !isNaN(cr)) payload.coupon_rate = cr;
            const nom = parseFloat(document.getElementById('edit-modal-custom-nominal').value);
            if (nom > 0) payload.custom_nominal = nom;
            const freq = parseInt(document.getElementById('edit-modal-custom-freq').value, 10);
            if (freq > 0) payload.custom_coupon_freq = freq;
            const mat = document.getElementById('edit-modal-custom-maturity').value;
            if (mat) payload.custom_maturity = mat;
          }
          const resp = await apiFetch(`/portfolios/${portfolioId}/instruments/${_editItemId}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          const body = await resp.json();
          if (!resp.ok) throw new Error(body.detail || 'Не удалось сохранить.');
          closeEditInstrumentModal();
          await syncTableFromServer();  // refresh table with recalculated values
        } catch (err) {
          statusEl.textContent = err.message || 'Ошибка сохранения';
          statusEl.style.color = 'var(--red-400)';
        }
      }

      async function submitAddInstrumentModal() {
        const statusEl = document.getElementById('add-modal-status');
        const isCustom = document.getElementById('add-modal-custom').checked;
        const tickerRaw = document.getElementById('add-modal-ticker').value.trim();
        const ticker = tickerRaw.toUpperCase();
        const qty = parseInt(document.getElementById('add-modal-qty').value) || 1;
        const priceRaw = document.getElementById('add-modal-price').value.trim();
        const price = priceRaw ? parseFloat(priceRaw) : null;
        if (!tickerRaw) { statusEl.textContent = isCustom ? 'Введите название' : 'Введите тикер'; return; }
        if (isCustom && (!price || price <= 0)) { statusEl.textContent = 'Укажите цену покупки'; return; }
        statusEl.textContent = 'Добавление...';
        statusEl.style.color = 'var(--text-muted)';
        try {
          // First-ever instrument with no portfolio yet → create a default one.
          if (!portfolioId) {
            const pr = await apiFetch('/portfolios', {
              method: 'POST', headers: {'Content-Type':'application/json'},
              body: JSON.stringify({ name: 'Мой портфель' }),
            });
            const pd = await pr.json().catch(() => ({}));
            if (!pr.ok) {
              statusEl.textContent = pd.detail || 'Не удалось создать портфель';
              statusEl.style.color = 'var(--red-400)';
              return;
            }
            portfolioId = pd.id;
            localStorage.setItem('mvp_active_portfolio_id', String(portfolioId));
            await loadPortfolios();
            updatePortfolioSelector();
          }
          const addPayload = { ticker, quantity: qty, purchase_price: price };
          const addDate = document.getElementById('add-modal-date').value || null;
          if (addDate) addPayload.purchase_date = addDate;
          if (isCustom) {
            addPayload.is_custom = true;
            addPayload.instrument_type = document.getElementById('add-modal-custom-type').value;
            addPayload.custom_name = tickerRaw;  // use the entered name as-is (not uppercased)
            const cp = parseFloat(document.getElementById('add-modal-custom-price').value);
            if (cp > 0) addPayload.current_price = cp;
            const cr = parseFloat(document.getElementById('add-modal-custom-coupon').value);
            if (cr >= 0 && !isNaN(cr)) addPayload.coupon_rate = cr;
            const nom = parseFloat(document.getElementById('add-modal-custom-nominal').value);
            if (nom > 0) addPayload.custom_nominal = nom;
            const freq = parseInt(document.getElementById('add-modal-custom-freq').value, 10);
            if (freq > 0) addPayload.custom_coupon_freq = freq;
            const mat = document.getElementById('add-modal-custom-maturity').value;
            if (mat) addPayload.custom_maturity = mat;
          }
          const resp = await apiFetch(`/portfolios/${portfolioId}/instruments`, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify(addPayload)
          });
          if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            statusEl.textContent = err.detail || 'Ошибка добавления';
            statusEl.style.color = 'var(--red-400)';
            return;
          }
          closeAddInstrumentModal();
          await syncTableFromServer();
        } catch(e) {
          statusEl.textContent = 'Ошибка сети';
          statusEl.style.color = 'var(--red-400)';
        }
      }

      // ── LANGUAGE SWITCHER: TRANSLATIONS вынесены в /static/translations.js (грузится до этого скрипта) ──

      // TRANSLATIONS are now loaded; re-apply lang so all elements get correct text
      // (window._lang was set early above, before IIFE; just re-apply to cover new DOM)
      // No re-init needed — applyLang call below handles it.

      // ── ONBOARDING ────────────────────────────────────────────────
      let _obRisk = null;
      let _obAmount = 100000;
      let _obSuggested = [];

      function initOnboarding() {
        const forceStart = localStorage.getItem('mvp_start_onboarding') === '1';
        if (forceStart) {
          // New user just registered — clear flag and launch wizard immediately
          localStorage.removeItem('mvp_start_onboarding');
          localStorage.removeItem('mvp_onboarding_done');
          const overlay = document.getElementById('onboarding-overlay');
          if (overlay) { overlay.style.display = 'flex'; }
          return;
        }
        // Show only for returning users with no instruments
        if (localStorage.getItem('mvp_onboarding_done')) return;
        if (!portfolioId) return;
        if (tableRows && tableRows.length > 0) return;
        const overlay = document.getElementById('onboarding-overlay');
        if (overlay) { overlay.style.display = 'flex'; }
      }

      function closeOnboarding() {
        localStorage.setItem('mvp_onboarding_done', '1');
        const overlay = document.getElementById('onboarding-overlay');
        if (overlay) overlay.style.display = 'none';
      }

      function obGoStep1() {
        document.getElementById('ob-step-1').style.display = '';
        document.getElementById('ob-step-2').style.display = 'none';
        document.getElementById('ob-step-3').style.display = 'none';
        document.getElementById('ob-progress').style.width = '33%';
      }

      function obGoStep2() {
        document.getElementById('ob-step-1').style.display = 'none';
        document.getElementById('ob-step-2').style.display = '';
        document.getElementById('ob-step-3').style.display = 'none';
        document.getElementById('ob-progress').style.width = '66%';
        obUpdateAmount();
      }

      async function obGoStep3() {
        const amount = parseInt(document.getElementById('ob-amount').value) || 100000;
        if (amount < 10000) { toast(t('wizard.minAmount'), 'warn'); return; }
        _obAmount = amount;
        document.getElementById('ob-step-2').style.display = 'none';
        document.getElementById('ob-step-3').style.display = '';
        document.getElementById('ob-progress').style.width = '100%';
        const resultEl = document.getElementById('ob-result');
        resultEl.innerHTML = `<div style="text-align:center;padding:24px;color:#64748b;">${t('wizard.analyzing')}</div>`;
        document.getElementById('ob-add-all').disabled = true;
        try {
          const yieldMid = {ultra_low:9,low:12,moderate:15,elevated:18,high:21}[_obRisk] || 12;
          const r = await apiFetch(`/bonds/suggest?amount=${_obAmount}&yield=${yieldMid}&risk=${_obRisk || 'low'}`);
          const data = await r.json();
          _obSuggested = (data.bonds || []).map(b => ({
            ticker: b.ticker,
            name: b.name,
            instrument_type: 'bond',
            quantity: b.lots * b.lot_size,
            lots: b.lots,
            lot_size: b.lot_size,
            purchase_price: b.purchase_price,
            coupon_percent: b.coupon_percent,
            maturity: b.maturity,
            total_cost: b.total_cost,
          }));
          if (_obSuggested.length === 0) {
            resultEl.innerHTML = `<div style="text-align:center;padding:24px;color:#64748b;">${t('wizard.noResults')}</div>`;
            return;
          }
          resultEl.innerHTML = '';
          _obSuggested.forEach(item => {
            const div = document.createElement('div');
            div.className = 'ob-result-item';
            const left = document.createElement('div');
            const name = document.createElement('div');
            name.className = 'ob-result-name';
            name.textContent = item.name || item.ticker;
            const meta = document.createElement('div');
            meta.className = 'ob-result-meta';
            meta.textContent = `${item.ticker} · купон ${item.coupon_percent}% · ${item.lots} лот${item.lots === 1 ? '' : item.lots < 5 ? 'а' : 'ов'}`;
            left.appendChild(name); left.appendChild(meta);
            const right = document.createElement('div');
            right.style.textAlign = 'right';
            const qty = document.createElement('div');
            qty.className = 'ob-result-qty';
            qty.textContent = `${item.quantity} шт.`;
            const price = document.createElement('div');
            price.className = 'ob-result-price';
            price.textContent = item.total_cost ? `≈ ${Math.round(item.total_cost).toLocaleString('ru')} ₽` : '';
            right.appendChild(qty); right.appendChild(price);
            div.appendChild(left); div.appendChild(right);
            resultEl.appendChild(div);
          });
          document.getElementById('ob-add-all').disabled = false;
        } catch(e) {
          resultEl.innerHTML = `<div style="text-align:center;padding:24px;color:#f87171;">${t('wizard.error')}</div>`;
        }
      }

      async function obAddAll() {
        if (!_obSuggested.length) return;
        const btn = document.getElementById('ob-add-all');
        btn.disabled = true;
        btn.textContent = t('wizard.creating');
        try {
          // If user has no portfolio yet — create one named after risk level
          let targetPortfolioId = portfolioId;
          if (!targetPortfolioId) {
            const riskNames = {ultra_low:'Консервативный', low:'Осторожный', moderate:'Умеренный', elevated:'Активный', high:'Агрессивный'};
            const portName = (riskNames[_obRisk] || 'Мой') + ' портфель';
            const portRes = await apiFetch('/portfolios', {
              method: 'POST',
              headers: {'Content-Type':'application/json'},
              body: JSON.stringify({ name: portName }),
            });
            const portData = await portRes.json();
            if (!portRes.ok) throw new Error('Ошибка создания портфеля');
            targetPortfolioId = portData.id;
          }

          btn.textContent = t('wizard.adding');
          const payloads = _obSuggested.map(item => ({
            ticker: item.ticker,
            instrument_type: item.instrument_type,
            quantity: item.quantity,
            purchase_price: item.purchase_price || item.current_price || 100,
          }));
          await apiFetch(`/portfolios/${targetPortfolioId}/instruments/bulk`, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify(payloads),
          });

          // Success celebration
          btn.textContent = t('wizard.done');
          btn.style.background = 'var(--green-600)';
          const overlay = document.getElementById('onboarding-overlay');
          if (overlay) {
            const card = overlay.querySelector('.ob-card');
            if (card) {
              card.innerHTML = `
                <div style="text-align:center;padding:40px 24px;">
                  <div style="font-size:56px;margin-bottom:16px;">🎉</div>
                  <div style="font-size:22px;font-weight:700;color:var(--slate-800);margin-bottom:8px;">Портфель собран!</div>
                  <div style="color:var(--slate-500);font-size:14px;margin-bottom:24px;">AI подобрал ${_obSuggested.length} облигаций под ваш профиль</div>
                  <button onclick="closeOnboarding();location.reload();" style="background:var(--blue-600);color:#fff;border:none;border-radius:8px;padding:12px 28px;font-size:15px;font-weight:600;cursor:pointer;">
                    Смотреть портфель →
                  </button>
                </div>`;
            }
          }

          // If new portfolio was created, save it so page reload picks it up
          if (!portfolioId && targetPortfolioId) {
            localStorage.setItem('mvp_active_portfolio', String(targetPortfolioId));
          }
        } catch(e) {
          btn.disabled = false;
          btn.textContent = '✓ Добавить всё в портфель';
          toast(t('err.addFailed', 'Ошибка добавления. Попробуйте ещё раз.'), 'error');
        }
      }

      function obSetAmount(val) {
        document.getElementById('ob-amount').value = val;
        obUpdateAmount();
      }

      function obUpdateAmount() {
        const v = parseInt(document.getElementById('ob-amount').value) || 0;
        const el = document.getElementById('ob-amount-display');
        if (el) el.textContent = v > 0 ? `${v.toLocaleString('ru')} рублей` : '';
      }

      // Risk card selection
      document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.ob-risk-card').forEach(card => {
          card.addEventListener('click', () => {
            document.querySelectorAll('.ob-risk-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            _obRisk = card.dataset.risk;
            const btn = document.getElementById('ob-next-1');
            btn.style.opacity = '1';
            btn.style.pointerEvents = 'auto';
          });
        });
      });

      // Второй аргумент — текст на случай отсутствующего ключа. Без него
      // возвращается сам ключ: старая идиома с оператором || НЕ работала,
      // потому что непустая строка-ключ истинна и запасной текст не брался —
      // пользователь видел 'auth.sessionExpired' вместо сообщения.
      function t(key, fallback) {
        if (typeof TRANSLATIONS === 'undefined') return fallback ?? key;
        return TRANSLATIONS[window._lang]?.[key]
            ?? TRANSLATIONS['ru']?.[key]
            ?? fallback
            ?? key;
      }

      function applyLang(lang) {
        window._lang = lang;
        localStorage.setItem('mvp_lang', lang);
        document.querySelectorAll('[data-i18n]').forEach(el => {
          const key = el.dataset.i18n;
          const val = TRANSLATIONS[lang]?.[key];
          if (val === undefined) return;
          // Preserve sort arrow for sortable table headers
          if (el.tagName === 'TH' && el.dataset.sortKey) {
            const arrow = el.textContent.match(/\s[↑↓]$/) ? el.textContent.match(/\s[↑↓]$/)[0] : '';
            el.textContent = val + arrow;
          } else {
            el.textContent = val;
          }
        });
        document.querySelectorAll('[data-i18n-html]').forEach(el => {
          const key = el.dataset.i18nHtml;
          const val = TRANSLATIONS[lang]?.[key];
          if (val === undefined) return;
          if (el.tagName === 'TH' && el.dataset.sortKey) {
            const arrowSpan = el.querySelector('span');
            const arrow = arrowSpan ? arrowSpan.outerHTML : '';
            el.innerHTML = val + arrow;
          } else {
            el.innerHTML = val;
          }
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
          const key = el.dataset.i18nPlaceholder;
          const val = TRANSLATIONS[lang]?.[key];
          if (val !== undefined) el.placeholder = val;
        });
        document.documentElement.dataset.lang = lang;
        document.querySelectorAll('.lang-btn').forEach(b => {
          b.classList.toggle('active', b.textContent.toLowerCase() === lang);
        });
        // Update subtle login page lang buttons
        document.querySelectorAll('.login-lang-btn').forEach(b => {
          const isActive = b.dataset.lang === lang;
          b.style.color = isActive ? '#f1f5f9' : 'rgba(241,245,249,.45)';
          b.style.borderColor = isActive ? 'rgba(148,163,184,.5)' : 'rgba(148,163,184,.2)';
        });
        // Refresh stat card sub-labels and chart (set dynamically, not via data-i18n)
        try {
          if (typeof updateStatCards === 'function' && typeof tableRows !== 'undefined') {
            const summary = calculateSummary(tableRows);
            updateStatCards(summary);
          }
          if (typeof initCouponPeriodBtns === 'function') initCouponPeriodBtns();
          if (typeof drawMonthlyChart === 'function' && typeof tableRows !== 'undefined') {
            drawMonthlyChart(tableRows);
          }
          if (typeof setProfitMode === 'function') setProfitMode(window._profitModeDesired);
        } catch(e) {}
        // Re-render settings portfolios table (risk labels are dynamic, only in authenticated view)
        if (!isReadOnly) {
          try {
            const portfoliosSection = document.getElementById('settings-portfolios-body');
            if (portfoliosSection && typeof settingsLoadPortfolios === 'function') {
              settingsLoadPortfolios();
            }
          } catch(e) {}
        }
      }

      function setLang(lang) { applyLang(lang); }

      // ── MOBILE MODE ───────────────────────────────────────────────
      function initMobileMode() {
        const isMobileDevice = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
        const saved = localStorage.getItem('mvp_mobile_mode');
        const mobile = saved !== null ? saved === '1' : isMobileDevice;
        applyMobileMode(mobile);

        // Auto-switch based on viewport width (always active, overrides saved pref on resize)
        const MOBILE_BREAKPOINT = 768;
        new ResizeObserver(() => {
          const narrow = window.innerWidth < MOBILE_BREAKPOINT;
          if (narrow !== document.body.classList.contains('mobile-mode')) {
            // In shared view always auto-switch; in normal mode only if no manual preference saved
            if (isReadOnly || localStorage.getItem('mvp_mobile_mode') === null) {
              applyMobileMode(narrow);
            }
          }
        }).observe(document.body);
      }

      function setMobileMode(on) {
        localStorage.setItem('mvp_mobile_mode', on ? '1' : '0');
        applyMobileMode(on);
      }

      function applyMobileMode(on) {
        document.body.classList.toggle('mobile-mode', on);
        const db = document.getElementById('mobile-btn-desktop');
        const mb = document.getElementById('mobile-btn-mobile');
        if (db) db.classList.toggle('active', !on);
        if (mb) mb.classList.toggle('active', on);
        // Recalculate topbar height after DOM settles
        requestAnimationFrame(updateTopbarHeight);
      }

      function updateTopbarHeight() {
        // Triple rAF ensures layout is fully painted before measuring (mobile nav needs extra frame)
        requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(() => {
          const topbar = document.querySelector('.topbar');
          if (!topbar) return;
          const h = topbar.getBoundingClientRect().height;
          document.documentElement.style.setProperty('--topbar-h', (h + 16) + 'px');
        })));
      }

      // Keep topbar offset accurate on resize
      window.addEventListener('resize', updateTopbarHeight);
      window.addEventListener('load', updateTopbarHeight);

      // Apply saved language on load (before login so translations are ready)
      applyLang(window._lang);

      // Redraw charts on theme change
      document.addEventListener('theme-change', () => {
        if (tableRows.length) {
          drawMonthlyChart(tableRows);
          const histCanvas = document.getElementById('analytics-history-chart');
          if (histCanvas && histCanvas._snapshots) drawHistoryChart(histCanvas, histCanvas._snapshots, true);
          else if (histCanvas && histCanvas.style.display !== 'none') drawHistoryChartEmpty(histCanvas);
        }
      });

      // Register service worker for PWA + auto-update.
      // When a new SW takes control (after a deploy), reload once so the page
      // picks up the fresh HTML/JS instead of serving stale cached assets —
      // this is what previously required a manual "Clear site data".
      if ('serviceWorker' in navigator) {
        let _swReloading = false;
        // Reload once when a new SW takes control (after a deploy) so the page
        // picks up fresh HTML/JS instead of stale cached assets. Skip the very
        // first install on a fresh page (no controller to swap from).
        const _hadController = !!navigator.serviceWorker.controller;
        navigator.serviceWorker.addEventListener('controllerchange', () => {
          if (_swReloading || !_hadController) return;
          _swReloading = true;
          location.reload();
        });
        // Promote a waiting worker immediately so users don't sit on a stale SW
        // until every tab closes — the usual cause of "my changes don't show up".
        const _activateWaiting = (reg) => {
          if (reg && reg.waiting) {
            try { reg.waiting.postMessage('SKIP_WAITING'); } catch (e) {}
          }
        };
        window.addEventListener('load', () => {
          navigator.serviceWorker.register('/sw.js').then((reg) => {
            _activateWaiting(reg);
            reg.addEventListener('updatefound', () => {
              const sw = reg.installing;
              if (sw) sw.addEventListener('statechange', () => {
                if (sw.state === 'installed') _activateWaiting(reg);
              });
            });
            // Poll for an updated SW on load, on focus, and hourly.
            reg.update().catch(() => {});
            window.addEventListener('focus', () => reg.update().catch(() => {}));
            setInterval(() => reg.update().catch(() => {}), 60 * 60 * 1000);
          }).catch(() => {});
        });
      }

      // Hotkey R — manual refresh
      // Hotkey Escape — close open modals
      document.addEventListener('keydown', e => {
        if (e.key.toLowerCase() === 'r' && !e.ctrlKey && !e.metaKey && !e.altKey
          && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) {
          if (typeof manualRefresh === 'function') manualRefresh();
        }
        if (e.key === 'Escape') {
          // Close the top-most actually-visible modal-overlay.
          const open = [...document.querySelectorAll('.modal-overlay')].filter(_isModalVisible).pop();
          if (open) _dismissModal(open);
        }
      });

      // True only when the overlay is actually on screen (covers both the
      // display:flex inline mechanism and the .show class mechanism). Note: the
      // overlay is position:fixed, so offsetParent is null even when visible —
      // rely on the computed display instead.
      function _isModalVisible(el) {
        if (!el) return false;
        const cs = getComputedStyle(el);
        return cs.display !== 'none' && cs.visibility !== 'hidden';
      }
      // Close a modal the way it was opened: prefer its named close handler, fall
      // back to the .show class, then to inline display.
      function _dismissModal(el) {
        const closeBtn = el.querySelector('.modal-close, [onclick*="close"], [data-close-modal]');
        if (closeBtn) { closeBtn.click(); return; }
        if (el.classList.contains('show')) el.classList.remove('show');
        else el.style.display = 'none';
      }
      // Universal "click on the dim backdrop to dismiss" for every modal — applied
      // once via delegation so individual modals don't each need their own handler.
      document.addEventListener('mousedown', e => {
        if (e.target && e.target.classList && e.target.classList.contains('modal-overlay')
            && _isModalVisible(e.target)) {
          _dismissModal(e.target);
        }
      });

      // ── GUIDE MODAL ────────────────────────────────────────────────
      const GUIDE_CONTENT = {
        basics: `
          <div class="guide-section">
            <h3>💡 Что такое облигация?</h3>
            <p>Облигация — это долговая ценная бумага. Покупая облигацию, вы <strong>даёте деньги в долг</strong> эмитенту (государству, региону или компании). Взамен эмитент обязуется регулярно выплачивать вам проценты (купоны) и вернуть номинал в дату погашения.</p>
          </div>
          <div class="guide-section">
            <h3>🔄 Как это работает?</h3>
            <div class="guide-card">
              <h4>1. Вы покупаете облигацию</h4>
              <p>На бирже по рыночной цене. Цена выражается в % от номинала — например, 97,5% при номинале 1000 ₽ означает цену 975 ₽.</p>
            </div>
            <div class="guide-card">
              <h4>2. Получаете купоны</h4>
              <p>Периодически (обычно раз в полгода или квартал) эмитент перечисляет купонный доход на ваш брокерский счёт.</p>
            </div>
            <div class="guide-card">
              <h4>3. В дату погашения</h4>
              <p>Эмитент возвращает вам номинальную стоимость (обычно 1000 ₽ за облигацию) независимо от рыночной цены.</p>
            </div>
          </div>
          <div class="guide-section">
            <h3>⚖️ Облигации vs Акции vs Вклад</h3>
            <div class="guide-card">
              <h4>🏦 Банковский вклад</h4>
              <p>Фиксированный доход, страховка АСВ до 1,4 млн ₽. Простой, но низкая доходность, нельзя продать досрочно без потери процентов.</p>
            </div>
            <div class="guide-card">
              <h4>📄 Облигации</h4>
              <p>Фиксированный купонный доход, можно продать в любой момент. Доходность выше вклада, риски умеренные. Нет страховки АСВ.</p>
            </div>
            <div class="guide-card">
              <h4>📈 Акции</h4>
              <p>Потенциально высокий доход, но цена может упасть в разы. Подходят для долгосрочных инвесторов с аппетитом к риску.</p>
            </div>
          </div>`,

        types: `
          <div class="guide-section">
            <h3>🏛️ По эмитенту</h3>
            <div class="guide-card">
              <h4>🇷🇺 ОФЗ — Облигации Федерального Займа <span class="guide-badge guide-badge-green">Минимальный риск</span></h4>
              <p>Выпускаются Минфином России. Самый надёжный рублёвый инструмент. Купонный доход освобождён от НДФЛ. Ликвидный рынок.</p>
            </div>
            <div class="guide-card">
              <h4>🏙️ Муниципальные (субфедеральные) <span class="guide-badge guide-badge-green">Низкий риск</span></h4>
              <p>Выпускают регионы и муниципалитеты. Чуть выше доходность, чем у ОФЗ. Риск дефолта минимален при поддержке федерального бюджета.</p>
            </div>
            <div class="guide-card">
              <h4>🏢 Корпоративные <span class="guide-badge guide-badge-yellow">Средний риск</span></h4>
              <p>Выпускают компании. Доходность выше ОФЗ за счёт кредитного риска. Надёжность зависит от рейтинга: ААА–ВВВ — инвестиционный уровень, BB и ниже — высокодоходные (ВДО).</p>
            </div>
            <div class="guide-card">
              <h4>⚡ ВДО — Высокодоходные облигации <span class="guide-badge guide-badge-red">Высокий риск</span></h4>
              <p>Облигации небольших компаний с рейтингом BB и ниже или без рейтинга. Купон 18–25%+, но риск дефолта значительно выше. Требуют диверсификации и анализа.</p>
            </div>
          </div>
          <div class="guide-section">
            <h3>📋 По типу купона</h3>
            <div class="guide-card">
              <h4>Фиксированный купон</h4>
              <p>Ставка неизменна весь срок. Удобно планировать доход. Но при росте ставок ЦБ цена облигации падает.</p>
            </div>
            <div class="guide-card">
              <h4>Переменный (флоатер)</h4>
              <p>Купон привязан к ключевой ставке ЦБ или RUONIA. Защищает от роста ставок — купон растёт вместе с ними.</p>
            </div>
            <div class="guide-card">
              <h4>Индексируемый (линкер)</h4>
              <p>Номинал индексируется на инфляцию (ОФЗ-ИН). Защита от обесценивания рубля, но доходность ниже фиксированных.</p>
            </div>
          </div>
          <div class="guide-section">
            <h3>📅 По сроку</h3>
            <ul>
              <li><strong>Краткосрочные</strong> — до 1–2 лет. Меньше процентный риск.</li>
              <li><strong>Среднесрочные</strong> — 2–5 лет. Баланс доходности и риска.</li>
              <li><strong>Долгосрочные</strong> — 5+ лет. Выше доходность, но сильнее реагируют на ставки ЦБ.</li>
            </ul>
          </div>`,

        metrics: `
          <div class="guide-section">
            <h3>📊 Ключевые показатели таблицы</h3>
            <dl>
              <div class="guide-term-row">
                <dt>Чистая цена</dt>
                <dd>Рыночная цена облигации без учёта НКД. Именно её вы видите на бирже и в котировках. Выражается в рублях (для MOEX — от номинала 1000 ₽).</dd>
              </div>
              <div class="guide-term-row">
                <dt>НКД — Накопленный купонный доход</dt>
                <dd>Часть купона, накопленная с даты последней выплаты. При покупке вы доплачиваете НКД продавцу, при продаже — получаете НКД от покупателя. Включается в стоимость позиции.</dd>
              </div>
              <div class="guide-term-row">
                <dt>Стоимость позиции</dt>
                <dd>Считается по грязной цене: (чистая цена + НКД) × количество. Отражает реальные деньги, которые вы получите при продаже сегодня.</dd>
              </div>
              <div class="guide-term-row">
                <dt>Купон, ₽</dt>
                <dd>Размер одной купонной выплаты на одну облигацию в рублях. Например, 42,38 ₽ за полгода.</dd>
              </div>
              <div class="guide-term-row">
                <dt>Купон, %</dt>
                <dd>Годовая купонная ставка от номинала. Например, 8,5% от 1000 ₽ = 85 ₽ в год.</dd>
              </div>
              <div class="guide-term-row">
                <dt>YTM — доходность к погашению</dt>
                <dd>Годовая доходность, если держать облигацию до погашения и реинвестировать купоны. Учитывает разницу между текущей ценой и номиналом. Основной показатель для сравнения.</dd>
              </div>
              <div class="guide-term-row">
                <dt>Рейтинг</dt>
                <dd>Кредитный рейтинг эмитента от АКРА, Эксперт РА или S&P. AAA — максимальная надёжность, D — дефолт. Инвестиционный уровень: BBB и выше.</dd>
              </div>
              <div class="guide-term-row">
                <dt>Оферта (put-оферта)</dt>
                <dd>Дата, когда эмитент обязан выкупить облигации по номиналу по вашему требованию. После оферты купон может измениться. Важно следить за датой.</dd>
              </div>
              <div class="guide-term-row">
                <dt>Доля в портфеле</dt>
                <dd>Процент от общей рыночной стоимости портфеля. Помогает контролировать диверсификацию.</dd>
              </div>
            </dl>
          </div>`,

        risks: `
          <div class="guide-section">
            <h3>⚠️ Основные риски</h3>
            <div class="guide-card" style="border-color:rgba(248,113,113,.25);">
              <h4>💥 Кредитный риск (риск дефолта) <span class="guide-badge guide-badge-red">Высокий</span></h4>
              <p>Эмитент может не выплатить купон или не погасить номинал. Характерен для корпоративных облигаций с низким рейтингом (ВДО). Снижается диверсификацией и выбором надёжных эмитентов.</p>
            </div>
            <div class="guide-card" style="border-color:rgba(245,158,11,.2);">
              <h4>📉 Процентный риск <span class="guide-badge guide-badge-yellow">Средний</span></h4>
              <p>При росте ключевой ставки ЦБ цены облигаций падают (и наоборот). Чем длиннее срок — тем сильнее реакция. Решение: держать до погашения или выбирать флоатеры.</p>
            </div>
            <div class="guide-card" style="border-color:rgba(245,158,11,.2);">
              <h4>💧 Риск ликвидности <span class="guide-badge guide-badge-yellow">Средний</span></h4>
              <p>Некоторые выпуски торгуются редко — трудно продать быстро по справедливой цене. Особенно актуально для ВДО и небольших выпусков. Проверяйте объём торгов перед покупкой.</p>
            </div>
            <div class="guide-card" style="border-color:rgba(245,158,11,.2);">
              <h4>📋 Риск оферты <span class="guide-badge guide-badge-yellow">Средний</span></h4>
              <p>После оферты эмитент может сильно снизить купон. Если не предъявить к выкупу и «прозевать» оферту — рискуете получить невыгодный купон. Следите за датами оферт в таблице.</p>
            </div>
            <div class="guide-card">
              <h4>📊 Инфляционный риск <span class="guide-badge guide-badge-blue">Низкий</span></h4>
              <p>Реальная доходность может оказаться ниже инфляции. Частично решается выбором ОФЗ-ИН (линкеров) или флоатеров с привязкой к ставке ЦБ.</p>
            </div>
            <div class="guide-card">
              <h4>🏛️ Налоговый риск <span class="guide-badge guide-badge-blue">Низкий</span></h4>
              <p>Купонный доход и прибыль от продажи облагаются НДФЛ 13–15%. Исключение: ОФЗ — купон не облагается НДФЛ. Используйте ИИС для налоговых льгот.</p>
            </div>
          </div>
          <div class="guide-section">
            <h3>🛡️ Как снизить риски?</h3>
            <ul>
              <li><strong>Диверсификация</strong> — не более 5–7% в одного эмитента, не более 15–20% в одну отрасль</li>
              <li><strong>Рейтинг</strong> — для консервативного портфеля выбирайте BBB и выше (АКРА/Эксперт РА)</li>
              <li><strong>Лесенка по срокам</strong> — разные даты погашения снижают процентный риск</li>
              <li><strong>Следите за офертами</strong> — заранее решайте: предъявлять или держать дальше</li>
              <li><strong>ИИС</strong> — налоговый вычет до 52 000 ₽/год или освобождение от НДФЛ на доход</li>
            </ul>
          </div>`,

        glossary: `
          <div class="guide-section">
            <h3>🔤 Глоссарий</h3>
            <dl>
              <div class="guide-term-row"><dt>Номинал</dt><dd>Базовая стоимость облигации, которую эмитент возвращает при погашении. Обычно 1000 ₽.</dd></div>
              <div class="guide-term-row"><dt>Купон</dt><dd>Периодическая выплата процентов держателю облигации.</dd></div>
              <div class="guide-term-row"><dt>НКД</dt><dd>Накопленный купонный доход — часть купона от последней выплаты до сегодня.</dd></div>
              <div class="guide-term-row"><dt>Чистая цена</dt><dd>Рыночная цена без НКД. Отображается в котировках.</dd></div>
              <div class="guide-term-row"><dt>Грязная цена</dt><dd>Чистая цена + НКД. Реальная сумма, уплачиваемая при покупке.</dd></div>
              <div class="guide-term-row"><dt>YTM</dt><dd>Yield to Maturity — доходность к погашению с учётом реинвестирования купонов.</dd></div>
              <div class="guide-term-row"><dt>Дюрация</dt><dd>Средневзвешенный срок до получения денежных потоков. Мера процентного риска: выше дюрация — сильнее реакция цены на изменение ставок.</dd></div>
              <div class="guide-term-row"><dt>Оферта</dt><dd>Право держателя потребовать досрочного погашения по номиналу в определённую дату.</dd></div>
              <div class="guide-term-row"><dt>Флоатер</dt><dd>Облигация с переменным купоном, привязанным к ставке ЦБ или RUONIA.</dd></div>
              <div class="guide-term-row"><dt>Линкер (ОФЗ-ИН)</dt><dd>Облигация с номиналом, индексируемым на инфляцию.</dd></div>
              <div class="guide-term-row"><dt>ОФЗ</dt><dd>Облигации Федерального Займа — государственные долговые бумаги России.</dd></div>
              <div class="guide-term-row"><dt>ВДО</dt><dd>Высокодоходные облигации — с рейтингом ниже BB или без рейтинга, купон 18%+.</dd></div>
              <div class="guide-term-row"><dt>АКРА / Эксперт РА</dt><dd>Российские рейтинговые агентства. Рейтинги: ААА → D (АКРА), ruAAA → ruD (Эксперт РА).</dd></div>
              <div class="guide-term-row"><dt>ИИС</dt><dd>Индивидуальный инвестиционный счёт — даёт налоговые льготы (вычет тип А или освобождение тип Б).</dd></div>
              <div class="guide-term-row"><dt>Листинг (уровень)</dt><dd>Уровень допуска к торгам на MOEX: 1-й — наивысший, 3-й — минимальные требования.</dd></div>
              <div class="guide-term-row"><dt>Амортизация</dt><dd>Погашение номинала частями в течение срока обращения, а не единым платежом в конце.</dd></div>
            </dl>
          </div>`
      };

      const TH_TIPS = {
        rating:        { title: 'Рейтинг', text: 'Кредитный рейтинг от АКРА/Эксперт РА. AAA — максимальная надёжность, D — дефолт. Инвестиционный уровень: BBB и выше.' },
        clean_price:   { title: 'Текущая цена (чистая)', text: 'Рыночная цена без НКД. Именно её вы видите в котировках на бирже. При покупке к ней добавляется НКД.' },
        current_value: { title: 'Стоимость позиции', text: 'Рассчитывается по грязной цене (чистая + НКД) × количество. Отражает реальную рыночную стоимость вашей позиции.' },
        profit:        { title: 'Прибыль / Убыток', text: 'Кнопка ⇄ переключает режимы. <b>Полная</b> (для портфелей Т-Банка) — переоценка тела + текущий НКД + купоны, полученные с момента покупки. <b>От покупки</b> — (текущая чистая цена − цена покупки) × количество. <b>За день</b> — изменение за последнюю торговую сессию.' },
        coupon:        { title: 'Купон, ₽', text: 'Размер одной купонной выплаты на одну облигацию в рублях.' },
        coupon_rate:   { title: 'Купонная ставка, %', text: 'Годовой купон в процентах от номинала (1000 ₽). Например, 8.5% = 85 ₽ в год на бумагу.' },
        annual_coupon: { title: 'Годовой купон по позиции', text: 'Купон × Количество облигаций × Частота выплат в год. Ваш ожидаемый годовой купонный доход от этой бумаги.' },
        ytm:           { title: 'Рыночная доходность (YTM)', text: 'Доходность к погашению: годовая доходность при удержании до погашения с реинвестированием купонов. Главный показатель для сравнения облигаций.' },
        maturity:      { title: 'Дата погашения', text: 'Дата, когда эмитент вернёт номинал (обычно 1000 ₽). Чем дальше — тем выше процентный риск.' },
        offer:         { title: 'Оферта (put)', text: 'Дата, когда вы можете потребовать досрочный выкуп по номиналу. После оферты купон может измениться — следите за этой датой!' },
      };

      function openGuideModal(startTab) {
        const modal = document.getElementById('guide-modal');
        modal.style.display = 'flex';
        setTimeout(() => modal.classList.add('show'), 10);
        const validTab = typeof startTab === 'string' ? startTab : null;
        const lastTab = sessionStorage.getItem('guideTab') || 'basics';
        switchGuideTab(validTab || lastTab);
        document.body.style.overflow = 'hidden';
      }

      function closeGuideModal() {
        const modal = document.getElementById('guide-modal');
        modal.classList.remove('show');
        setTimeout(() => { modal.style.display = 'none'; document.body.style.overflow = ''; }, 200);
      }

      function switchGuideTab(tab) {
        sessionStorage.setItem('guideTab', tab);
        document.querySelectorAll('.guide-tab').forEach(b => {
          b.classList.toggle('active', b.dataset.tab === tab);
        });
        const content = document.getElementById('guide-content');
        if (content) {
          content.innerHTML = GUIDE_CONTENT[tab] || '';
          content.scrollTop = 0;
        }
      }

      // ⓘ popover on table headers
      (function initThPopovers() {
        let _pop = null;
        function removePop() { if (_pop) { _pop.remove(); _pop = null; } }

        document.addEventListener('mouseover', e => {
          const el = e.target.closest('.th-info');
          if (!el) return;
          removePop();
          const tip = TH_TIPS[el.dataset.tip];
          if (!tip) return;
          _pop = document.createElement('div');
          _pop.className = 'th-popover';
          _pop.innerHTML = `<strong>${tip.title}</strong>${tip.text}`;
          document.body.appendChild(_pop);
          const rect = el.getBoundingClientRect();
          let top = rect.bottom + 6;
          let left = rect.left + rect.width / 2 - 130;
          if (left < 8) left = 8;
          if (left + 260 > window.innerWidth - 8) left = window.innerWidth - 268;
          if (top + 120 > window.innerHeight) top = rect.top - 120;
          _pop.style.top = top + 'px';
          _pop.style.left = left + 'px';
        });

        document.addEventListener('mouseout', e => {
          if (e.target.closest('.th-info')) removePop();
        });

        // Prevent sort when clicking ⓘ
        document.addEventListener('click', e => {
          if (e.target.closest('.th-info')) {
            e.stopPropagation();
            const tip = TH_TIPS[e.target.closest('.th-info').dataset.tip];
            if (tip) {
              // Map tip key to glossary tab or open basics
              const tabMap = { rating:'metrics', clean_price:'metrics', current_value:'metrics',
                profit:'metrics', coupon:'metrics', coupon_rate:'metrics',
                annual_coupon:'metrics', ytm:'metrics', maturity:'metrics', offer:'metrics' };
              openGuideModal(tabMap[e.target.closest('.th-info').dataset.tip] || 'glossary');
            }
          }
        }, true);
      })();

