"""PDF または画像を解析し、ビューア用の実行成果物を保存する。"""

from __future__ import annotations

import hashlib
import re
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import rag_engine.resources.model_pool as model_pool
from rag_engine.adapters.parsers import ENGINE_LABELS, ENGINE_ORDER, build_adapters
from rag_engine.adapters.parsers.base import AnalysisContext
from rag_engine.config import Settings
from rag_engine.knowledge.classification import classification_from_metadata
from rag_engine.knowledge.document_metadata import normalize_document_metadata
from rag_engine.models.layout import AnalysisRun, EngineStatus, LayoutRecord, PageImage, write_json
from rag_engine.parsing.checkpoints import CheckpointError, checkpoint_lock, file_digest, load_base_checkpoint, save_base_checkpoint
from rag_engine.parsing.pages import parse_page_range
from rag_engine.parsing.parse_inputs import RestoredParseInput, file_sha256, load_parse_inputs, save_parse_input
from rag_engine.parsing.rendering import get_source_page_count, prepare_source_for_analysis, source_frame_warnings
from rag_engine.parsing.visual_artifacts import persist_semantic_visual_crops
from rag_engine.resources.jsonl import write_jsonl


def _installed_parser_version(engine_id: str) -> str | None:
    """checkpoint の同一性判定に使う、ローカル parser の実際の配布版。未導入・外部 API は None。"""
    if engine_id != "docling":
        return None
    try:
        return version("docling")
    except PackageNotFoundError:
        return None


def create_run_id(
    pdf_path: str | Path,
    page_range: str,
    engine_ids: list[str],
    *,
    source_sha256: str = "",
) -> str:
    """入力ファイル、ページ指定、解析エンジンから一意な実行 ID を作ります。

    source_sha256 は計算済みの入力 SHA-256。省略時はファイルをストリームで読んで計算する
    （大きな PDF 全体をメモリへ載せない）。
    """
    digest = hashlib.sha256()
    digest.update((source_sha256 or file_sha256(pdf_path)).encode("ascii"))
    digest.update(page_range.encode("utf-8"))
    digest.update(",".join(engine_ids).encode("utf-8"))
    digest.update(str(time.time_ns()).encode("ascii"))
    return digest.hexdigest()[:16]


