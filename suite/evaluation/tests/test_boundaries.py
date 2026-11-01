"""依存の向き（#3240）: suite → platform の公開の package だけ。

製品の module・DB・保存ファイルを読まない。

- suite の src は標準 library・httpx・pydantic・openpyxl（人手の判定の記入用のブック）・platform の
  `pr_evaluation_scoring`
  /`pr_enterprise_ai`（モデルの API の呼び方）・自分だけを import する（製品の `app.*`・
  `rag_*`・`nl2sql`・Agent の package を import しない）。
- suite の src は製品の DB の driver（`oracledb`）を import せず、製品のディレクトリの path を書か
  ない。
- suite の src は製品を公開の MCP（`POST /api/mcp`）だけで呼ぶ（画面の API・Cookie / CSRF を使わ
  ない）。製品・platform の設定と DB の object の名前（`RAG_`・`NL2SQL_`・`AGENT_`・`PLATFORM_` で
  始まる名前）を持たない（suite の設定は `SUITE_`）。
- suite の依存の閉包（`uv.lock`）にある自分たちの package は、platform の公開の package だけ。
- suite が使う platform の package（採点の部品・モデルの API の呼び方）は、標準 library と
  自分だけを import する（製品・suite を import しない）。
- 製品と platform の package は suite に依存しない。

docstring は説明の文なので、文字列の検査に入れない。製品の側の同じ検査は製品のテスト（rag は
`rag/backend/tests/test_product_boundaries.py`）にある。
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

SUITE_ROOT = Path(__file__).resolve().parents[1]
SUITE_SRC = SUITE_ROOT / "src" / "pr_suite_evaluation"
REPO = Path(__file__).resolve().parents[3]
PLATFORM_SCORING_SRC = REPO / "platform" / "packages" / "evaluation_scoring" / "src"
PLATFORM_ENTERPRISE_AI_SRC = REPO / "platform" / "packages" / "enterprise_ai" / "src"
SUITE_ALLOWED = {
    "httpx",
    "pydantic",
    "openpyxl",
    "pr_evaluation_scoring",
    "pr_enterprise_ai",
    "pr_suite_evaluation",
}
SCORING_ALLOWED = {"pr_evaluation_scoring"}
ENTERPRISE_AI_ALLOWED = {"pr_enterprise_ai"}
# 製品のディレクトリの path（実行時に製品の保存ファイル・設定を読まない）。
PRODUCT_PATH = re.compile(r"""["'](?:\.\./)*(?:rag|nl2sql|agent)/(?:backend|frontend|packages)""")
# suite の依存に入ってよい自分たちの package（suite 自身と、platform の公開の package）。
OWN_PACKAGES_ALLOWED = {
    "production-ready-assistant-evaluation",
    "production-ready-evaluation-scoring",
    "production-ready-enterprise-ai",
}
# 自分たちの package の名前（製品の backend・platform の package・rag の package とサービス）。
OWN_PACKAGE = re.compile(r"^(?:production-ready-|rag-|parser-|preprocess-)")
# 製品の API の path。suite が呼んでよいのは公開の MCP（`/api/mcp`）だけ。
API_PATH = re.compile(r"/api/[\w./-]*")
SCREEN_SESSION = re.compile(r"csrf|cookie", re.IGNORECASE)
# 製品・platform の設定（.env）と DB の object の名前（docs/agents/naming.md）。
PRODUCT_NAME = re.compile(r"\b(?:RAG|NL2SQL|AGENT|PLATFORM)_[A-Z0-9_]+\b")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _sources(root: Path) -> list[Path]:
    paths = sorted(root.rglob("*.py"))
    assert paths
    return paths


