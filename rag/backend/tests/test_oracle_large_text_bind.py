"""長い文字列の bind は CLOB で送る（#2342。8192 バイト以上の VARCHAR の bind で ORA-03146 など）。

実 Oracle での保存は ``test_long_answer_save_oracle.py`` が確かめる。ここは共通の実行が bind の型を
決める規則（型を指定しない長い文字列だけを CLOB にし、短い文字列・指定済みの bind は変えない）。
"""

import oracledb

from app.clients.oracle import (
    LARGE_TEXT_BIND_BYTES,
    _execute,
    _execute_count,
    _executemany,
    _large_text_input_sizes,
)


class _Cursor:
    def __init__(self) -> None:
        self.input_sizes: dict[str, object] = {}
        self.binds: object = None
        self.rowcount = 1

    def setinputsizes(self, **sizes: object) -> None:
        self.input_sizes.update(sizes)

    def execute(self, statement: str, binds: object) -> None:
        self.binds = binds

    def executemany(self, statement: str, rows: object) -> None:
        self.binds = rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.cursors: list[_Cursor] = []

    def cursor(self) -> _Cursor:
        cursor = _Cursor()
        self.cursors.append(cursor)
        return cursor


# 日本語 1,400 字は 4,200 バイト（文字数は境目より少なく、バイト数は境目を超える）。
MULTIBYTE = "あ" * 1400


def test_large_text_input_sizes_marks_only_long_untyped_strings() -> None:
    sizes = _large_text_input_sizes(
        {
            "short": "a" * (LARGE_TEXT_BIND_BYTES - 1),
            "ascii": "a" * LARGE_TEXT_BIND_BYTES,
            "multibyte": MULTIBYTE,
            "number": 10**6,
            "typed": "b" * 9000,
        },
        {"typed": oracledb.DB_TYPE_NCLOB},
    )

    assert sizes == {
        "ascii": oracledb.DB_TYPE_CLOB,
        "multibyte": oracledb.DB_TYPE_CLOB,
        "typed": oracledb.DB_TYPE_NCLOB,
    }


def test_execute_binds_long_answer_as_clob() -> None:
    connection = _Connection()
    answer = "回答" * 5000

    _execute(
        connection,  # type: ignore[arg-type]
        "MERGE INTO t USING (SELECT :id AS id FROM dual) s ON (t.id = s.id) "
        "WHEN MATCHED THEN UPDATE SET t.answer = :answer "
        "WHEN NOT MATCHED THEN INSERT (id, answer, j) VALUES (:id, :answer, :j)",
        {"id": "x", "answer": answer, "j": "{}", "unused": "c" * 9000},
        input_sizes={"j": oracledb.DB_TYPE_CLOB},
    )

    cursor = connection.cursors[0]
    # SQL に無い bind は型の指定にも入れない（thin は DPY-4008 にする）。
    assert cursor.input_sizes == {"answer": oracledb.DB_TYPE_CLOB, "j": oracledb.DB_TYPE_CLOB}
    assert cursor.binds == {"id": "x", "answer": answer, "j": "{}"}


def test_execute_without_long_strings_does_not_set_input_sizes() -> None:
    connection = _Connection()

    _execute(connection, "UPDATE t SET a = :a", {"a": "short"})  # type: ignore[arg-type]

    assert connection.cursors[0].input_sizes == {}


def test_execute_count_binds_long_content_as_clob() -> None:
    connection = _Connection()

    count = _execute_count(
        connection,  # type: ignore[arg-type]
        "UPDATE rag_messages SET content = :content, citations_json = :citations_json "
        "WHERE message_id = :message_id",
        {"content": MULTIBYTE * 3, "citations_json": "[" + "1," * 20000 + "1]", "message_id": "m"},
    )

    assert count == 1
    assert connection.cursors[0].input_sizes == {
        "content": oracledb.DB_TYPE_CLOB,
        "citations_json": oracledb.DB_TYPE_CLOB,
    }


def test_executemany_binds_column_as_clob_when_any_row_is_long() -> None:
    connection = _Connection()
    rows = [{"text": "short"}, {"text": "a" * 9000}]

    _executemany(connection, "INSERT INTO t (text) VALUES (:text)", rows)  # type: ignore[arg-type]

    assert connection.cursors[0].input_sizes == {"text": oracledb.DB_TYPE_CLOB}
    assert connection.cursors[0].binds == rows
