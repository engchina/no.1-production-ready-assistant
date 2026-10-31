"""engine registry の挙動を保護するテスト。"""

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from rag_engine.adapters.parsers import ENGINE_LABELS, ENGINE_ORDER, build_adapters
from rag_engine.config import get_settings


class EngineRegistryTests(unittest.TestCase):
    def test_only_supported_engines_are_registered(self):
        self.assertEqual(
            ENGINE_ORDER,
            ["docling"],
        )
        self.assertEqual(
            [ENGINE_LABELS[engine] for engine in ENGINE_ORDER],
            ["Docling"],
        )

    def test_build_adapters_follows_engine_order(self):
        settings = replace(get_settings(dotenv_path=None), enabled_engines=["all"])

        self.assertEqual(list(build_adapters(settings)), ENGINE_ORDER)

    def test_retired_engines_cannot_be_reenabled_by_old_settings(self):
        settings = get_settings(
            dotenv_path=None,
            environ={
                "RAG_ENGINE_ENABLED_ENGINES": "docling,dots_mocr_api,mineru_api",
                "DOTS_MOCR_BASE_URL": "http://retired.invalid/v1",
                "MINERU_API_URL": "http://retired.invalid",
            },
        )
        self.assertEqual(list(build_adapters(settings)), ["docling"])
        self.assertFalse(hasattr(settings, "dots_mocr_base_url"))
        self.assertFalse(hasattr(settings, "mineru_api_url"))
        self.assertEqual(build_adapters(replace(settings, enabled_engines=["mineru_api"])), {})

    def test_default_settings_use_docling_only_pipeline(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("rag_engine.config.load_dotenv"),
        ):
            settings = get_settings(dotenv_path=None)

        self.assertEqual(settings.enabled_engines, ["docling"])
        self.assertEqual(list(build_adapters(settings)), ["docling"])


if __name__ == "__main__":
    unittest.main()
