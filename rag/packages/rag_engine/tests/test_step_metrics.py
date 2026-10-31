"""工程の記録に、呼び出し側の累積の計測値（DB の SQL の回数など）の差分を足すこと（#1490）を検証する。"""

import unittest

from rag_engine.generation.execution_record import _execution_step, _structured_steps, bind_step_metrics


class StepMetricsTests(unittest.TestCase):
    def _run(self, provider, body):
        steps = []
        token = _structured_steps.set(steps)
        try:
            with bind_step_metrics(provider):
                body()
        finally:
            _structured_steps.reset(token)
        return steps

    def test_step_records_delta_of_cumulative_metrics(self):
        counters = {"db_execute_count": 3, "db_seconds": 0.5}

        def body():
            with _execution_step("回答生成フロー"):
                counters["db_execute_count"] += 1
                with _execution_step("文書検索"):
                    counters["db_execute_count"] += 2
                    counters["db_seconds"] += 0.25

        steps = self._run(lambda: dict(counters), body)
        by_name = {step["name"]: step for step in steps}
        self.assertEqual(by_name["文書検索"]["db_execute_count"], 2)
        self.assertEqual(by_name["文書検索"]["db_seconds"], 0.25)
        # 外側の工程は内側の分を含む。
        self.assertEqual(by_name["回答生成フロー"]["db_execute_count"], 3)
        # 入れ子の深さ（画面の処理の経過の字下げ。#2096）。
        self.assertEqual(by_name["回答生成フロー"]["depth"], 0)
        self.assertEqual(by_name["文書検索"]["depth"], 1)

    def test_without_provider_step_has_no_metrics(self):
        steps = []
        token = _structured_steps.set(steps)
        try:
            with _execution_step("文書検索"):
                pass
        finally:
            _structured_steps.reset(token)
        self.assertEqual(set(steps[0]), {"name", "depth", "status", "elapsed_seconds", "llm_calls"})

    def test_failing_provider_does_not_stop_the_step(self):
        def broken():
            raise RuntimeError("metrics down")

        ran = []

        def body():
            with _execution_step("文書検索"):
                ran.append(True)

        steps = self._run(broken, body)
        self.assertEqual(ran, [True])
        self.assertEqual(steps[0]["status"], "complete")
        self.assertNotIn("db_execute_count", steps[0])


if __name__ == "__main__":
    unittest.main()
