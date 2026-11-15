"""
Phase 14: documentation accuracy check.

Every file path, ``path.py::Name`` reference and dotted code name mentioned in the
README and docs/ must exist in the code. A local build log (``PROGRESS.md``), if present,
is skipped: it is historical and mentions files that later changes removed on purpose.
"""

import ast
import importlib
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
DOC_FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
HISTORICAL_LOGS = {"PROGRESS.md"}
PROJECT_PACKAGES = ("accounts", "bookings", "config", "core", "events", "payments")
FILE_SUFFIXES = ("py", "md", "html", "css", "js", "yml", "toml", "txt")
NOT_YET_CREATED = ("docs/screenshots/",)

CODE_SPAN = re.compile(r"`([^`\n]+)`")
FILE_PATH = re.compile(rf"^[\w./-]+\.(?:{'|'.join(FILE_SUFFIXES)})(?:::[\w.]+)?$")
DOTTED_NAME = re.compile(rf"^(?:{'|'.join(PROJECT_PACKAGES)})(?:\.\w+)+")


def code_spans() -> list[tuple[str, str]]:
    """Every inline code span in the checked documents, as (document name, text)."""
    spans = []
    for doc in DOC_FILES:
        if doc.name in HISTORICAL_LOGS:
            continue
        spans.extend((doc.name, span.strip()) for span in CODE_SPAN.findall(doc.read_text("utf-8")))
    return spans


def python_names_in(path: Path) -> set[str]:
    """Top-level names and ``Class.method`` names defined in a Python file."""
    names = set()
    for node in ast.parse(path.read_text("utf-8")).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        if isinstance(node, ast.ClassDef):
            names.update(
                f"{node.name}.{child.name}"
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            )
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def resolves(dotted: str) -> bool:
    """True if ``dotted`` is an importable module, a module attribute, or an app model."""
    parts = dotted.split(".")
    if len(parts) == 2 and parts[1][:1].isupper():
        try:
            apps.get_model(dotted)
            return True
        except LookupError:
            pass
    for split in range(len(parts), 0, -1):
        try:
            target = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attribute in parts[split:]:
            if not hasattr(target, attribute):
                return False
            target = getattr(target, attribute)
        return True
    return False


class DocumentationAccuracyTests(SimpleTestCase):
    def test_every_documented_file_path_exists(self):
        """Backticked paths such as `bookings/services.py` point at real files."""
        missing = []
        for doc, span in code_spans():
            if FILE_PATH.match(span) and not span.startswith(NOT_YET_CREATED):
                path = span.split("::")[0]
                if not (ROOT / path).exists():
                    missing.append(f"{doc}: {span}")
        self.assertEqual(missing, [])

    def test_every_path_and_name_reference_exists(self):
        """`file.py::name` references name a real function, class or method in that file."""
        missing = []
        for doc, span in code_spans():
            if "::" in span and FILE_PATH.match(span):
                path, name = span.split("::")
                file = ROOT / path
                if file.suffix == ".py" and file.exists() and name not in python_names_in(file):
                    missing.append(f"{doc}: {span}")
        self.assertEqual(missing, [])

    def test_every_dotted_code_name_resolves(self):
        """Names such as `bookings.services.hold_seats` import and resolve."""
        missing = []
        for doc, span in code_spans():
            match = DOTTED_NAME.match(span)
            if match and not FILE_PATH.match(span) and not resolves(match.group(0)):
                missing.append(f"{doc}: {span}")
        self.assertEqual(missing, [])
