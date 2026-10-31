"""ファイル準備 csv_to_json / excel_to_json の改名に合わせて、global の設定のファイルを移す(#2457)。

DB の保存値(文書レシピ・取込ジョブ・その記録)は system schema の migration が移す。ここは RAG の
``backend/.env`` を移す。

- 前処理のサービスの URL の変数 ``RAG_PREPROCESS_{CSV,EXCEL}_TO_JSON_SERVICE_URL`` を
  ``*_TO_JSONL_SERVICE_URL`` に改名する(旧名は読まない)。
- ``RAG_PREPROCESS_PROFILE`` の ``csv_to_json`` / ``excel_to_json`` を ``*_jsonl`` にする。
- 解析エンジンは書き換えない。構造化レコードの JSONL は Docling・Unstructured のサービスが読む
  (#21。以前の版がこの移行で書いた専用の解析エンジンの値は、読み込みで既定のエンジンへ寄る)。
- ``RAG_CHUNKING_STRATEGY=fixed_delimiter`` で ``RAG_CHUNK_DELIMITER`` が無ければ、旧い既定の
  ``\\n\\n`` を明示する(既定の分割符を記録の境界のマーカーに変えても、分割の結果を変えない)。

旧い値のままでは Settings の検証で起動できないため、Settings を読まずにファイルを直接読む。
``--apply`` が無ければ書き換えの予定だけを出す。何度実行しても同じ結果になる(冪等)。

使い方: ``uv run python -m app.rag.structured_records_settings_migration [--apply]``
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from pr_system_settings.env_file import write_env_values

PREPROCESS_PROFILE_RENAMES: dict[str, str] = {
    "csv_to_json": "csv_to_jsonl",
    "excel_to_json": "excel_to_jsonl",
}
SERVICE_URL_ENV_RENAMES: dict[str, str] = {
    "RAG_PREPROCESS_CSV_TO_JSON_SERVICE_URL": "RAG_PREPROCESS_CSV_TO_JSONL_SERVICE_URL",
    "RAG_PREPROCESS_EXCEL_TO_JSON_SERVICE_URL": "RAG_PREPROCESS_EXCEL_TO_JSONL_SERVICE_URL",
}
PREVIOUS_DEFAULT_CHUNK_DELIMITER = "\\n\\n"
_SECTION_COMMENT = "# 構造化レコード JSONL の移行(#2457)"


@dataclass
class SettingsMigrationPlan:
    """書き換えの予定。``env_values`` は ``write_env_values`` の形(None は削除)。"""

    env_values: dict[str, str | None] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.env_values


def default_env_file() -> Path:
    """RAG の ``backend/.env``(``RAG_ENV_FILE`` で上書きできる。Settings と同じ解決)。"""
    from app.config import BACKEND_ENV_FILE

    return BACKEND_ENV_FILE


def plan_settings_migration(env_file: Path) -> SettingsMigrationPlan:
    """``backend/.env`` を読み、書き換えの予定を作る(書き換えない)。"""
    plan = SettingsMigrationPlan()
    env = dict(dotenv_values(env_file)) if env_file.is_file() else {}
    for old, new in SERVICE_URL_ENV_RENAMES.items():
        if old not in env:
            continue
        plan.env_values[old] = None
        if new not in env and env[old] is not None:
            plan.env_values[new] = env[old]
        plan.changes.append(f"{env_file}: {old} を {new} に改名します。")

    profile = (env.get("RAG_PREPROCESS_PROFILE") or "").strip()
    renamed = PREPROCESS_PROFILE_RENAMES.get(profile)
    if renamed is not None:
        plan.env_values["RAG_PREPROCESS_PROFILE"] = renamed
        plan.changes.append(
            f"{env_file}: RAG_PREPROCESS_PROFILE を {profile} から {renamed} にします。"
        )

    strategy = (env.get("RAG_CHUNKING_STRATEGY") or "").strip()
    if strategy == "fixed_delimiter" and not (env.get("RAG_CHUNK_DELIMITER") or "").strip():
        plan.env_values["RAG_CHUNK_DELIMITER"] = PREVIOUS_DEFAULT_CHUNK_DELIMITER
        plan.changes.append(
            f"{env_file}: 固定分割の分割符(既定のまま)を "
            f"{PREVIOUS_DEFAULT_CHUNK_DELIMITER} で明示します。"
        )
    return plan


def apply_settings_migration(plan: SettingsMigrationPlan, env_file: Path) -> None:
    """予定どおりにファイルを書き換える(``.env`` のコメントと無関係な値は保つ)。"""
    if plan.env_values:
        write_env_values(env_file, plan.env_values, section_comment=_SECTION_COMMENT)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="structured-records-settings-migration",
        description=(
            "ファイル準備 csv_to_json / excel_to_json の改名(#2457)に合わせて、"
            "RAG の backend/.env を移します。"
        ),
    )
    parser.add_argument("--env-file", type=Path, help="RAG の backend/.env(既定は Settings と同じ)")
    parser.add_argument("--apply", action="store_true", help="書き換える(無ければ予定だけを出す)")
    args = parser.parse_args(argv)
    env_file: Path = args.env_file or default_env_file()
    plan = plan_settings_migration(env_file)
    if plan.empty:
        print("移す設定はありません。")
        return 0
    for change in plan.changes:
        print(change)
    if not args.apply:
        print("--apply を付けると書き換えます。")
        return 0
    apply_settings_migration(plan, env_file)
    print("書き換えました。backend と取込 worker を再起動してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
