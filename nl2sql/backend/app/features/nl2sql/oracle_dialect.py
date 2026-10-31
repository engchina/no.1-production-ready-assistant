"""Oracle の変換エラー既定値を、省略せず AST に保持する dialect。"""

from __future__ import annotations

from typing import cast

from sqlglot import exp
from sqlglot.dialects.oracle import Oracle
from sqlglot.generators.oracle import OracleGenerator
from sqlglot.parsers.oracle import OracleParser
from sqlglot.tokenizer_core import TokenType


class ConversionDefault(exp.Expression):
    arg_types = {"this": True, "default": True}


class OracleConversion(Oracle):
    class Parser(OracleParser):
        FUNCTION_PARSERS = {
            **Oracle.Parser.FUNCTION_PARSERS,
            "TO_DATE": lambda self: self._parse_date_conversion(),
        }

        def _parse_date_conversion(self) -> exp.Expression:
            value = self._parse_assignment()
            if value is None:
                self.raise_error("Expected TO_DATE input")
            if self._match(TokenType.DEFAULT):
                fallback = self._parse_assignment()
                if fallback is None or not self._match_text_seq("ON", "CONVERSION", "ERROR"):
                    self.raise_error("Expected DEFAULT expression ON CONVERSION ERROR")
                value = self.expression(ConversionDefault(this=value, default=fallback))
            args = [value]
            while self._match(TokenType.COMMA):
                argument = self._parse_assignment()
                if argument is None:
                    self.raise_error("Expected TO_DATE argument")
                args.append(argument)
            if len(args) > 3:
                self.raise_error("TO_DATE accepts at most three arguments")
            if not isinstance(value, ConversionDefault):
                builder = self.FUNCTIONS["TO_DATE"]
                try:
                    return cast(exp.Expression, self.validate_expression(builder(args), args))
                except TypeError:
                    return cast(
                        exp.Expression,
                        self.validate_expression(builder(args, dialect=self.dialect), args),
                    )
            # Anonymous は引数の子ノードを保つ。既定値の関数・列も通常の安全検査の対象になる。
            return self.expression(exp.Anonymous(this="TO_DATE", expressions=args))

    class Generator(OracleGenerator):
        TRANSFORMS = {
            **Oracle.Generator.TRANSFORMS,
            ConversionDefault: lambda self, expression: (
                f"{self.sql(expression, 'this')} DEFAULT "
                f"{self.sql(expression, 'default')} ON CONVERSION ERROR"
            ),
        }
