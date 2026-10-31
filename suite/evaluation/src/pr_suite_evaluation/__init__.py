"""製品横断の評価（suite の評価。#3240）。

Dataset（`dataset`）・Target（`targets`）・Observation（`observation`）・Evaluator（`evaluators`）・
Experiment（`experiment`）・Report（`report`）を分ける。製品は公開の MCP の契約だけで呼び、製品の
Python の module・DB・保存ファイルは読まない。採点の部品は platform の `pr_evaluation_scoring`。
"""
