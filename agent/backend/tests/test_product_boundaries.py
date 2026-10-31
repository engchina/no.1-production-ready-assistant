"""製品の境界（#3240 の 3 章）: Agent は RAG・NL2SQL・suite の内部に依存しない。

Agent → RAG / NL2SQL は呼び先の公開の MCP（`POST /api/mcp`）と公開の契約
（platform/contracts/mcp/*.json・platform/contracts/answer-passages/**）だけで連携する（root の
AGENTS.md「構成」）。RAG の側の同じ検査は rag/backend/tests/test_product_boundaries.py。Agent の
Python のコード（backend。テストを含む）を静的に調べ、次を確かめる:

- import: `app.*` は Agent の backend の `app` にある module だけ（3 製品の backend は同じ `app` の
  名前を使うので、RAG・NL2SQL の module は名前だけでは区別できない）。RAG の package（`rag_engine`
  など）・suite の package を import しない。
- ファイル: RAG・NL2SQL・suite のディレクトリを読まない（読む契約は platform/contracts の下）。
- DB: RAG・NL2SQL の表（`RAG_*`・`NL2SQL_*`。docs/agents/naming.md）を参照しない。
- HTTP: RAG・NL2SQL の画面の API（Cookie / CSRF の API）と配備の URL（`/rag/api`・`/nl2sql/api`）を
  呼ばない。図・原文のプレビューの path（`/api/figures`・`/api/source-previews`）は RAG が
  `rag_read_source` の出力で返す公開の契約（rag-tools.json の pattern）なので対象にしない。
- 依存: Agent の `pyproject.toml` は他の製品・suite の package と、そのディレクトリの path に依存
  しない。

docstring は説明の文なので調べない（コメントも AST に入らない）。既知の例外は無い（足すときは
rag の検査と同じく Issue と一緒に例外の一覧を置く）。
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2]
REPO = AGENT.parent
BACKEND_APP = AGENT / "backend" / "app"
THIS_FILE = Path(__file__).resolve()

# 調べないディレクトリ（仮想環境・キャッシュ・frontend）。
SKIPPED_DIRS = frozenset({"node_modules", "frontend", "__pycache__"})
OTHER_PRODUCT_DIRS = ("rag", "nl2sql", "suite")
# RAG・suite の Python の package（RAG の backend・NL2SQL の backend は `app`）。
FOREIGN_PACKAGES = frozenset(
    {"rag_engine", "rag_parser_core", "rag_pipeline_core", "pr_suite_evaluation"}
)
OTHER_PACKAGE_NAMES = frozenset(
    {
        "production-ready-rag-backend",
        "production-ready-nl2sql-backend",
        "production-ready-suite-evaluation",
        "rag-engine",
        "rag-parser-core",
        "rag-pipeline-core",
    }
)
# 他の製品・suite のディレクトリ（`rag/backend`・`../nl2sql/frontend`・`suite/evaluation` など）と、
# 配備の URL（`/rag/api`・`/nl2sql/api`）。
OTHER_PRODUCT_PATH = re.compile(
    r"(?:^|/)(?:rag|nl2sql|suite)/"
    r"(?:backend|frontend|packages|services|evaluation|docs|scripts|AGENTS\.md|init_script\.sh)"
    r"(?:/|$)"
    r"|(?:^|/)(?:rag|nl2sql)/api(?:/|$)"
)
# RAG・NL2SQL の DB の object を SQL で参照する文（docs/agents/naming.md: 製品固有の表は製品の
# 接頭辞で始まる）。RAG・NL2SQL のツールの失敗の code（`RAG_ANSWER_AI_API_FAILED` など。公開の
# 契約）・定数の名前は表ではないので、SQL の句の後ろだけを見る。
FOREIGN_DB_OBJECTS = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|EXISTS)\s+(?:\"?\w+\"?\.)?\"?(?:RAG|NL2SQL)_[A-Z0-9_]+",
    re.IGNORECASE,
)
# RAG・NL2SQL の画面の API（Agent に同じ名前の資源は無い）。
FOREIGN_API_PATH = re.compile(
    r"^/(?:api/)?(?:search|search-answer-profiles|documents|document-categories|knowledge-bases"
    r"|synthetic-data|history|schema|services|chat|nl2sql)(?:/|$)"
)


def _files(suffix: str) -> list[Path]:
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(AGENT):
        dirnames[:] = sorted(
            name for name in dirnames if not name.startswith(".") and name not in SKIPPED_DIRS
        )
        paths.extend(Path(directory) / name for name in sorted(filenames) if name.endswith(suffix))
    assert paths
    return paths


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
        ):
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _path_parts(node: ast.expr) -> list[str] | None:
    """`base / "rag" / "backend"` の文字列の部分（文字列が 2 つ以上のときだけ）。"""
    parts: list[str] = []

    def collect(item: ast.expr) -> None:
        if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Div):
            collect(item.left)
            collect(item.right)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            parts.append(item.value)
        else:
            parts.append("\0")

    collect(node)
    strings = [part for part in parts if part != "\0"]
    return parts if len(strings) >= 2 else None


def _strings(tree: ast.AST) -> Iterator[str]:
    """docstring を除く文字列の定数と、path の `/` の連結と、`join` の文字列の引数。"""
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = _path_parts(node)
            if parts is not None:
                yield "/".join(part for part in parts if part != "\0")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"join", "joinpath"}
        ):
            args = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if len(args) >= 2:
                yield "/".join(args)


def _resolves_in_agent(module: str) -> bool:
    """`app.x.y` が Agent の backend の `app` にある module（か package）か。"""
    target = BACKEND_APP.joinpath(*module.split(".")[1:])
    return target.with_suffix(".py").is_file() or target.is_dir()


def _imports(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            if node.module == "app":
                yield from (f"app.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call) and node.args:
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            first = node.args[0]
            if (
                name in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                yield first.value


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for module in _imports(tree):
        root = module.split(".")[0]
        if root in FOREIGN_PACKAGES:
            found.append(f"RAG・suite の package の import: {module}")
        elif root == "app" and module != "app" and not _resolves_in_agent(module):
            found.append(f"Agent に無い app の module: {module}")
    for text in _strings(tree):
        if OTHER_PRODUCT_PATH.search(text):
            found.append(f"他の製品・suite のディレクトリか URL: {text!r}")
        if FOREIGN_DB_OBJECTS.search(text):
            found.append(f"他の製品の DB の object: {text!r}")
        if FOREIGN_API_PATH.search(text):
            found.append(f"RAG・NL2SQL の画面の API: {text!r}")
    return found


def _relative(path: Path) -> str:
    return path.relative_to(AGENT).as_posix()


def test_python_code_stays_within_agent() -> None:
    """Agent の Python のコードは RAG・NL2SQL・suite の module・ファイル・表・画面の API に
    依存しない。"""
    unexpected: dict[str, list[str]] = {}
    for path in _files(".py"):
        if path == THIS_FILE:
            continue
        violations = _violations(path)
        if violations:
            unexpected[_relative(path)] = violations
    assert not unexpected, unexpected


def test_manifests_do_not_depend_on_other_products_or_the_suite() -> None:
    """Agent の pyproject.toml は他の製品・suite の package にも、そのディレクトリにも依存
    しない。"""
    manifests = [path for path in _files("pyproject.toml") if path.name == "pyproject.toml"]
    others = [REPO / name for name in OTHER_PRODUCT_DIRS]
    assert manifests
    for manifest in manifests:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        project = data.get("project", {})
        requirements = [
            *project.get("dependencies", []),
            *(
                item
                for group in project.get("optional-dependencies", {}).values()
                for item in group
            ),
            *(
                item
                for group in data.get("dependency-groups", {}).values()
                for item in group
                if isinstance(item, str)
            ),
        ]
        names = {re.split(r"[\s\[<>=!~;]", item, maxsplit=1)[0].lower() for item in requirements}
        assert not names & OTHER_PACKAGE_NAMES, (
            _relative(manifest),
            sorted(names & OTHER_PACKAGE_NAMES),
        )
        sources = data.get("tool", {}).get("uv", {}).get("sources", {})
        for name, source in sources.items():
            entries = source if isinstance(source, list) else [source]
            for entry in entries:
                if "path" not in entry:
                    continue
                target = (manifest.parent / entry["path"]).resolve()
                assert not any(target.is_relative_to(other) for other in others), (
                    _relative(manifest),
                    name,
                )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from app.rag.answer_engine import validate", "Agent に無い app の module"),
        ("from rag_engine.generation.operation_audit import answer_passages", "package の import"),
        ("import pr_suite_evaluation.cli", "package の import"),
        ('ROOT = REPO / "rag" / "backend"', "他の製品・suite のディレクトリか URL"),
        ('SQL = "SELECT 1 FROM RAG_CHUNKS"', "他の製品の DB の object"),
        ('SQL = "insert into nl2sql_profiles (id) values (:id)"', "他の製品の DB の object"),
        ('client.post("/api/search", json=body)', "RAG・NL2SQL の画面の API"),
        ('URL = "https://suite.invalid/rag/api/documents"', "他の製品・suite のディレクトリか URL"),
        ('importlib.import_module("app.api.routes.search")', "Agent に無い app の module"),
    ],
    ids=[
        "rag-module",
        "rag-package",
        "suite-package",
        "rag-path",
        "rag-table",
        "nl2sql-table",
        "rag-screen-api",
        "rag-url",
        "dynamic-import",
    ],
)
def test_checker_detects_each_kind_of_dependency(
    tmp_path: Path, source: str, expected: str
) -> None:
    """検査そのものが、それぞれの種類の依存を見つける（合成のコード）。"""
    path = tmp_path / "sample.py"
    path.write_text(source + "\n", encoding="utf-8")
    assert any(expected in violation for violation in _violations(path)), _violations(path)


def test_checker_ignores_docstrings_and_agent_code(tmp_path: Path) -> None:
    """docstring の説明・Agent 自身の module・公開の契約と MCP の URL・設定の名前は違反にしない。"""
    path = tmp_path / "sample.py"
    path.write_text(
        '"""RAG の `rag_engine` と `rag/backend` の説明。"""\n'
        "from app.features.agent.answer_validation import align_claims\n"
        "from app import settings\n"
        'CONTRACT = "platform/contracts/mcp/rag-tools.json"\n'
        'URL = "http://rag.test/api/mcp"\n'
        'FIGURE = "/api/figures/"\n'
        'ENV = "AGENT_EXTERNAL_RAG_MCP_URL"\n'
        'CODE = "RAG_ANSWER_AI_API_FAILED"\n'
        'SQL = "SELECT 1 FROM AGENT_RUNTIME_ENTITIES"\n',
        encoding="utf-8",
    )
    assert _violations(path) == []
