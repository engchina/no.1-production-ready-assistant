"""旧 Markdown に混在した開発診断を業務本文から区別する。"""

import re

_EVIDENCE_MESSAGE = re.compile(
    r"(?:[A-Za-z0-9_.* /:-]+(?:各IDの)?[ /:]*)?"
    r"(?:evidence[ :]*検証不能|証拠の資料・位置・原文を照合できません。|"
    r"根拠を確認できない推論です。業務担当者の確認が必要です。)[。.]?$"
)


def is_evidence_note(message: str) -> bool:
    return bool(_EVIDENCE_MESSAGE.fullmatch(message.strip().removeprefix("- ")))


def is_developer_note(line: str) -> bool:
    text = line.strip().removeprefix("- ")
    return (
        is_evidence_note(text)
        or text.startswith("概念 ID:")
        or (text.startswith("根拠:") and "source_id:" in text)
        or (text.startswith("business_text_chunks") and "正の業務記述なし" in text)
    )
