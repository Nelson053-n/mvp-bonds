"""Деплой обязан ставить зависимости из requirements.txt.

09.09.2026: reportlab был добавлен в код (экспорт PDF), но ops/deploy.sh ставил
только код — pip он не вызывал вовсе. На проде пакета не оказалось, и
/portfolios/{id}/report.pdf отдавал 500 двум живым пользователям. Smoke-гейт
такое не ловит: reportlab импортируется ВНУТРИ функции, а не на верхнем уровне
модуля, поэтому `import app.main` проходит успешно.

Проверяем сам bash-скрипт как текст (запускать его в тестах нельзя — он ходит
в git, pip и systemctl), а также порядок шагов: установка обязана идти ДО
smoke-импорта, иначе импорт упадёт раньше, чем зависимость появится.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DEPLOY_SH = REPO_ROOT / "ops" / "deploy.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def test_deploy_installs_requirements(script):
    assert "pip install" in script, (
        "ops/deploy.sh не устанавливает зависимости — пакет, добавленный в "
        "requirements.txt, на прод не попадёт (инцидент с reportlab 09.09)"
    )
    assert "-r requirements.txt" in script


def _exec_line(script: str, needle: str) -> int:
    """Позиция ИСПОЛНЯЕМОЙ строки с needle (комментарии шапки пропускаем).

    Обе искомые команды упоминаются ещё и в комментариях (шапка описывает
    гейт импорта), поэтому простой .index() указал бы не туда.
    """
    for i, line in enumerate(script.splitlines(keepends=True)):
        if needle in line and not line.lstrip().startswith("#"):
            return sum(len(x) for x in script.splitlines(keepends=True)[:i])
    raise AssertionError(f"исполняемая строка с {needle!r} не найдена")


def test_install_runs_before_smoke_import(script):
    """Установка — до smoke-импорта, иначе гейт упадёт на отсутствующем пакете."""
    install_at = _exec_line(script, "pip install")
    import_at = _exec_line(script, "-c 'import app.main'")
    assert install_at < import_at, (
        "pip install должен идти ДО smoke-импорта app.main"
    )


def test_failed_install_rolls_back_and_spares_service(script):
    """Сбой установки обязан вести себя как сбой импорта: откат, сервис не трогаем.

    Иначе неудачный pip оставит на проде новый код со старым окружением.
    """
    block = script[_exec_line(script, "pip install"):_exec_line(script, "-c 'import app.main'")]
    assert "git reset --hard" in block, "при сбое pip install нужен откат pull"
    assert "exit 1" in block
    assert "systemctl restart" not in block, (
        "при сбое установки работающий сервис трогать нельзя"
    )


def test_install_is_conditional_on_requirements_change(script):
    """Обычный деплой не должен ходить в сеть на каждом коммите."""
    assert "requirements.txt" in script
    assert "git diff --quiet" in script, (
        "установка должна запускаться только при изменившемся requirements.txt"
    )


def test_dependency_change_forces_full_restart(script):
    """После установки пакета нужен полный restart, а не SIGHUP.

    SIGHUP переиспользует родительский процесс uvicorn со старым sys.path и уже
    импортированными модулями — свежепоставленный пакет он не увидит.
    """
    assert "DEPS_CHANGED" in script, (
        "изменение зависимостей должно принудительно включать полный restart"
    )
    restart_cond = script[script.index("MAIN_PID="):script.index("sleep 6")]
    assert "DEPS_CHANGED" in restart_cond, (
        "флаг DEPS_CHANGED обязан участвовать в выборе способа перезапуска"
    )


def test_user_force_restart_flag_still_honoured(script):
    """Свой DEPLOY_FORCE_RESTART=1 не должен затираться внутренним флагом."""
    assert "DEPLOY_FORCE_RESTART:-0" in script
    # Внутренний флаг именно отдельный, а не присваивание пользовательского.
    assert "DEPLOY_FORCE_RESTART=1\n" not in script, (
        "внутренняя логика не должна присваивать пользовательскую переменную"
    )


# ── Накатка конфигов nginx ───────────────────────────────────────────────────

def test_deploy_applies_nginx_configs(script):
    """Конфиги nginx катятся деплоем, а не руками на сервере.

    Иначе правила (limit_req, 444 для сканеров) живут только в /etc: не
    воспроизводятся, теряются при потере сервера и разъезжаются с репозиторием.
    """
    assert "nginx-prod-sites-bondai.ru" in script
    assert "nginx-prod-conf.d-bondai-ratelimit.conf" in script
    assert "systemctl reload nginx" in script


def test_nginx_applied_only_when_changed(script):
    """Обычный деплой не должен трогать nginx."""
    block = script[_exec_line(script, "NGINX_SITE_SRC="):_exec_line(script, "-c 'import app.main'")]
    assert "git diff --quiet" in block, (
        "конфиги должны применяться только при их изменении в этом деплое"
    )


def test_nginx_failure_restores_previous_config(script):
    """Битый конфиг роняет ВСЕ сайты сервера — нужен бэкап и откат."""
    block = script[_exec_line(script, "NGINX_SITE_SRC="):_exec_line(script, "-c 'import app.main'")]
    assert "nginx -t" in block, "перед reload обязательна проверка конфига"
    assert "NGINX_BAK" in block, "нужен бэкап прежних файлов"
    assert "прежний конфиг восстановлен" in block, "нужен откат при сбое"


def test_nginx_test_result_not_swallowed_by_pipe(script):
    """`nginx -t | tail` вернул бы код tail — битый конфиг прошёл бы проверку.

    Регрессия поймана при написании: пайп проглатывал ошибку, и reload
    выполнялся на неисправном конфиге.
    """
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "nginx -t" not in stripped:
            continue
        if "if " in stripped:
            assert "|" not in stripped.split("nginx -t")[1].split("&&")[0], (
                f"результат nginx -t проглатывается пайпом: {stripped}"
            )


def test_repo_header_stripped_before_install(script):
    """В git у site-файла есть шапка-комментарий; на сервер идёт чистый конфиг."""
    assert "/^server {/,$p" in script, (
        "шапка репозитория должна отрезаться при накатке"
    )


def test_nginx_header_does_not_collide_with_sed_anchor():
    """В шапке конфига не должно быть строки, начинающейся с "server {".

    deploy.sh вырезает тело через sed -n '/^server {/,$p'. Если такая строка
    появится в шапке-комментарии (например в примере команды), sed зацепится
    за неё и утащит хвост комментариев в конфиг — nginx его отвергнет и
    положит все сайты сервера. Поймано вхолостую при написании накатки.
    """
    path = REPO_ROOT / "ops" / "nginx-prod-sites-bondai.ru"
    lines = path.read_text(encoding="utf-8").splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith("server {"))
    body = lines[first:]
    assert body[0] == "server {"
    # Всё до тела — комментарии или пустые строки, ни одного «server {».
    for line in lines[:first]:
        assert not line.startswith("server {"), (
            f"шапка содержит якорь sed: {line!r}"
        )
    assert "limit_req zone=bond_rl" in "\n".join(body)
    assert "return 444" in "\n".join(body)
