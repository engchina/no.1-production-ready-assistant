"""既存の文書の分類の表記を正規化する運用 CLI(#547)。

保存・検索の絞り込みと同じ `normalize_category_value`(NFKC・前後の空白・連続する空白)を、
保存済みの `rag_documents.classification` に当てる。番号の接頭辞(`10_`)は付け替えない
(絞り込みは接頭辞を除いた名前で比べるため、付け替えなくても一致する)。

既定は dry-run(件数だけ)。Oracle のバックアップを取ってから `--apply` で上書きする。
冪等で、原文の値は出力しない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from pydantic import ValidationError

from app.clients.oracle import OracleClient
from app.rag.request_context import with_system_access
from app.schemas.document import DocumentClassification


@dataclass
class ClassificationNormalizationCounts:
    """値を含まない実行集計。"""

    scanned: int = 0
    updated: int = 0
    failed: int = 0


def normalized_classification(raw: dict[str, object]) -> dict[str, object] | None:
    """保存済みの分類を、保存 API と同じ規則で正規化した値にする(空なら None)。

    検証できない値(期間の逆転・長すぎる値など)は ValueError(書き換えない)。
    schema に無い key(rag_poc から移した値の `source` など)は消さずに残す。
    """
    if not raw:
        raise ValueError("分類が空か、JSON として読めません。")
    try:
        classification = DocumentClassification.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("分類を検証できません。") from exc
    extra = {
        key: value for key, value in raw.items() if key not in DocumentClassification.model_fields
    }
    if classification.is_empty() and not extra:
        return None
    return {**extra, **classification.model_dump(mode="json", exclude_none=True)}


async def normalize_document_classifications(
    *,
    apply: bool,
    batch_size: int = 200,
    oracle: OracleClient | None = None,
) -> ClassificationNormalizationCounts:
    """全 tenant の文書の分類を走査し、表記が変わる行を数える(apply なら上書き)。"""
    client = oracle or OracleClient()
    counts = ClassificationNormalizationCounts()
    after: str | None = None
    while True:
        rows = await client.list_document_classifications_for_normalization(
            limit=batch_size, after_document_id=after
        )
        if not rows:
            break
        for document_id, raw in rows:
            counts.scanned += 1
            current = raw if isinstance(raw, dict) else {}
            try:
                normalized = normalized_classification(current)
            except ValueError:
                counts.failed += 1
                continue
            if normalized == current:
                continue
            counts.updated += 1
            if apply:
                await client.update_document_classification_for_normalization(
                    document_id, normalized
                )
        after = rows[-1][0]
    return counts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="既存の文書の分類の表記を正規化します。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="変更件数だけを確認します(既定)。")
    mode.add_argument(
        "--apply", action="store_true", help="正規化した分類を Oracle へ上書きします。"
    )
    parser.add_argument("--batch-size", type=int, default=200, choices=range(1, 1001))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    counts = asyncio.run(
        normalize_document_classifications(apply=bool(args.apply), batch_size=args.batch_size)
    )
    payload = {"mode": "apply" if args.apply else "dry-run", **asdict(counts)}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("\n".join(f"{key}: {value}" for key, value in payload.items()))
    return 0 if counts.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
