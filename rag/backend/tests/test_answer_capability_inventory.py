"""検索・回答の能力の一覧(``rag/docs/answer-capability-inventory.md``。#3262)をコードとそろえる。

- 診断の ``degraded_steps`` の処理の種類(#3259)は、文書の「``degraded_steps`` の処理の種類」の
  表と一致する(``record_degraded("…")`` / ``report_degraded("…")`` の文字列)。種類を足したら、表に
  足すまで失敗する。
- 文書が示すコードの file と関数・class の名前は、コードにある(名前を変えたら文書も直す)。

コードは読むだけで、import も実行もしない(AST と文字列)。
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "rag/docs/answer-capability-inventory.md"
# degraded_steps を記録するコード(backend の回答と rag_engine)。
CODE_ROOTS = (
    REPO / "rag/backend/app",
    REPO / "rag/packages/rag_engine/rag_engine",
)
RECORDERS = frozenset({"record_degraded", "report_degraded"})
KINDS_HEADING = "## `degraded_steps` の処理の種類"
KIND_ROW = re.compile(r"^\| `([a-z][a-z0-9_]*)` \|", re.MULTILINE)
CODE_REF = re.compile(r"`(rag/[\w./-]+\.py)(?:::([A-Za-z_][\w.]*))?`")


def _recorder_name(node: ast.Call) -> str | None:
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    return name if name in RECORDERS else None


def _code_kinds() -> tuple[set[str], list[str]]:
    """コードが記録する処理の種類と、種類を文字列の定数で渡していない呼び出しの場所。"""
    kinds: set[str] = set()
    dynamic: list[str] = []
    for root in CODE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            # 記録の関数の中の転送(``current.report_degraded(kind, exc)``)は数えない。
            forwarders = {
                id(node)
                for function in ast.walk(tree)
                if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
                and function.name in RECORDERS
                for node in ast.walk(function)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _recorder_name(node) is None:
                    continue
                if id(node) in forwarders:
                    continue
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    kinds.add(first.value)
                else:
                    dynamic.append(f"{path.relative_to(REPO)}:{node.lineno}")
    return kinds, dynamic


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _doc_kinds() -> set[str]:
    text = _doc_text()
    assert KINDS_HEADING in text, f"{DOC.name} に「{KINDS_HEADING}」の節が無い"
    section = text.split(KINDS_HEADING, 1)[1].split("\n## ", 1)[0]
    return set(KIND_ROW.findall(section))


def test_degraded_kinds_in_code_are_documented() -> None:
    code, dynamic = _code_kinds()
    assert code, "record_degraded / report_degraded の呼び出しが見つからない(検査が壊れている)"
    # 種類を変数で渡すと、この検査で数えられない。文字列の定数で渡す。
    assert not dynamic, f"処理の種類を文字列の定数で渡していない呼び出し: {dynamic}"
    documented = _doc_kinds()
    missing = sorted(code - documented)
    stale = sorted(documented - code)
    assert not missing, f"コードが degraded_steps に記録する種類が {DOC.name} の表に無い: {missing}"
    assert not stale, f"{DOC.name} の表に、コードが記録しない種類がある: {stale}"


def test_code_references_in_document_exist() -> None:
    text = _doc_text()
    refs = CODE_REF.findall(text)
    assert refs, "文書にコードの参照が無い(検査が壊れている)"
    problems: list[str] = []
    for relative, name in sorted(set(refs)):
        path = REPO / relative
        if not path.is_file():
            problems.append(f"file が無い: {relative}")
            continue
        if not name:
            continue
        source = path.read_text(encoding="utf-8")
        for part in name.split("."):
            if not re.search(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(part)}\b", source, re.M):
                problems.append(f"{relative} に {part} の定義が無い({name})")
    assert not problems, "\n".join(problems)
