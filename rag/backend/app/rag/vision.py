"""解析後の図・画像の読み取り(Vision)。解析エンジンに依存しない共通の段(#497)。

文書のレシピの「図・画像を AI で読み取る」(``rag_vision_enabled``)が有効なとき、取込の解析の
直後に、Docling を含む全ての解析エンジンの図を OCI Enterprise AI の既定の Vision モデルで読み取る。

読み取りの規則は rag_engine の ``describe_layout_pictures``(旧 Docling サービスの Vision)を
そのまま使い、二重に実装しない。

- 対象の図の切り出しと、対象をマゼンタの枠で示したページ全体の 2 枚の画像を渡す。
- Picture で覆われていない画像を含む Table も読み取る(PDF の画像 object を調べる)。
- 装飾画像(ロゴ・帯・小さなアイコン)は読み取らない。
- prompt に bbox・周辺の record・表の既存 HTML などの metadata を入れる。

``parser_artifacts["layout_records"]`` の record がある解析エンジン(Docling)は、その record を
そのまま入力にし、結果を record にも書き戻す(親子階層（small-to-big）の分割と画面の
「Vision の読み取り内容」がその record を読む)。
ほかの解析エンジンは、要素と図の asset の bbox をページ画像の px へそろえた LayoutRecord を作る。
bbox の単位は解析エンジンごとに違い、確かでないものは切り出さずに warning を残す
(``_bbox_scale`` の表)。画像の件数の上限は設けない。

1 件ごとの失敗は元の text を保ち、要素の metadata の ``vision_status`` を ``failed`` にして
warning を残す(取込は止めない)。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import mimetypes
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from pr_backend_core.bounded_calls import Attempt, BoundedCaller, BoundedCallFailed
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pydantic import ValidationError
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.models.llm import PictureDescriptionOutput
from rag_engine.parsing.layout_metadata import (
    layout_record_element_metadata,
    layout_record_vision_metadata,
    layout_record_vision_summary,
)
from rag_engine.parsing.picture_text import format_docling_picture_ocr_text
from rag_engine.parsing.rendering import (
    SUPPORTED_SOURCE_FILE_TYPES,
    copy_pdf_to_run,
    get_source_page_count,
    prepare_source_for_analysis,
)
from rag_parser_core.oci_enterprise_ai import sdk_retries_disabled
from rag_pipeline_core.chunking import table_vision_supplement

from app.schemas.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionMetadataValue,
    ExtractionPage,
    StructuredExtraction,
)

logger = logging.getLogger(__name__)

LAYOUT_ARTIFACT = "layout_records"
DOCLING_ENGINE = "docling"
# Docling 以外のページ画像の解像度。Docling サービスの既定(RAG_ENGINE_RENDER_DPI=300)にそろえる。
VISION_RENDER_DPI = 300
# 図として読み取る asset の種類(source_image は画像ファイル全体を 1 枚の図として扱う)。
VISION_ASSET_KINDS = frozenset(
    {"figure", "image", "picture", "chart", "diagram", "graph", "plot", "source_image"}
)
# 読み取り済み(再開時に読み直さない)とみなす状態。failed は読み直す。
_FINAL_VISION_STATUSES = frozenset({"succeeded", "skipped"})
# bbox の値が座標系の外へはみ出してよい割合(丸め誤差)。
_BBOX_TOLERANCE = 0.01
# 原点が左下(y が上向き)の座標系。Unstructured の ``Orientation.CARTESIAN`` の PointSpace と
# RelativeCoordinateSystem(unstructured/documents/coordinates.py)。PixelSpace は左上原点
# (``Orientation.SCREEN``)。0.27.8 の PDF はどの戦略でも PixelSpace(pdfminer の左下原点は
# ``rect_to_bbox`` で左上へ直してある)なので、通常はこの変換を通らない(#512)。
_BOTTOM_LEFT_COORDINATE_SYSTEMS = frozenset({"pointspace", "relativecoordinatesystem"})

WARNING_PARTIAL_FAILURE = "vision_partial_failure"
WARNING_BBOX_UNIT_UNKNOWN = "vision_bbox_unit_unknown"
WARNING_BBOX_MISSING = "vision_bbox_missing"
WARNING_TABLE_DETECTION_FAILED = "vision_table_detection_failed"
WARNING_SOURCE_UNSUPPORTED = "vision_source_unsupported"
WARNING_FAILED = "vision_failed"

# 共通抽出 schema の要素 kind -> LayoutRecord の category。
_CATEGORY_BY_KIND = {
    "title": "Section-header",
    "text": "Text",
    "list": "List-item",
    "table": "Table",
    "table_caption": "Caption",
    "figure": "Picture",
    "figure_caption": "Caption",
    "header": "Page-header",
    "footer": "Page-footer",
    "equation": "Formula",
}


class VisionImageReader(Protocol):
    """既定の Vision モデルを呼ぶ client(``OciEnterpriseAiClient.generate_from_images``)。"""

    def generate_from_images(
        self,
        images: Sequence[bytes],
        prompt: str,
        *,
        system_prompt: str = "",
        response_schema: Mapping[str, Any] | None = None,
        response_schema_name: str = "image_description",
        mime_type: str = "image/png",
    ) -> Awaitable[str]: ...


class VisionResponseError(ValueError):
    """Vision の応答が画像説明の schema を満たさない。"""


def parse_picture_description(raw: str) -> dict[str, Any]:
    """Vision の応答(JSON)を ``PictureDescriptionOutput`` で検証して dict にする。

    欠けた key は空(文字列は空文字、配列は空配列)で補う。prompt の出力契約が同じ指示をして
    いるため。JSON でない・型が合わない応答は ``VisionResponseError``。
    """
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise VisionResponseError("Vision の応答が JSON ではありません。")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VisionResponseError("Vision の応答の JSON を読めません。") from exc
    if not isinstance(data, dict):
        raise VisionResponseError("Vision の応答が JSON object ではありません。")
    for name, info in PictureDescriptionOutput.model_fields.items():
        if name not in data:
            data[name] = [] if _is_list_annotation(info.annotation) else ""
    try:
        return PictureDescriptionOutput.model_validate(data).model_dump(mode="json")
    except ValidationError as exc:
        raise VisionResponseError("Vision の応答が画像説明の形式を満たしません。") from exc


def _is_list_annotation(annotation: object) -> bool:
    return getattr(annotation, "__origin__", None) is list


class EnterpriseAiPictureDescriber:
    """rag_engine の ``PictureDescriber`` を OCI Enterprise AI の既定の Vision モデルで実装する。

    rag_engine の読み取りは同期処理(thread)で動くので、非同期の client は取込の event loop へ
    ``run_coroutine_threadsafe`` で投げる。``caller``(上限つきの並行の実行器。#2002)を渡すと、
    ``submit`` で複数の図を同時に呼び(上限は ``max_concurrency``)、エラーの種類ごとの再試行・
    全体の待ち・circuit breaker を実行器に任せる(SDK の再試行は切る)。再試行を使い切った図は、
    ほかの呼び出しがすべて終わってから最後にもう一度呼ぶ。
    """

    api_mode = "responses"
    max_tokens = 0

    def __init__(
        self,
        reader: VisionImageReader,
        loop: asyncio.AbstractEventLoop,
        *,
        model_id: str,
        endpoint: str = "",
        project_id: str = "",
        caller: BoundedCaller | None = None,
    ) -> None:
        from rag_engine.adapters.oci import VISION_SYSTEM_PROMPT
        from rag_engine.resources.runtime import resolve_prompt

        self.system_prompt = resolve_prompt("picture_description.system", VISION_SYSTEM_PROMPT)
        self._reader = reader
        self._loop = loop
        self._caller = caller
        self._active = 0
        self._idle: asyncio.Event | None = None
        self._provider = SimpleNamespace(
            provider_id="oci_enterprise_ai",
            model=model_id,
            region="",
            base_url=endpoint,
            project_id=project_id,
        )

    @property
    def max_concurrency(self) -> int:
        return self._caller.policy.max_concurrency if self._caller is not None else 1

    def provider(self) -> Any:
        return self._provider

    def describe(
        self,
        crop_path: Path,
        metadata: dict[str, Any],
        *,
        context_image_paths: Sequence[Path],
        target_kind: str,
        rendered_prompt: str,
    ) -> dict[str, Any]:
        return self.submit(
            crop_path,
            metadata,
            context_image_paths=context_image_paths,
            target_kind=target_kind,
            rendered_prompt=rendered_prompt,
        ).result()

    def submit(
        self,
        crop_path: Path,
        metadata: dict[str, Any],
        *,
        context_image_paths: Sequence[Path],
        target_kind: str,
        rendered_prompt: str,
    ) -> concurrent.futures.Future[dict[str, Any]]:
        """1 件の Vision の呼び出しを event loop へ投げ、結果の future を返す。"""
        _ = target_kind  # prompt へ描画済み。送るのは画像と prompt だけ。
        images = [crop_path.read_bytes()]
        images.extend(Path(path).read_bytes() for path in context_image_paths)
        page = metadata.get("page") if isinstance(metadata, Mapping) else None
        label = f"p.{page} {metadata.get('record_id', '')}".strip() if page else ""
        return asyncio.run_coroutine_threadsafe(
            self._describe(images, rendered_prompt, label), self._loop
        )

    async def _describe(self, images: list[bytes], prompt: str, label: str) -> dict[str, Any]:
        caller = self._caller
        if caller is None:
            raw = await _generate(self._reader, images, prompt, system_prompt=self.system_prompt)
            return parse_picture_description(raw)

        async def attempt_call(attempt: Attempt) -> dict[str, Any]:
            # 大きすぎる(413・context の長さ)ときは、画像の解像度を下げてやり直す。
            sent = (
                images
                if attempt.adaptation == 0
                else await asyncio.to_thread(downscale_images, images, attempt.adaptation)
            )
            with sdk_retries_disabled():
                raw = await _generate(self._reader, sent, prompt, system_prompt=self.system_prompt)
            return parse_picture_description(raw)

        self._enter()
        try:
            try:
                return await caller.call(attempt_call, label=label, adaptive=True)
            except BoundedCallFailed as exc:
                if not exc.final_retry:
                    raise
            # 再試行を使い切った図は、ほかの呼び出しが終わってから最後にもう一度呼ぶ。
            self._leave()
            await self._wait_idle()
            self._enter()
            return await caller.call(attempt_call, label=label, adaptive=True, final=True)
        finally:
            self._leave()

    def _idle_event(self) -> asyncio.Event:
        if self._idle is None:
            self._idle = asyncio.Event()
            self._idle.set()
        return self._idle

    def _enter(self) -> None:
        self._active += 1
        self._idle_event().clear()

    def _leave(self) -> None:
        self._active -= 1
        if self._active <= 0:
            self._active = 0
            self._idle_event().set()

    async def _wait_idle(self) -> None:
        await self._idle_event().wait()


def downscale_images(images: Sequence[bytes], level: int) -> list[bytes]:
    """画像の幅と高さを ``0.5 ** level`` 倍にして PNG にする(大きすぎる入力のやり直し。#2002)。"""
    from io import BytesIO

    from PIL import Image

    factor = 0.5 ** max(1, level)
    resized: list[bytes] = []
    for data in images:
        with Image.open(BytesIO(data)) as image:
            width = max(1, int(image.width * factor))
            height = max(1, int(image.height * factor))
            buffer = BytesIO()
            image.convert("RGB").resize((width, height)).save(buffer, format="PNG")
        resized.append(buffer.getvalue())
    return resized


async def _generate(
    reader: VisionImageReader,
    images: Sequence[bytes],
    prompt: str,
    *,
    system_prompt: str,
) -> str:
    return await async_call_with_model_purpose(
        "document.read",
        reader.generate_from_images,
        images,
        prompt,
        system_prompt=system_prompt,
        response_schema=PictureDescriptionOutput.model_json_schema(),
        response_schema_name="picture_description",
    )


@dataclass
class _Target:
    """LayoutRecord と、書き戻す先の要素 / asset の対応。"""

    record: LayoutRecord
    element_index: int | None = None
    asset_index: int | None = None
    # 図だけの asset の説明を新しい要素として入れる位置(直前の要素の index)。
    anchor_element_index: int | None = None


@dataclass
class _Layout:
    records: list[LayoutRecord] = field(default_factory=list)
    targets: list[_Target] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    # 読み取れない理由で skip した要素 / asset(metadata へ理由を残す)。
    skipped_elements: dict[int, str] = field(default_factory=dict)
    skipped_assets: dict[int, str] = field(default_factory=dict)


def read_figures_with_vision(
    extraction: StructuredExtraction,
    *,
    source_bytes: bytes,
    content_type: str,
    file_name: str,
    parser_backend: str,
    describer: Any,
    prompt_template: str,
    cancel_check: Callable[[], None] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    work_dir: Path | None = None,
) -> StructuredExtraction:
    """解析結果の図・画像を Vision で読み取り、要素・asset(・layout_records)へ反映する。

    同期処理(ページの描画・VLM 呼び出しを含む)。取込からは ``asyncio.to_thread`` で呼ぶ。
    ``cancel_check`` は対象ごとの前に呼び、例外で中断する(取込の取り消し)。
    ``progress_callback`` は Vision の段の記録(``vision/progress.json`` と同じ内容)が変わる
    たびに呼ぶ(取込の job の進捗。#1999)。
    """
    from rag_engine.parsing.picture_descriptions import describe_layout_pictures

    suffix = _source_suffix(content_type, file_name)
    layout_engine = _layout_engine(extraction)
    has_layout = layout_engine is not None
    engine = layout_engine or (parser_backend.strip().casefold() or "parser")
    if suffix is None:
        if not _has_vision_candidates(extraction):
            return extraction
        return _with_summary(
            extraction,
            summary={"enabled": True, "engine": engine, "error": "source_unsupported"},
            warnings=[WARNING_SOURCE_UNSUPPORTED],
        )
    with _vision_run_dir(work_dir) as run_dir:
        source_path = run_dir / f"input{suffix}"
        source_path.write_bytes(source_bytes)
        page_count = get_source_page_count(source_path)
        if has_layout:
            layout = _docling_records(extraction)
            pages_wanted = sorted(
                {
                    record.page
                    for record in layout.records
                    if record.category in {"Picture", "Table"} and 1 <= record.page <= page_count
                }
            )
            if not pages_wanted:
                return extraction
            pdf_path, pages = _render_docling_pages(
                source_path,
                pages_wanted,
                run_dir,
                _docling_layout_pages(extraction),
                reuse_rendered=work_dir is not None,
            )
        else:
            pages_wanted = _adapter_candidate_pages(extraction, page_count)
            if not pages_wanted:
                return _with_bbox_missing(extraction, engine)
            pdf_path, pages = prepare_source_for_analysis(
                source_path, pages_wanted, run_dir, VISION_RENDER_DPI
            )
            layout = _adapter_records(
                extraction,
                engine=engine,
                parser_backend=parser_backend,
                pages=pages,
                source_is_image=suffix != ".pdf",
            )
        if not any(record.category in {"Picture", "Table"} for record in layout.records):
            return _apply_skips(extraction, layout, engine=engine, stats=None)
        stats = describe_layout_pictures(
            layout.records,
            pages,
            run_dir=run_dir,
            pdf_name=file_name or source_path.name,
            describer=describer,
            pdf_path=Path(pdf_path),
            engine=engine,
            prompt_template=prompt_template,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
    if has_layout:
        updated = _write_back_docling(extraction, layout)
    else:
        updated = _write_back_adapter(extraction, layout)
    return _apply_skips(updated, layout, engine=engine, stats=stats)


@contextlib.contextmanager
def _vision_run_dir(work_dir: Path | None) -> Iterator[Path]:
    """Vision の作業ディレクトリ(ページの画像・切り出し・checkpoint)。

    ``work_dir`` を渡すと、その中で読み取り、終わっても消さない(同じ job の中で段ごとに先に
    読み取った応答を、全体の読み取りが checkpoint から使う。#2002)。無ければ一時ディレクトリ。
    """
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        yield work_dir
        return
    with tempfile.TemporaryDirectory(prefix="rag-vision-") as work:
        yield Path(work)


# --- 入力の判定 -------------------------------------------------------------------------


def _source_suffix(content_type: str, file_name: str) -> str | None:
    """解析に使ったファイルの拡張子(PDF / 画像のときだけ。切り出せない形式は None)。"""
    mime = (content_type or "").split(";", 1)[0].strip().casefold()
    if mime == "application/pdf":
        return ".pdf"
    guessed = mimetypes.guess_extension(mime) if mime.startswith("image/") else None
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in SUPPORTED_SOURCE_FILE_TYPES:
        return guessed
    suffix = Path(file_name or "").suffix.casefold()
    if suffix in SUPPORTED_SOURCE_FILE_TYPES and (not mime or mime == "application/octet-stream"):
        return suffix
    return None


def _has_vision_candidates(extraction: StructuredExtraction) -> bool:
    if _docling_layout(extraction) is not None:
        return True
    return any(element.kind == "figure" for element in extraction.elements) or any(
        asset.kind.casefold() in VISION_ASSET_KINDS for asset in extraction.assets
    )


# --- Docling(layout_records の record)-----------------------------------------------------


def _docling_layout(extraction: StructuredExtraction) -> Mapping[str, Any] | None:
    """``layout_records``(Docling の LayoutRecord)。record が無ければ None。"""
    layout = extraction.parser_artifacts.get(LAYOUT_ARTIFACT)
    if isinstance(layout, Mapping) and layout.get("records"):
        return layout
    return None


def _layout_engine(extraction: StructuredExtraction) -> str | None:
    """layout_records があれば Docling(``describe_layout_pictures`` が対象を選ぶ engine)。"""
    layout = _docling_layout(extraction)
    if layout is None:
        return None
    return DOCLING_ENGINE


_LAYOUT_RECORD_FIELDS = frozenset(item.name for item in fields(LayoutRecord))


def _docling_records(extraction: StructuredExtraction) -> _Layout:
    layout = _docling_layout(extraction) or {}
    records = []
    for raw in layout.get("records") or []:
        if not isinstance(raw, Mapping):
            continue
        values = {key: value for key, value in raw.items() if key in _LAYOUT_RECORD_FIELDS}
        values["raw"] = dict(values.get("raw") or {})
        values["bbox"] = [float(value) for value in values.get("bbox") or []]
        records.append(LayoutRecord(**values))
    return _Layout(records=records)


def _docling_layout_pages(extraction: StructuredExtraction) -> dict[int, Mapping[str, Any]]:
    layout = _docling_layout(extraction) or {}
    pages: dict[int, Mapping[str, Any]] = {}
    for page in layout.get("pages") or []:
        if isinstance(page, Mapping) and isinstance(page.get("page"), int):
            pages[int(page["page"])] = page
    return pages


def _render_docling_pages(
    source_path: Path,
    page_numbers: list[int],
    run_dir: Path,
    layout_pages: Mapping[int, Mapping[str, Any]],
    *,
    reuse_rendered: bool = False,
) -> tuple[str, list[PageImage]]:
    """Docling サービスと同じ座標(ページ画像 px)でページを描き直す。

    record の bbox はサービスが描いたページ画像の px なので、同じ寸法の画像を作る。解像度は
    サービスの寸法と PDF の寸法から戻し、丸めで 1px ずれたら画像をその寸法へ合わせる。

    ``reuse_rendered`` なら、同じ作業ディレクトリで同じ原本・解像度・寸法で描いたページは
    描き直さない
    (段ごとに先に読んだページを、全体の読み取りが使う。#2002)。
    """
    dpi = _docling_render_dpi(layout_pages)
    manifest_path = run_dir / "pages" / "rendered.json"
    source_sha = _file_sha256(source_path) if reuse_rendered else ""
    manifest = _load_render_manifest(manifest_path) if reuse_rendered else {}
    cached: dict[int, PageImage] = {}
    for number in page_numbers:
        entry = manifest.get(str(number))
        expected = layout_pages.get(number)
        if not (isinstance(entry, Mapping) and entry.get("source") == source_sha):
            continue
        if entry.get("dpi") != dpi or not Path(str(entry.get("image_path"))).is_file():
            continue
        if expected and (
            (expected.get("width") or entry.get("width")) != entry.get("width")
            or (expected.get("height") or entry.get("height")) != entry.get("height")
        ):
            continue
        cached[number] = PageImage(
            page=number,
            width=int(entry["width"]),
            height=int(entry["height"]),
            pdf_width=float(entry["pdf_width"]),
            pdf_height=float(entry["pdf_height"]),
            image_path=str(entry["image_path"]),
        )
    missing = [number for number in page_numbers if number not in cached]
    if missing:
        pdf_path, rendered = prepare_source_for_analysis(source_path, missing, run_dir, dpi)
    else:
        pdf_path, rendered = copy_pdf_to_run(source_path, run_dir), []
    pages: list[PageImage] = list(cached.values())
    for page in rendered:
        expected = layout_pages.get(page.page)
        width = int(expected.get("width") or page.width) if expected else page.width
        height = int(expected.get("height") or page.height) if expected else page.height
        if (width, height) != (page.width, page.height):
            from PIL import Image

            with Image.open(page.image_path) as image:
                image.convert("RGB").resize((width, height)).save(page.image_path)
        pages.append(
            PageImage(
                page=page.page,
                width=width,
                height=height,
                pdf_width=page.pdf_width,
                pdf_height=page.pdf_height,
                image_path=page.image_path,
            )
        )
        if reuse_rendered:
            manifest[str(page.page)] = {
                "source": source_sha,
                "dpi": dpi,
                "width": width,
                "height": height,
                "pdf_width": page.pdf_width,
                "pdf_height": page.pdf_height,
                "image_path": page.image_path,
            }
    if reuse_rendered and rendered:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return pdf_path, sorted(pages, key=lambda item: item.page)


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_render_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _docling_render_dpi(layout_pages: Mapping[int, Mapping[str, Any]]) -> int:
    for page in layout_pages.values():
        width = _positive_float(page.get("width"))
        pdf_width = _positive_float(page.get("pdf_width"))
        if width and pdf_width:
            return max(36, min(600, round(width * 72 / pdf_width)))
    return VISION_RENDER_DPI


def _write_back_docling(extraction: StructuredExtraction, layout: _Layout) -> StructuredExtraction:
    """Vision の結果を layout_records の record と要素・asset へ書き戻す(旧サービスと同じ形)。"""
    by_id = {record.id: record for record in layout.records}
    elements_by_id = {
        element.element_id: element for element in extraction.elements if element.element_id
    }
    new_elements: list[DocumentElement] = []
    raw_text = extraction.raw_text
    section_path: list[str] = []
    for record in layout.records:
        element = elements_by_id.get(record.id)
        if element is not None:
            section_path = list(element.section_path)
            if record.category in {"Picture", "Table"}:
                element = element.model_copy(
                    update={"metadata": {**element.metadata, **_vision_metadata_of(record)}}
                )
            new_elements.append(element)
            continue
        if record.category != "Picture" or not record.text.strip():
            continue
        # 読み取る前は本文が空で要素がなかった図。サービスの変換と同じ形で要素を作る。
        new_element = DocumentElement(
            kind="figure",
            text=record.text,
            element_id=record.id,
            source_parser="docling_layout",
            page_number=record.page,
            bbox=[float(value) for value in record.bbox],
            section_path=section_path,
            metadata=_metadata_values(layout_record_element_metadata(record)),
        )
        raw_text = _insert_raw_text(raw_text, new_elements, new_element.text)
        new_elements.append(new_element)
    # record と対応しない要素(通常はない)は順序を保って最後に置く。
    new_elements.extend(
        element
        for element in extraction.elements
        if not element.element_id or element.element_id not in by_id
    )
    assets = []
    for asset in extraction.assets:
        record_id = str(asset.metadata.get("element_id") or "")
        asset_record = by_id.get(record_id)
        if asset_record is None:
            assets.append(asset)
            continue
        summary = layout_record_vision_summary(asset_record)
        assets.append(
            asset.model_copy(
                update={
                    "summary": summary or asset.summary,
                    "metadata": {
                        **asset.metadata,
                        "vision_status": str(asset_record.raw.get("vision_status") or ""),
                        "visual_role": str(asset_record.raw.get("visual_role") or ""),
                    },
                }
            )
        )
    artifacts = dict(extraction.parser_artifacts)
    layout_artifact = dict(_docling_layout(extraction) or {})
    layout_artifact["records"] = [_json_value(record.to_dict()) for record in layout.records]
    artifacts[LAYOUT_ARTIFACT] = layout_artifact
    return extraction.model_copy(
        update={
            "elements": _renumbered(new_elements),
            "assets": assets,
            "raw_text": raw_text,
            "parser_artifacts": artifacts,
        }
    )


# --- Docling 以外(要素と asset から LayoutRecord を作る)------------------------------------


def _adapter_candidate_pages(extraction: StructuredExtraction, page_count: int) -> list[int]:
    """読み取りの対象(図・表)があるページ。ページ番号がなければ画像ファイルの 1 ページ目。"""
    pages: set[int] = set()
    for element in extraction.elements:
        if element.kind in {"figure", "table"} and element.bbox:
            pages.add(element.page_number or 1)
    for asset in extraction.assets:
        if asset.kind.casefold() in VISION_ASSET_KINDS and asset.bbox:
            pages.add(asset.page_number or 1)
    return sorted(page for page in pages if 1 <= page <= page_count)


def _adapter_records(
    extraction: StructuredExtraction,
    *,
    engine: str,
    parser_backend: str,
    pages: Sequence[PageImage],
    source_is_image: bool,
) -> _Layout:
    """要素と図の asset を、ページ画像の px 座標の LayoutRecord にする。

    図の要素の本文(OCR・キャプション)は、Docling と同じく図と同じ bbox の ``picture_ocr_text``
    record にして prompt と装飾の判定へ渡し、図の record 自体は本文を空にして読み取りの対象にする。
    """
    layout = _Layout()
    page_by_number = {page.page: page for page in pages}
    extraction_pages = {page.page_number: page for page in extraction.pages}
    linked_assets: dict[str, int] = {}
    for index, asset in enumerate(extraction.assets):
        element_id = str(asset.metadata.get("element_id") or "")
        if element_id:
            linked_assets[element_id] = index
    element_ids = {element.element_id for element in extraction.elements if element.element_id}
    element_px: dict[int, tuple[int, list[float]]] = {}
    for index, element in enumerate(extraction.elements):
        category = _CATEGORY_BY_KIND.get(element.kind, "Text")
        is_figure = element.kind == "figure"
        page = page_by_number.get(element.page_number or 1)
        if page is None or not element.bbox:
            if is_figure and _vision_pending(element.metadata):
                layout.skipped_elements[index] = WARNING_BBOX_MISSING
            continue
        bbox = _page_px_bbox(
            element.bbox,
            parser_backend=parser_backend,
            metadata=element.metadata,
            extraction_page=extraction_pages.get(element.page_number or 1),
            page=page,
            source_is_image=source_is_image,
        )
        if bbox is None:
            if is_figure and _vision_pending(element.metadata):
                layout.skipped_elements[index] = WARNING_BBOX_UNIT_UNKNOWN
            continue
        element_px[index] = (page.page, bbox)
        record_id = f"v{len(layout.records):04d}"
        seq_no = (element.order + 1) * 10
        if not is_figure:
            layout.records.append(
                _record(record_id, engine, page, seq_no, bbox, category, element.text)
            )
            if category == "Table":
                layout.targets.append(_Target(layout.records[-1], element_index=index))
            continue
        asset_index = linked_assets.get(element.element_id or "")
        pending = _vision_pending(element.metadata)
        # 読み取り済みの図は本文を持つ Picture として残す(表内画像の検出で「覆う図」になる)。
        picture = _record(
            record_id,
            engine,
            page,
            seq_no,
            bbox,
            "Picture",
            "" if pending else element.text,
            raw_type="picture",
        )
        layout.records.append(picture)
        if pending:
            layout.targets.append(_Target(picture, element_index=index, asset_index=asset_index))
            if element.text.strip():
                # Docling と同じく、図の中の文字は category=Picture の picture_ocr_text にする。
                layout.records.append(
                    _record(
                        f"{record_id}-ocr",
                        engine,
                        page,
                        seq_no,
                        bbox,
                        "Picture",
                        format_docling_picture_ocr_text(element.text),
                        raw_type="picture_ocr_text",
                        # 持ち主の図(分割が図の画像に結び付ける。#3455)。
                        raw={"source_picture_ref": record_id},
                    )
                )
    for index, asset in enumerate(extraction.assets):
        if asset.kind.casefold() not in VISION_ASSET_KINDS:
            continue
        if str(asset.metadata.get("element_id") or "") in element_ids:
            continue  # 要素と対応する asset は要素の側で読み取る。
        if not _vision_pending(asset.metadata):
            continue
        page = page_by_number.get(asset.page_number or 1)
        if page is None or not asset.bbox:
            layout.skipped_assets[index] = WARNING_BBOX_MISSING
            continue
        bbox = _page_px_bbox(
            asset.bbox,
            parser_backend=parser_backend,
            metadata=asset.metadata,
            extraction_page=extraction_pages.get(asset.page_number or 1),
            page=page,
            source_is_image=source_is_image,
        )
        if bbox is None:
            layout.skipped_assets[index] = WARNING_BBOX_UNIT_UNKNOWN
            continue
        anchor = _anchor_element(element_px, page.page, bbox)
        seq_no = (extraction.elements[anchor].order + 1) * 10 + 5 if anchor is not None else 5
        picture = _record(
            f"v{len(layout.records):04d}",
            engine,
            page,
            seq_no,
            bbox,
            "Picture",
            "",
            raw_type="picture",
        )
        layout.records.append(picture)
        layout.targets.append(_Target(picture, asset_index=index, anchor_element_index=anchor))
    return layout


def _record(
    record_id: str,
    engine: str,
    page: PageImage,
    seq_no: int,
    bbox: list[float],
    category: str,
    text: str,
    *,
    raw_type: str | None = None,
    raw: dict[str, Any] | None = None,
) -> LayoutRecord:
    return LayoutRecord(
        id=record_id,
        engine=engine,
        page=page.page,
        seq_no=seq_no,
        bbox=bbox,
        coord_system="image_top_left",
        page_width=float(page.width),
        page_height=float(page.height),
        category=category,
        text=text,
        raw_type=raw_type or category.casefold(),
        raw=dict(raw or {}),
    )


def _vision_pending(metadata: Mapping[str, object]) -> bool:
    return str(metadata.get("vision_status") or "") not in _FINAL_VISION_STATUSES


def _anchor_element(
    element_px: Mapping[int, tuple[int, list[float]]],
    page_number: int,
    bbox: list[float],
) -> int | None:
    """図だけの asset を入れる位置: 同じページで図より上にある最後の要素(なければ前のページ)。"""
    before_page = [index for index, (page, _) in element_px.items() if page < page_number]
    same_page = [
        index
        for index, (page, element_bbox) in element_px.items()
        if page == page_number and element_bbox[1] <= bbox[1]
    ]
    candidates = same_page or before_page
    return max(candidates) if candidates else None


def _page_px_bbox(
    bbox: Sequence[float],
    *,
    parser_backend: str,
    metadata: Mapping[str, object],
    extraction_page: ExtractionPage | None,
    page: PageImage,
    source_is_image: bool,
) -> list[float] | None:
    """解析エンジンの bbox をページ画像の px(``page`` の寸法)へ写す。確かでなければ None。"""
    scale = _bbox_scale(
        bbox,
        parser_backend=parser_backend,
        metadata=metadata,
        extraction_page=extraction_page,
        page=page,
        source_is_image=source_is_image,
    )
    if scale is None:
        return None
    base_width, base_height = scale
    x0, y0, x1, y1 = (float(value) for value in bbox[:4])
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    if _is_bottom_left_origin(metadata):
        # 左下原点を左上原点へ(ai-foundations-lab の pdf_bottom_left_to_image_top_left と同じ)。
        top, bottom = base_height - bottom, base_height - top
    if (
        left < -base_width * _BBOX_TOLERANCE
        or top < -base_height * _BBOX_TOLERANCE
        or right > base_width * (1 + _BBOX_TOLERANCE)
        or bottom > base_height * (1 + _BBOX_TOLERANCE)
        or right <= left
        or bottom <= top
    ):
        # 座標系の外にはみ出す bbox は単位の見込み違い。違う領域を切り出さない。
        return None
    sx = page.width / base_width
    sy = page.height / base_height
    return [
        max(0.0, left * sx),
        max(0.0, top * sy),
        min(float(page.width), right * sx),
        min(float(page.height), bottom * sy),
    ]


def _is_bottom_left_origin(metadata: Mapping[str, object]) -> bool:
    system = str(metadata.get("bbox_coordinate_system") or "").strip().casefold()
    return system in _BOTTOM_LEFT_COORDINATE_SYSTEMS


def _bbox_scale(
    bbox: Sequence[float],
    *,
    parser_backend: str,
    metadata: Mapping[str, object],
    extraction_page: ExtractionPage | None,
    page: PageImage,
    source_is_image: bool,
) -> tuple[float, float] | None:
    """bbox の座標系の寸法(幅, 高さ)。解析エンジンごとの単位の表(#497・#502)。

    x と y はそれぞれの寸法で別々に換算する(縦横比は保たない)。原点は左上。ただし座標系が
    左下原点(``_BOTTOM_LEFT_COORDINATE_SYSTEMS``)なら ``_page_px_bbox`` が y を反転する。
    ai-foundations-lab(engchina/ai-foundations-lab、commit 572e9fa の ``20260819/``)の
    ビューアで、ページ画像に重ねて確かめた換算と同じ(#512。根拠は docs/rag-engine.md)。

    - 値が全て 1 以下: ページに対する割合(0-1。VLM・画像全体の source_image)。
    - Unstructured: coordinates の座標系の寸法(layout_width / height。registry が
      ``page_width`` / ``page_height`` に写す)。無ければ不明。hi_res の PDF は PixelSpace
      (350 dpi 相当のページ画像の px)、画像ファイルは元画像の px(実サービスで確認。#502)。
      lab の ``adapters/unstructured_adapter.py`` も ``system.width / height`` で割る。
    - それ以外: 不明。
    """
    values = [abs(float(value)) for value in bbox[:4]]
    if len(values) < 4:
        return None
    largest = max(values)
    if largest <= 1.0:
        return (1.0, 1.0)
    backend = parser_backend.strip().casefold()
    if backend == "unstructured":
        return _metadata_page_size(metadata)
    return None


def _metadata_page_size(metadata: Mapping[str, object]) -> tuple[float, float] | None:
    width = _positive_float(metadata.get("page_width"))
    height = _positive_float(metadata.get("page_height"))
    return (width, height) if width and height else None


def _write_back_adapter(extraction: StructuredExtraction, layout: _Layout) -> StructuredExtraction:
    """Vision の結果を要素の本文・metadata と asset の要約へ書き戻す。"""
    elements = list(extraction.elements)
    assets = list(extraction.assets)
    raw_text = extraction.raw_text
    inserts: dict[int | None, list[DocumentElement]] = {}
    for target in layout.targets:
        record = target.record
        vision = _vision_metadata_of(record)
        described = record.category == "Picture" and bool(record.text.strip())
        if target.element_index is not None:
            element = elements[target.element_index]
            metadata = {**element.metadata, **vision}
            text = element.text
            if described:
                if element.text.strip():
                    metadata["vision_source_text"] = element.text
                text = record.text
                raw_text = _replace_raw_text(raw_text, element.text, text, elements, target)
            elif record.category == "Table" and (
                supplement := table_vision_supplement(
                    str(record.raw.get("table_vision_text") or "")
                )
            ):
                # 分割(rag_pipeline_core.chunking)と同じ書式。分割は本文に説明文がある表へ
                # 補足を二重に足さない(#513)。
                text = f"{element.text}\n{supplement}"
                raw_text = _replace_raw_text(raw_text, element.text, text, elements, target)
            elements[target.element_index] = element.model_copy(
                update={"text": text, "metadata": metadata}
            )
        if target.asset_index is not None:
            asset = assets[target.asset_index]
            summary = layout_record_vision_summary(record) or (record.text if described else "")
            assets[target.asset_index] = asset.model_copy(
                update={
                    "summary": summary or asset.summary,
                    "metadata": {**asset.metadata, **vision},
                }
            )
            if target.element_index is None and described:
                inserts.setdefault(target.anchor_element_index, []).append(
                    _asset_element(asset, record, elements, target.anchor_element_index)
                )
    merged: list[DocumentElement] = list(inserts.get(None, []))
    for index, element in enumerate(elements):
        merged.append(element)
        merged.extend(inserts.get(index, []))
    for anchor, new_elements in inserts.items():
        for new_element in new_elements:
            anchor_text = elements[anchor].text if anchor is not None else ""
            raw_text = _insert_after(raw_text, anchor_text, new_element.text)
    return extraction.model_copy(
        update={"elements": _renumbered(merged), "assets": assets, "raw_text": raw_text}
    )


def _asset_element(
    asset: ExtractionAsset,
    record: LayoutRecord,
    elements: Sequence[DocumentElement],
    anchor: int | None,
) -> DocumentElement:
    """図だけの asset(本文の要素がない図)の説明を、検索できる図の要素にする。"""
    metadata: dict[str, ExtractionMetadataValue] = {
        key: value
        for key, value in asset.metadata.items()
        if key
        in {
            "source_parser",
            "parser_backend",
            "bbox_unit",
            "bbox_coordinate_mode",
            "page_width",
            "page_height",
        }
    }
    metadata.update({"asset_id": asset.asset_id, "asset_kind": asset.kind})
    metadata.update(_vision_metadata_of(record))
    element_id = str(asset.metadata.get("element_id") or "") or f"vision-{asset.asset_id}"
    return DocumentElement(
        kind="figure",
        text=record.text,
        element_id=element_id[:128],
        content_kind="figure",
        source_parser=str(asset.metadata.get("source_parser") or "") or None,
        page_number=asset.page_number,
        bbox=list(asset.bbox) if asset.bbox else None,
        section_path=list(elements[anchor].section_path) if anchor is not None else [],
        metadata=metadata,
    )


# --- 共通 -------------------------------------------------------------------------------


def _vision_metadata_of(record: LayoutRecord) -> dict[str, ExtractionMetadataValue]:
    return _metadata_values(layout_record_vision_metadata(record))


def _metadata_values(values: Mapping[str, object]) -> dict[str, ExtractionMetadataValue]:
    return {
        key: value
        for key, value in values.items()
        if value is None or isinstance(value, str | int | float | bool)
    }


def _renumbered(elements: Sequence[DocumentElement]) -> list[DocumentElement]:
    return [element.model_copy(update={"order": order}) for order, element in enumerate(elements)]


def _replace_raw_text(
    raw_text: str,
    old: str,
    new: str,
    elements: Sequence[DocumentElement],
    target: _Target,
) -> str:
    """raw_text(固定長の分割などが使う)の元の本文を置き換える。無ければ直前の要素の後に入れる。"""
    if old.strip() and old in raw_text:
        return raw_text.replace(old, new, 1)
    index = target.element_index
    anchor = elements[index - 1].text if index is not None and index > 0 else ""
    return _insert_after(raw_text, anchor, new)


def _insert_raw_text(raw_text: str, preceding: Sequence[DocumentElement], text: str) -> str:
    for element in reversed(preceding):
        if element.text.strip() and element.text in raw_text:
            return _insert_after(raw_text, element.text, text)
    return _insert_after(raw_text, "", text)


def _insert_after(raw_text: str, anchor: str, text: str) -> str:
    if not text.strip():
        return raw_text
    if anchor.strip() and anchor in raw_text:
        position = raw_text.index(anchor) + len(anchor)
        return f"{raw_text[:position]}\n\n{text}{raw_text[position:]}"
    return f"{raw_text}\n\n{text}".strip() if raw_text else text


def _with_bbox_missing(extraction: StructuredExtraction, engine: str) -> StructuredExtraction:
    layout = _Layout()
    for index, element in enumerate(extraction.elements):
        if element.kind == "figure" and _vision_pending(element.metadata):
            layout.skipped_elements[index] = WARNING_BBOX_MISSING
    for index, asset in enumerate(extraction.assets):
        if (
            asset.kind.casefold() in VISION_ASSET_KINDS
            and _vision_pending(asset.metadata)
            and not str(asset.metadata.get("element_id") or "")
        ):
            layout.skipped_assets[index] = WARNING_BBOX_MISSING
    return _apply_skips(extraction, layout, engine=engine, stats=None)


def _apply_skips(
    extraction: StructuredExtraction,
    layout: _Layout,
    *,
    engine: str,
    stats: Any,
) -> StructuredExtraction:
    """読み取れなかった図に理由を残し、warning と結果の集計を parser_artifacts へ入れる。"""
    elements = list(extraction.elements)
    assets = list(extraction.assets)
    reasons: dict[str, int] = {}
    for index, reason in layout.skipped_elements.items():
        element = elements[index]
        elements[index] = element.model_copy(
            update={"metadata": {**element.metadata, **_skip_metadata(reason)}}
        )
        reasons[reason] = reasons.get(reason, 0) + 1
    for index, reason in layout.skipped_assets.items():
        asset = assets[index]
        assets[index] = asset.model_copy(
            update={"metadata": {**asset.metadata, **_skip_metadata(reason)}}
        )
        reasons[reason] = reasons.get(reason, 0) + 1
    summary: dict[str, object] = {"enabled": True, "engine": engine}
    warnings = sorted(reasons)
    if stats is not None:
        summary.update(
            targets=stats.targets,
            succeeded=stats.succeeded,
            failed=stats.failed,
            discovery_failed=stats.discovery_failed,
        )
        if stats.failed:
            warnings.append(WARNING_PARTIAL_FAILURE)
        if stats.discovery_failed:
            warnings.append(WARNING_TABLE_DETECTION_FAILED)
    for reason, count in reasons.items():
        summary[f"skipped_{reason.removeprefix('vision_')}"] = count
    updated = extraction.model_copy(update={"elements": elements, "assets": assets})
    return _with_summary(updated, summary=summary, warnings=warnings)


def _skip_metadata(reason: str) -> dict[str, ExtractionMetadataValue]:
    return {"vision_status": "skipped", "vision_skip_reason": reason.removeprefix("vision_")}


def _with_summary(
    extraction: StructuredExtraction,
    *,
    summary: Mapping[str, object],
    warnings: Sequence[str],
) -> StructuredExtraction:
    artifacts = dict(extraction.parser_artifacts)
    artifacts["vision"] = _json_value(dict(summary))
    merged_warnings = list(extraction.warnings)
    for warning in warnings:
        if warning not in merged_warnings:
            merged_warnings.append(warning)
    return extraction.model_copy(
        update={"parser_artifacts": artifacts, "warnings": merged_warnings}
    )


def vision_failure(extraction: StructuredExtraction, engine: str) -> StructuredExtraction:
    """Vision の段の全体が失敗したときの記録(元の解析結果は変えず、warning を残す)。"""
    return _with_summary(
        extraction,
        summary={"enabled": True, "engine": engine, "error": "vision_failed"},
        warnings=[WARNING_FAILED],
    )


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _json_value(value: Any) -> Any:
    """Path など JSON にできない値を文字列へ寄せる(layout_records と parser_artifacts 用)。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_value(item) for item in value]
    return str(value)
