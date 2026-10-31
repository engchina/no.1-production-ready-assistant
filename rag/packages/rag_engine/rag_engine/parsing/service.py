"""解析器を差し替え可能にするサービス。"""

from rag_engine.models.contracts import ParsedDocument, ParseRequest
from rag_engine.ports import Parser


class ParsingService:
    """選択された解析器に検証済み要求を渡す。"""

    def __init__(self, parser: Parser):
        self.parser = parser

    def parse(self, request: ParseRequest) -> ParsedDocument:
        """元ファイルを変更せず解析する。不存在は FileNotFoundError。"""
        if not request.source.is_file():
            raise FileNotFoundError(request.source)
        return self.parser.parse(request)
