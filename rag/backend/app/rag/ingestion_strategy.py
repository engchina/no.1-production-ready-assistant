"""source profile に基づく抽出戦略。"""

from dataclasses import dataclass

from app.schemas.document import SourceProfile


@dataclass(frozen=True)
class ExtractionStrategy:
    """VLM/テキスト抽出へ渡す方針。"""

    parser_profile: str
    prompt: str


BASE_EXTRACTION_REQUIREMENTS = (
    "出力は StructuredExtraction schema に従ってください。"
    "raw_text には読み順の本文全体を入れ、elements はページ順・読み順で返してください。"
    "header/footer は本文と混ぜず、表・図・リストは別 element としてください。"
)

PROFILE_INSTRUCTIONS = {
    "enterprise_ai_pdf_layout": (
        "PDF レイアウト解析方針: ページ番号、章節見出し、段組み、表、図表キャプションを"
        "保持してください。表は Markdown table または行構造が分かるテキストで独立させ、"
        "同じ表を本文段落へ重複混入しないでください。"
    ),
    "enterprise_ai_image_ocr": (
        "画像 OCR 方針: 低解像度・傾き・手書き・図中文字を慎重に読み取り、判読不能箇所は"
        "推測せず warnings に入れてください。図や写真の説明は figure element にしてください。"
    ),
    "enterprise_ai_text_structure": (
        "テキスト構造化方針: 既存の Markdown、見出し、箇条書き、表、コードブロックを壊さず、"
        "セクション境界と raw_text の順序を維持してください。"
    ),
    "enterprise_ai_office_structure": (
        "Office 文書構造化方針: スライド、シート、段落、表、図表キャプションを別 element とし、"
        "ページまたはスライド番号が分かる場合は page_number に入れてください。"
    ),
    "enterprise_ai_generic": (
        "汎用解析方針: 読み順、章節、表、図、リストを可能な範囲で保持し、"
        "不明な構造は other element として分離してください。"
    ),
}


def extraction_strategy_for_source(
    *,
    source_profile: SourceProfile | None,
    base_prompt: str,
) -> ExtractionStrategy:
    """source profile の parser_profile を VLM/抽出 prompt へ反映する。"""
    parser_profile = (
        source_profile.parser_profile if source_profile is not None else "enterprise_ai_generic"
    )
    profile_instruction = PROFILE_INSTRUCTIONS.get(
        parser_profile,
        PROFILE_INSTRUCTIONS["enterprise_ai_generic"],
    )
    source_context = _source_context(source_profile)
    prompt = "\n".join(
        part
        for part in [
            base_prompt.strip(),
            BASE_EXTRACTION_REQUIREMENTS,
            profile_instruction,
            source_context,
        ]
        if part
    )
    return ExtractionStrategy(parser_profile=parser_profile, prompt=prompt)


def _source_context(source_profile: SourceProfile | None) -> str:
    if source_profile is None:
        return ""
    warnings = ", ".join(source_profile.quality_warnings) or "なし"
    return (
        "原本メタデータ: "
        f"modality={source_profile.modality.value}, "
        f"content_type={source_profile.content_type}, "
        f"extension={source_profile.extension or 'なし'}, "
        f"quality_warnings={warnings}。"
    )
