"""旧検査方式を OCI Guardrails の有効状態へ移す一度限りの設定更新。"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from dotenv import dotenv_values

_OLD_KEYS = frozenset(
    {
        "RAG_GUARDRAIL_BACKEND",
        "RAG_GUARDRAILS_ENABLED",
        "RAG_GUARDRAIL_POLICY",
        "RAG_GUARDRAIL_MAX_QUERY_CHARS",
        "RAG_GUARDRAIL_BLOCK_PROMPT_INJECTION",
        "RAG_GUARDRAIL_MASK_SENSITIVE_IDENTIFIERS",
    }
)
_KEYS = ("RAG_GUARDRAILS_INPUT_ENABLED", "RAG_GUARDRAILS_OUTPUT_ENABLED")
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def migrate_env(path: Path) -> bool:
    """既存の明示値を優先し、旧設定だけを取り除く。他の設定は変更しない。"""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    values = dotenv_values(path)
    if not _OLD_KEYS.intersection(values):
        return False
    backend = values.get("RAG_GUARDRAIL_BACKEND")
    if backend not in {None, "local", "oci_guardrails"}:
        raise ValueError("旧検査方式が不明なため、自動で移行できません。")
    lines = [
        line
        for line in text.splitlines(keepends=True)
        if not ((match := _ASSIGNMENT.match(line)) and match.group(1) in _OLD_KEYS)
    ]
    content = "".join(lines)
    enabled = values.get("RAG_GUARDRAILS_ENABLED")
    if enabled is None:
        enabled = str(backend == "oci_guardrails").lower()
    if enabled.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError("旧検査スイッチが不明なため、自動で移行できません。")
    for key in _KEYS:
        if key not in values:
            content = content.rstrip("\n") + f"\n{key}={enabled}\n"
    # 同じディレクトリで置き換え、元のアクセス権を保持する。
    staging = path.with_name(path.name + ".guardrails-migration")
    created = False
    try:
        with staging.open("x", encoding="utf-8") as handle:
            created = True
            os.chmod(staging, path.stat().st_mode & 0o777)
            handle.write(content)
        staging.replace(path)
    finally:
        if created:
            staging.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="OCI Guardrails の有効状態へ設定を移行します。")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    print("updated" if migrate_env(args.env_file) else "unchanged")


if __name__ == "__main__":
    main()
