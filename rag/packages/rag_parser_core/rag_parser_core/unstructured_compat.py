"""pi-heif を入れずに unstructured の PDF・画像の解析を動かすための互換(#3351)。

pi-heif(同梱の libheif / libde265 が LGPL-3.0)はライセンスの理由で入れない。
unstructured 0.27 の ``unstructured/partition/pdf.py`` は module の先頭で
``from pi_heif import register_heif_opener`` を import し、PDF・画像の解析のたびに呼んで
Pillow に HEIC / HEIF の opener を登録する。HEIC / HEIF はアップロードで止める(対応しない)
ので opener は要らない。pi-heif が無いときだけ、何もしない ``register_heif_opener`` を持つ
module を ``sys.modules`` に置き、PDF・画像の解析の import を満たす
(HEIC / HEIF は Pillow が開けないまま)。
"""

from __future__ import annotations

import importlib.util
import sys
import types

PI_HEIF_MODULE = "pi_heif"


def _register_heif_opener(*_args: object, **_kwargs: object) -> None:
    """HEIC / HEIF の opener を登録しない(HEIC / HEIF は対応しない)。"""


def ensure_pi_heif_absent_stub() -> bool:
    """pi-heif が無ければ stub を置く。置いたら True、本物か stub が既にあれば False。"""
    if PI_HEIF_MODULE in sys.modules:
        return False
    try:
        installed = importlib.util.find_spec(PI_HEIF_MODULE) is not None
    except (ImportError, ValueError):
        installed = False
    if installed:
        return False
    stub = types.ModuleType(PI_HEIF_MODULE)
    stub.__doc__ = "pi-heif を入れない環境の stub(rag_parser_core.unstructured_compat)。"
    stub.register_heif_opener = _register_heif_opener  # type: ignore[attr-defined]
    sys.modules[PI_HEIF_MODULE] = stub
    return True
