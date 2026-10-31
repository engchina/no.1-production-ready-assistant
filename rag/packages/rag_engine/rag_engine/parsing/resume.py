"""保存済みDocling基礎解析からVisionと解析成果物保存を再開するCLI。"""

from __future__ import annotations

import argparse
import json
import re

from rag_engine.config import get_settings
from rag_engine.parsing.analysis import analyze_pdf
from rag_engine.parsing.checkpoints import load_base_checkpoint


def main() -> None:
    """run IDとcheckpoint内の元条件を使用する。chunking・embeddingは実行しない。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="基礎checkpointを持つ16桁のrun ID")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{16}", args.run_id):
        parser.error("run_idは16桁の16進数で指定してください")
    settings = get_settings()
    checkpoint = load_base_checkpoint(settings.output_dir / args.run_id / "base_checkpoint.json")
    result = analyze_pdf(**checkpoint["request"], settings=settings, resume_run_id=args.run_id)
    print(result.json_path)
    progress = settings.output_dir / args.run_id / "docling/vision/progress.json"
    if checkpoint["request"]["use_docling_vision"] and (
        not progress.is_file() or json.loads(progress.read_text(encoding="utf-8"))["phase"] != "completed"
    ):
        raise SystemExit(1)
    if any(not status.available for status in result.statuses) or any(
        record.raw.get("vision_status") == "failed" for record in result.records
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
