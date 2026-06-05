# Admin Panel Enhancement Plan

## Context

Панель администратора показывает только базовые данные о пользователях (ID, имя, кол-во портфелей, дата регистрации, роль). Нужно добавить:
- Когда пользователь последний раз входил + сколько дней прошло
- Есть ли у него автосинхронизация T-Bank
- Есть ли уведомления в Telegram
- Поделился ли портфелем
- Журнал действий администратора с IP

## Files to Modify

1. `app/services/storage_service.py` — миграции, новый метод `get_all_users`, новые методы audit log
2. `app/services/auth_service.py` — обновлять `last_login` при логине
3. `app/api/admin.py` — добавить `Request`, писать в audit log, новый endpoint `/audit-log`
4. `app/ui/dashboard.html` — 4 новых столбца, секция журнала

---

## Step 1: `app/services/storage_service.py`

### 1a. Миграция: добавить `last_login` в таблицу `users`

Добавить после блока `coupon_notif_days` (pattern: try/except OperationalError):

```python
try:
    conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
except sqlite3.OperationalError:
    pass
```

### 1b. Новая таблица `admin_audit_log`

Добавить в блок `CREATE TABLE IF NOT EXISTS` (рядом с другими таблицами):

```sql
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INTEGER,
    details TEXT,
    ip_address TEXT,
    created_at TEXT NOT NULL
)
```

Индексы:

```python
"CREATE INDEX IF NOT EXISTS idx_audit_log_admin ON admin_audit_log(admin_user_id, created_at)",
"CREATE INDEX IF NOT EXISTS idx_audit_log_created ON admin_audit_log(created_at)",
```

### 1c. Обновить `get_all_users()` (~line 970)

Заменить SQL-запрос на запрос с JOIN-ами, возвращающий 4 новых поля:

```sql
SELECT
    u.id,
    u.username,
    u.is_admin,
    u.created_at,
    u.last_login,
    COUNT(DISTINCT p.id) AS portfolio_count,
    COALESCE(MAX(ps.sync_enabled), 0) AS has_autosync,
    CASE WHEN u.coupon_notif_enabled = 1
              AND u.tg_chat_id IS NOT NULL THEN 1 ELSE 0 END AS has_tg_notif,
    MAX(CASE WHEN p.share_token IS NOT NULL
              AND (p.share_expires_at IS NULL
                   OR p.share_expires_at > unixepoch()) THEN 1 ELSE 0 END
    ) AS has_sharing
FROM users u
LEFT JOIN portfolios p ON p.user_id = u.id
LEFT JOIN portfolio_sync ps ON ps.portfolio_id = p.id
GROUP BY u.id
ORDER BY u.id ASC
```

Возвращаемый dict добавить ключи: `last_login`, `has_autosync`, `has_tg_notif`, `has_sharing`.

### 1d. Новый метод `update_last_login(user_id: int)`

```python
def update_last_login(self, user_id: int) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
        conn.commit()
```

### 1e. Новый метод `write_audit_log(...)`

```python
def write_audit_log(
    self,
    admin_user_id: int,
    action: str,
    target_type: str | None,
    target_id: int | None,
    details: str | None,
    ip_address: str | None,
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
        conn.execute(
            """INSERT INTO admin_audit_log
               (admin_user_id, action, target_type, target_id, details, ip_address, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (admin_user_id, action, target_type, target_id, details, ip_address, now),
        )
        conn.commit()
```

### 1f. Новый метод `get_audit_log(limit, offset)`

```python
def get_audit_log(self, limit: int = 100, offset: int = 0) -> list[dict]:
    with self._connect() as conn:
        rows = conn.execute(
            """SELECT a.id, a.admin_user_id, u.username AS admin_username,
                      a.action, a.target_type, a.target_id,
                      a.details, a.ip_address, a.created_at
               FROM admin_audit_log a
               LEFT JOIN users u ON u.id = a.admin_user_id
               ORDER BY a.id DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "admin_user_id": int(row[1]),
            "admin_username": row[2],
            "action": row[3],
            "target_type": row[4],
            "target_id": int(row[5]) if row[5] is not None else None,
            "details": row[6],
            "ip_address": row[7],
            "created_at": row[8],
        }
        for row in rows
    ]
```

---

## Step 2: `app/services/auth_service.py`

