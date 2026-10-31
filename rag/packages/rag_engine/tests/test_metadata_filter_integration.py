"""任意の ADB 接続で業務フィルタの SQL/Python 一致を検証する。SELECT のみ実行する。"""

import json
import os
import unittest
from types import SimpleNamespace

from rag_engine.adapters.oracle.connection import connect_adb_thin, load_adb_settings
from rag_engine.adapters.oracle.store import _chunks_matching_metadata_filter, _metadata_filter_sql
from rag_engine.retrieval.inquiry_conditions import InquiryMetadataFilter


@unittest.skipUnless(os.environ.get("RAG_TEST_ADB_ENV"), "RAG_TEST_ADB_ENV 未指定: ADB 統合テストは opt-in")
class MetadataFilterIntegrationTests(unittest.TestCase):
    """機密データを読み出さず、合成 JSON を DUAL 上で比較する。"""

    def test_sql_matches_business_values_with_unicode_and_escaped_literals(self):
        import oracledb

        cases = [
            ("販売管理", {"販売管理": True}, False),
            ("販売管理", {"parser": {"engine_label": "販売管理"}}, False),
            ("販売管理", {"retrieval_profile": {"business_domains": ["販売管理"]}}, True),
            ("販売管理", {"retrieval_profile": {"business_domains": "販売管理"}}, True),
            ("販売管理", {"retrieval_profile": {"business_domains": ["販売管理以外"]}}, False),
            ("販売管理", {"retrieval_profile": {"business_domains": {"販売管理": True}}}, False),
            ("販売管理", {"document": {"classification": {"large_category": "10_販売管理"}}}, True),
            ("10_販売管理", {"document": {"classification": {"large_category": "販売管理"}}}, True),
            ("操作説明書", {"retrieval_profile": {"document_kinds": ["操作説明書"]}}, True),
            ("操作説明書", {"document": {"classification": {"middle_category": "20_操作説明書"}}}, True),
            ("販売管理", {"document": {"classification": {"large_category": None}}}, False),
            ("販売管理", {}, False),
            ("a\"b\\c' OR 1=1 --", {"document": {"classification": {"small_category": "a\"b\\c' OR 1=1 --"}}}, True),
        ]
        with connect_adb_thin(load_adb_settings(os.environ["RAG_TEST_ADB_ENV"])) as connection, connection.cursor() as cursor:
            cursor.setinputsizes(payload=oracledb.DB_TYPE_CLOB)
            for term, metadata, expected in cases:
                for ensure_ascii in (False, True):
                    with self.subTest(term=term, metadata=metadata, ensure_ascii=ensure_ascii):
                        condition = InquiryMetadataFilter(metadata_terms=(term,))
                        sql, binds = _metadata_filter_sql("c", condition)
                        cursor.execute(
                            "SELECT COUNT(*) FROM (SELECT :payload AS metadata_json FROM dual) c WHERE 1=1 " + sql,
                            {"payload": json.dumps(metadata, ensure_ascii=ensure_ascii), **binds},
                        )
                        # 業務語は SQL の WHERE にしない（#848）ので SQL は常に 1 行。一致判定は Python 側だけ。
                        self.assertEqual(cursor.fetchone()[0], 1)
                        chunk = SimpleNamespace(source_file_name="manual.pdf", metadata=metadata)
                        self.assertEqual(bool(_chunks_matching_metadata_filter([chunk], condition)), expected)
