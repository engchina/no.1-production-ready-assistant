"""API の失敗のうち、画面が `error_code` で見分ける必要があるもの。"""

from __future__ import annotations

from fastapi import HTTPException

# 文書の原本・処理後ファイルが保存先に無い（保存先の設定の変更・削除など。#1210）。
# 画面のプレビューは、この code のときだけ iframe の表示に戻さず理由と対処を出す。
DOCUMENT_FILE_MISSING_CODE = "RAG_DOCUMENT_FILE_MISSING"


class DocumentFileMissingError(HTTPException):
    """文書のファイルが保存先に無い（404）。応答の envelope に `error_code` を付ける。"""

    code = DOCUMENT_FILE_MISSING_CODE

    def __init__(self, message: str) -> None:
        super().__init__(status_code=404, detail=message)
