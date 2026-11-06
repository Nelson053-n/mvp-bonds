"""Тесты не должны писать в рабочую базу data/portfolio.db.

Именно оттуда 04.11.2026 восстанавливали прод, и вместе с базой туда уехали
~660 тестовых пользователей и 1485 портфелей "snap-guard-test".
"""
from pathlib import Path

from app.config import settings as import_time_settings
from app.services.storage_service import StorageService, storage_service

REAL_DB = (Path(__file__).resolve().parent.parent / "data" / "portfolio.db").resolve()


def test_new_storage_service_uses_test_db(settings_override) -> None:
    path = Path(StorageService().db_path).resolve()
    assert path != REAL_DB
    assert path == Path(settings_override.sqlite_db_path).resolve()


def test_global_storage_uses_test_db(settings_override) -> None:
    assert Path(storage_service.db_path).resolve() != REAL_DB


def test_import_time_settings_never_default_path() -> None:
    assert Path(import_time_settings.sqlite_db_path).resolve() != REAL_DB
