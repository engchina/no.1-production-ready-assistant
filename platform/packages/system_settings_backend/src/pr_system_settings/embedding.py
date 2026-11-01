"""埋め込みの次元の正本（#46）。

3 製品の Oracle のベクトル列（`VECTOR(EMBEDDING_DIMENSIONS, FLOAT32)`）・埋め込みの呼び出しの
`dimensions`・返った次元の検証・画面の表示は、この 1 つの値を使う。既定は Aliyun 百煉の
`text-embedding-v4` の既定の出力次元（1024）。

Oracle の列は固定の次元で、値を変えるとベクトルの列と索引の作り直しと全件の再埋め込みが要るため、
設定（`.env`・設定画面）では変えられない値にする。platform を import できない層（RAG の
`rag_engine`、SQL の DDL のファイル）は同じ値を持ち、一致をテストで確かめる。
"""

from __future__ import annotations

from typing import Final

EMBEDDING_DIMENSIONS: Final = 1024
EMBEDDING_VECTOR_COLUMN_TYPE: Final = f"VECTOR({EMBEDDING_DIMENSIONS}, FLOAT32)"
