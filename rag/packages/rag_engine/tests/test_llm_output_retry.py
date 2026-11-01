"""出力の上限で未完了になった応答の送り直しの記録（#1527）を、OpenAI の client を差し替えて確かめる。

上限（`answer_max_tokens` とモデルの出力の上限の小さい方。#1719）で未完了なら上限を 2 倍にして 1 回だけ送り直す
（#987）。2 倍はモデルの出力の上限で抑え、広げられなければ送り直さない（#1719）。その回数と未完了の理由を、
工程の記録（execution_steps の llm_usage）と log（`llm_call_usage`）に残す。
"""

import unittest
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from rag_engine.adapters import model_api
from rag_engine.config import get_settings
from rag_engine.generation import execution_record
from rag_engine.models.llm import TextOutput
from rag_engine.resources.deadline import Deadline, DeadlineExceeded, bind_deadline
from rag_engine.resources.llm_usage import LlmUsage, llm_usage, usage_line

PROVIDER = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="")
MEDIUM_PROVIDER = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="medium")
USAGE = SimpleNamespace(input_tokens=100, output_tokens=50, input_tokens_details=None, output_tokens_details=None)


class _Client:
    """`responses.parse` が順に応答を返し、渡した出力の上限を記録する。"""

    def __init__(self, responses):
        self.responses_left, self.budgets, self.efforts = list(responses), [], []
        self.responses = SimpleNamespace(parse=self._parse)

    def with_options(self, **_options):
        return self

    def _parse(self, **kwargs):
        self.budgets.append(kwargs["max_output_tokens"])
        self.efforts.append((kwargs.get("reasoning") or {}).get("effort", ""))
        response = self.responses_left.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _incomplete(reason):
    return SimpleNamespace(status="incomplete", incomplete_details=SimpleNamespace(reason=reason), usage=USAGE)


def _completed():
    return SimpleNamespace(status="completed", output_parsed=TextOutput(text="ok"), usage=USAGE)