def analyze_pdf(
    pdf_path: str | Path,
    page_range: str,
    engine_ids: list[str],
    settings: Settings,
    min_confidence: float = 0.0,
    dpi: int | None = None,
    use_docling_vision: bool = False,
    parse_entire_file: bool = False,
    classification: Any = None,
    document_metadata: dict[str, Any] | None = None,
    resume_run_id: str | None = None,
) -> AnalysisRun:
    """PDF/画像を解析し、検証済み文書情報と観測品質を結果 JSON に保存する。

    document_metadata は任意の文書ID・版・日付・出典情報。不正値はエンジン実行や
    成果物作成前に ValueError とする。未知の値は推測しない。
    resume_run_idはDocling単独runの明示再開。入力・解析設定が一致する基礎checkpointを
    必須とし、OCRを再実行しない。Vision設定変更時は一致する項目だけを再利用する。
    保存失敗・checkpoint不一致・同じrunの同時処理はCheckpointErrorを送出する。
    再開時は元ファイルが無くても、run 内の検証済み source PDF・ページ画像で続行する。
    """
    source_path = Path(pdf_path)
    # 元ファイルは Gradio の一時ファイル等で、再開までに消えていることがある。
    source_available = source_path.exists()
    if not source_available and resume_run_id is None:
        raise FileNotFoundError("PDF / 画像ファイルが見つかりません。")
    document_info = normalize_document_metadata(document_metadata, source_file_name=source_path.name)
    adapters = build_adapters(settings)
    selected = [engine for engine in ENGINE_ORDER if engine in engine_ids and engine in adapters]
    if not selected:
        raise ValueError("利用するエンジンを 1 つ以上選択してください。")
    document_classification = classification_from_metadata(classification).to_metadata()

    if resume_run_id is not None and (selected != ["docling"] or not re.fullmatch(r"[0-9a-f]{16}", resume_run_id)):
        raise ValueError("再開は16桁run IDを持つDocling単独解析だけに対応します。")
    count_source = source_path
    if not source_available:
        # run 内の source.pdf は元 PDF のコピー、または画像から作った 1 ページ PDF で、ページ数は元と一致する。
        count_source = settings.output_dir / str(resume_run_id) / "source.pdf"
        if not count_source.is_file():
            raise CheckpointError("元ファイルと保存済みsource PDFのどちらもありません。")
    page_count = get_source_page_count(count_source)
    pages_to_run = (
        list(range(1, page_count + 1)) if parse_entire_file else parse_page_range(page_range, page_count, settings.max_default_pages)
    )
    effective_dpi = dpi or settings.render_dpi
    # 入力の SHA-256 は run ID・checkpoint identity・parse input で共有し、ファイルを 1 回だけ読む。
    source_digest = file_sha256(source_path) if source_available else ""
    run_id = resume_run_id or create_run_id(
        source_path,
        ",".join(str(page) for page in pages_to_run),
        selected,
        source_sha256=source_digest,
    )
    run_dir = settings.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with checkpoint_lock(run_dir):
        checkpoint = load_base_checkpoint(run_dir / "base_checkpoint.json") if resume_run_id else None
        identity = None
        # 元ファイルが無い再開では保存済みの値を使う。入力の同一性は checkpoint の checksum と
        # run 内 source PDF・ページ画像の hash 照合で確認する。
        if not source_available:
            source_digest = str(checkpoint["identity"].get("source_sha256") or "")
        if selected == ["docling"]:
            try:
                slim_version = version("docling-slim")
            except PackageNotFoundError:
                slim_version = None
            identity = {
                "source_sha256": source_digest,
                "source_name": source_path.name,
                "pages": pages_to_run,
                "dpi": effective_dpi,
                "min_confidence": min_confidence,
                "parse_entire_file": parse_entire_file,
                "classification": document_classification,
                "document_metadata": document_info,
                "parser_version": _installed_parser_version("docling"),
                "slim_version": slim_version,
                "checkpoint_version": 1,
                "parser_settings": {
                    name: getattr(settings, name)
                    for name in (
                        "docling_device",
                        "docling_num_threads",
                        "docling_do_ocr",
                        "docling_do_table_structure",
                        "docling_keep_picture_child_text",
                    )
                },
            }
        if checkpoint is not None:
            if checkpoint["identity"] != identity:
                raise CheckpointError("入力または解析設定がcheckpointと一致しません。新規解析を実行してください。")
            pages = [PageImage(**page) for page in checkpoint["pages"]]
            hashes = checkpoint["page_hashes"]
            for page in pages:
                image = _resumed_path(page.image_path, run_dir / "pages" / Path(page.image_path).name)
                if not image.is_file() or hashes.get(page.image_path) != file_digest(image):
                    raise CheckpointError(f"解析ページ画像が欠落または変更されています: {image}")
                page.image_path = str(image)
            run_pdf_path = str(_resumed_path(checkpoint["pdf_path"], run_dir / "source.pdf"))
            if not Path(run_pdf_path).is_file() or file_sha256(Path(run_pdf_path)) != checkpoint["pdf_sha256"]:
                raise CheckpointError("保存済みsource PDFが欠落または変更されています。")
        else:
            run_pdf_path, pages = prepare_source_for_analysis(
                source_path,
                pages_to_run,
                run_dir,
                effective_dpi,
            )
            for page in pages:
                page.image_url = f"/artifacts/{run_id}/pages/{Path(page.image_path).name}"

        context = AnalysisContext(
            pdf_path=Path(run_pdf_path),
            run_dir=run_dir,
            pages=pages,
            settings=settings,
            min_confidence=min_confidence,
        )
        records: list[LayoutRecord] = []
        statuses: list[EngineStatus] = []
        for engine_id in selected:
            adapter = adapters[engine_id]
            availability = adapter.availability()
            if not availability.available:
                statuses.append(
                    EngineStatus(
                        engine=engine_id,
                        label=ENGINE_LABELS[engine_id],
                        available=False,
                        message=availability.message,
                        count=0,
                        use_docling_vision=engine_id == "docling" and bool(use_docling_vision),
                    )
                )
                continue
            started = time.monotonic()
            try:
                engine_records = (
                    [LayoutRecord(**record) for record in checkpoint["records"]] if checkpoint is not None else adapter.analyze(context)
                )
            except Exception as exc:
                statuses.append(
                    EngineStatus(
                        engine=engine_id,
                        label=ENGINE_LABELS[engine_id],
                        available=False,
                        message=f"解析に失敗しました: {exc}",
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        count=0,
                        use_docling_vision=engine_id == "docling" and bool(use_docling_vision),
                    )
                )
                continue
            persist_semantic_visual_crops(engine_records, pages, run_dir=run_dir)
            if selected == ["docling"] and checkpoint is None:
                save_base_checkpoint(
                    run_dir / "base_checkpoint.json",
                    identity=identity,
                    request={
                        "pdf_path": str(source_path.resolve()),
                        "page_range": page_range,
                        "engine_ids": selected,
                        "min_confidence": min_confidence,
                        "dpi": effective_dpi,
                        "use_docling_vision": use_docling_vision,
                        "parse_entire_file": parse_entire_file,
                        "classification": document_classification,
                        "document_metadata": document_info,
                    },
                    pages=pages,
                    records=engine_records,
                    pdf_path=str(Path(run_pdf_path).resolve()),
                )
            message = "解析が完了しました。"
            if engine_id == "docling" and use_docling_vision:
                try:
                    from rag_engine.parsing.picture_descriptions import describe_docling_pictures

                    vision = describe_docling_pictures(
                        engine_records,
                        pages,
                        run_dir=run_dir,
                        pdf_name=source_path.name,
                        settings=settings,
                        pdf_path=Path(run_pdf_path),
                    )
                    message = f"解析が完了しました。 Vision 説明: 成功 {vision.succeeded}/{vision.targets} 件 / 失敗 {vision.failed} 件。"
                    if vision.discovery_failed:
                        message += " Table画像検出が失敗したため対象総数は未確定です。"
                except CheckpointError:
                    raise
                except Exception as exc:
                    message = f"解析が完了しました。Vision 説明の準備に失敗しました: {exc}"
            saved_input_path: str | None = None
            if parse_entire_file:
                try:
                    saved_path = save_parse_input(
                        output_dir=settings.output_dir,
                        source_path=source_path,
                        source_sha256=source_digest,
                        page_count=page_count,
                        engine_id=engine_id,
                        engine_label=ENGINE_LABELS[engine_id],
                        pages=pages,
                        records=engine_records,
                        dpi=effective_dpi,
                        min_confidence=min_confidence,
                        use_docling_vision=engine_id == "docling" and use_docling_vision,
                        classification=document_classification,
                        document_metadata=document_info,
                    )
                except Exception as exc:
                    message += f" 分割用入力の保存に失敗しました。以前の保存結果は変更されていません: {exc}"
                else:
                    saved_input_path = str(saved_path)
                    message += " 分割用入力を保存しました。"
            records.extend(engine_records)
            statuses.append(
                EngineStatus(
                    engine=engine_id,
                    label=ENGINE_LABELS[engine_id],
                    available=True,
                    message=message,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    count=len(engine_records),
                    saved_input_path=saved_input_path,
                    use_docling_vision=engine_id == "docling" and bool(use_docling_vision),
                )
            )

        # モデルは常駐させたまま、解放済みの VRAM だけ返す（失敗した解析の作業メモリを他プロセスへ渡すため）
        model_pool.trim_cuda_cache()

        json_path = run_dir / "results.json"
        jsonl_path = run_dir / "results.jsonl"
        viewer_data_path = run_dir / "viewer-data.json"
        run = AnalysisRun(
            run_id=run_id,
            pdf_name=source_path.name,
            pdf_path=run_pdf_path,
            pages=pages,
            records=records,
            statuses=statuses,
            output_dir=str(run_dir),
            json_path=str(json_path),
            jsonl_path=str(jsonl_path),
            viewer_data_path=str(viewer_data_path),
            source_page_count=page_count,
            warnings=source_frame_warnings(source_path) if source_available else [],
            classification=document_classification,
            document_metadata=document_info,
        )
        write_json(json_path, run.to_dict())
        write_jsonl(jsonl_path, records, pages)
        write_json(viewer_data_path, run.viewer_payload())
        return run


