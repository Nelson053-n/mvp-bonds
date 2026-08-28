"""Миграции обязаны применяться от одного лишь `import app.main`.

На этом держится автодеплой: `ops/deploy.sh` делает smoke-импорт, и именно
там (storage_service.__init__ → _ensure_db → _run_migrations) схема
приводится в порядок — до перезапуска воркеров. Сломается это свойство —
деплой начнёт выкатывать код на неподготовленную БД.

27.08 так и вышло: ALTER лежал в `_ensure_db`, а SCHEMA_VERSION не подняли.
На проде БД стояла на v5, `_run_migrations` до нужного шага не доходил —
колонка не появилась ни при импорте, ни при старте, и код читал её три часа
(81 отказ 500 на /auth/me). На ЧИСТОЙ базе тот же код отрабатывал: там
`with self._connect()` коммитит на выходе — потому баг и не ловился локально.

Проверяем в ОТДЕЛЬНОМ процессе: внутри тестовой сессии `app.main` уже
импортирован, повторный импорт закэширован и ничего бы не доказал. По той
же причине читаем схему тоже отдельно — откат виден только после того, как
соединение мигрирующего процесса закрылось.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.storage_service import StorageService

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(code: str, db_path: str) -> str:
    """Выполнить код отдельным процессом с изолированной БД."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "MVP_SQLITE_DB_PATH": db_path,
            "MVP_JWT_SECRET": "test_secret_for_migration_check",
            "MVP_LLM_MODE": "stub",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"процесс упал: {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr[-2000:]}"
    )
    return result.stdout


def _columns_read_by_get_user_by_id() -> set[str]:
    """Колонки users, которые код реально читает — из SELECT в storage-слое.

    Разбираем исходник, а не перечисляем руками: список, набранный вручную,
    устареет ровно тогда, когда появится новая миграция.
    """
    import inspect
    import re

    from app.services.storage.users import UsersMixin

    src = inspect.getsource(UsersMixin.get_user_by_id)
    match = re.search(r"SELECT (.+?) FROM users", src, re.S)
    assert match, "не нашёл SELECT в get_user_by_id"
    return {c.strip() for c in match.group(1).split(",")}


@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    return str(tmp_path / "migrate.db")


