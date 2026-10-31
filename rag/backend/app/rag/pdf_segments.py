"""PDF を VLM 向けの小さな page segment へ分割する。"""

from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader, PdfWriter


@dataclass(frozen=True)
class PdfPageSegment:
    """元 PDF の連続ページを含む小さな PDF 断片。"""

    index: int
    page_start: int
    page_end: int
    content: bytes

    @property
    def page_count(self) -> int:
        """この segment に含まれるページ数。"""
        return self.page_end - self.page_start + 1


def split_pdf_page_segments(
    pdf_bytes: bytes,
    *,
    max_pages_per_segment: int,
    page_number_offset: int = 0,
) -> list[PdfPageSegment]:
    """PDF bytes を連続ページ segment に分ける。

    解析できない PDF は空 list を返す。既存の全体抽出 fallback を使うため、
    ここでは利用者向けエラーに変換しない。
    """
    if max_pages_per_segment < 1:
        raise ValueError("max_pages_per_segment は 1 以上で指定してください。")

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001 - pypdf は複数種の復号例外を返す
                return []
        page_total = len(reader.pages)
    except Exception:  # noqa: BLE001 - 壊れた PDF は既存 fallback へ回す
        return []

    if page_total < 1:
        return []

    segments: list[PdfPageSegment] = []
    for zero_based_start in range(0, page_total, max_pages_per_segment):
        zero_based_end = min(zero_based_start + max_pages_per_segment, page_total)
        writer = PdfWriter()
        for page_index in range(zero_based_start, zero_based_end):
            writer.add_page(reader.pages[page_index])
        output = BytesIO()
        writer.write(output)
        segments.append(
            PdfPageSegment(
                index=len(segments),
                page_start=page_number_offset + zero_based_start + 1,
                page_end=page_number_offset + zero_based_end,
                content=output.getvalue(),
            )
        )
    return segments
