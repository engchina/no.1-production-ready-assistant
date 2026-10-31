"""検索文の表記差だけをそろえる同一性のキー。"""

import unicodedata


def normalized_query_key(query: str) -> str:
    """NFKC と前後・連続空白を正規化し、語間の区切りと大小文字は保持する。"""
    return " ".join(unicodedata.normalize("NFKC", query).split())
