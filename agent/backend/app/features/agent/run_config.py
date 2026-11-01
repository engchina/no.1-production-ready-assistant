"""Run の実行構成の指紋（#67）。

Run が実際に使った構成を、本文・資格情報を含まない hash の参照として Run の metadata
（`run_config`）に残す。モデル・MCP の schema・スキル・指示が変わった後に、回帰が業務 Agent の
変更か実行環境の変更かを切り分ける材料にし、記録再生（`app.cli.run_replay`）が「記録したモデルの
出力が今の構成に当てはまるか」を確かめるのに使う。

- 指紋（`fingerprint`）に入るのは、モデルへの入力を決める構成だけ: 指示の変わらない部分
  （業務 Agent の指示・スキル・データの範囲の案内）・モデルに渡すツールの定義・スキルの定義・
  MCP 接続のツールの schema・モデルの設定・データの範囲。ツール権限の「承認」/「自動」は
  モデルに渡すツールを変えないので入らない（再生で承認の壁の違いを検出する対象）。「拒否」は
  ツールを渡さないので、ツールの定義の hash が変わる。
- 参照文書の本文は固定していない（#1855。読むのは今の本文）。Run の開始の時点の本文の hash を
  `skills[].references` に、読んだ時点の hash をツールの結果（`skill_reference_read` の
  `sha256`）に残し、`observed_references` で Run の間の変化を出す（変わっていれば、その Run は
  同じ本文を読み続けた保証が無い＝比較できない）。
- RAG の検索対象の版は Agent が持たない。RAG のツールの結果が契約で返す回答の provenance
  （検索・回答プロファイルの ID・設定の sha256・更新時刻・プロンプトの版）と根拠の
  `chunk_set_id` を `observed_rag_targets` で拾う（RAG の内部の版を推測しない）。
- モデルの重みの版は provider が公開しないので `model.snapshot = "unverified"`（同じモデルの ID の
  中身の変更は指紋で検出できない）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

RUN_CONFIG_METADATA_KEY = "run_config"
RUN_CONFIG_SCHEMA_VERSION = 1
FINGERPRINT_CHARS = 16
# 指紋に入れる部分（`run_config` の key）。
FINGERPRINT_PARTS = ("instructions", "tools", "skills", "mcp", "model", "data_scopes")


def sha256(value: object) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_configuration(
    *,
    static_instructions: str,
    tools: Sequence[Any],
    skills: Sequence[Mapping[str, Any]],
    mcp_tools: Iterable[Any],
    model: Mapping[str, Any],
    data_scopes: Any,
    sdk_version: str,
) -> dict[str, Any]:
    """Run の構成（hash の参照）と指紋。

    `tools` はモデルに渡す SDK の function tool（name・description・params_json_schema）、
    `mcp_tools` は MCP 接続のツールの定義（`ToolDefinition`）、`skills` は実行が使うスキルの定義
    （`AgentSkillDefinition` の dump）。
    """
    exposed = sorted(
        (
            {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "")),
                "parameters": getattr(tool, "params_json_schema", None),
            }
            for tool in tools
        ),
        key=lambda item: str(item["name"]),
    )
    by_server: dict[str, list[dict[str, Any]]] = {}
    for definition in mcp_tools:
        server_id = getattr(definition, "mcp_server_id", None) or ""
        by_server.setdefault(server_id, []).append(
            {
                "name": getattr(definition, "mcp_tool_name", None) or definition.name,
                "input_schema": definition.input_schema,
                "output_schema": definition.output_schema,
                "side_effects": definition.side_effects,
            }
        )
    scopes = data_scopes.model_dump(mode="json") if hasattr(data_scopes, "model_dump") else None
    config: dict[str, Any] = {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "instructions": {"static_sha256": sha256(static_instructions)},
        "tools": {
            "sha256": sha256(exposed),
            "names": [item["name"] for item in exposed],
        },
        "skills": [
            {
                "id": str(skill.get("id", "")),
                "sha256": sha256(
                    {
                        key: skill.get(key)
                        for key in ("name", "description", "instructions", "mcp_requirements")
                    }
                    | {"references": [item.get("path") for item in skill.get("references", [])]}
                ),
                "references": [
                    {"path": item.get("path"), "sha256": item.get("sha256") or None}
                    for item in skill.get("references", [])
                ],
            }
            for skill in skills
        ],
        "mcp": {
            server_id: {
                "tools_sha256": sha256(sorted(items, key=lambda item: str(item["name"]))),
                "tools": sorted(str(item["name"]) for item in items),
            }
            for server_id, items in sorted(by_server.items())
        },
        "model": {**dict(model), "snapshot": "unverified"},
        "data_scopes": {"sha256": sha256(scopes) if scopes is not None else None},
        "sdk_version": sdk_version,
    }
    config["fingerprint"] = fingerprint(config)
    return config


def fingerprint(config: Mapping[str, Any]) -> str:
    return sha256({part: config.get(part) for part in FINGERPRINT_PARTS})[:FINGERPRINT_CHARS]


def config_differences(base: Mapping[str, Any], target: Mapping[str, Any]) -> list[str]:
    """2 つの構成で違う部分（指紋に入る部分の名前と、ツール・接続は名前の差）。"""
    differences: list[str] = []
    for part in FINGERPRINT_PARTS:
        if base.get(part) == target.get(part):
            continue
        if part == "tools":
            before = set((base.get("tools") or {}).get("names") or [])
            after = set((target.get("tools") or {}).get("names") or [])
            added, removed = sorted(after - before), sorted(before - after)
            detail = "; ".join(
                text
                for text in (
                    f"足された: {', '.join(added)}" if added else "",
                    f"消えた: {', '.join(removed)}" if removed else "",
                )
                if text
            )
            differences.append(f"tools（{detail or 'ツールの説明か schema が違う'}）")
        else:
            differences.append(part)
    return differences


def observed_references(
    steps: Iterable[Any], config: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Run が読んだ参照文書と読んだ時点の hash。Run の開始の時点の hash と違えば `changed`。"""
    started = {
        (str(skill.get("id")), str(item.get("path"))): item.get("sha256")
        for skill in (config or {}).get("skills", [])
        for item in skill.get("references", [])
    }
    seen: dict[tuple[str, str], set[str]] = {}
    for step in steps:
        call = getattr(step, "tool_call", None)
        result = getattr(step, "tool_result", None)
        if call is None or result is None or not result.success:
            continue
        output = result.output if isinstance(result.output, dict) else {}
        digest = output.get("sha256")
        if call.name != "skill_reference_read" or not isinstance(digest, str):
            continue
        key = (str(output.get("skill_id")), str(output.get("path")))
        seen.setdefault(key, set()).add(digest)
    return [
        {
            "skill_id": skill_id,
            "path": path,
            "sha256": sorted(digests),
            "start_sha256": started.get((skill_id, path)),
            "changed": len(digests) > 1 or (started.get((skill_id, path)) not in (None, *digests)),
        }
        for (skill_id, path), digests in sorted(seen.items())
    ]


_PROVENANCE_KEYS = (
    "search_answer_profile_id",
    "search_answer_profile_config_sha256",
    "search_answer_profile_updated_at",
    "prompt_version",
)


def observed_rag_targets(steps: Iterable[Any]) -> dict[str, Any]:
    """RAG のツールの結果が契約で返した、回答の provenance と根拠の chunk_set の ID。"""
    provenances: list[dict[str, Any]] = []
    chunk_sets: set[str] = set()
    for step in steps:
        result = getattr(step, "tool_result", None)
        if result is None or not result.success or not isinstance(result.output, dict):
            continue
        provenance = result.output.get("provenance")
        if isinstance(provenance, dict):
            item = {key: provenance.get(key) for key in _PROVENANCE_KEYS}
            if any(item.values()) and item not in provenances:
                provenances.append(item)
        evidence = result.output.get("evidence")
        for entry in evidence if isinstance(evidence, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("chunk_set_id"), str):
                chunk_sets.add(entry["chunk_set_id"])
    return {"provenance": provenances, "chunk_set_ids": sorted(chunk_sets)}
