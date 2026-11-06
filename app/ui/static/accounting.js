// The journal uses the same authenticated API and portfolio selection as the cabinet.
window._manualAccountState = null;
let manualBusy = false;
let manualRequest = null;
let manualDialogPortfolio = null;
let manualPreviousOverflow = '';
const manualNames = {opening:'Начальная позиция',deposit:'Пополнение',withdraw:'Вывод',buy:'Покупка',sell:'Продажа',coupon:'Купон',amortization:'Амортизация',redemption:'Погашение',fee:'Комиссия'};
const ma = id => document.getElementById('ma-' + id);
const manualMoney = value => value == null ? 'Нет актуальной оценки' : Number(value).toLocaleString('ru-RU', {minimumFractionDigits:2,maximumFractionDigits:2}) + ' ₽';

async function openManualAccounting(kind, itemId) {
  const pid = portfolioId;
  await loadManualAccounting(pid);
  if (String(pid) !== String(portfolioId) || !window._manualAccountState?.available) return;
  const dialog = ma('dialog');
  if (!dialog.open) {
    manualDialogPortfolio = pid;
    manualPreviousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
  }
  if (kind) {
    ma('kind').value = kind;
    ma('item').value = itemId == null ? '' : String(itemId);
    manualFormKind();
    (kind === 'buy' && itemId == null ? ma('ticker') : ma('amount')).focus();
  }
}

function closeManualAccounting() {
  if (ma('dialog').open) ma('dialog').close();
}

ma('dialog').addEventListener('close', () => {
  document.body.style.overflow = manualPreviousOverflow;
  manualDialogPortfolio = null;
});
ma('dialog').addEventListener('click', event => {
  if (event.target === ma('dialog')) closeManualAccounting();
});

async function manualApi(suffix, options = {}) {
  const response = await apiFetch(`/portfolios/${portfolioId}/accounting${suffix}`, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить операцию. Проверьте поля.');
  }
  return response;
}

async function loadManualAccounting(pid) {
  const card = document.getElementById('manual-accounting-card');
  if (ma('dialog').open && String(manualDialogPortfolio) !== String(pid)) closeManualAccounting();
  if (!card || isReadOnly || isAllMode || !pid) {
    window._manualAccountState = null;
    if (card) card.style.display = 'none';
    ma('launcher').style.display = 'none';
    closeManualAccounting();
    return false;
  }
  try {
    const response = await apiFetch(`/portfolios/${pid}/accounting`);
    if (!response.ok) throw new Error('Не удалось загрузить журнал');
    const data = await response.json();
    if (String(pid) !== String(portfolioId)) return false;
    window._manualAccountState = data;
    card.style.display = data.available ? '' : 'none';
    ma('launcher').style.display = data.available ? '' : 'none';
    if (!data.available) { closeManualAccounting(); return false; }
    ma('initialize').style.display = data.enabled ? 'none' : '';
    ma('active').style.display = data.enabled ? '' : 'none';
    if (!data.enabled) { ma('status').textContent = 'Учёт выплат ещё не включён. Начальные данные будут зафиксированы только после вашего подтверждения.'; return false; }
    ma('status').textContent = `Учёт с ${data.started_on}. База: ${data.basis === 'current' ? 'оценка на начало учёта' : 'цены покупки (без неизвестных НКД и комиссий)'}.`;
    ma('totals').textContent = `Бумаги: ${manualMoney(data.totals.securities_value)} · Свободно: ${manualMoney(data.totals.cash)} · Активы: ${manualMoney(data.totals.assets)} · Результат: ${manualMoney(data.totals.result)}`;
    const previous = ma('item').value;
    ma('item').replaceChildren(new Option('Весь портфель / новый тикер', ''));
    for (const item of data.items) ma('item').add(new Option(`${item.ticker} · ${item.quantity} шт.`, item.id));
    if ([...ma('item').options].some(o => o.value === previous)) ma('item').value = previous;
    ma('history').replaceChildren();
    const items = new Map(data.items.map(i => [i.id, i.ticker]));
    for (const op of [...data.operations].reverse()) {
      const tr = document.createElement('tr');
      for (const text of [op.event_date, manualNames[op.kind], items.get(op.item_id) || '—', manualMoney(op.amount_minor / 100), Number(op.quantity) || '—', op.source === 'opening' ? 'Начальное состояние' : op.source === 'moex_confirmed' ? 'MOEX · подтверждено' : 'Подтверждено вручную']) {
        const td = document.createElement('td'); td.textContent = text; tr.append(td);
      }
      ma('history').append(tr);
    }
    if (!ma('date').value) ma('date').value = new Date().toLocaleDateString('sv-SE');
    ma('date').min = data.started_on;
    ma('date').max = new Date().toLocaleDateString('sv-SE');
    manualFormKind();
    const changed = Number(window.cashTotalRub || 0) !== data.totals.cash;
    window.cashTotalRub = data.totals.cash;
    if (changed) renderTable();
    else updateStatCards(calculateSummary(tableRows));
    return true;
  } catch (e) {
    if (String(pid) !== String(portfolioId)) return false;
    ma('message').textContent = e.message;
    return !!window._manualAccountState?.enabled;
  }
}

