"""生成時に決めた意味監査の扱いを、後処理と記録の再利用にも引き継ぐ。"""

from collections.abc import Mapping
from typing import Any


def semantic_audit_is_diagnostic_only(diagnostics: Mapping[str, Any]) -> bool:
    """現在の設定から過去の判定を推定せず、その回答に記録された方針を読む。"""
    policy = diagnostics.get("semantic_audit_policy")
    return isinstance(policy, Mapping) and policy.get("mode") == "diagnostic_only"
