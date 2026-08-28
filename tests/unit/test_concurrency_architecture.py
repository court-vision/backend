"""Static guardrails for request-path concurrency boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
REQUEST_ROOTS = (ROOT / "api", ROOT / "services", ROOT / "core")
# Deliberately empty: the two modules that used to sit here (services/auth_service.py
# and services/etl_service.py) were unreachable dead code holding 17 blocking
# queries, and allowlisting them meant re-wiring either one would silently
# reintroduce loop-blocking. They were deleted instead.
ASYNC_DB_ALLOWLIST: set = set()
BLOCKING_HTTP_ALLOWLIST = {
    ROOT / "core" / "clerk_auth.py",       # FastAPI sync dependency
    ROOT / "core" / "resilience.py",       # offline extractor compatibility
}
MODEL_QUERY_METHODS = {
    "select", "get", "get_by_id", "get_or_none", "create", "update",
    "delete", "delete_by_id", "insert", "insert_many", "replace", "raw",
}


def _python_files():
    for root in REQUEST_ROOTS:
        yield from root.rglob("*.py")


def _loop_body(function: ast.AST):
    """Yield only the nodes that execute on the event loop.

    A nested def/lambda is how work is handed to a worker (`run_db(fn)`), so its
    body runs off the loop and must not be flagged. Without this the guardrail
    rejects the correct `run_db("op", lambda: Model.select())` form; it passes
    today only because every call site happens to use the @db_operation
    decorator instead. The trade-off is that a nested function which is *called*
    inline rather than handed to a worker is no longer inspected.
    """
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _model_symbols(tree: ast.AST) -> set[str]:
    symbols = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("db.models"):
            symbols.update(alias.asname or alias.name for alias in node.names)
    return symbols


@pytest.mark.unit
def test_async_request_code_does_not_execute_peewee_queries():
    violations = []
    for path in _python_files():
        if path in ASYNC_DB_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        models = _model_symbols(tree)
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
            for node in _loop_body(function):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                receiver = node.func.value
                direct_model_query = (
                    isinstance(receiver, ast.Name)
                    and receiver.id in models
                    and node.func.attr in MODEL_QUERY_METHODS
                )
                direct_save = node.func.attr == "save"
                if direct_model_query or direct_save:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} ({function.name})")
    assert not violations, "Peewee call inside async request code:\n" + "\n".join(violations)


@pytest.mark.unit
def test_request_modules_do_not_import_blocking_http_clients():
    violations = []
    for path in _python_files():
        if path in BLOCKING_HTTP_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            if any(name == "requests" or name.startswith("curl_cffi") for name in imported):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "Blocking HTTP import in request code:\n" + "\n".join(violations)


@pytest.mark.unit
def test_async_provider_calls_are_awaited_and_db_middleware_is_not_installed():
    violations = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name not in {"provider_get", "provider_post"}:
                continue
            if not isinstance(parents.get(node), ast.Await):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "Provider call is not awaited:\n" + "\n".join(violations)
    assert "add_middleware(DatabaseMiddleware" not in (ROOT / "main.py").read_text()
    assert not (ROOT / "core" / "db_middleware.py").exists()
    assert "def run_in_db_thread" not in (ROOT / "db" / "base.py").read_text()