function manualFormKind() {
  const kind = ma('kind').value;
  ma('quantity-label').style.display = ['buy','sell','redemption'].includes(kind) ? '' : 'none';
  ma('ticker-label').style.display = kind === 'buy' && !ma('item').value ? '' : 'none';
  ma('item').disabled = ['deposit','withdraw'].includes(kind);
  ma('amount-label').textContent = kind === 'valuation' ? 'Цена одной бумаги, ₽' : 'Общая сумма, ₽';
  ma('amount').min = kind === 'valuation' ? '0' : '0.01';
}
ma('item').addEventListener('change', manualFormKind);

async function manualAction(action) {
  if (manualBusy) return;
  manualBusy = true; ma('submit').disabled = true; ma('message').textContent = ''; ma('message').style.color = 'var(--red-600)';
  const pid = portfolioId;
  try {
    await action();
    manualRequest = null;
    if (String(pid) === String(portfolioId)) {
      ma('payments').replaceChildren();
      await syncTableFromServer();
      await loadManualAccounting(pid);
      ma('message').textContent = 'Сохранено'; ma('message').style.color = 'var(--green-600)';
    }
  } catch (e) { ma('message').textContent = e.message; }
  finally { manualBusy = false; ma('submit').disabled = false; }
}

function manualInitialize() {
  if (!confirm('Зафиксировать начальные позиции и деньги? После включения покупки, продажи и выплаты вносятся через журнал.')) return;
  manualAction(async () => { await manualApi('/initialize', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cash:ma('opening-cash').value || '0',basis:ma('basis').value})}); setProfitMode('full'); });
}

function manualSubmit(event) {
  event.preventDefault();
  const kind = ma('kind').value;
  const itemId = ma('item').value ? Number(ma('item').value) : null;
  if (kind === 'valuation') {
    manualAction(() => manualApi('/valuation', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:itemId,price:ma('amount').value})}));
    return;
  }
  const data = {kind,event_date:ma('date').value,amount:ma('amount').value,quantity:['buy','sell','redemption'].includes(kind)?ma('quantity').value:'0',item_id:['deposit','withdraw'].includes(kind)?null:itemId};
  if (kind === 'buy' && !itemId) data.ticker = ma('ticker').value.trim().toUpperCase();
  const signature = JSON.stringify([portfolioId,data]);
  if (!manualRequest || manualRequest.signature !== signature) manualRequest = {signature,id:crypto.randomUUID()};
  data.request_id = manualRequest.id;
  manualAction(() => manualApi('/operations', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}));
}

function manualUndo() {
  const operations = (window._manualAccountState?.operations || []).filter(o => o.source !== 'opening');
  const last = operations.reduce((a,b) => !a || a.id < b.id ? b : a, null);
  if (!last || !confirm('Отменить последнюю добавленную операцию?')) return;
  manualAction(() => manualApi('/operations/' + last.id, {method:'DELETE'}));
}

async function manualLoadPayments() {
  if (manualBusy) return;
  manualBusy = true; const pid = portfolioId;
  ma('message').textContent = 'Загружаем графики MOEX…';
  try {
    const data = await (await manualApi('/payments')).json();
    if (String(pid) !== String(portfolioId)) return;
    ma('payments').replaceChildren();
    ma('message').textContent = data.notice + (data.unavailable_tickers.length ? ' Не удалось загрузить: ' + data.unavailable_tickers.join(', ') : '');
    if (!data.payments.length) { const p=document.createElement('p');p.textContent='Неподтверждённых выплат по доступным графикам не найдено.';ma('payments').append(p); }
    for (const payment of data.payments) {
      const p=document.createElement('p');
      const text=document.createElement('span');text.textContent=`${payment.event_date} · ${payment.ticker} · ${manualNames[payment.kind]} · по графику ${manualMoney(payment.amount)} `;
      const button=document.createElement('button');button.type='button';button.className='btn btn-secondary';button.textContent='Подтвердить зачисление';
      button.onclick=()=>{
        const amount=prompt('Фактически полученная общая сумма, ₽. Проверьте выписку брокера.',payment.amount);
        if (amount === null) return;
        manualAction(()=>manualApi('/operations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:crypto.randomUUID(),kind:payment.kind,event_date:payment.event_date,amount:amount.replace(',','.'),item_id:payment.item_id,payment_key:payment.external_key})}));
      };
      p.append(text,button);ma('payments').append(p);
    }
  } catch(e) { ma('message').textContent=e.message; }
  finally { manualBusy=false; }
}

async function manualExport() {
  try {
    const response=await manualApi('/export');
    const url=URL.createObjectURL(await response.blob());
    const a=document.createElement('a');a.href=url;a.download='operations.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  } catch(e) { ma('message').textContent=e.message; }
}
