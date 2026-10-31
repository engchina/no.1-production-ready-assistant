"""backend ランタイムが解析実体(rag_parser_core.registry)を load しないことを固定する。

方針: 解析・前処理は parser マイクロサービスへ委譲し、backend(`app.main` グラフ)では
解析コードを import・実行しない。`rag_parser_core.registry`(local parser + 外部 adapter remap)が
`app.main` の import グラフに含まれていないことを、汚染のないサブプロセスで検証する。
"""

import subprocess
import sys


def test_app_main_does_not_load_parser_registry() -> None:
    code = (
        "import app.main, sys; "
        "loaded = 'rag_parser_core.registry' in sys.modules; "
        "sys.exit(0 if not loaded else 1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "backend(app.main)が rag_parser_core.registry(解析実体)を load しています。"
        "解析は parser マイクロサービスへ委譲し、backend は型・契約(result/source)だけを"
        f"参照してください。\nstderr:\n{result.stderr}"
    )
