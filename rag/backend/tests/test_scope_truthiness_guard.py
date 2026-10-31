"""データの範囲（`allowed_*`）を真偽値で判定するコードを見つけるガード（#3250）。

範囲は「None = 制限なし」「空の集合 = 何も使えない（拒否全件）」で表す。`if not allowed_*:`・
`if allowed_*:`・`allowed_* or ...` のような真偽値の判定は、この 2 つを同じに扱うため、範囲が空に
なったときに全件が見える（fail-open）。真偽値で判定してよいのは、同じ関数の中で先に
`is None` / `is not None` で None を分けたあとだけ。

`rag/backend/app` の Python を AST で読み、範囲のフィールド（`AuditRequestContext` と `Principal`
の `allowed_*`）と、それを代入しただけの局所変数の真偽値の判定を探す。
"""

from __future__ import annotations

import ast
import dataclasses
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.rag.request_context import AuditRequestContext
from app.security.domain import Principal

APP_DIR = Path(__file__).resolve().parents[1] / "app"
SCOPE_FIELDS = frozenset(
    field.name
    for cls in (AuditRequestContext, Principal)
    for field in dataclasses.fields(cls)
    if field.name.startswith("allowed_")
)

type _Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.Module


def _scope_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr in SCOPE_FIELDS:
        return node.attr
    if isinstance(node, ast.Name) and node.id in SCOPE_FIELDS:
        return node.id
    return None


def _truthy_operands(node: ast.expr) -> Iterator[ast.expr]:
    """真偽値として評価される式（`and` / `or` / `not` の中まで開く）。"""
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _truthy_operands(value)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from _truthy_operands(node.operand)
    else:
        yield node


def _truthiness_tests(node: ast.AST) -> Iterator[ast.expr]:
    """node が真偽値として評価する式。"""
    if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
        yield from _truthy_operands(node.test)
    elif isinstance(node, ast.comprehension):
        for condition in node.ifs:
            yield from _truthy_operands(condition)
    elif isinstance(node, ast.BoolOp):
        # `x = allowed or frozenset()` のように、条件文の外の and / or も真偽値の判定。
        yield from _truthy_operands(node)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from _truthy_operands(node.operand)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "bool"
        and len(node.args) == 1
    ):
        yield node.args[0]


def _own_nodes(scope: _Scope) -> Iterator[ast.AST]:
    """関数（またはモジュール）の中の node。入れ子の関数の中は、その関数の分として数える。"""
    stack: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _none_checks(nodes: list[ast.AST]) -> list[tuple[str, tuple[int, int]]]:
    """`<式> is None` / `<式> is not None` の式（unparse）と位置。"""
    found: list[tuple[str, tuple[int, int]]] = []
    for node in nodes:
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Is, ast.IsNot))
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None
        ):
            found.append((ast.unparse(node.left), _position(node)))
    return found


def _aliases(nodes: list[ast.AST]) -> set[str]:
    """範囲のフィールドを代入しただけの局所変数（`allowed = context.allowed_*`）。"""
    names: set[str] = set()
    for node in nodes:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _scope_name(node.value) is not None
        ):
            names.add(node.targets[0].id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and _scope_name(node.value) is not None
        ):
            names.add(node.target.id)
    return names


def find_scope_truthiness(source: str) -> tuple[list[str], int]:
    """None を分けずに範囲を真偽値で判定している箇所（`行: 式`）と、None を分けたあとの判定の数。"""
    tree = ast.parse(source)
    offenders: list[str] = []
    guarded = 0
    scopes: list[_Scope] = [tree]
    scopes.extend(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    )
    for scope in scopes:
        nodes = list(_own_nodes(scope))
        aliases = _aliases(nodes)
        none_checks = _none_checks(nodes)
        tests = {id(test): test for node in nodes for test in _truthiness_tests(node)}
        for test in tests.values():
            is_scope = _scope_name(test) is not None or (
                isinstance(test, ast.Name) and test.id in aliases
            )
            if not is_scope:
                continue
            expression = ast.unparse(test)
            position = _position(test)
            if any(checked == expression and at < position for checked, at in none_checks):
                guarded += 1
            else:
                offenders.append(f"{position[0]}: {expression}")
    return sorted(set(offenders)), guarded


def test_app_does_not_judge_access_scope_by_truthiness() -> None:
    offenders: list[str] = []
    guarded = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        found, count = find_scope_truthiness(path.read_text(encoding="utf-8"))
        guarded += count
        offenders.extend(f"{path.relative_to(APP_DIR.parent)}:{item}" for item in found)
    assert offenders == [], (
        "範囲（allowed_*）を、None（制限なし）と空の集合（拒否全件）を分けずに真偽値で判定して"
        "います。先に `is None` / `is not None` で分けてください:\n" + "\n".join(offenders)
    )
    # ガードが範囲の判定を読めていること（None を分けたあとの判定は app に今もある）。
    assert guarded > 0


def test_scope_fields_are_the_access_scopes() -> None:
    assert {
        "allowed_document_ids",
        "allowed_category_names",
        "allowed_knowledge_base_ids",
        "allowed_search_answer_profile_ids",
    } <= SCOPE_FIELDS


@pytest.mark.parametrize(
    "source",
    [
        "def f(c):\n    if not c.allowed_knowledge_base_ids:\n        return []",
        "def f(c):\n    if c.allowed_document_ids:\n        return 1",
        "def f(c):\n    return c.allowed_category_names or frozenset()",
        "def f(c):\n    ok = bool(c.allowed_search_answer_profile_ids)",
        "def f(c):\n    return [x for x in y if c.allowed_knowledge_base_ids]",
        "def f(c):\n    allowed = c.allowed_knowledge_base_ids\n"
        "    if not allowed:\n        return []",
        "def f(c):\n    if not c.allowed_document_ids:\n        pass\n"
        "    if c.allowed_document_ids is None:\n        pass",
        "def f(c, d):\n    if d.allowed_document_ids is None:\n        return\n"
        "    if not c.allowed_knowledge_base_ids:\n        return",
        "def f(c):\n    if c.allowed_knowledge_base_ids is None:\n        return\n"
        "    def g():\n        return not c.allowed_knowledge_base_ids",
    ],
    ids=[
        "if-not",
        "if",
        "or-default",
        "bool-call",
        "comprehension",
        "alias",
        "none-check-after",
        "none-check-of-other-field",
        "nested-function",
    ],
)
def test_guard_finds_truthiness_without_none_check(source: str) -> None:
    offenders, _ = find_scope_truthiness(source)
    assert offenders, source


@pytest.mark.parametrize(
    "source",
    [
        """
        def f(c):
            if c.allowed_knowledge_base_ids is not None:
                if not c.allowed_knowledge_base_ids:
                    return ["1 = 0"]
        """,
        """
        def f(c):
            allowed = c.allowed_search_answer_profile_ids
            if allowed is None:
                return []
            if not allowed:
                return ["1 = 0"]
        """,
        """
        def f(c):
            return c.allowed_document_ids is not None and not c.allowed_document_ids
        """,
        """
        def f(c, item):
            allowed = c.allowed_knowledge_base_ids
            return allowed is None or item in allowed
        """,
        """
        def f(c, ids):
            if ids or c.allowed_knowledge_base_ids is not None:
                return 1
        """,
    ],
    ids=["nested-none-check", "early-return", "same-expression", "membership", "other-operand"],
)
def test_guard_accepts_explicit_none_checks(source: str) -> None:
    offenders, _ = find_scope_truthiness(textwrap.dedent(source))
    assert offenders == [], offenders