class TestMigrationAppliedOnImport:
    def test_import_creates_schema(self, fresh_db: str) -> None:
        """`import app.main` на пустом месте поднимает схему целиком."""
        _run("import app.main", fresh_db)

        assert Path(fresh_db).exists(), "БД не создана импортом"
        con = sqlite3.connect(fresh_db)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            version = con.execute(
                "SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        finally:
            con.close()

        assert "users" in tables
        assert version == StorageService.SCHEMA_VERSION

    def test_import_upgrades_stale_db(self, fresh_db: str) -> None:
        """Главное свойство: импорт догоняет БД, отставшую на версию.

        Ровно сценарий деплоя — на диске БД предыдущей версии, приезжает код
        с новой миграцией.
        """
        _run("import app.main", fresh_db)

        # Откатываем схему на версию назад: убираем колонку последней
        # миграции и понижаем номер — как выглядит БД перед деплоем.
        con = sqlite3.connect(fresh_db)
        try:
            con.execute("ALTER TABLE users DROP COLUMN tg_chat_id_reset")
            con.execute("UPDATE schema_version SET version = ?",
                        (StorageService.SCHEMA_VERSION - 1,))
            con.commit()
            cols = {c[1] for c in con.execute("PRAGMA table_info(users)")}
            assert "tg_chat_id_reset" not in cols, "подготовка не сработала"
        finally:
            con.close()

        _run("import app.main", fresh_db)

        # Отдельным соединением: незакоммиченный DDL к этому моменту откатится.
        con = sqlite3.connect(fresh_db)
        try:
            cols = {c[1] for c in con.execute("PRAGMA table_info(users)")}
            version = con.execute(
                "SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        finally:
            con.close()

        assert "tg_chat_id_reset" in cols, (
            "импорт не применил миграцию — деплой выкатит код на старую схему"
        )
        assert version == StorageService.SCHEMA_VERSION

    def test_migration_survives_connection_close(self, fresh_db: str) -> None:
        """DDL должен быть закоммичен, а не жить до закрытия соединения.

        Тот самый баг: ALTER без commit() виден внутри своей транзакции и
        исчезает после неё. Проверяем именно снаружи процесса-мигратора.

        Сверяем ВСЕ колонки, которые код ожидает от users, а не одну: следующая
        потерянная миграция добавит другое поле, и точечная проверка её
        пропустит. Список берём из самого кода — из SELECT в get_user_by_id.
        """
        _run("import app.main", fresh_db)

        out = _run(
            "import sqlite3, os, json;"
            "con = sqlite3.connect(os.environ['MVP_SQLITE_DB_PATH']);"
            "print(json.dumps([c[1] for c in con.execute('PRAGMA table_info(users)')]))",
            fresh_db,
        )
        cols = set(json.loads(out.strip().splitlines()[-1]))

        expected = _columns_read_by_get_user_by_id()
        missing = expected - cols
        assert not missing, (
            f"код читает колонки, которых нет в БД: {sorted(missing)} — "
            "миграция не закоммичена"
        )

    def test_column_missing_under_current_version_is_not_healed(
        self, fresh_db: str
    ) -> None:
        """Версия отмечена применённой → миграция больше не выполнится.

        Не баг, а свойство versioned-механизма, но знать его надо: если
        колонку потеряли, а schema_version уже стоит на новой версии,
        импорт НЕ починит — `_run_migrations` пропустит шаг как выполненный.
        Чинится только руками (ALTER + правка версии), как и было на проде.

        Тест фиксирует это поведение, чтобы следующий человек не рассчитывал
        на самоизлечение и не потерял три часа, как я.
        """
        _run("import app.main", fresh_db)

        con = sqlite3.connect(fresh_db)
        try:
            con.execute("ALTER TABLE users DROP COLUMN tg_chat_id_reset")
            con.commit()
            version = con.execute(
                "SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        finally:
            con.close()
        assert version == StorageService.SCHEMA_VERSION, "версия должна остаться новой"

        _run("import app.main", fresh_db)

        con = sqlite3.connect(fresh_db)
        try:
            cols = {c[1] for c in con.execute("PRAGMA table_info(users)")}
        finally:
            con.close()
        assert "tg_chat_id_reset" not in cols, (
            "поведение изменилось: миграции стали применяться повторно — "
            "перечитай _run_migrations, тест описывает старый контракт"
        )

    def test_import_is_idempotent(self, fresh_db: str) -> None:
        """Повторный импорт не ломает уже мигрированную БД."""
        _run("import app.main", fresh_db)
        _run("import app.main", fresh_db)

        con = sqlite3.connect(fresh_db)
        try:
            version = con.execute(
                "SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
            users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            con.close()

        assert version == StorageService.SCHEMA_VERSION
        assert users == 1, "второй импорт не должен плодить bootstrap-админа"


class TestEveryMigrationRegistered:
    """SCHEMA_VERSION и список миграций обязаны сходиться.

    Забыть пару `(N, self._migration_vN)` при бампе версии — значит получить
    БД, помеченную новой версией, но без её изменений.
    """

    def test_version_matches_migration_count(self) -> None:
        import inspect

        src = inspect.getsource(StorageService._run_migrations)
        registered = src.count("self._migration_v")
        assert registered == StorageService.SCHEMA_VERSION, (
            f"SCHEMA_VERSION={StorageService.SCHEMA_VERSION}, "
            f"а в списке {registered} миграций"
        )

    def test_every_migration_method_exists(self) -> None:
        for n in range(1, StorageService.SCHEMA_VERSION + 1):
            assert hasattr(StorageService, f"_migration_v{n}"), \
                f"нет метода _migration_v{n}"
