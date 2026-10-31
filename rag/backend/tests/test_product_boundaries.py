"""製品の境界（#3240）: rag は Agent・NL2SQL・suite の内部に依存しない。

依存の向きは Agent → RAG だけで、RAG は Agent を呼ばない（root の AGENTS.md「構成」）。製品同士は
コードで依存せず、呼ぶ側が公開の MCP の契約だけを使う。rag の Python のコード（backend・packages・
services。テストを含む）を静的に調べ、次を確かめる:

- import: `app.*` は rag の backend の `app` にある module だけ（3 製品の backend は同じ `app` の
  名前を使うので、Agent・NL2SQL の module は名前だけでは区別できない）。suite の package を
  import しない。
- ファイル: Agent・NL2SQL・suite のディレクトリと、rag が使わない platform の契約（Agent・NL2SQL の
  MCP の契約・Agent の評価の共有の fixture・Agent の回答の節の事例）を読まない。rag が読む契約は
  `rag-tools.json` と `answer-passages` の事例で、CI の `changes` の `rag_contracts` と同じ。
- DB: Agent の表（`AGENT_RUNTIME_*` など）と NL2SQL の表を参照しない。
- HTTP: Agent の API（`/agents`・`/skills`）と Agent の URL（`/agent/api`）を呼ばない。
- 依存: rag の `pyproject.toml` は他の製品・suite の package と、そのディレクトリの path に依存
  しない。

docstring は説明の文なので調べない（コメントも AST に入らない）。既知の例外は `KNOWN_EXCEPTIONS`
に Issue と一緒に置き、直してそのファイルに違反が無くなったら例外も外す（外し忘れも失敗にする）。
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

RAG = Path(__file__).resolve().parents[2]
REPO = RAG.parent
BACKEND_APP = RAG / "backend" / "app"
THIS_FILE = Path(__file__).resolve()

# 調べないディレクトリ（仮想環境・キャッシュ・frontend）。
SKIPPED_DIRS = frozenset({"node_modules", "frontend", "__pycache__"})
OTHER_PRODUCT_DIRS = ("agent", "nl2sql", "suite")
SUITE_PACKAGES = frozenset({"pr_suite_evaluation"})
OTHER_PACKAGE_NAMES = frozenset(
    {
        "production-ready-agent-backend",
        "production-ready-nl2sql-backend",
        "production-ready-suite-evaluation",
    }
)
# 他の製品・suite のディレクトリ（`agent/backend`・`../nl2sql/frontend`・`suite/evaluation` など）
# と、配備の URL（`/agent/api`・`/nl2sql/api`）。RAG 自身の API の path（`/api/settings/nl2sql/...`
# の削除の確認など）は対象にしない。
OTHER_PRODUCT_PATH = re.compile(
    r"(?:^|/)(?:agent|nl2sql|suite)/"
    r"(?:backend|frontend|packages|evaluation|docs|scripts|AGENTS\.md|init_script\.sh)(?:/|$)"
    r"|(?:^|/)(?:agent|nl2sql)/api(?:/|$)"
)
# rag が読まない platform の契約（RAG は Agent の回答の形式・Agent の MCP を持たない）。
FOREIGN_CONTRACTS = re.compile(
    r"(?:agent-tools|nl2sql-tools|agent-evaluation-baseline|unverified-section)\.json"
)
# Agent・NL2SQL の DB の object（docs/agents/naming.md: 製品固有の表は製品の接頭辞で始まる）。
# RAG 自身の列 `AGENT_MEMORY_*`（RAG の監査の旧い計測）は Agent の表ではないので、Agent の表の
# 接頭辞だけを見る。
FOREIGN_DB_OBJECTS = re.compile(
    r"\bAGENT_(?:RUNTIME|CONTROL_PLANE|RUN_FACTS|ROLE|SCHEMA)_[A-Z0-9_]*|\bNL2SQL_[A-Z0-9_]+"
)
# Agent の API（RAG に同じ名前の資源は無い）。
AGENT_API_PATH = re.compile(r"^/(?:api/)?(?:agents|skills)(?:/|$)")

# 既知の例外（ファイル → Issue）。直したら外す。今は無い（合成資料の再構築の CLI は #3545 で
# Agent の段を Agent の側の命令へ移した）。
KNOWN_EXCEPTIONS: dict[str, str] = {}


def _files(suffix: str) -> list[Path]:
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(RAG):
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
    """`base / "agent" / "backend"` の文字列の部分（文字列が 2 つ以上のときだけ）。"""
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
    """docstring を除く文字列の定数と、f-string の定数の部分と、path の `/` の連結。"""
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = _path_parts(node)
            if parts is not None:
                # 文字列でない部分（base の path）は区切りにする。
                yield "/".join(part for part in parts if part != "\0")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"join", "joinpath"}
        ):
            # os.path.join(...)・Path.joinpath(...) の文字列の引数。
            args = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if len(args) >= 2:
                yield "/".join(args)


def _app_dir(path: Path) -> Path:
    """そのファイルの uv project（近い `pyproject.toml`）の `app`（backend と各サービス）。"""
    for parent in path.parents:
        if (parent / "pyproject.toml").is_file() or parent == RAG:
            return parent / "app"
    return BACKEND_APP


def _resolves_in_rag(module: str, app_dir: Path) -> bool:
    """`app.x.y` がそのファイルの project の `app` にある module（か package）か。"""
    target = app_dir.joinpath(*module.split(".")[1:])
    return target.with_suffix(".py").is_file() or target.is_dir()


def _imports(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            if node.module == "app":
                # `from app import x` の x は app の下の module。
                yield from (f"app.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call) and node.args:
            # 動的な import（importlib.import_module・__import__）の名前。
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
    app_dir = _app_dir(path)
    found: list[str] = []
    for module in _imports(tree):
        root = module.split(".")[0]
        if root in SUITE_PACKAGES:
            found.append(f"suite の import: {module}")
        elif root == "app" and module != "app" and not _resolves_in_rag(module, app_dir):
            found.append(f"rag に無い app の module: {module}")
    for text in _strings(tree):
        if OTHER_PRODUCT_PATH.search(text):
            found.append(f"他の製品・suite のディレクトリか URL: {text!r}")
        if FOREIGN_CONTRACTS.search(text):
            found.append(f"rag が使わない契約: {text!r}")
        if FOREIGN_DB_OBJECTS.search(text):
            found.append(f"他の製品の DB の object: {text!r}")
        if AGENT_API_PATH.search(text):
            found.append(f"Agent の API: {text!r}")
    return found


def _relative(path: Path) -> str:
    return path.relative_to(RAG).as_posix()


def test_python_code_stays_within_rag() -> None:
    """rag の Python のコードは Agent・NL2SQL・suite の module・ファイル・表・API に依存しない。"""
    unexpected: dict[str, list[str]] = {}
    for path in _files(".py"):
        if path == THIS_FILE or _relative(path) in KNOWN_EXCEPTIONS:
            continue
        violations = _violations(path)
        if violations:
            unexpected[_relative(path)] = violations
    assert not unexpected, unexpected


def test_known_exceptions_still_need_the_exception() -> None:
    """既知の例外のファイルに違反が無くなったら、例外を外す（Issue が閉じられる状態の印）。

    例外が無いときも skip にせず通す（parametrize の空の一覧は skip になる）。
    """
    for relative, issue in sorted(KNOWN_EXCEPTIONS.items()):
        path = RAG / relative
        assert path.is_file(), f"{relative} が無い。KNOWN_EXCEPTIONS から外す（{issue}）"
        assert _violations(path), f"{relative} に違反が無い。KNOWN_EXCEPTIONS から外す（{issue}）"


def test_manifests_do_not_depend_on_other_products_or_the_suite() -> None:
    """rag の pyproject.toml は他の製品・suite の package にも、そのディレクトリにも依存しない。"""
    manifests = [path for path in _files("pyproject.toml") if path.name == "pyproject.toml"]
    others = [REPO / name for name in OTHER_PRODUCT_DIRS]
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
        ("from app.features.agent.runtime import RunRepository", "rag に無い app の module"),
        ("import pr_suite_evaluation.cli", "suite の import"),
        ('ROOT = REPO / "agent" / "backend"', "他の製品・suite のディレクトリか URL"),
        ('CONTRACT = "platform/contracts/mcp/agent-tools.json"', "rag が使わない契約"),
        ('SQL = "SELECT 1 FROM AGENT_RUNTIME_RUNS"', "他の製品の DB の object"),
        ('client.get(f"/agents/{agent_id}/publish")', "Agent の API"),
        ('URL = "https://suite.invalid/agent/api"', "他の製品・suite のディレクトリか URL"),
        ('importlib.import_module("app.features.nl2sql.service")', "rag に無い app の module"),
    ],
    ids=[
        "agent-module",
        "suite-package",
        "agent-path",
        "agent-contract",
        "agent-table",
        "agent-api",
        "agent-url",
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


def test_checker_ignores_docstrings_and_rag_modules(tmp_path: Path) -> None:
    """docstring の説明と rag 自身の module・RAG 自身の列は違反にしない。"""
    path = tmp_path / "sample.py"
    path.write_text(
        '"""Agent の adapter（`suite/evaluation`）と `agent-tools.json` の説明。"""\n'
        "from app.rag.evaluation import score_case_answers\n"
        "from app import config\n"
        'COLUMN = "AGENT_MEMORY_RETRIEVED_COUNT"\n'
        'HEADER = "x-rag-agent-id"\n',
        encoding="utf-8",
    )
    assert _violations(path) == []