def _resumed_path(stored: str, fallback: Path) -> Path:
    """保存時の path が無効（相対 path と作業ディレクトリの変更、出力先の移動）なら run 内の既定位置を返す。"""
    path = Path(stored)
    return path if path.is_file() else fallback


def preview_pdf(
    pdf_path: str | Path,
    settings: Settings,
    dpi: int | None = None,
) -> AnalysisRun:
    """解析を実行せず全ページ画像と復元済み parse input を viewer payload にします。"""
    source_path = Path(pdf_path)
    if not source_path.exists():
        raise FileNotFoundError("PDF / 画像ファイルが見つかりません。")

    page_count = get_source_page_count(source_path)
    page_numbers = list(range(1, page_count + 1))
    if not page_numbers:
        raise ValueError("ファイルにページがありません。")

    run_id = create_run_id(source_path, "preview-all-pages", [])
    run_dir = settings.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_pdf_path, pages = prepare_source_for_analysis(
        source_path,
        page_numbers,
        run_dir,
        dpi or settings.render_dpi,
    )
    for page in pages:
        page.image_url = f"/artifacts/{run_id}/pages/{Path(page.image_path).name}"

    restored = load_parse_inputs(
        output_dir=settings.output_dir,
        source_path=source_path,
        page_count=page_count,
        pages=pages,
        engine_order=ENGINE_ORDER,
    )
    # 分類と文書情報は同じ基準（保存日時が新しい parse input）から復元する。
    latest_first = sorted(restored.inputs, key=lambda item: item.completed_at, reverse=True)
    document_classification = _restored_classification(latest_first)
    document_info = normalize_document_metadata(
        next((item.document_metadata for item in latest_first if item.document_metadata), None),
        source_file_name=source_path.name,
    )
    records = [record for restored_input in restored.inputs for record in restored_input.records]
    # 保存済み record の相対 asset path は旧 run を指すため、現在の preview page から作り直す。
    persist_semantic_visual_crops(records, pages, run_dir=run_dir)
    statuses = [
        EngineStatus(
            engine=restored_input.engine_id,
            label=ENGINE_LABELS[restored_input.engine_id],
            available=True,
            message=(f"保存済みのファイル全体の解析結果を読み込みました。 保存日時: {restored_input.completed_at}"),
            count=len(restored_input.records),
            saved_input_path=str(restored_input.path),
            use_docling_vision=restored_input.use_docling_vision,
        )
        for restored_input in restored.inputs
    ]

    json_path = run_dir / "results.json"
    jsonl_path = run_dir / "results.jsonl"
    viewer_data_path = run_dir / "viewer-data.json"
    run = AnalysisRun(
        run_id=run_id,
        pdf_name=source_path.name,
        pdf_path=run_pdf_path,
        pages=pages,
        records=records,
        statuses=statuses,
        output_dir=str(run_dir),
        json_path=str(json_path),
        jsonl_path=str(jsonl_path),
        viewer_data_path=str(viewer_data_path),
        source_page_count=page_count,
        warnings=[*restored.warnings, *source_frame_warnings(source_path)],
        classification=document_classification,
        document_metadata=document_info,
    )
    write_json(json_path, run.to_dict())
    write_jsonl(jsonl_path, records, pages)
    write_json(viewer_data_path, run.viewer_payload())
    return run


