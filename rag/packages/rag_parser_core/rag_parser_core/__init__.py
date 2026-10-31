"""rag_parser_core — backend と parser マイクロサービスが共有する parser 契約。

- schema: `extraction`(StructuredExtraction 系)/ `source`(SourceProfile 系)
- `routing`: source kind 別 adapter 優先順
- `result`: HTTP 契約 + 共有 contract 型(`ParseResponse` / `ParserRegistryResult` / 各 backend 集合)
- `registry`: ローカル parser + 外部 adapter remap(`parse_with_registry`)= 解析実体

`registry`(解析実体)は **eager import しない**。backend は型・契約(result/source)だけを軽量に
参照し、解析コードを load・実行しない。解析実体が必要な service / offline ツールは
`from rag_parser_core.registry import parse_with_registry` を明示 import する。
外部 parser 依存(docling/unstructured/...)は registry が遅延 import する任意依存。
"""

from rag_parser_core.preprocess import (
    DEFAULT_PREPROCESS_PROFILE,
    PREPROCESS_PROFILES,
    ConvertHealth,
    ConvertOutcome,
    ConvertResponse,
    SourceDerivation,
    normalize_preprocess_profile,
    supported_profiles_from,
)
from rag_parser_core.result import ParseHealth, ParseResponse, ParserRegistryResult

__all__ = [
    "DEFAULT_PREPROCESS_PROFILE",
    "PREPROCESS_PROFILES",
    "ConvertHealth",
    "ConvertOutcome",
    "ConvertResponse",
    "ParseHealth",
    "ParseResponse",
    "ParserRegistryResult",
    "SourceDerivation",
    "normalize_preprocess_profile",
    "supported_profiles_from",
]
