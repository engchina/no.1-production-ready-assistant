"""製品の `backend/.env` を、共通 `platform/.env` と製品接頭辞の `backend/.env` へ移す（#211）。

旧名の環境変数は読まれなくなったため、既存環境の更新で1回だけ実行する。

    uv run --project platform/packages/backend_core \\
        python platform/scripts/migrate_env_to_platform.py --product nl2sql          # 確認だけ
    uv run --project platform/packages/backend_core \\
        python platform/scripts/migrate_env_to_platform.py --product nl2sql --apply  # 書き換え

- 共通の変数（`PLATFORM_SETTING_FIELDS`）は `PLATFORM_*` に改名して共通 `.env` へ移す。
  共通 `.env` に同じ変数が別の値で既にあるときは共通 `.env` の値を残し、競合として表示する。
- それ以外は製品の接頭辞を付けて製品の `.env` に残す（コメントと順序は保つ）。
- 製品の `model-settings.json` は、共通側になければ共通 `.env` と同じ場所へコピーする。
- `--apply` では書き換える前に `<file>.bak-211` を作る。
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from pr_backend_core.config import (
    PLATFORM_SETTING_FIELDS,
    platform_env_name,
    product_env_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIXES = {"rag": "RAG_", "nl2sql": "NL2SQL_", "agent": "AGENT_"}
# 属性名から機械的に決まらない旧名（製品の旧 alias・廃止した旧名）。値が None のものは移さない。
EXPLICIT_RENAMES: dict[str, dict[str, str | None]] = {
    "rag": {
        "HF_TOKEN": "RAG_HUGGINGFACE_TOKEN",
        "HF_ENDPOINT": "RAG_HUGGINGFACE_ENDPOINT",
    },
    "nl2sql": {
        "APP_ADMIN_USERNAME": None,
        "APP_ADMIN_PASSWORD": None,
        "OCI_PROFILE": None,
    },
    "agent": {
        name: None
        for name in (
            "ENTERPRISE_AI_ENDPOINT",
            "ENTERPRISE_AI_PROJECT_OCID",
            "ENTERPRISE_AI_API_KEY",
            "ENTERPRISE_AI_DEFAULT_MODEL_ID",
            "ENTERPRISE_AI_API_PATH",
            "ENTERPRISE_AI_VLM_INPUT_MODE",
            "ENTERPRISE_AI_TEXT_PAYLOAD_TEMPLATE",
            "ENTERPRISE_AI_VISION_PAYLOAD_TEMPLATE",
            "ENTERPRISE_AI_TEXT_RESPONSE_PATH",
            "ENTERPRISE_AI_VISION_RESPONSE_PATH",
            "ENTERPRISE_AI_TIMEOUT_SECONDS",
            "ENTERPRISE_AI_MAX_RETRIES",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIM",
            "RERANK_MODEL",
            "ORACLE_WALLET_UPLOADED",
            "ORACLE_REGION",
            "ADB_OCID",
            "OCI_KEY_FILE_EXISTS",
            "OCI_CONFIG_FILE_EXISTS",
        )
    },
}
# 接頭辞で決まる改名（Agent の binding ごとの MCP token）。
PREFIX_RENAMES: dict[str, dict[str, str]] = {
    "agent": {"CONTROL_PLANE_MCP_TOKEN_": "AGENT_BINDING_MCP_TOKEN_"},
}
ASSIGNMENT_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")


def new_name(product: str, key: str) -> str | None:
    upper = key.upper()
    explicit = EXPLICIT_RENAMES[product]
    if upper in explicit:
        return explicit[upper]
    for old_prefix, new_prefix in PREFIX_RENAMES.get(product, {}).items():
        if upper.startswith(old_prefix):
            return new_prefix + upper.removeprefix(old_prefix)
    prefix = PREFIXES[product]
    if upper.startswith(("PLATFORM_", prefix)):
        return upper
    field = key.lower()
    if field in PLATFORM_SETTING_FIELDS:
        return platform_env_name(field)
    return product_env_name(prefix, field)


def _existing_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = ASSIGNMENT_RE.match(line)
            if match:
                values[match.group(2)] = match.group(4)
    return values


def migrate(product: str, backend_env: Path, platform_env: Path) -> tuple[str, str, list[str]]:
    """(新しい製品の .env, 新しい共通 .env, 表示するメッセージ) を返す。"""
    messages: list[str] = []
    platform_values = _existing_values(platform_env)
    product_lines: list[str] = []
    platform_additions: list[str] = []
    for line in backend_env.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT_RE.match(line)
        if not match:
            product_lines.append(line)
            continue
        lead, key, equals, value = match.groups()
        renamed = new_name(product, key)
        if renamed is None:
            messages.append(f"削除: {key}（廃止した旧名）")
            continue
        if not renamed.startswith("PLATFORM_"):
            if renamed != key:
                messages.append(f"改名: {key} → {renamed}")
            product_lines.append(f"{lead}{renamed}{equals}{value}")
            continue
        current = platform_values.get(renamed)
        if current is None:
            platform_values[renamed] = value
            platform_additions.append(f"{renamed}={value}")
            messages.append(f"共通へ移動: {key} → {renamed}")
        elif current.strip() != value.strip():
            messages.append(f"競合: {renamed} は共通 .env の値を残す（{key} の値は捨てる）")
        else:
            messages.append(f"共通と同じ値: {key}（製品から削除）")
    platform_text = platform_env.read_text(encoding="utf-8") if platform_env.is_file() else ""
    if platform_additions:
        if platform_text and not platform_text.endswith("\n"):
            platform_text += "\n"
        if platform_text:
            platform_text += "\n"
        platform_text += f"# {product} の backend/.env から移動（#211）\n"
        platform_text += "\n".join(platform_additions) + "\n"
    return "\n".join(product_lines).rstrip() + "\n", platform_text, messages


def _write(path: Path, content: str) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak-211"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--product", choices=sorted(PREFIXES), required=True)
    parser.add_argument("--backend-env", type=Path)
    parser.add_argument("--platform-env", type=Path, default=REPO_ROOT / "platform" / ".env")
    parser.add_argument("--apply", action="store_true", help="確認だけでなく書き換える")
    args = parser.parse_args(argv)
    backend_env = args.backend_env or REPO_ROOT / args.product / "backend" / ".env"
    if not backend_env.is_file():
        parser.error(f"{backend_env} がありません。")
    product_text, platform_text, messages = migrate(args.product, backend_env, args.platform_env)
    for message in messages:
        print(message)
    model_settings = backend_env.parent / "model-settings.json"
    shared_model_settings = args.platform_env.parent / "model-settings.json"
    copy_model_settings = model_settings.is_file() and not shared_model_settings.exists()
    if copy_model_settings:
        print(f"コピー: {model_settings} → {shared_model_settings}")
    if not args.apply:
        print("確認のみ（--apply で書き換える）")
        return 0
    _write(backend_env, product_text)
    _write(args.platform_env, platform_text)
    if copy_model_settings:
        shutil.copy2(model_settings, shared_model_settings)
    print(f"更新しました: {backend_env} / {args.platform_env}（元のファイルは *.bak-211）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
