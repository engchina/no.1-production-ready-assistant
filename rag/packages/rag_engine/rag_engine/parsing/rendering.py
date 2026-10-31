"""PDF と画像を解析・ビューア共通のページ画像へ変換する。"""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Literal

from rag_engine.models.layout import PageImage

SourceFileKind = Literal["pdf", "image"]

SUPPORTED_IMAGE_FILE_TYPES = (".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif", ".tif", ".tiff")
SUPPORTED_SOURCE_FILE_TYPES = (".pdf", *SUPPORTED_IMAGE_FILE_TYPES)
MAX_IMAGE_PDF_PAGE_DIMENSION = 14400.0
# PDFium はスレッド安全ではない（pypdfium2 の Known limitations「Incompatibility with Threading」）。
# 同じ process の中の PDFium の呼び出し（開く・描く・読む・閉じる）は、すべてこの 1 つの lock の中で行う
# （#3362）。rag_engine の外（RAG backend など）から呼ぶときも、この lock を使う。
PDFIUM_LOCK = RLock()


def source_file_kind(source_path: str | Path) -> SourceFileKind:
    """入力ファイルを PDF または画像として分類します。"""
    suffix = Path(source_path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in SUPPORTED_IMAGE_FILE_TYPES:
        return "image"
    supported = ", ".join(SUPPORTED_SOURCE_FILE_TYPES)
    raise ValueError(f"対応しているファイル形式は PDF または画像です。対応拡張子: {supported}")


def get_source_page_count(source_path: str | Path) -> int:
    """PDF または画像のページ数を返します。"""
    if source_file_kind(source_path) == "image":
        return 1
    return get_pdf_page_count(source_path)


def prepare_source_for_analysis(
    source_path: str | Path,
    page_numbers: list[int],
    output_dir: str | Path,
    dpi: int,
) -> tuple[str, list[PageImage]]:
    """入力ファイルを解析対象ページ画像と共通参照ファイルへ変換します。"""
    if source_file_kind(source_path) == "image":
        return prepare_image_for_analysis(source_path, page_numbers, output_dir)

    run_pdf_path = copy_pdf_to_run(source_path, output_dir)
    return run_pdf_path, render_pdf_pages(run_pdf_path, page_numbers, output_dir, dpi)


def source_frame_warnings(source_path: str | Path) -> list[str]:
    """複数フレーム画像（複数ページ TIFF、アニメーション GIF 等）の未解析フレームを警告文にします。

    画像は常に 1 ページとして扱い、先頭フレームだけを解析する。残りを黙って捨てないための通知で、
    PDF と単一フレーム画像、フレーム数を読めない画像では空 list を返す（読込み失敗は解析側で報告する）。
    """
    if source_file_kind(source_path) != "image":
        return []
    try:
        from PIL import Image

        with Image.open(str(source_path)) as image:
            frames = int(getattr(image, "n_frames", 1))
    except Exception:
        return []
    if frames <= 1:
        return []
    return [f"画像に {frames} フレームあります。先頭フレームだけを解析し、残り {frames - 1} フレームは対象外です。"]


def get_pdf_page_count(pdf_path: str | Path) -> int:
    """pypdfium2 を使って PDF のページ数を取得します。"""
    pdfium = _import_pdfium()
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            return len(document)
        finally:
            document.close()


def render_pdf_pages(
    pdf_path: str | Path,
    page_numbers: list[int],
    output_dir: str | Path,
    dpi: int,
    subdir: str = "pages",
) -> list[PageImage]:
    """PDF の選択ページを PNG 化し、全エンジン共通の座標基準にする。"""
    pdfium = _import_pdfium()
    pages_dir = Path(output_dir) / subdir
    pages_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[PageImage] = []
    scale = float(dpi) / 72.0
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            for page_number in page_numbers:
                page = document[page_number - 1]
                try:
                    pdf_width, pdf_height = page.get_size()
                    bitmap = page.render(scale=scale, rotation=0)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        image_path = pages_dir / f"page_{page_number:04d}.png"
                        image.save(image_path)
                        width, height = image.size
                    finally:
                        bitmap.close()
                    rendered.append(
                        PageImage(
                            page=page_number,
                            width=int(width),
                            height=int(height),
                            pdf_width=float(pdf_width),
                            pdf_height=float(pdf_height),
                            image_path=str(image_path),
                        )
                    )
                finally:
                    page.close()
        finally:
            document.close()
    return rendered


def pdf_text_lines_in_bbox(pdf_path: str | Path, page_number: int, bbox: dict) -> list[str]:
    """PDF の text layer から、bbox（PDF point。l/t/r/b と coord_origin）内の原文を行単位で返します。

    text layer がない（スキャン PDF）場合は空。bbox は Docling の provenance の形式を受け取る。
    bbox の端にかかる行は途中から切り取られた文字列になるため、ページ全体の行と一致する行だけを返す。
    """
    pdfium = _import_pdfium()

    def lines(text: str) -> list[str]:
        return [line for line in (" ".join(raw.split()) for raw in text.splitlines()) if line]

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            page = document[page_number - 1]
            try:
                height = page.get_height()
                top, bottom = float(bbox["t"]), float(bbox["b"])
                if str(bbox.get("coord_origin") or "").upper().endswith("TOPLEFT"):
                    top, bottom = height - top, height - bottom
                textpage = page.get_textpage()
                try:
                    page_lines = set(lines(textpage.get_text_range()))
                    bounded = lines(
                        textpage.get_text_bounded(
                            left=float(bbox["l"]), bottom=min(top, bottom), right=float(bbox["r"]), top=max(top, bottom)
                        )
                    )
                finally:
                    textpage.close()
            finally:
                # render_pdf_pages と同じく page も明示的に閉じる。document.close() が子を閉じるかは版依存 (#791)。
                page.close()
        finally:
            document.close()
    return [line for line in bounded if line in page_lines]


def pdf_image_object_bounds(pdf_path: str | Path, page_number: int) -> list[tuple[float, float, float, float]]:
    """PDF のページに描いた画像（image object）の配置（PDF point。左下原点の left, bottom, right, top）。

    ページの内容に直接置いた画像だけを返す（フォーム XObject の中の画像は座標がページの座標とは限らない）。
    """
    pdfium = _import_pdfium()
    import pypdfium2.raw as pdfium_c

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            page = document[page_number - 1]
            try:
                return [
                    (float(left), float(bottom), float(right), float(top))
                    for left, bottom, right, top in (
                        image.get_bounds() for image in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=0)
                    )
                ]
            finally:
                page.close()
        finally:
            document.close()