После успешной проверки пароля (bcrypt) и до формирования JWT токена добавить:

```python
storage_service.update_last_login(user["id"])
```

---

## Step 3: `app/api/admin.py`

### 3a. Импорты

```python
import json
from fastapi import APIRouter, Depends, HTTPException, Request, status
```

### 3b. Хелпер для IP

```python
def _get_ip(request: Request) -> str:
    return request.client.host if request.client else ""
```

### 3c–3f. Обновить 4 endpoint-а

Добавить параметр `request: Request` и вызов `storage_service.write_audit_log(...)` в:
- `DELETE /users/{user_id}` → action=`"delete_user"`, target_type=`"user"`
- `PATCH /users/{user_id}/password` → action=`"change_password"`, target_type=`"user"`
- `PATCH /users/{user_id}/role` → action=`"grant_admin"` или `"revoke_admin"`, target_type=`"user"`
- `DELETE /portfolios/{portfolio_id}` → action=`"delete_portfolio"`, target_type=`"portfolio"`

`details` — JSON строка с контекстом (username, portfolio name и т.д.)

### 3g. Новый endpoint `GET /admin/audit-log`

```python
@router.get("/audit-log")
async def get_audit_log(
    limit: int = 100,
    offset: int = 0,
    admin: dict = Depends(get_admin_user),
) -> list:
    if limit > 500:
        limit = 500
    return storage_service.get_audit_log(limit=limit, offset=offset)
```

---

## Step 4: `app/ui/dashboard.html`

### 4a. Заголовок таблицы пользователей (line ~1477)

Добавить 4 новых `<th>` после "Дата регистрации":

```html
<th>Последний вход</th>
<th>Автосинк</th>
<th>TG</th>
<th>Шеринг</th>
```

### 4b. Секция журнала (между users card и portfolios card)

```html
<div class="card" style="margin-bottom:16px;" id="adm-audit-card">
  <div class="card-header">
    <h2 class="card-title">Журнал администратора</h2>
    <button class="btn btn-secondary" style="height:28px;font-size:12px;padding:0 12px;" onclick="adminLoadAuditLog()">↺ Обновить</button>
  </div>
  <div class="card-body" style="padding:0;">
    <div class="table-wrap" style="max-height:320px;overflow-y:auto;">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Админ</th><th>Действие</th><th>Тип цели</th>
            <th>Цель ID</th><th>Детали</th><th>IP</th><th>Время</th>
          </tr>
        </thead>
        <tbody id="adm-audit-body">
          <tr><td colspan="8" style="text-align:center;color:var(--slate-400);padding:16px;">Нажмите ↺ для загрузки</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>
```

### 4c. Обновить `adminLoadUsers()` JS

1. Изменить `colspan="6"` → `colspan="10"` в loading/error строках.
2. Добавить хелпер `_featureBadge(active)` — создаёт span с зелёным или серым стилем.
3. Добавить 4 новых `<td>`: `tdLastLogin`, `tdSync`, `tdTg`, `tdShare`.
4. Обновить `tr.append(...)`.

`tdLastLogin` — если `u.last_login` есть: показать "Сегодня"/"Вчера"/`Nд назад` с tooltip'ом даты. Иначе "—".

`tdSync`, `tdTg`, `tdShare` — `_featureBadge(u.has_autosync)` и т.д.

### 4d. Новая функция `adminLoadAuditLog()`

- Показывает загрузку, делает `apiFetch('/admin/audit-log?limit=100&offset=0')`
- Рендерит строки таблицы с 8 колонками
- Обрезает `details` до 80 символов

### 4e. Вызвать в `adminInit()`

```js
adminLoadAuditLog().catch(() => {});
```

---

## Verification

1. Запустить сервер → убедиться нет ошибок при старте (миграции прошли)
2. `sqlite3 data/portfolio.db "PRAGMA table_info(users)"` → видим `last_login`
3. `sqlite3 data/portfolio.db "SELECT name FROM sqlite_master WHERE name='admin_audit_log'"` → видим таблицу
4. Войти под любым пользователем → `SELECT last_login FROM users` → есть timestamp
5. В admin панели: столбцы "Последний вход", "Автосинк", "TG", "Шеринг" отображаются
6. Выполнить admin действие (удалить пользователя/портфель, сменить пароль/роль)
7. В журнале: нажать ↺ → запись появляется с IP, действием, временем
