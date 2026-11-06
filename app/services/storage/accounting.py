"""Atomic journal writes and position updates for manual accounting."""
import logging
from datetime import date, datetime, timezone

from app.services.accounting_service import replay, minor

logger = logging.getLogger(__name__)


class AccountingMixin:
    def assert_legacy_position_write(self, portfolio_id: int):
        if self.accounting_enabled(portfolio_id):
            from app.exceptions import ValidationError
            raise ValidationError('В этом портфеле количество и состав меняются через журнал операций')

    def accounting_enabled(self, portfolio_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute('SELECT 1 FROM manual_accounts WHERE portfolio_id=?', (portfolio_id,)).fetchone() is not None

    @staticmethod
    def _account_operations(conn, portfolio_id):
        cursor = conn.execute('SELECT id, item_id, kind, event_date, amount_minor, quantity, source, external_key '
                              'FROM manual_operations WHERE portfolio_id=? ORDER BY event_date, id', (portfolio_id,))
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def get_account_operations(self, portfolio_id: int) -> list[dict]:
        with self._connect() as conn:
            return self._account_operations(conn, portfolio_id)

    def get_account_info(self, portfolio_id: int):
        with self._connect() as conn:
            row = conn.execute('SELECT started_on, basis FROM manual_accounts WHERE portfolio_id=?', (portfolio_id,)).fetchone()
        return {'started_on': row[0], 'basis': row[1]} if row else None

    def initialize_account(self, portfolio_id: int, opening_cash: int, basis: str, rows: list) -> None:
        today = date.today().isoformat()
        started = min((r.purchase_date for r in rows), default=today) if basis == 'purchase' else today
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM manual_accounts WHERE portfolio_id=?', (portfolio_id,)).fetchone():
                raise ValueError('Учёт уже включён')
            if conn.execute('SELECT 1 FROM portfolio_sync WHERE portfolio_id=?', (portfolio_id,)).fetchone():
                raise ValueError('Для брокерского портфеля ручной учёт недоступен')
            current = conn.execute('SELECT id, quantity, purchase_price FROM portfolio_items '
                                   'WHERE portfolio_id=? AND deleted_at IS NULL', (portfolio_id,)).fetchall()
            if {r[0]: (r[1], r[2]) for r in current} != {r.id: (r.quantity, r.purchase_price) for r in rows}:
                raise ValueError('Позиции изменились. Обновите страницу и повторите.')
            conn.execute('INSERT INTO manual_accounts VALUES (?, ?, ?)', (portfolio_id, started, basis))
            for row in rows:
                cost = minor(row.purchase_price * row.quantity if basis == 'purchase' else row.current_value)
                when = row.purchase_date if basis == 'purchase' else today
                conn.execute('INSERT INTO manual_operations (portfolio_id,item_id,kind,event_date,amount_minor,quantity,source) '
                             'VALUES (?,?,?,?,?,?,?)', (portfolio_id, row.id, 'opening', when, cost, str(row.quantity), 'opening'))
            if opening_cash:
                conn.execute('INSERT INTO manual_operations (portfolio_id,kind,event_date,amount_minor,source) '
                             'VALUES (?,?,?,?,?)', (portfolio_id, 'deposit', started, opening_cash, 'opening'))
            conn.commit()
        logger.info('AUDIT manual_account_initialized portfolio_id=%d basis=%s', portfolio_id, basis)

    def add_account_operation(self, portfolio_id: int, data: dict, new_item: dict | None = None) -> int:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            account = conn.execute('SELECT started_on FROM manual_accounts WHERE portfolio_id=?', (portfolio_id,)).fetchone()
            if not account:
                raise ValueError('Сначала включите учёт операций')
            if not account[0] <= data['event_date'] <= date.today().isoformat():
                raise ValueError('Дата должна быть между началом учёта и сегодняшним днём')
            existing = conn.execute('SELECT id FROM manual_operations WHERE portfolio_id=? AND external_key=?',
                                    (portfolio_id, data['external_key'])).fetchone()
            if existing:
                return existing[0]
            iid = data.get('item_id')
            if new_item:
                count = conn.execute('SELECT COUNT(*) FROM portfolio_items WHERE portfolio_id=? AND deleted_at IS NULL', (portfolio_id,)).fetchone()[0]
                from app.config import settings
                if count >= settings.max_items_per_portfolio:
                    raise ValueError('Достигнут лимит инструментов')
                iid = conn.execute('INSERT INTO portfolio_items (portfolio_id,ticker,instrument_type,quantity,purchase_price,source,purchase_date) '
                                   "VALUES (?,?,'bond',0,?,'manual',?)",
                                   (portfolio_id, new_item['ticker'], new_item['price'], data['event_date'])).lastrowid
            if iid:
                item = conn.execute('SELECT instrument_type,source FROM portfolio_items WHERE id=? AND portfolio_id=?', (iid, portfolio_id)).fetchone()
                if not item or item[0] != 'bond' or item[1] == 'tbank':
                    raise ValueError('Облигация не принадлежит этому ручному портфелю')
            if data['kind'] in ('buy', 'sell', 'coupon', 'amortization', 'redemption') and not iid:
                raise ValueError('Выберите облигацию')
            cur = conn.execute('INSERT INTO manual_operations (portfolio_id,item_id,kind,event_date,amount_minor,quantity,source,external_key) '
                               'VALUES (?,?,?,?,?,?,?,?)', (portfolio_id, iid, data['kind'], data['event_date'], data['amount_minor'],
                                data.get('quantity', '0'), data.get('source', 'manual'), data['external_key']))
            operations = self._account_operations(conn, portfolio_id)
            state = replay(operations)
            self._apply_account_positions(conn, portfolio_id, state)
            conn.commit()
            op_id = cur.lastrowid
        logger.info('AUDIT manual_operation_added portfolio_id=%d operation_id=%d kind=%s', portfolio_id, op_id, data['kind'])
        return op_id

    @staticmethod
    def _apply_account_positions(conn, portfolio_id, state):
        for iid, pos in state['positions'].items():
            # Keep closed positions outside the table while their cash flows remain in the journal.
            deleted = datetime.now(timezone.utc).isoformat() if not pos['quantity'] else None
            conn.execute('UPDATE portfolio_items SET quantity=?, deleted_at=? WHERE id=? AND portfolio_id=?',
                         (float(pos['quantity']), deleted, iid, portfolio_id))

    def undo_account_operation(self, portfolio_id: int, operation_id: int):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            last = conn.execute("SELECT id FROM manual_operations WHERE portfolio_id=? AND source!='opening' ORDER BY id DESC LIMIT 1", (portfolio_id,)).fetchone()
            if not last or last[0] != operation_id:
                raise ValueError('Можно отменить только последнюю добавленную операцию')
            conn.execute('DELETE FROM manual_operations WHERE id=? AND portfolio_id=?', (operation_id, portfolio_id))
            state = replay(self._account_operations(conn, portfolio_id))
            conn.execute('UPDATE portfolio_items SET quantity=0,deleted_at=? WHERE portfolio_id=? AND id NOT IN '
                         '(SELECT item_id FROM manual_operations WHERE portfolio_id=? AND item_id IS NOT NULL)',
                         (datetime.now(timezone.utc).isoformat(), portfolio_id, portfolio_id))
            self._apply_account_positions(conn, portfolio_id, state)
            conn.commit()
        logger.info('AUDIT manual_operation_undone portfolio_id=%d operation_id=%d', portfolio_id, operation_id)
