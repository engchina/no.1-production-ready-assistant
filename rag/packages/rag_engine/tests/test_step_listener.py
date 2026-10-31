"""工程の開始・終了の通知(進捗の表示。#593)を検証する。"""

import unittest

from rag_engine.generation.execution_record import _execution_step, bind_step_listener


class StepListenerTests(unittest.TestCase):
    def test_listener_receives_nested_steps_in_order(self):
        events = []
        with (
            bind_step_listener(lambda name, outcome, elapsed, record: events.append((name, outcome))),
            _execution_step("回答生成フロー"),
            _execution_step("文書検索"),
        ):
            pass
        self.assertEqual(
            events,
            [
                ("回答生成フロー", "started"),
                ("文書検索", "started"),
                ("文書検索", "success"),
                ("回答生成フロー", "success"),
            ],
        )

    def test_failed_step_is_reported_as_error_and_still_raises(self):
        events = []
        with (
            bind_step_listener(lambda name, outcome, elapsed, record: events.append((name, outcome))),
            self.assertRaises(ValueError),
            _execution_step("文書検索"),
        ):
            raise ValueError("boom")
        self.assertEqual(events, [("文書検索", "started"), ("文書検索", "error")])

    def test_listener_failure_does_not_stop_the_step(self):
        def broken(name, outcome, elapsed, record):
            raise RuntimeError("listener down")

        ran = []
        with bind_step_listener(broken), _execution_step("文書検索"):
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_finished_step_passes_its_details_llm_calls_and_status(self):
        """終わった工程の通知に、その工程の詳細・LLM の回数・結果の説明を渡す（実行中から段を開ける。#2373）。"""
        events = []
        with (
            bind_step_listener(lambda name, outcome, elapsed, record: events.append((name, outcome, record))),
            _execution_step("質問拡張戦略", "目的の文") as step,
        ):
            step.field("設定", "単純検索")
            step.status = "省略"
        started, finished = events
        self.assertEqual(started, ("質問拡張戦略", "started", {}))
        self.assertEqual(finished[1], "success")
        self.assertEqual(
            finished[2],
            {
                "details": {"fields": [{"label": "目的", "value": "目的の文"}, {"label": "設定", "value": "単純検索"}]},
                "llm_calls": 0,
                "status": "省略",
            },
        )

    def test_no_listener_outside_binding(self):
        events = []
        with bind_step_listener(lambda name, outcome, elapsed, record: events.append(name)):
            pass
        with _execution_step("文書検索"):
            pass
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
