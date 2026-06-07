#!/usr/bin/env python3
"""Ops helper for quick prod checks — one command instead of multi-line `python -c`.

Usage (on prod, from /opt/mvp-bonds, with .env loaded):
    set -a && . ./.env && set +a
    .venv/bin/python3 manage.py <command> [args]

Commands:
    table <pid>            Per-position metrics: profit / full_profit / day / YTM
    sync [pid|all]         Run T-Bank sync (one portfolio or all enabled)
    coupons <pid>          Realized coupons stored per figi
    syncs                  List all enabled T-Bank sync configs
    snapshot-check         Find duplicate portfolio_snapshots (should be none)
    backups                Backup inventory by type (startup/auto/manual) + freshness
    anomalies <pid>        Analytics anomalies for a portfolio (yield-to-offer, etc.)
    token <user_id>        Mint a JWT for a user (debugging APIs)
    user <email>           Look up user id + their portfolios
"""
import asyncio
import sys


def _p(*a):
    print(*a, flush=True)


async def cmd_table(pid: int):
    from app.services.portfolio_service import portfolio_service
    rows = await portfolio_service.get_table(pid)
    ti = sum(float(r.purchase_price or 0) * float(r.quantity or 0) for r in rows)
    tp = sum(float(r.profit or 0) for r in rows)
    fp = sum(float(r.full_profit) if r.full_profit is not None else float(r.profit or 0) for r in rows)
    dp = sum(float(r.day_profit) for r in rows if r.day_profit is not None)
    _p(f"portfolio {pid}: {len(rows)} позиций | вложено {ti:,.0f}")
    _p(f"  от покупки : {tp:>14,.2f}  ({tp / ti * 100:+.2f}%)" if ti else f"  от покупки : {tp:,.2f}")
    _p(f"  полная     : {fp:>14,.2f}  ({fp / ti * 100:+.2f}%)" if ti else f"  полная     : {fp:,.2f}")
    _p(f"  за день    : {dp:>14,.2f}")
    miss_y = [r.ticker for r in rows if r.type == "bond" and not r.market_yield]
    miss_full = [r.ticker for r in rows if r.type == "bond" and r.full_profit is None]
    if miss_y:
        _p(f"  ⚠ без YTM: {miss_y}")
    if miss_full:
        _p(f"  ⚠ без full_profit: {miss_full}")


async def cmd_sync(which: str):
    from app.services.storage_service import storage_service
    from app.services.tbank_sync_service import do_sync_one
    syncs = storage_service.get_all_enabled_syncs()
    if which != "all":
        syncs = [s for s in syncs if s["portfolio_id"] == int(which)]
    for s in syncs:
        pid = s["portfolio_id"]
        try:
            res = await do_sync_one(pid, s)
            cm = storage_service.get_tbank_coupons(pid)
            csum = sum(v["coupons_total"] for v in cm.values())
            _p(f"pid {pid}: added={res.get('added')} updated={res.get('updated')} | figis={len(cm)} coupons={csum:,.0f}")
        except Exception as e:
            _p(f"pid {pid}: ERROR {type(e).__name__}: {str(e)[:160]}")


def cmd_coupons(pid: int):
    from app.services.storage_service import storage_service
    cm = storage_service.get_tbank_coupons(pid)
    _p(f"portfolio {pid}: {len(cm)} figi, всего купонов {sum(v['coupons_total'] for v in cm.values()):,.2f}")
    for figi, v in sorted(cm.items(), key=lambda kv: -kv[1]["coupons_total"])[:10]:
        _p(f"  {figi}: {v['coupons_total']:>12,.2f}  first_buy={v['first_buy_date']}")


def cmd_syncs():
    from app.services.storage_service import storage_service
    for s in storage_service.get_all_enabled_syncs():
        _p(f"pid {s['portfolio_id']} | user {s['user_id']} | acct {s['tbank_account_id']} | enabled={s['sync_enabled']}")


def cmd_snapshot_check():
    from app.services.storage_service import storage_service
    with storage_service._connect() as c:
        rows = c.execute(
            "SELECT portfolio_id, substr(snapshot_date,1,10) d, COUNT(*) n "
            "FROM portfolio_snapshots GROUP BY portfolio_id, d HAVING n>1 ORDER BY d DESC LIMIT 20"
        ).fetchall()
    if not rows:
        _p("OK: дубликатов снапшотов нет")
    else:
        _p(f"⚠ {len(rows)} дублей (portfolio_id, date):")
        for r in rows:
            _p(f"  pid {r[0]} date {r[1]} count {r[2]}")


