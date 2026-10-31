"""Parser adapter compatibility matrix CLI。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pr_backend_core.logging import configure_cli_logging

from app.config import get_settings
from app.rag.parser_adapter_contract import (
    parser_adapter_contract_artifact_payload,
    parser_adapter_fixture_root_from_manifest,
    parser_adapter_fixture_specs_from_manifest,
    parser_service_probe_settings,
    run_parser_adapter_compatibility_matrix,
    strict_parser_adapter_settings,
)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    configure_cli_logging("rag")
    parser = argparse.ArgumentParser(
        prog="rag-parser-adapter-contract",
        description=(
            "Docling / Unstructured adapter が本プロジェクト schema へ "
            "remap できるかを非機密 JSON artifact として出力します。"
        ),
    )
    parser.add_argument("--output", type=Path, help="結果 JSON の保存先。未指定なら stdout。")
    parser.add_argument(
        "--fixture-root",
        type=Path,
        help="compatibility smoke に使う fixture root。",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="staging golden manifest の fixture_root/cases を smoke 対象にする。",
    )
    parser.add_argument(
        "--backend",
        action="append",
        choices=["docling", "unstructured"],
        help="対象 backend。複数指定可。未指定なら全 adapter。",
    )
    parser.add_argument(
        "--source-kind",
        action="append",
        choices=["pdf", "image", "office", "html", "email", "text"],
        help="対象 source kind。複数指定可。未指定なら主要 source kind。",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "代表外部 adapter を明示選択し、parser サービス未達 / schema remap "
            "failure を gate 失敗にします。staging 昇格 smoke 用。"
            "未指定時は、選択済み adapter の parser サービスが見つからなくても記録だけにします。"
        ),
    )
    args = parser.parse_args(argv)
    try:
        # parse は parser サービスへ HTTP 委譲するため、可用性も同じサービスの /health で
        # 判定する(backend の venv の package では判定しない。#343)。
        settings = parser_service_probe_settings(get_settings())
        if args.strict:
            settings = strict_parser_adapter_settings(settings)
        manifest = _load_manifest(args.manifest) if args.manifest is not None else None
        fixture_specs = (
            parser_adapter_fixture_specs_from_manifest(
                manifest,
                require_declared_schema_remap=args.strict,
            )
            if manifest is not None
            else None
        )
        fixture_root = args.fixture_root
        if fixture_root is None and manifest is not None:
            fixture_root = parser_adapter_fixture_root_from_manifest(
                manifest,
                manifest_path=args.manifest,
            )
        matrix = run_parser_adapter_compatibility_matrix(
            settings,
            fixture_root=fixture_root,
            source_kinds=args.source_kind,
            fixture_specs=fixture_specs,
            backends=args.backend,
            require_routed=bool(args.strict and args.backend),
            require_backend_evidence=bool(args.strict),
            require_selected_adapter_runtime=bool(args.strict),
        )
        payload = parser_adapter_contract_artifact_payload(matrix)
    except Exception as exc:
        payload = {"passed": False, "error_type": type(exc).__name__}
        _write_payload(payload, args.output)
        return 2
    _write_payload(payload, args.output)
    return 0 if matrix.passed else 1


def _load_manifest(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be JSON object")
    return raw


def _write_payload(payload: object, output_path: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output_path is None:
        print(text)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
