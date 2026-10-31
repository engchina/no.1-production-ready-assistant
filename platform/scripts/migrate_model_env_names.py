"""共通 `platform/.env` の既定モデルの変数を、#499 の新しい名前へ書き換える。

#499 で既定のモデルを「既定のテキストモデル」と「既定の Vision モデル」の 2 つにした。旧名の
環境変数は読まれなくなったため、既存環境の更新で1回だけ実行する。

    uv run --project platform/packages/backend_core \\
        python platform/scripts/migrate_model_env_names.py            # 確認だけ
    uv run --project platform/packages/backend_core \\
        python platform/scripts/migrate_model_env_names.py --apply    # 書き換え

- `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL` → `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL`
- `PLATFORM_OCI_ENTERPRISE_AI_LLM_MODEL` → `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL`
  （`DEFAULT_MODEL` が無いか空のときだけ）
- `PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL` → `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL`

- 旧版はテキストの呼び出しに `DEFAULT_MODEL`（空なら `LLM_MODEL`）を使っていたため、同じ値を
  `DEFAULT_TEXT_MODEL` にする。使われなかった側の行は削除する
- 新名が既にあるときは新名の値を残し、旧名の行を削除する（競合として表示する）
- 画面で保存した `model-settings.json` の旧 key（`default_model_id`）は、backend が読み込むときに
  移すため、このスクリプトでは触らない
- `--apply` では書き換える前に `<file>.bak-499` を作る
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT = "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL"
VISION = "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL"
# 旧名 → 新名。テキストは優先順（前ほど優先）に並べる。
TEXT_SOURCES = ("PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL", "PLATFORM_OCI_ENTERPRISE_AI_LLM_MODEL")
VISION_SOURCES = ("PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL",)
ASSIGNMENT_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")


def _is_blank(value: str) -> bool:
    return value.strip().strip("'\"").strip() == ""


def migrate(text: str) -> tuple[str, list[str]]:
    """(書き換えた `.env` の内容, 表示するメッセージ) を返す。コメントと順序は保つ。"""
    lines = text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        match = ASSIGNMENT_RE.match(line)
        if match:
            values[match.group(2)] = match.group(4)

    messages: list[str] = []
    plan: dict[str, str | None] = {}  # 旧名 → 置き換える新名（None は削除）
    for new, sources in ((TEXT, TEXT_SOURCES), (VISION, VISION_SOURCES)):
        present = [name for name in sources if name in values]
        if not present:
            continue
        chosen = next((name for name in present if not _is_blank(values[name])), present[0])
        if new in values:
            for name in present:
                plan[name] = None
                messages.append(f"競合: {new} が既にあるため {name} を削除（{new} の値を残す）")
            continue
        for name in present:
            plan[name] = new if name == chosen else None
            if name == chosen:
                messages.append(f"改名: {name} → {new}")
            else:
                messages.append(f"削除: {name}（{chosen} の値を {new} に使う）")

    output: list[str] = []
    for line in lines:
        match = ASSIGNMENT_RE.match(line)
        if not match or match.group(2) not in plan:
            output.append(line)
            continue
        lead, key, equals, value = match.groups()
        renamed = plan[key]
        if renamed is not None:
            output.append(f"{lead}{renamed}{equals}{value}")
    content = "\n".join(output)
    if text.endswith("\n") or not text:
        content += "\n"
    return content, messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / "platform" / ".env")
    parser.add_argument("--apply", action="store_true", help="確認だけでなく書き換える")
    args = parser.parse_args(argv)
    env_file: Path = args.env_file
    if not env_file.is_file():
        parser.error(f"{env_file} がありません。")
    original = env_file.read_text(encoding="utf-8")
    content, messages = migrate(original)
    for message in messages:
        print(message)
    if content == original:
        print("変更はありません。")
        return 0
    if not args.apply:
        print("確認のみ（--apply で書き換える）")
        return 0
    shutil.copy2(env_file, env_file.with_name(env_file.name + ".bak-499"))
    env_file.write_text(content, encoding="utf-8")
    env_file.chmod(0o600)
    print(f"更新しました: {env_file}（元のファイルは {env_file.name}.bak-499）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
