"""ファイル準備 csv_to_json / excel_to_json の改名に合わせて、global の設定のファイルを移す(#2457)。

DB の保存値(文書レシピ・取込ジョブ・その記録)は system schema の migration
(``20261019_003_structured_records_jsonl_profiles``)が移す。ここは RAG の ``backend/.env`` と
``parser-settings.json`` を移す。

- 前処理のサービスの URL の変数 ``RAG_PREPROCESS_{CSV,EXCEL}_TO_JSON_SERVICE_URL`` を
  ``*_TO_JSONL_SERVICE_URL`` に改名する(旧名は読まない)。
- ``RAG_PREPROCESS_PROFILE`` の ``csv_to_json`` / ``excel_to_json`` を ``*_jsonl`` にする。
- global のファイル準備が ``*_jsonl`` で、global の解析エンジンが Docling / Unstructured(未指定は
  Docling)なら構造化レコード JSONL にする(``parser-settings.json`` があればそちら、無ければ
  ``backend/.env``)。分割の方式は書き換えない。
- ``RAG_CHUNKING_STRATEGY=fixed_delimiter`` で ``RAG_CHUNK_DELIMITER`` が無ければ、旧い既定の
  ``\\n\\n`` を明示する(既定の分割符を記録の境界のマーカーに変えても、分割の結果を変えない)。

旧い値のままでは Settings の検証で起動できないため、Settings を読まずにファイルを直接読む。
``--apply`` が無ければ書き換えの予定だけを出す。何度実行しても同じ結果になる(冪等)。

使い方: ``uv run python -m app.rag.structured_records_settings_migration [--apply]``
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from pr_system_settings.env_file import write_env_values
from rag_parser_core.structured_records import STRUCTURED_RECORDS_JSONL_BACKEND

PREPROCESS_PROFILE_RENAMES: dict[str, str] = {
    "csv_to_json": "csv_to_jsonl",
    "excel_to_json": "excel_to_jsonl",
}
SERVICE_URL_ENV_RENAMES: dict[str, str] = {
    "RAG_PREPROCESS_CSV_TO_JSON_SERVICE_URL": "RAG_PREPROCESS_CSV_TO_JSONL_SERVICE_URL",
    "RAG_PREPROCESS_EXCEL_TO_JSON_SERVICE_URL": "RAG_PREPROCESS_EXCEL_TO_JSONL_SERVICE_URL",
}
STRUCTURED_RECORDS_PREPROCESS = frozenset(PREPROCESS_PROFILE_RENAMES.values())
# 構造化レコードの JSONL を受けなくなった解析エンジン(未指定は既定の Docling)。
_RETIRED_STRUCTURED_RECORDS_BACKENDS = frozenset({"docling", "unstructured"})
_DEFAULT_PARSER_BACKEND = "docling"
PREVIOUS_DEFAULT_CHUNK_DELIMITER = "\\n\\n"
_SECTION_COMMENT = "# 構造化レコード JSONL の移行(#2457)"


@dataclass
class SettingsMigrationPlan:
    """書き換えの予定。``env_values`` は ``write_env_values`` の形(None は削除)。"""

    env_values: dict[str, str | None] = field(default_factory=dict)
    parser_settings: dict[str, object] | None = None
    changes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.env_values and self.parser_settings is None


def default_env_file() -> Path:
    """RAG の ``backend/.env``(``RAG_ENV_FILE`` で上書きできる。Settings と同じ解決)。"""
    from app.config import BACKEND_ENV_FILE

    return BACKEND_ENV_FILE


def parser_settings_file(env: Mapping[str, str | None], env_file: Path) -> Path:
    """``RAG_PARSER_SETTINGS_FILE`` を ``backend/.env`` と同じディレクトリ基準で解決する。"""
    value = (env.get("RAG_PARSER_SETTINGS_FILE") or "").strip() or "parser-settings.json"
    path = Path(value).expanduser()
    return path if path.is_absolute() else (env_file.parent / path).resolve()


def plan_settings_migration(
    env_file: Path, parser_file: Path | None = None
) -> SettingsMigrationPlan:
    """``backend/.env`` と ``parser-settings.json`` を読み、書き換えの予定を作る(書き換えない)。"""
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
        profile = renamed

    if profile in STRUCTURED_RECORDS_PREPROCESS:
        _plan_parser_backend(
            plan, env, env_file, parser_file or parser_settings_file(env, env_file)
        )

    strategy = (env.get("RAG_CHUNKING_STRATEGY") or "").strip()
    if strategy == "fixed_delimiter" and not (env.get("RAG_CHUNK_DELIMITER") or "").strip():
        plan.env_values["RAG_CHUNK_DELIMITER"] = PREVIOUS_DEFAULT_CHUNK_DELIMITER
        plan.changes.append(
            f"{env_file}: 固定分割の分割符(既定のまま)を "
            f"{PREVIOUS_DEFAULT_CHUNK_DELIMITER} で明示します。"
        )
    return plan


def _plan_parser_backend(
    plan: SettingsMigrationPlan,
    env: Mapping[str, str | None],
    env_file: Path,
    parser_file: Path,
) -> None:
    """global の解析エンジンが Docling / Unstructured なら構造化レコード JSONL にする予定を足す。"""
    if parser_file.is_file():
        raw = json.loads(parser_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"文書解析の設定ファイルが JSON object ではありません: {parser_file}")
        backend = str(raw.get("adapter_backend") or _DEFAULT_PARSER_BACKEND).strip().casefold()
        if backend in _RETIRED_STRUCTURED_RECORDS_BACKENDS:
            plan.parser_settings = {
                **raw,
                "adapter_backend": STRUCTURED_RECORDS_JSONL_BACKEND,
                "structured_records_jsonl_enabled": True,
            }
            plan.changes.append(
                f"{parser_file}: 解析エンジンを {backend} から "
                f"{STRUCTURED_RECORDS_JSONL_BACKEND} にします。"
            )
        return
    backend = (env.get("RAG_PARSER_ADAPTER_BACKEND") or _DEFAULT_PARSER_BACKEND).strip().casefold()
    if backend in _RETIRED_STRUCTURED_RECORDS_BACKENDS:
        plan.env_values["RAG_PARSER_ADAPTER_BACKEND"] = STRUCTURED_RECORDS_JSONL_BACKEND
        plan.env_values["RAG_PARSER_STRUCTURED_RECORDS_JSONL_ENABLED"] = "true"
        plan.changes.append(
            f"{env_file}: RAG_PARSER_ADAPTER_BACKEND を {backend} から "
            f"{STRUCTURED_RECORDS_JSONL_BACKEND} にします。"
        )


def apply_settings_migration(
    plan: SettingsMigrationPlan, env_file: Path, parser_file: Path
) -> None:
    """予定どおりにファイルを書き換える(``.env`` のコメントと無関係な値は保つ)。"""
    if plan.env_values:
        write_env_values(env_file, plan.env_values, section_comment=_SECTION_COMMENT)
    if plan.parser_settings is not None:
        _write_json_atomic(parser_file, plan.parser_settings)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """``parser-settings.json`` の保存と同じく、flock の下で一時ファイルから置き換える。"""
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        tmp_path = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(tmp_path, mode)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="structured-records-settings-migration",
        description=(
            "ファイル準備 csv_to_json / excel_to_json の改名(#2457)に合わせて、"
            "RAG の backend/.env と parser-settings.json を移します。"
        ),
    )
    parser.add_argument("--env-file", type=Path, help="RAG の backend/.env(既定は Settings と同じ)")
    parser.add_argument("--parser-settings", type=Path, help="parser-settings.json")
    parser.add_argument("--apply", action="store_true", help="書き換える(無ければ予定だけを出す)")
    args = parser.parse_args(argv)
    env_file: Path = args.env_file or default_env_file()
    env = dict(dotenv_values(env_file)) if env_file.is_file() else {}
    parser_file: Path = args.parser_settings or parser_settings_file(env, env_file)
    plan = plan_settings_migration(env_file, parser_file)
    if plan.empty:
        print("移す設定はありません。")
        return 0
    for change in plan.changes:
        print(change)
    if not args.apply:
        print("--apply を付けると書き換えます。")
        return 0
    apply_settings_migration(plan, env_file, parser_file)
    print("書き換えました。backend と取込 worker を再起動してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