async def cmd_anomalies(pid: int):
    import json
    import urllib.request
    from app.services.auth_service import auth_service
    from app.services.storage_service import storage_service
    p = next((x for x in storage_service.get_all_portfolios_raw() if x["id"] == pid), None)
    if not p:
        _p(f"portfolio {pid} not found")
        return
    tok = auth_service.create_token(p["user_id"], "ops", False)
    req = urllib.request.Request(
        f"http://127.0.0.1:8000/portfolios/{pid}/analytics-extra",
        headers={"Authorization": "Bearer " + tok},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=40).read())
    anoms = data.get("anomalies", [])
    _p(f"portfolio {pid}: {len(anoms)} аномалий")
    for a in anoms:
        _p(f"  [{a.get('severity')}] {a.get('text')}")


def cmd_backups():
    from app.services.storage_service import storage_service
    backups = storage_service.get_backups()  # newest first
    if not backups:
        _p("⚠ бэкапов нет")
        return

    def kind(fn: str) -> str:
        if fn.endswith("_startup.db"):
            return "startup"
        if fn.endswith("_auto.db"):
            return "auto"
        if fn.endswith("_manual.db"):
            return "manual"
        return "legacy"

    by_kind: dict[str, list] = {}
    for b in backups:
        by_kind.setdefault(kind(b["filename"]), []).append(b)

    total_mb = sum(b["size"] for b in backups) / 1024 / 1024
    keep = storage_service.get_setting("backup_keep_count", "30")
    hour = storage_service.get_setting("backup_daily_hour", "2")
    _p(f"всего {len(backups)} бэкапов, {total_mb:.1f} МБ | keep_count={keep} | daily в {hour}:00 UTC")
    for k in ("auto", "startup", "manual", "legacy"):
        lst = by_kind.get(k)
        if not lst:
            continue
        newest = lst[0]["filename"]
        _p(f"  {k:8s}: {len(lst):2d} шт | свежий {newest}")
    # warn if no fresh daily backup in the last ~26h
    import datetime
    autos = by_kind.get("auto", [])
    if autos:
        try:
            ts = autos[0]["created_at"]  # YYYYMMDD_HHMMSS
            dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=datetime.timezone.utc)
            age_h = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
            mark = "⚠ устарел" if age_h > 26 else "OK"
            _p(f"  daily-бэкап: {age_h:.1f} ч назад  {mark}")
        except Exception:
            pass
    else:
        _p("  ⚠ daily (_auto) бэкапов ещё нет — первый создастся в daily-час")


def cmd_token(user_id: int):
    from app.services.auth_service import auth_service
    from app.services.storage_service import storage_service
    u = storage_service.get_user_by_id(user_id)
    name = u.get("username", "user") if u else "user"
    _p(auth_service.create_token(user_id, name, bool(u.get("is_admin")) if u else False))


def cmd_user(email: str):
    from app.services.storage_service import storage_service
    with storage_service._connect() as c:
        u = c.execute("SELECT id, username, is_admin FROM users WHERE username=?", (email,)).fetchone()
    if not u:
        _p(f"user {email} not found")
        return
    _p(f"user id={u[0]} username={u[1]} admin={bool(u[2])}")
    for p in storage_service.get_all_portfolios_raw():
        if p["user_id"] == u[0]:
            _p(f"  portfolio {p['id']}: {p.get('name')}")


def main():
    args = sys.argv[1:]
    if not args:
        _p(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "table":
        asyncio.run(cmd_table(int(rest[0])))
    elif cmd == "sync":
        asyncio.run(cmd_sync(rest[0] if rest else "all"))
    elif cmd == "coupons":
        cmd_coupons(int(rest[0]))
    elif cmd == "syncs":
        cmd_syncs()
    elif cmd == "snapshot-check":
        cmd_snapshot_check()
    elif cmd == "backups":
        cmd_backups()
    elif cmd == "anomalies":
        asyncio.run(cmd_anomalies(int(rest[0])))
    elif cmd == "token":
        cmd_token(int(rest[0]))
    elif cmd == "user":
        cmd_user(rest[0])
    else:
        _p(f"unknown command: {cmd}")
        _p(__doc__)


if __name__ == "__main__":
    main()
