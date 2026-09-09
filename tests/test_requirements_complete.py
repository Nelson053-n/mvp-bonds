"""Каждый сторонний импорт из app/ обязан быть в requirements.txt.

Деплой (`ops/deploy.sh`) зависимости НЕ ставит — он только делает smoke-импорт
`app.main`. Поэтому пакет, оказавшийся в venv разработчика, но не в
requirements.txt, на прод не попадает никогда, и smoke-гейт его не ловит:
импорт лежит внутри функции, а не на верхнем уровне модуля.

09.09.2026 так и вышло с reportlab: экспорт PDF (`app/api/pdf.py`) импортирует
его внутри `_generate_pdf_reportlab`, fallback на fpdf — внутри
`_generate_pdf_fpdf`. Ни того, ни другого в requirements.txt не было, на проде
не стоял ни один. Оба импорта падали с ImportError, и наружу летел 500:
6 отказов на /portfolios/*/report.pdf у двух реальных пользователей за двое
суток. Локально reportlab стоял в venv, поэтому вручную баг не воспроизводился.

Тест смотрит ИМЕННО исходники, а не установленное окружение: проверять
`pip show` бессмысленно — локально пакет как раз есть, в том и суть бага.
"""
import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "app"

# Модули стандартной библиотеки и сам проект в requirements не объявляют.
_STDLIB = set(getattr(__import__("sys"), "stdlib_module_names", ()))

# Имя пакета в PyPI не всегда совпадает с именем импорта.
_IMPORT_TO_PKG = {
    "jwt": "pyjwt",
    "dotenv": "python-dotenv",
    "multipart": "python-multipart",
    "pythonjsonlogger": "python-json-logger",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
    "PIL": "pillow",
}

# Осознанно НЕ объявленные: импорт защищён try/except и код работает без них.
# Сейчас таких нет — мёртвый fpdf-фолбэк удалён вместе с его импортом.
_OPTIONAL: set[str] = set()


def _declared_packages() -> set[str]:
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!~\[; ]", line, 1)[0].strip().lower()
        if name:
            # PyPI не различает "-" и "_": pydantic-settings ставится как
            # pydantic_settings, и обратно.
            names.add(name.replace("_", "-"))
    return names


def _imported_roots() -> dict[str, set[str]]:
    """Корневые имена сторонних импортов -> файлы, где они встречаются.

    Обходим ВСЕ импорты, включая вложенные в функции: именно такой импорт
    (reportlab внутри _generate_pdf_reportlab) не виден smoke-гейту деплоя.
    """
    found: dict[str, set[str]] = {}
    for path in _APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # Относительные импорты (level > 0) — свои же модули.
                if node.level or not node.module:
                    continue
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                if root in _STDLIB or root == "app":
                    continue
                found.setdefault(root, set()).add(
                    str(path.relative_to(_ROOT))
                )
    return found


def test_all_third_party_imports_are_declared():
    declared = _declared_packages()
    missing = {}
    for root, files in _imported_roots().items():
        pkg = _IMPORT_TO_PKG.get(root, root).lower().replace("_", "-")
        if pkg in _OPTIONAL:
            continue
        if pkg not in declared:
            missing[root] = sorted(files)

    assert not missing, (
        "Импорты без записи в requirements.txt — на прод такой пакет не "
        "попадёт, деплой зависимости не ставит:\n"
        + "\n".join(f"  {mod}: {', '.join(files)}" for mod, files in sorted(missing.items()))
    )


def test_pdf_export_dependency_installed():
    """reportlab должен быть реально импортируем — иначе /report.pdf отдаёт 500.

    Именно этот путь сломался на проде 09.09: обе ветки _generate_pdf падали
    с ImportError, и пользователь получал 500 вместо файла.
    """
    import importlib

    importlib.import_module("reportlab")
