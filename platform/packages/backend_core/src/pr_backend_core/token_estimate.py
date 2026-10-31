"""本文の入力 token の見積もり（3 製品で共通の 1 つ。#1437・#3321）。

`pr_system_settings.model` の入力の予算（`input_token_budget`）と、検索の結果・観測の本文の大きさの
規則（`pr_backend_core.source_text`）が同じ見積もりを使う。
"""


def estimate_input_tokens(text: str) -> int:
    """本文の入力 token の見積もり。

    モデルごとの tokenizer を持たないため、多めに見積もる: ASCII は 4 字で 1 token、それ以外
    （かな・漢字など）は 1 字で 1 token。見積もりは文字数を超えないので、文字の予算を token の
    予算以下にすれば token も予算を超えない。
    """
    value = text or ""
    ascii_count = sum(1 for char in value if ord(char) < 0x80)
    return (len(value) - ascii_count) + (ascii_count + 3) // 4
