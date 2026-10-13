"""Файлы, которые создаёт сервис, закрыты от других пользователей сервера.

Аудит 12.10.2026, H2: с umask 022 база и бэкапы получали 644 и читались
любым пользователем общего сервера (www-data, соседние проекты).
"""
import os
import stat


def test_new_files_private(tmp_path) -> None:
    import app.main  # noqa: F401 — umask ставится при импорте приложения

    p = tmp_path / "probe.db"
    p.write_bytes(b"x")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode & 0o077 == 0, oct(mode)
