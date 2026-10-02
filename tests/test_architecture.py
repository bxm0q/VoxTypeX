"""Small import-graph guard, not a framework or runtime dependency."""

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "voxtypex"


def source_graph():
    sources = {}
    for path in ROOT.rglob("*.py"):
        parts = list(path.relative_to(ROOT.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        sources[".".join(parts)] = path
    graph = {}
    for name, path in sources.items():
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        dependencies = set()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = "." * node.level + (node.module or "")
                dependencies.add(resolve_name(imported, package) if node.level else imported)
        graph[name] = dependencies
    return graph


def test_internal_imports_have_no_cycles():
    graph = source_graph()
    visited = set()

    def visit(name, ancestors):
        assert name not in ancestors, "Import cycle: " + " -> ".join([*ancestors, name])
        if name in visited:
            return
        for dependency in graph[name] & graph.keys():
            visit(dependency, [*ancestors, name])
        visited.add(name)

    for name in graph:
        visit(name, [])


def test_core_never_depends_on_qt_ui_or_entrypoint():
    graph = source_graph()
    for name, dependencies in graph.items():
        if name.startswith("voxtypex.ui") or name in ("voxtypex.main", "voxtypex.__main__"):
            continue
        assert not any(
            dep.startswith(("PySide6", "voxtypex.ui")) or dep == "voxtypex.main" for dep in dependencies
        ), name