def _strings(path: Path) -> Iterator[str]:
    """docstring を除く文字列の定数（f-string の定数の部分を含む）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        ):
            docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


@pytest.mark.parametrize(
    ("root", "allowed"),
    [
        (SUITE_SRC, SUITE_ALLOWED),
        (PLATFORM_SCORING_SRC, SCORING_ALLOWED),
        (PLATFORM_ENTERPRISE_AI_SRC, ENTERPRISE_AI_ALLOWED),
    ],
    ids=["suite", "platform-scoring", "platform-enterprise-ai"],
)
def test_imports_stay_within_the_allowed_packages(root: Path, allowed: set[str]) -> None:
    for path in _sources(root):
        outside = {
            name
            for name in _imports(path)
            if name not in allowed and name not in sys.stdlib_module_names and name != "__future__"
        }
        assert not outside, (path.relative_to(REPO), sorted(outside))


def test_suite_does_not_read_product_files() -> None:
    for path in _sources(SUITE_SRC):
        text = path.read_text(encoding="utf-8")
        assert not PRODUCT_PATH.search(text), path.relative_to(REPO)
        assert "oracledb" not in _imports(path), path.relative_to(REPO)


def test_suite_calls_products_only_through_the_public_mcp() -> None:
    """製品は `POST /api/mcp` だけで呼ぶ（画面の API と Cookie / CSRF のログインを使わない）。"""
    for path in _sources(SUITE_SRC):
        for text in _strings(path):
            assert set(API_PATH.findall(text)) <= {"/api/mcp"}, (path.relative_to(REPO), text)
            assert not SCREEN_SESSION.search(text), (path.relative_to(REPO), text)


def test_suite_does_not_name_product_settings_or_db_objects() -> None:
    """製品・platform の設定（.env）と DB の object の名前を持たない（suite の設定は `SUITE_`）。"""
    for path in _sources(SUITE_SRC):
        for text in _strings(path):
            assert not PRODUCT_NAME.search(text), (path.relative_to(REPO), text)


def test_suite_lock_has_only_the_public_platform_packages() -> None:
    """suite の依存の閉包（uv.lock）にある自分たちの package は platform の公開の package だけ。

    製品の backend・rag の package・platform の FastAPI の package を推移的にも入れない。
    """
    lock = tomllib.loads((SUITE_ROOT / "uv.lock").read_text(encoding="utf-8"))
    own = {package["name"] for package in lock["package"] if OWN_PACKAGE.match(package["name"])}
    assert own == OWN_PACKAGES_ALLOWED, sorted(own ^ OWN_PACKAGES_ALLOWED)


@pytest.mark.parametrize(
    ("source", "check"),
    [
        ('URL = "https://example.invalid/agent/api/agents"', "api"),
        ('HEADERS = {"X-CSRF-Token": token}', "session"),
        ('KEY = os.environ["AGENT_RUNTIME_REPOSITORY_BACKEND"]', "name"),
        ('SQL = "SELECT 1 FROM RAG_DOCUMENTS"', "name"),
        ('URL = "http://localhost:8020/api/mcp"', None),
    ],
    ids=["screen-api", "csrf", "product-setting", "product-table", "public-mcp"],
)
def test_string_checks_detect_violations(tmp_path: Path, source: str, check: str | None) -> None:
    """文字列の検査そのものが、画面の API・Cookie / CSRF・製品の名前を見つける（合成のコード）。

    docstring の説明（下の 1 行目）は数えない。
    """
    path = tmp_path / "sample.py"
    path.write_text(f'"""説明の /api/agents・Cookie・RAG_X。"""\n{source}\n', encoding="utf-8")
    texts = list(_strings(path))
    found = {
        "api": any(set(API_PATH.findall(text)) - {"/api/mcp"} for text in texts),
        "session": any(SCREEN_SESSION.search(text) for text in texts),
        "name": any(PRODUCT_NAME.search(text) for text in texts),
    }
    assert found == {key: key == check for key in found}


def test_products_and_platform_do_not_depend_on_the_suite() -> None:
    """製品と platform の package は suite に依存しない（venv に無いので import もできない）。"""
    manifests = [
        *sorted((REPO / "platform" / "packages").glob("*/pyproject.toml")),
        *(REPO / product / "backend" / "pyproject.toml" for product in ("rag", "nl2sql", "agent")),
    ]
    for path in manifests:
        assert "production-ready-assistant-evaluation" not in path.read_text(encoding="utf-8"), path


def test_suite_does_not_install_fastapi() -> None:
    """suite の venv に FastAPI を入れない（モデルの API の呼び方は標準 library だけの
    `pr_enterprise_ai` から使う。#3418）。"""
    assert "production-ready-system-settings-backend" not in (
        SUITE_ROOT / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert 'name = "fastapi"' not in (SUITE_ROOT / "uv.lock").read_text(encoding="utf-8")