def _restored_classification(restored_inputs: list[RestoredParseInput]) -> dict[str, Any]:
    for restored_input in restored_inputs:
        raw = restored_input.classification
        if isinstance(raw, dict):
            return classification_from_metadata(raw).to_metadata()
    return classification_from_metadata(None).to_metadata()


def _page_summary(run: AnalysisRun) -> str:
    analyzed = ", ".join(str(page.page) for page in run.pages)
    return f"{analyzed} ページ目 / 全 {run.source_page_count} ページ"


def summarize_run(run: AnalysisRun) -> str:
    """解析実行結果を Gradio 表示用の Markdown に整形します。"""
    lines = ["### 解析結果", f"- Run ID: `{run.run_id}`", f"- ファイル: `{run.pdf_name}`", f"- 解析ページ: {_page_summary(run)}", ""]
    for status in run.statuses:
        state = "有効" if status.available else "無効"
        elapsed = f" / {status.elapsed_seconds:.3f}s" if status.elapsed_seconds is not None else ""
        lines.append(f"- {status.label}: {state} / {status.count} 件{elapsed} / {status.message}")
    return "\n".join(lines)


def summarize_preview(run: AnalysisRun) -> str:
    """プレビュー実行結果と復元 warning を Markdown に整形します。"""
    return "\n".join(
        [
            "### ファイルプレビュー",
            f"- Run ID: `{run.run_id}`",
            f"- ファイル: `{run.pdf_name}`",
            f"- ページ数: {run.source_page_count}",
            "- 解析はまだ実行されていません。",
        ]
    )
