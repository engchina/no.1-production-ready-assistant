"""組み込みのスキル（repo 直下の `skills/`）は Agent Skills の仕様と外部へ配る形を満たす（#1816）。

- 仕様の検証は公式の `skills-ref`（anthropics/agentskills の参照実装）の `validate`。
- 仕様・best practices の目安（本文 500 行以内、参照は本文から 1 段の深さ）と、本プロダクトの拡張
  （`metadata` の MCP ツールの宣言が RAG / NL2SQL の MCP の契約にあるツールか）を確かめる。
- Claude Code の plugin marketplace（`.claude-plugin/marketplace.json` と
  `skills/.claude-plugin/plugin.json`）の必須の項目と、plugin の置き場所。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import skills_ref  # type: ignore[import-untyped]  # 型の情報（py.typed）が無い
import yaml

from app.features.agent.skills import BUILTIN_SKILL_IDS, BUILTIN_SKILLS_DIR, skill_registry
from app.features.agent.skills_loader import METADATA_MCP_TOOLS

REPO_ROOT = BUILTIN_SKILLS_DIR.parent
CONTRACTS = REPO_ROOT / "platform/contracts/mcp"
SKILL_DIRS = sorted(
    path for path in BUILTIN_SKILLS_DIR.iterdir() if path.is_dir() and not path.name.startswith(".")
)
SKILL_NAMES = [path.name for path in SKILL_DIRS]


def _frontmatter(skill_dir: Path) -> tuple[dict[str, object], str]:
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    _, raw, body = text.split("---", 2)
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    return data, body


def _contract_tools(server: str) -> set[str]:
    contract = json.loads((CONTRACTS / f"{server}-tools.json").read_text(encoding="utf-8"))
    return {str(tool["name"]) for tool in contract["tools"]}


def test_builtin_skills_are_the_skill_folders() -> None:
    assert set(SKILL_NAMES) == set(BUILTIN_SKILL_IDS)
    for name in SKILL_NAMES:
        skill = skill_registry.get(name)
        assert skill is not None and skill.source == "builtin"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_NAMES)
def test_skill_folder_passes_the_reference_validator(skill_dir: Path) -> None:
    assert skills_ref.validate(skill_dir) == []


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_NAMES)
def test_skill_md_follows_best_practices(skill_dir: Path) -> None:
    data, body = _frontmatter(skill_dir)
    description = str(data["description"])
    # 外部の Agent が選べるよう、何をするか＋いつ使うか（「使う」）と MCP サーバーを書く。
    assert "使う" in description and "MCP サーバー" in description
    # 本文は 500 行以内。長い参照は references/ に出し、本文から 1 段の深さで参照する。
    assert len(body.strip().splitlines()) <= 500
    linked = set(re.findall(r"\]\((references/[^)]+)\)", body))
    files = (
        {
            path.relative_to(skill_dir).as_posix()
            for path in (skill_dir / "references").rglob("*")
            if path.is_file()
        }
        if (skill_dir / "references").is_dir()
        else set()
    )
    assert linked == files
    for reference in files:
        assert "](references/" not in (skill_dir / reference).read_text(encoding="utf-8")


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_NAMES)
def test_skill_mcp_tools_exist_in_the_mcp_contracts(skill_dir: Path) -> None:
    data, body = _frontmatter(skill_dir)
    metadata = data["metadata"]
    assert isinstance(metadata, dict)
    declared = str(metadata[METADATA_MCP_TOOLS]).split()
    for token in declared:
        server, _, tool = token.partition(":")
        assert server in {"rag", "nl2sql"}
        assert tool in _contract_tools(server), token
        # 本文は MCP サーバー名と、宣言したツールの素の名前で書く（ツールの一覧の節は置かない。
        # 一覧は metadata とモデルに渡すツールの定義にある）。
        assert f"MCP サーバー `{server}`" in body
        assert f"`{tool}`" in body
    skill = skill_registry.get(skill_dir.name)
    assert skill is not None
    assert sorted(
        f"{item.server_id}:{tool}" for item in skill.mcp_requirements for tool in item.tool_names
    ) == sorted(declared)


# 組み込み Runtime だけがツールの結果に足すフィールド（外部の Agent には返らない）。スキルの本文では
# 「組み込みの Agent」の行にだけ書く（読み手を分ける。docs/agents/prompts.md）。
RUNTIME_ONLY_FIELDS = frozenset({"next_step", "rag_calls_remaining"})
BUILTIN_AGENT_MARK = "組み込みの Agent"
_CODE_SPAN = re.compile(r"`([^`]+)`")
# 識別子とみなす `code`（日本語の例・「mcp__rag__」のような前置きの例は対象外）。
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_LITERALS = frozenset({"true", "false", "null"})


_DESCRIBED_VALUE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9_]*(?![A-Za-z0-9_])")


def _schema_vocabulary(node: object, found: set[str]) -> None:
    """JSON schema の中のフィールド名・enum / const の文字列の値・説明に書いた値の名前を集める。

    説明の値は、文字列のフィールドの値の一覧
    （`role` の「retrieved_anchor / catalog_article」など）。
    """
    if isinstance(node, dict):
        description = node.get("description")
        if isinstance(description, str):
            found.update(_DESCRIBED_VALUE.findall(description))
        for key in ("properties", "$defs"):
            children = node.get(key)
            if isinstance(children, dict):
                if key == "properties":
                    found.update(str(name) for name in children)
                for child in children.values():
                    _schema_vocabulary(child, found)
        for key in ("enum", "const"):
            values = node.get(key)
            for value in values if isinstance(values, list) else [values]:
                if isinstance(value, str):
                    found.add(value)
        for key, value in node.items():
            if key not in ("properties", "$defs", "enum", "const"):
                _schema_vocabulary(value, found)
    elif isinstance(node, list):
        for item in node:
            _schema_vocabulary(item, found)


def _contract_vocabulary(server: str) -> set[str]:
    contract = json.loads((CONTRACTS / f"{server}-tools.json").read_text(encoding="utf-8"))
    found = {str(tool["name"]) for tool in contract["tools"]}
    for tool in contract["tools"]:
        _schema_vocabulary(tool.get("inputSchema"), found)
        _schema_vocabulary(tool.get("outputSchema"), found)
    return found


def _identifiers(text: str) -> list[str]:
    """`code` の識別子（`format: "compact"`・`wait_seconds=40` は引数の名前）。"""
    names: list[str] = []
    for raw in _CODE_SPAN.findall(text):
        name = re.split(r"[:=]", raw, maxsplit=1)[0].strip()
        if _IDENTIFIER.fullmatch(name) and "__" not in name:
            names.append(name)
    return names


def _declared_servers(skill_dir: Path) -> set[str]:
    data, _ = _frontmatter(skill_dir)
    metadata = data["metadata"]
    assert isinstance(metadata, dict)
    return {token.partition(":")[0] for token in str(metadata[METADATA_MCP_TOOLS]).split()}


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_NAMES)
def test_skill_body_fields_exist_in_the_mcp_contracts(skill_dir: Path) -> None:
    """本文が書くツール・引数・結果のフィールドと値は、宣言した MCP サーバーの契約にある。

    外部の Agent にも配るので、MCP が実際に返すものだけを書く。組み込み Runtime だけが足す
    フィールドは `RUNTIME_ONLY_FIELDS` に限り、「組み込みの Agent」の行にだけ書く。
    """
    _, body = _frontmatter(skill_dir)
    servers = _declared_servers(skill_dir)
    vocabulary = set().union(*(_contract_vocabulary(server) for server in servers))
    allowed = vocabulary | servers | _LITERALS | RUNTIME_ONLY_FIELDS
    unknown = sorted(
        {name for name in _identifiers(body) for part in name.split(".") if part not in allowed}
    )
    assert unknown == [], unknown
    for line in body.splitlines():
        runtime_only = RUNTIME_ONLY_FIELDS & {
            part for name in _identifiers(line) for part in name.split(".")
        }
        assert not runtime_only or BUILTIN_AGENT_MARK in line, line


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=SKILL_NAMES)
def test_skill_body_calls_only_declared_tools(skill_dir: Path) -> None:
    """本文で呼ぶ MCP のツールは frontmatter の `production-ready-mcp-tools` で宣言したもの。"""
    data, body = _frontmatter(skill_dir)
    metadata = data["metadata"]
    assert isinstance(metadata, dict)
    declared = {token.partition(":")[2] for token in str(metadata[METADATA_MCP_TOOLS]).split()}
    all_tools = _contract_tools("rag") | _contract_tools("nl2sql")
    called = {name for name in _identifiers(body) if name in all_tools}
    assert called <= declared, sorted(called - declared)


def test_claude_code_plugin_marketplace_lists_the_skills_folder() -> None:
    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace["name"] and marketplace["owner"]["name"]
    [entry] = marketplace["plugins"]
    assert entry["source"] == "./skills"
    plugin_root = REPO_ROOT / entry["source"]
    manifest = json.loads((plugin_root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    # entry の名前（install の ID）と plugin.json の名前（スキルの前置き）をそろえる。
    assert manifest["name"] == entry["name"]
    # plugin の root（skills/）の直下の <name>/SKILL.md をスキルとして読ませる。
    assert manifest["skills"] == "./"
