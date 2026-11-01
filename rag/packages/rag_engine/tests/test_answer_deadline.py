"""回答の期限（協調的な取り消し。#1422）で、LLM の呼び出しが止まることを検証する。"""

import threading
import unittest
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from rag_engine.adapters import model_api
from rag_engine.config import get_settings
from rag_engine.generation.execution_record import _execution_step
from rag_engine.models.llm import TextOutput
from rag_engine.resources.deadline import (
    Deadline,
    DeadlineExceeded,
    bind_deadline,
    check_cancelled,
    check_deadline,
    remaining_seconds,
)

PROVIDER = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="")


def _settings(**overrides):
    base = get_settings(environ={}, dotenv_path=None)
    values = {
        "llm_request_timeout_seconds": 600,
        "llm_retries": 3,
        "llm_retry_initial_wait_seconds": 0.0,
        "llm_retry_max_wait_seconds": 0.0,
    }
    values.update(overrides)
    return replace(base, **values)


class _Responses:
    def __init__(self, owner):
        self._owner = owner

    def parse(self, **kwargs):
        self._owner.calls.append(self._owner.options)
        if self._owner.errors:
            raise self._owner.errors.pop(0)
        return SimpleNamespace(status="completed", output_parsed=TextOutput(text="ok"))


class _FakeClient:
    """OpenAI の client の代わり。`with_options` で渡した timeout / max_retries を呼び出しごとに記録する。"""

    def __init__(self, errors=(), calls=None, options=None):
        self.errors = list(errors)
        self.calls = [] if calls is None else calls
        self.options = options or {}
        self.responses = _Responses(self)

    def with_options(self, **options):
        clone = _FakeClient(calls=self.calls, options=options)
        clone.errors = self.errors
        return clone


def _parse(client, settings):
    with mock.patch.object(model_api, "_openai_client_for_provider", return_value=client):
        return model_api._parse_with_retry(
            lambda: model_api._openai_parsed([], settings, TextOutput, PROVIDER),
            "Responses text",
            settings,
            retries=model_api._llm_transport_retries(settings),
        )


class DeadlineTests(unittest.TestCase):
    def test_deadline_remaining_and_cancel_propagate_to_child(self):
        parent = Deadline(100.0)
        child = Deadline(None, parent=parent)
        self.assertLessEqual(child.remaining(), 100.0)
        self.assertFalse(child.expired)
        parent.cancel()
        self.assertEqual(child.remaining(), 0.0)
        with self.assertRaises(DeadlineExceeded):
            child.check()

    def test_without_deadline_nothing_happens(self):
        self.assertIsNone(remaining_seconds())
        check_deadline()

    def test_deadline_is_visible_in_worker_thread_with_copied_context(self):
        seen = []
        deadline = Deadline(50.0)
        with bind_deadline(deadline):
            context = copy_context()
        thread = threading.Thread(target=lambda: context.run(lambda: seen.append(remaining_seconds())))
        thread.start()
        thread.join()
        self.assertIsNotNone(seen[0])
        self.assertLessEqual(seen[0], 50.0)


class LlmCallDeadlineTests(unittest.TestCase):
    def test_without_deadline_client_is_used_as_is(self):
        client = _FakeClient()
        self.assertEqual(_parse(client, _settings()).text, "ok")
        # 期限が無ければ client の設定（LLM 1 回の timeout・SDK の再試行）を変えない。
        self.assertEqual(client.calls, [{}])

    def test_timeout_is_shrunk_to_remaining_and_sdk_retry_is_disabled(self):
        client = _FakeClient()
        with bind_deadline(Deadline(30.0)):
            self.assertEqual(_parse(client, _settings()).text, "ok")
        [options] = client.calls
        self.assertEqual(options["max_retries"], 0)
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 30.0)

    def test_cancel_only_deadline_keeps_timeout_and_disables_sdk_retry(self):
        client = _FakeClient()
        with bind_deadline(Deadline(None)):
            self.assertEqual(_parse(client, _settings()).text, "ok")
        # 時刻の期限が無くても、再試行は取り消しを確かめる側（_parse_with_retry）で行う。
        self.assertEqual(client.calls, [{"timeout": 600.0, "max_retries": 0}])

    def test_expired_deadline_does_not_call_llm(self):
        client = _FakeClient()
        with bind_deadline(Deadline(0.0)), self.assertRaises(DeadlineExceeded):
            _parse(client, _settings())
        self.assertEqual(client.calls, [])

    def test_cancelled_deadline_does_not_call_llm(self):
        client = _FakeClient()
        deadline = Deadline(300.0)
        deadline.cancel()
        with bind_deadline(deadline), self.assertRaises(DeadlineExceeded):
            _parse(client, _settings())
        self.assertEqual(client.calls, [])

    def test_retries_transport_errors_within_deadline(self):
        client = _FakeClient(errors=[TimeoutError("slow"), ConnectionError("reset")])
        with bind_deadline(Deadline(60.0)):
            self.assertEqual(_parse(client, _settings()).text, "ok")
        self.assertEqual(len(client.calls), 3)

    def test_does_not_retry_when_wait_exceeds_remaining(self):
        client = _FakeClient(errors=[TimeoutError("slow")])
        settings = _settings(llm_retry_initial_wait_seconds=30.0, llm_retry_max_wait_seconds=30.0)
        with bind_deadline(Deadline(1.0)), self.assertRaises(DeadlineExceeded):
            _parse(client, settings)
        self.assertEqual(len(client.calls), 1)

    def test_does_not_retry_after_cancel_during_call(self):
        deadline = Deadline(60.0)
        client = _FakeClient()

        class _CancelOnCall(_Responses):
            def parse(self, **kwargs):
                self._owner.calls.append(self._owner.options)
                # 呼び出しの最中に回答が打ち切られた（呼び出し側が取り消した）。
                deadline.cancel()
                raise TimeoutError("timed out")

        original = client.with_options

        def with_options(**options):
            clone = original(**options)
            clone.responses = _CancelOnCall(clone)
            return clone

        client.with_options = with_options
        with bind_deadline(deadline), self.assertRaises(DeadlineExceeded):
            _parse(client, _settings())
        self.assertEqual(len(client.calls), 1)


class StepBoundaryCancelTests(unittest.TestCase):
    """取り消した回答の thread は、次の工程を始めない（#2691）。"""

    def test_cancelled_answer_does_not_start_the_next_step(self):
        started = []
        deadline = Deadline(None)
        with bind_deadline(deadline):
            with _execution_step("工程 A"):
                started.append("A")
            deadline.cancel()
            with self.assertRaises(DeadlineExceeded), _execution_step("工程 B"):
                started.append("B")
        self.assertEqual(started, ["A"])

    def test_cancel_of_the_outer_answer_stops_a_child_deadline_step(self):
        outer = Deadline(None)
        with bind_deadline(outer), bind_deadline(Deadline(60.0, parent=outer)):
            outer.cancel()
            with self.assertRaises(DeadlineExceeded):
                check_cancelled()

    def test_expired_deadline_does_not_stop_the_step(self):
        """時刻の期限で切れただけでは工程を止めない（確かめ終わった分で確定する。#1516）。"""
        with bind_deadline(Deadline(0.0)):
            check_cancelled()
            with _execution_step("回答の確定"):
                pass

    def test_without_deadline_steps_run(self):
        check_cancelled()
        with _execution_step("工程"):
            pass


if __name__ == "__main__":
    unittest.main()
