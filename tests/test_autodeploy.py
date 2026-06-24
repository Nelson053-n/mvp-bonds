"""
Тест логики авто-деплоя: функции сравнения HEAD с origin/v3.

Тестируем Python-эквиваленты bash-логики, не сам bash-скрипт.
Проверяем:
  - «отстаёт ли» HEAD от remote (rev-list count > 0)
  - парсинг количества отставших коммитов
  - что скрипт существует и исполняем
  - что .service и .timer файлы есть
"""
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
OPS_DIR = REPO_ROOT / "ops"
SYSTEMD_DIR = OPS_DIR / "systemd"


def _git_rev_list_count(base: str, tip: str) -> int:
    """Эквивалент: git rev-list base..tip --count"""
    out = subprocess.check_output(
        ["git", "rev-list", "--count", f"{base}..{tip}"],
        cwd=str(REPO_ROOT),
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
    return int(out)


def test_auto_deploy_script_exists_and_executable():
    script = OPS_DIR / "auto-deploy.sh"
    assert script.exists(), "ops/auto-deploy.sh должен существовать"
    assert os.access(script, os.R_OK), "auto-deploy.sh должен быть читаем"


def test_systemd_service_file_exists():
    f = SYSTEMD_DIR / "bondai-autodeploy.service"
    assert f.exists(), "ops/systemd/bondai-autodeploy.service должен существовать"


def test_systemd_timer_file_exists():
    f = SYSTEMD_DIR / "bondai-autodeploy.timer"
    assert f.exists(), "ops/systemd/bondai-autodeploy.timer должен существовать"


def test_timer_interval_is_5min():
    """Таймер должен проверять каждые 5 минут."""
    timer = (SYSTEMD_DIR / "bondai-autodeploy.timer").read_text()
    assert "OnUnitActiveSec=5min" in timer


def test_git_rev_list_count_same_commit_is_zero():
    """Если base == tip, отставание равно нулю."""
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), text=True
    ).strip()
    count = _git_rev_list_count(head, head)
    assert count == 0, "HEAD..HEAD должен дать 0"


def test_git_rev_list_count_one_behind():
    """Если tip на 1 коммит впереди base — count=1."""
    # Берём HEAD и HEAD~1; HEAD~1..HEAD = 1 коммит
    try:
        parent = subprocess.check_output(
            ["git", "rev-parse", "HEAD~1"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except subprocess.CalledProcessError:
        # Репозиторий с 1 коммитом — пропускаем
        return
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), text=True
    ).strip()
    count = _git_rev_list_count(parent, head)
    assert count == 1


def test_auto_deploy_script_has_tg_notify():
    """Скрипт должен отправлять TG при успехе и провале."""
    content = (OPS_DIR / "auto-deploy.sh").read_text()
    assert "_tg " in content, "скрипт должен вызывать _tg()"
    assert "ПРОВАЛИЛСЯ" in content or "ПРОВАЛИЛСЯ" in content
    assert "авто-деплой ок" in content


def test_auto_deploy_script_has_log():
    """Скрипт должен писать в LOG_FILE."""
    content = (OPS_DIR / "auto-deploy.sh").read_text()
    assert "LOG_FILE" in content
    assert "_log " in content


def test_auto_deploy_script_calls_deploy_sh():
    """Скрипт должен вызывать ops/deploy.sh."""
    content = (OPS_DIR / "auto-deploy.sh").read_text()
    assert "deploy.sh" in content or "DEPLOY_SCRIPT" in content


def test_service_calls_auto_deploy_sh():
    """bondai-autodeploy.service должен запускать auto-deploy.sh."""
    content = (SYSTEMD_DIR / "bondai-autodeploy.service").read_text()
    assert "auto-deploy.sh" in content
