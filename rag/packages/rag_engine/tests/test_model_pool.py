"""model pool の挙動を保護するテスト。"""

import unittest

import rag_engine.resources.model_pool as model_pool


class ModelPoolTests(unittest.TestCase):
    def setUp(self):
        model_pool.unload_all()

    def tearDown(self):
        model_pool.unload_all()

    def test_get_loads_once_and_reuses(self):
        calls = []
        first = model_pool.get("engine_a", lambda: calls.append(1) or object(), signature="v1")
        second = model_pool.get("engine_a", lambda: calls.append(1) or object(), signature="v1")
        self.assertIs(first, second)
        self.assertEqual(calls, [1])
        self.assertEqual(model_pool.loaded(), ["engine_a"])

    def test_signature_change_reloads(self):
        first = model_pool.get("engine_a", object, signature="v1")
        second = model_pool.get("engine_a", object, signature="v2")
        self.assertIsNot(first, second)

    def test_unload_calls_unloader_and_forgets(self):
        released = []
        model_pool.get("engine_a", object, unloader=lambda: released.append(True))
        self.assertTrue(model_pool.unload("engine_a"))
        self.assertEqual(released, [True])
        self.assertEqual(model_pool.loaded(), [])
        self.assertFalse(model_pool.unload("engine_a"))

    def test_gpu_summary_is_text(self):
        self.assertIsInstance(model_pool.gpu_summary(), str)


if __name__ == "__main__":
    unittest.main()