class OutputRetryRecordTest(unittest.TestCase):
    def call(self, client, provider=PROVIDER):
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        steps: list = []
        step_token = execution_record._structured_steps.set(steps)
        usage_token = llm_usage.set(LlmUsage())
        try:
            with (
                mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
                self.assertLogs(model_api.__name__, level="INFO") as logs,
                execution_record._execution_step("生成"),
                suppress(model_api.IncompleteResponse),
            ):
                model_api._openai_parsed([], settings, TextOutput, provider)
        finally:
            llm_usage.reset(usage_token)
            execution_record._structured_steps.reset(step_token)
        return steps[0]["llm_usage"], logs.records

    def test_retry_after_the_output_limit_is_recorded(self):
        client = _Client([_incomplete("max_output_tokens"), _completed()])
        usage, records = self.call(client)
        self.assertEqual(client.budgets, [1000, 2000])
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["incomplete_responses"], 1)
        self.assertEqual(usage["output_retries"], 1)
        self.assertEqual(usage["incomplete_reasons"], {"max_output_tokens": 1})
        self.assertIn("未完了の応答 1 回（max_output_tokens 1 回）・出力の上限を広げた送り直し 1 回", usage_line(usage))
        self.assertEqual(
            [
                (r.llm_status, r.llm_incomplete_reason, r.llm_attempt, r.llm_max_output_tokens)
                for r in records
                if getattr(r, "event", "") == "llm_call_usage"
            ],
            [("incomplete", "max_output_tokens", 0, 1000), ("completed", "", 1, 2000)],
        )
        # 未完了は理由・上限・推論の量・次にすることを WARNING で残す（#2023。INFO が出ない配備でも残る）。
        warned = [r for r in records if getattr(r, "event", "") == "llm_incomplete_response"]
        self.assertEqual(
            [(r.levelname, r.llm_incomplete_reason, r.llm_max_output_tokens, r.llm_next_action) for r in warned],
            [("WARNING", "max_output_tokens", 1000, "widen")],
        )

    def test_other_incomplete_reasons_are_recorded_without_retry(self):
        client = _Client([_incomplete("content_filter")])
        usage, _records = self.call(client)
        self.assertEqual(client.budgets, [1000])
        self.assertEqual(usage["incomplete_reasons"], {"content_filter": 1})
        self.assertNotIn("output_retries", usage)

    def test_corrupted_generated_text_retries_without_repairing_characters(self):
        corrupted = SimpleNamespace(status="completed", output_parsed=TextOutput(text="説明\ufffd"), usage=USAGE)
        client = _Client([corrupted, _completed()])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with mock.patch.object(model_api, "_openai_client_for_provider", return_value=client):
            result = model_api._parse_text_with_retry("system", "prompt", settings, TextOutput, PROVIDER)
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(client.budgets), 2)

    def test_repeated_corruption_fails_with_existing_retry_limit(self):
        corrupted = SimpleNamespace(status="completed", output_parsed=TextOutput(text="説明\ufffd"), usage=USAGE)
        client = _Client([corrupted, corrupted])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertRaises(model_api.InvalidStructuredOutput),
        ):
            model_api._parse_text_with_retry("system", "prompt", settings, TextOutput, PROVIDER)
        self.assertEqual(len(client.budgets), 2)

    def test_rejected_corruption_names_the_fields_but_not_the_text(self):
        # 退けた出力の原文は残らないので、どの欄に置換文字があったかを例外と log に残す（#3849）。本文は残さない。
        corrupted = SimpleNamespace(status="completed", output_parsed=TextOutput(text="合成の説明�"), usage=USAGE)
        client = _Client([corrupted, corrupted])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="WARNING") as logs,
            self.assertRaises(model_api.InvalidStructuredOutput) as raised,
        ):
            model_api._parse_text_with_retry("system", "prompt", settings, TextOutput, PROVIDER)
        self.assertIn("欄: text", str(raised.exception))
        self.assertNotIn("合成の説明", str(raised.exception))
        rejected = [record for record in logs.records if getattr(record, "event", "") == "llm_output_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertEqual(rejected[0].llm_rejected_paths, ["text"])
        self.assertEqual(rejected[0].llm_rejected_reason, "replacement_character")
        self.assertNotIn("合成の説明", rejected[0].getMessage())

    def test_replacement_char_paths_point_into_nested_lists(self):
        value = {"request_coverage": [{"evidence_scope": [{"target": "a"}, {"target": "b�"}]}], "items": [{"text": "�"}]}
        self.assertEqual(model_api.replacement_char_paths(value), ["request_coverage[0].evidence_scope[1].target", "items[0].text"])
        self.assertEqual(model_api.replacement_char_paths({"xs": ["�"] * 20}, limit=2), ["xs[0]", "xs[1]"])

    def test_original_replacement_character_is_preserved(self):
        corrupted = SimpleNamespace(status="completed", output_parsed=TextOutput(text="原文\ufffd"), usage=USAGE)
        client = _Client([corrupted])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with mock.patch.object(model_api, "_openai_client_for_provider", return_value=client):
            result = model_api._parse_text_with_retry("system", "引用: 原文\ufffd", settings, TextOutput, PROVIDER)
        self.assertEqual(result.text, "原文\ufffd")
        self.assertEqual(len(client.budgets), 1)

    def test_text_call_reaches_the_responses_api(self):
        """text の呼び出し（`_parse_text_with_retry` → `_parse_openai_text_once`）が Responses API まで届く。"""
        client = _Client([_completed()])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
        ):
            result = model_api._parse_text_with_retry("system", "prompt", settings, TextOutput, PROVIDER)
        self.assertEqual((result.text, client.budgets), ("ok", [1000]))

    def test_retry_is_capped_by_the_model_output_limit(self):
        """送り直しの 2 倍は、モデルの出力の上限で抑える（#1719）。"""
        client = _Client([_incomplete("max_output_tokens"), _completed()])
        provider = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="", max_output_tokens=1500)
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
        ):
            model_api._openai_parsed([], settings, TextOutput, provider)
        self.assertEqual(client.budgets, [1000, 1500])

    def test_first_budget_is_capped_and_not_retried_when_it_cannot_widen(self):
        """最初の上限はモデルの出力の上限で抑え、それ以上に広げられなければ送り直さない（#1719）。

        失敗の文には、理由・送った上限・使った出力（うち推論）の token を出す。
        """
        client = _Client([_incomplete("max_output_tokens"), _completed()])
        provider = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="", max_output_tokens=1000)
        settings = get_settings(environ={}, dotenv_path=None)
        self.assertEqual(settings.answer_max_tokens, 32768)
        usage_token = llm_usage.set(LlmUsage())
        try:
            with (
                mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
                self.assertLogs(model_api.__name__, level="INFO"),
                self.assertRaises(model_api.IncompleteResponse) as raised,
            ):
                model_api._openai_parsed([], settings, TextOutput, provider)
            usage = llm_usage.get().snapshot()
        finally:
            llm_usage.reset(usage_token)
        self.assertEqual(client.budgets, [1000])
        self.assertNotIn("output_retries", usage)
        message = str(raised.exception)
        self.assertEqual(raised.exception.reason, "max_output_tokens")
        for expected in ("stub-model", "理由=max_output_tokens", "出力の上限=1000 tokens", "使った出力=50 tokens（うち推論 0）"):
            self.assertIn(expected, message)
        self.assertNotIn("送り直しても", message)

    def test_incomplete_after_retry_reports_the_widened_limit(self):
        client = _Client([_incomplete("max_output_tokens"), _incomplete("max_output_tokens")])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
            self.assertRaises(model_api.IncompleteResponse) as raised,
        ):
            model_api._openai_parsed([], settings, TextOutput, PROVIDER)
        self.assertEqual(client.budgets, [1000, 2000])
        self.assertIn("上限を広げて送り直しても", str(raised.exception))
        self.assertIn("出力の上限=2000 tokens", str(raised.exception))

    def test_reasoning_effort_is_lowered_after_the_widened_limit(self):
        """上限を広げても未完了なら、推論の量を 1 段下げて（medium → low）もう 1 回だけ送り直す（#2023）。"""
        client = _Client([_incomplete("max_output_tokens"), _incomplete("max_output_tokens"), _completed()])
        usage, records = self.call(client, provider=MEDIUM_PROVIDER)
        self.assertEqual(client.budgets, [1000, 2000, 2000])
        self.assertEqual(client.efforts, ["medium", "medium", "low"])
        self.assertEqual((usage["output_retries"], usage["effort_retries"]), (1, 1))
        self.assertIn("推論の量を下げた送り直し 1 回", usage_line(usage))
        warned = [r for r in records if getattr(r, "event", "") == "llm_incomplete_response"]
        self.assertEqual(
            [(r.llm_reasoning_effort, r.llm_next_action) for r in warned],
            [("medium", "widen"), ("medium", "lower_effort")],
        )

    def test_reasoning_effort_is_lowered_when_the_limit_cannot_widen(self):
        """モデルの出力の上限より広げられないときは、すぐ推論の量を下げて送り直す。"""
        client = _Client([_incomplete("max_output_tokens"), _completed()])
        provider = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="high", max_output_tokens=1000)
        self.call(client, provider=provider)
        self.assertEqual(client.budgets, [1000, 1000])
        self.assertEqual(client.efforts, ["high", "medium"])

    def test_incomplete_after_both_retries_names_the_fixes(self):
        """送り直しても未完了なら、利用者向けの文（何が起きたかと直し方）と「詳細」の文を分けて送出する。"""
        client = _Client([_incomplete("max_output_tokens")] * 3)
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
            self.assertRaises(model_api.IncompleteResponse) as raised,
        ):
            model_api._openai_parsed([], settings, TextOutput, MEDIUM_PROVIDER)
        error = raised.exception
        self.assertEqual(client.efforts, ["medium", "medium", "low"])
        self.assertIn("回答が長くなり", error.user_message)
        self.assertIn("出力の上限を広げ、推論の量を下げて送り直しても", error.user_message)
        self.assertIn("「出力の上限（tokens）」を大きくする", error.user_message)
        self.assertIn("質問を分けて", error.user_message)
        # 最後に送った low より下げられないので、直し方に推論の量を入れない。技術的な値は「詳細」の文だけ。
        self.assertNotIn("「推論の量」を下げる", error.user_message)
        self.assertNotIn("tokens（うち推論", error.user_message)
        self.assertIn("出力の上限=2000 tokens", error.detail)
        self.assertIn("推論の量=low", error.detail)

    def test_low_reasoning_effort_is_not_lowered_further(self):
        """low は下げない（none は「送らない」で endpoint の既定になり、推論が増えることがある）。"""
        client = _Client([_incomplete("max_output_tokens"), _incomplete("max_output_tokens"), _completed()])
        provider = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="low")
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
            self.assertRaises(model_api.IncompleteResponse),
        ):
            model_api._openai_parsed([], settings, TextOutput, provider)
        self.assertEqual(client.efforts, ["low", "low"])

    def test_bound_output_cap_limits_the_budget_and_never_widens(self):
        """役目に合わせた上限（`bind_output_cap`）は最初の上限を抑え、上限を広げて送り直さない。"""
        client = _Client([_incomplete("max_output_tokens"), _incomplete("max_output_tokens"), _completed()])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
            self.assertRaises(model_api.IncompleteResponse),
            model_api.bind_output_cap(300),
        ):
            model_api._openai_parsed([], settings, TextOutput, MEDIUM_PROVIDER)
        # 広げずに推論の量を 1 段下げて 1 回だけ送り直し、それでも未完了なら失敗にする。
        self.assertEqual(client.budgets, [300, 300])
        self.assertEqual(client.efforts, ["medium", "low"])

    def test_output_cap_is_dropped_outside_the_context(self):
        client = _Client([_completed()])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with model_api.bind_output_cap(300):
            pass
        with mock.patch.object(model_api, "_openai_client_for_provider", return_value=client), model_api.bind_output_cap(0):
            model_api._openai_parsed([], settings, TextOutput, PROVIDER)
        self.assertEqual(client.budgets, [1000])

    def test_content_filter_is_not_retried_and_says_so(self):
        client = _Client([_incomplete("content_filter")])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
            self.assertRaises(model_api.IncompleteResponse) as raised,
        ):
            model_api._openai_parsed([], settings, TextOutput, MEDIUM_PROVIDER)
        self.assertEqual(client.budgets, [1000])
        self.assertIn("安全の判定", raised.exception.user_message)

    def test_completed_call_keeps_the_usage_shape(self):
        usage, _records = self.call(_Client([_completed()]))
        self.assertEqual(set(usage), {"calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "seconds"})


class DeadlineOutputBudgetTest(unittest.TestCase):
    """回答の期限の残りに合わせた出力の上限（#1548）。"""

    def setUp(self):
        model_api._output_rates.clear()
        self.addCleanup(model_api._output_rates.clear)

    def test_budget_is_kept_without_deadline_or_measured_rate(self):
        self.assertEqual(model_api.deadline_output_budget(1000, PROVIDER), (1000, False))
        with bind_deadline(Deadline(10.0)):
            # 速さの実測が無ければ抑えない。
            self.assertEqual(model_api.deadline_output_budget(1000, PROVIDER), (1000, False))
        model_api._note_output_rate(PROVIDER, 4000, 100.0)  # 40 token/秒
        self.assertEqual(model_api.deadline_output_budget(1000, PROVIDER), (1000, False))

    def test_budget_is_capped_by_the_fastest_rate_with_margin(self):
        model_api._note_output_rate(PROVIDER, 3000, 100.0)  # 30 token/秒
        model_api._note_output_rate(PROVIDER, 4000, 100.0)  # 40 token/秒
        model_api._note_output_rate(PROVIDER, 100, 0.5)  # 出力の短い呼び出しは速さに数えない
        with bind_deadline(Deadline(10.0)):
            budget, capped = model_api.deadline_output_budget(24576, PROVIDER)
        self.assertTrue(capped)
        # 40 token/秒 × 1.25 × 残り（約 10 秒）。
        self.assertLessEqual(budget, 500)
        self.assertGreater(budget, 490)
        # 別のモデルの速さは使わない。
        with bind_deadline(Deadline(10.0)):
            self.assertEqual(model_api.deadline_output_budget(24576, SimpleNamespace(model="other")), (24576, False))

    def test_capped_incomplete_response_cuts_the_deadline_without_retry(self):
        model_api._note_output_rate(PROVIDER, 4000, 100.0)
        client = _Client([_incomplete("max_output_tokens"), _completed()])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=24576)
        outer = Deadline(600.0)
        child = Deadline(10.0, parent=outer)
        usage_token = llm_usage.set(LlmUsage())
        try:
            with (
                mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
                self.assertLogs(model_api.__name__, level="INFO") as logs,
                bind_deadline(child),
                self.assertRaises(DeadlineExceeded),
            ):
                model_api._openai_parsed([], settings, TextOutput, PROVIDER)
            usage = llm_usage.get().snapshot()
        finally:
            llm_usage.reset(usage_token)
        # 残りの時間で出せる分で抑え、届いたら送り直さない。子の期限を切り、外側は残す（確かめ終わった分で確定する）。
        self.assertEqual(len(client.budgets), 1)
        self.assertLess(client.budgets[0], 24576)
        self.assertTrue(child.cut_short)
        self.assertFalse(outer.expired)
        self.assertEqual(usage["incomplete_reasons"], {"max_output_tokens": 1})
        self.assertNotIn("output_retries", usage)
        self.assertTrue(logs.records[0].llm_deadline_capped)

    def test_uncapped_incomplete_response_is_still_retried_once(self):
        client = _Client([_incomplete("max_output_tokens"), _completed()])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        with (
            mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
            self.assertLogs(model_api.__name__, level="INFO"),
            bind_deadline(Deadline(600.0)),
        ):
            model_api._openai_parsed([], settings, TextOutput, PROVIDER)
        self.assertEqual(client.budgets, [1000, 2000])

    def test_failed_call_is_recorded_with_seconds_and_reason(self):
        client = _Client([TimeoutError("read timeout")])
        settings = replace(get_settings(environ={}, dotenv_path=None), answer_max_tokens=1000)
        usage_token = llm_usage.set(LlmUsage())
        try:
            with (
                mock.patch.object(model_api, "_openai_client_for_provider", return_value=client),
                self.assertLogs(model_api.__name__, level="INFO") as logs,
                self.assertRaises(TimeoutError),
            ):
                model_api._openai_parsed([], settings, TextOutput, PROVIDER)
            usage = llm_usage.get().snapshot()
        finally:
            llm_usage.reset(usage_token)
        self.assertEqual((usage["calls"], usage["output_tokens"], usage["incomplete_reasons"]), (1, 0, {"TimeoutError": 1}))
        logged = logs.records[0]
        self.assertEqual((logged.llm_status, logged.llm_incomplete_reason, logged.llm_max_output_tokens), ("failed", "TimeoutError", 1000))
        self.assertFalse(hasattr(logged, "llm_deadline_capped"))


if __name__ == "__main__":
    unittest.main()
