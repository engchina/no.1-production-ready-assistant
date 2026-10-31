"""保存済みの回答 JSON の引用証拠キーを移行する。実行時の旧名の別名は設けない。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


def migrate_answer_payload(value: Any) -> Any:
    """引用リストとその ID だけを改名し、画像メタデータと並び順を保つ。"""
    if isinstance(value, list):
        return [migrate_answer_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    for old, new in (("used_images", "cited_evidence"), ("used_image_ids", "cited_evidence_ids")):
        if old not in result:
            continue
        if new in result:
            raise ValueError("引用証拠の新旧キーが重複しています。重複を解消してから移行してください。")
        result[new] = result.pop(old)
    entries = result.get("cited_evidence")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or "image_id" not in entry:
                continue
            if "evidence_id" in entry:
                raise ValueError("引用証拠の新旧 ID が重複しています。重複を解消してから移行してください。")
            entry["evidence_id"] = entry.pop("image_id")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="保存済み回答 JSON の引用証拠キーを移行します。")
    parser.add_argument("path", type=Path, help="回答 JSON ファイル、または回答 artifact のディレクトリ")
    parser.add_argument("--apply", action="store_true", help="確認した変更を保存します（省略時は件数だけ確認）。")
    args = parser.parse_args(argv)
    if not args.path.exists():
        parser.error("対象の回答 JSON が見つかりません。")
    paths = sorted(args.path.rglob("*.json")) if args.path.is_dir() else [args.path]
    scanned = changed = 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        before = json.loads(path.read_text(encoding="utf-8"))
        after = migrate_answer_payload(before)
        scanned += 1
        if after == before:
            continue
        changed += 1
        if args.apply:
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                    stream.write(json.dumps(after, ensure_ascii=False, indent=2) + "\n")
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    print(json.dumps({"scanned": scanned, "changed": changed, "applied": args.apply}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