def copy_pdf_to_run(pdf_path: str | Path, output_dir: str | Path) -> str:
    """解析実行ディレクトリへ元 PDF をコピーします。"""
    target = Path(output_dir) / "source.pdf"
    shutil.copyfile(str(pdf_path), target)
    return str(target)


def prepare_image_for_analysis(
    image_path: str | Path,
    page_numbers: list[int],
    output_dir: str | Path,
) -> tuple[str, list[PageImage]]:
    """単一画像を 1 ページ分の PageImage として実行ディレクトリへコピーします。"""
    if page_numbers != [1]:
        raise ValueError("画像ファイルは 1 ページとして扱います。解析ページには 1 を指定してください。")

    pages_dir = Path(output_dir) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    page_image_path = pages_dir / "page_0001.png"
    with _open_image_as_rgb(image_path) as image:
        width, height = image.size
        image.save(page_image_path)

    source_pdf_path = Path(output_dir) / "source.pdf"
    pdf_width, pdf_height = _write_image_pdf(page_image_path, source_pdf_path, width, height)
    page = PageImage(
        page=1,
        width=width,
        height=height,
        pdf_width=pdf_width,
        pdf_height=pdf_height,
        image_path=str(page_image_path),
    )
    return str(source_pdf_path), [page]


def _open_image_as_rgb(image_path: str | Path):
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow が未インストールのため画像を読み込めません。`pip install pillow` を実行してください。") from exc

    try:
        with Image.open(str(image_path)) as image:
            image = ImageOps.exif_transpose(image)
            image.load()
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                return background
            return image.convert("RGB")
    except Exception as exc:
        raise ValueError("画像ファイルを読み込めませんでした。対応形式と破損がないか確認してください。") from exc


def _write_image_pdf(image_path: Path, target_path: Path, width: int, height: int) -> tuple[float, float]:
    pdfium = _import_pdfium()
    page_width, page_height = _image_pdf_page_size(width, height)
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument.new()
        try:
            with TemporaryDirectory(prefix="image_pdf_", dir=target_path.parent) as temp_dir:
                jpeg_path = Path(temp_dir) / "source.jpg"
                with _open_image_as_rgb(image_path) as image:
                    image.save(jpeg_path, format="JPEG", quality=95, subsampling=0)
                pdf_image = pdfium.PdfImage.new(document)
                pdf_image.load_jpeg(str(jpeg_path))
                pdf_image.set_matrix(pdfium.PdfMatrix().scale(page_width, page_height))
                page = document.new_page(page_width, page_height)
                try:
                    page.insert_obj(pdf_image)
                    page.gen_content()
                    document.save(str(target_path), version=17)
                finally:
                    page.close()
        finally:
            document.close()
    return page_width, page_height


def _import_pdfium():
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("pypdfium2 が未インストールのため PDF を処理できません。`pip install pypdfium2` を実行してください。") from exc
    return pdfium


def _image_pdf_page_size(width: int, height: int) -> tuple[float, float]:
    longest_side = max(float(width), float(height))
    if longest_side <= MAX_IMAGE_PDF_PAGE_DIMENSION:
        return float(width), float(height)
    scale = MAX_IMAGE_PDF_PAGE_DIMENSION / longest_side
    return float(width) * scale, float(height) * scale
