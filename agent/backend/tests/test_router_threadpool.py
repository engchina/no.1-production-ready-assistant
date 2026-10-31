"""API の `async def` の handler は Run の repository を threadpool で呼ぶ（#1939）。

repository の変更・読み込みは lock を取り、変更は保存先へ書く。event loop の上で直に呼ぶと、
lock の待ち・一覧の組み立ての間、ほかの要求（作成中の回答の stream・一覧）も止まる
（#1935 で `DELETE /api/threads/{id}` が 19 秒かかった間、backend 全体が止まった）。
新しい handler で直に呼ぶ書き方を検出する。
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.features.agent.router as agent_router


def _direct_repository_calls(source: str) -> list[str]:
    """`async def` の中で `runtime_repository.<method>(...)` を直に呼ぶ所（行と名前）。"""
    found: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scopes: list[str] = []

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.scopes.append(node.name)
            self.generic_visit(node)
            self.scopes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            # 同期の関数（threadpool で呼ぶ helper）は対象外。
            saved, self.scopes = self.scopes, []
            self.generic_visit(node)
            self.scopes = saved

        def visit_Lambda(self, node: ast.Lambda) -> None:
            saved, self.scopes = self.scopes, []
            self.generic_visit(node)
            self.scopes = saved

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                self.scopes
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "runtime_repository"
            ):
                found.append(f"{node.lineno}: {self.scopes[-1]} → {func.attr}")
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


def test_async_handlers_do_not_call_the_repository_on_the_event_loop() -> None:
    source = Path(agent_router.__file__).read_text(encoding="utf-8")

    assert _direct_repository_calls(source) == []


def test_detector_finds_direct_calls() -> None:
    source = """
async def handler():
    runtime_repository.cancel_run("run")
    await run_in_threadpool(runtime_repository.cancel_run, "run")

def helper():
    runtime_repository.cancel_run("run")
"""
    assert _direct_repository_calls(source) == ["3: handler → cancel_run"]
