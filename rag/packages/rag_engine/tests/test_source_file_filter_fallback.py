"""知識ベースに無い資料名の条件で検索が 0 件にならないことを確かめる (#1082)。"""

from rag_engine.adapters.oracle.store import _resolved_metadata_filter
from rag_engine.retrieval.inquiry_conditions import InquiryMetadataFilter


class _Cursor:
    """一致する文書があるかの 1 行問い合わせだけを模す。"""

    def __init__(self, rows, executed):
        self._rows, self._executed, self._result = rows, executed, None

    def execute(self, sql, binds):
        self._executed.append((sql, binds))
        self._result = self._rows

    def fetchone(self):
        return (1,) if self._result else None

    def close(self):
        pass


class _Connection:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def cursor(self):
        return _Cursor(self.rows, self.executed)


def test_unmatched_source_file_terms_are_dropped_with_their_page_numbers():
    condition = InquiryMetadataFilter(source_file_terms=("手元の資料.pdf",), metadata_terms=("販売管理",), page_numbers=(3,))

    resolved = _resolved_metadata_filter(_Connection(rows=False), "", condition, retrieval_scope="knowledge_base")

    assert resolved.source_file_terms == ()
    assert resolved.page_numbers == ()
    # 業務語は SQL の WHERE ではなく融合後の加点なので、落とさない (#848)。
    assert resolved.metadata_terms == ("販売管理",)


def test_matched_source_file_terms_are_kept():
    condition = InquiryMetadataFilter(source_file_terms=("地区別売上集計表.pdf",), page_numbers=(3,))

    resolved = _resolved_metadata_filter(_Connection(rows=True), "", condition, retrieval_scope="knowledge_base")

    assert resolved is condition


def test_filter_without_source_file_terms_is_returned_without_querying():
    condition = InquiryMetadataFilter(metadata_terms=("販売管理",))
    connection = _Connection(rows=False)

    assert _resolved_metadata_filter(connection, "", condition, retrieval_scope="knowledge_base") is condition
    assert connection.executed == []
