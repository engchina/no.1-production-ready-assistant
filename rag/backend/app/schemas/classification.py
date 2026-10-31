"""文書の分類の値の表記の正規化(保存・検索の絞り込み・候補・一括の正規化で共通。#547)。

schemas/document と schemas/search の両方から使うため、どちらにも依存しない module に置く。
"""

import re
import unicodedata

CLASSIFICATION_CATEGORY_KEYS = ("large_category", "middle_category", "small_category")
_WHITESPACE_RUN = re.compile(r"\s+")
# 分類の番号の接頭辞(`10_業務A` の `10_`)。後ろに 1 文字以上あるときだけ外す(`10_` だけの値は残す)。
# Oracle の REGEXP_REPLACE と同じ意味になるよう、Python の \d(Unicode の数字)ではなく [0-9] にする。
_CATEGORY_CODE_PREFIX = re.compile(r"^[0-9]+_(?=.)")


def normalize_category_value(value: object) -> str | None:
    """分類の値の表記をそろえる(保存・絞り込み・候補で共通。#547)。

    NFKC(全角英数・半角カナ・全角空白の統一)の後、前後の空白を除き、連続する空白を 1 つにする。
    番号の接頭辞(`10_`)は外さない。rag_poc のフォルダ名・`domain_profile.json` の語は番号で並び順を
    持つため、保存値は入力どおりに残し、比較のときだけ `category_label` で外す。空は None。
    """
    if value is None:
        return None
    text = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFKC", str(value))).strip()
    return text or None


def category_label(value: object) -> str:
    """比較用の分類の名前(正規化してから番号の接頭辞を外す)。

    rag_engine の `_category_label`(`10_業務A` と `業務A` を同じ業務として比べる)と同じ規則。
    検索の分類の絞り込み(`_classification_where`)は、この値と保存値の同じ変換を比べる。
    """
    return _CATEGORY_CODE_PREFIX.sub("", normalize_category_value(value) or "")
