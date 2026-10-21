"""registrations_14d в админской статистике."""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from app.services.storage_service import storage_service


def _insert(username: str, created_at: str) -> None:
    with storage_service._connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, 'x', ?)",
            (username, created_at),
        )
        conn.commit()


def _cleanup(prefix: str) -> None:
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM users WHERE username LIKE ?", (prefix + "%",))
        conn.commit()


def _counts(series: list[dict]) -> dict:
    return {r["date"]: r["count"] for r in series}


async def test_registrations_14d(client: AsyncClient, auth_headers: dict) -> None:
    now = datetime.now(timezone.utc)
    day = lambda n: now - timedelta(days=n)  # noqa: E731
    prefix = "reg14_"
    base = _counts(
        (await client.get("/admin/stats", headers=auth_headers)).json()["registrations_14d"]
    )
    try:
        _insert(prefix + "a", day(0).isoformat())
        _insert(prefix + "b", day(0).isoformat())
        _insert(prefix + "c", day(3).isoformat())
        _insert(prefix + "d", day(13).isoformat())   # граница окна: попадает
        _insert(prefix + "e", day(14).isoformat())   # вне окна
        _insert(prefix + "f", day(40).isoformat())   # вне окна

        r = await client.get("/admin/stats", headers=auth_headers)
        assert r.status_code == 200
        series = r.json()["registrations_14d"]
    finally:
        _cleanup(prefix)

    assert len(series) == 14
    dates = [x["date"] for x in series]
    assert dates == sorted(dates) and len(set(dates)) == 14
    assert dates[-1] == now.strftime("%Y-%m-%d")
    assert dates[0] == day(13).strftime("%Y-%m-%d")

    got = _counts(series)
    d = lambda n: day(n).strftime("%Y-%m-%d")  # noqa: E731
    assert got[d(0)] - base[d(0)] == 2
    assert got[d(3)] - base[d(3)] == 1
    assert got[d(13)] - base[d(13)] == 1
    # дни без регистраций присутствуют с нулём
    for n in (1, 2, 5, 12):
        assert d(n) in got and got[d(n)] == base[d(n)]
    assert d(14) not in got
