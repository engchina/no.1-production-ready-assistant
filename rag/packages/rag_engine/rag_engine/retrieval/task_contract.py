"""原質問の明示的な目的と条件を固定し、検索語の意味変化を記録する。"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from rag_parser_core.language import FIELD_SPECS, LanguagePack, current_pack, per_pack
from rag_parser_core.language.text import alternation, char_class, range_class

from rag_engine.retrieval.definition_evidence import definition_labels
from rag_engine.retrieval.enumeration import ENUMERATION_ASPECT, enumeration_request

# 質問の理解の手がかりの語は言語資源（question_cues。#2181 段 4）。このモジュールは語を組み立てる文法だけを持つ。
# 業務固有の語（自治体・業種の名前など）はコードにも言語資源にも置かず、profile（requester_terms など）で足す (#849)。


@dataclass(frozen=True)
class _Cues:
    """言語資源から組み立てた、質問の理解の正規表現と語。"""

    words: dict[str, tuple[str, ...]]
    alt: dict[str, str]
    name: str
    kana_kanji: str
    object_pattern: re.Pattern[str]
    existential: re.Pattern[str]
    past_request: re.Pattern[str]
    changed_field: re.Pattern[str]
    clause_split: re.Pattern[str]
    sentence_split: re.Pattern[str]
    unit: re.Pattern[str]
    tried_action: re.Pattern[str]
    observed_state: re.Pattern[str]
    action_target: re.Pattern[str]
    error_quoted: re.Pattern[str]
    error_phrase: re.Pattern[str]
    error_clause_split: re.Pattern[str]
    request_marker: re.Pattern[str]
    cause_target: re.Pattern[str]
    parallel_objects: re.Pattern[str]
    chained_operations: re.Pattern[str]
    circled: str
    # 小問の印（#2399）: 丸数字の並び（範囲ごと）・番号の型（(正規表現, 文中のどこでもよいか)）・印の後に続けば
    # 資料の番号への言及になる語・印の前の区切り（文頭・空白のほかに、文末・読点・コロンの字）。
    subquestion_circled: tuple[str, ...]
    subquestion_numbers: tuple[tuple[re.Pattern[str], bool], ...]
    subquestion_reference: re.Pattern[str]
    subquestion_boundary: str

    def has(self, key: str, text: str) -> bool:
        """語の一覧のどれかが text にあるか。"""
        return bool(re.search(self.alt[key], text))


def _build_cues(pack: LanguagePack) -> _Cues:
    words = {spec.key: pack.words("question_cues", spec.key) for spec in FIELD_SPECS if spec.section == "question_cues"}
    alt = {key: alternation(values) for key, values in words.items()}
    terms = pack.words("search_terms", "kanji_chars") + pack.words("search_terms", "katakana_chars")
    kana_kanji = range_class(terms)
    name = range_class((*terms, "A-Z", "a-z", "0-9"))
    name_head = range_class((*terms, "A-Z", "a-z"))
    kanji = range_class(pack.words("search_terms", "kanji_chars"))
    object_suffix = f"(?:{alt['object_suffixes']}|{kanji}{{1,3}}{char_class(words['document_kind_chars'])})"
    done = alt["done_marks"]
    quotes = pack.words("symbols", "quote_brackets")
    brackets = (*quotes, *pack.words("symbols", "bracket_pairs"))
    opens = char_class(tuple(pair[0] for pair in quotes))
    closes = char_class(tuple(pair[1] for pair in quotes))
    close_chars = re.escape("".join(pair[1] for pair in quotes))
    bracket_opens = re.escape("".join(pair[0] for pair in brackets))
    sentence = char_class(words["sentence_split_chars"])
    sentence_chars = re.escape("".join(words["sentence_split_chars"]))
    comma = char_class(words["comma_chars"])
    comma_chars = re.escape("".join(words["comma_chars"]))
    unit_chars = re.escape("".join(words["unit_split_chars"]))
    quote_chars = re.escape("".join(quotes))
    # 経緯の節の後の「が」（「〜したが」）。後読みは語ごとに分ける（長さの違う語も書ける）。
    contrast = "|".join(f"(?<={re.escape(ending)})" for ending in words["contrast_clause_endings"])
    field_chars = range_class((*terms, "A-Z", "a-z", "0-9", "_"))
    return _Cues(
        words=words,
        alt=alt,
        name=name,
        kana_kanji=kana_kanji,
        object_pattern=re.compile(rf"{name}{{1,20}}{object_suffix}"),
        # 「〈画面A〉に〈対象B〉がある」の B (#1068)。
        existential=re.compile(
            rf"{alt['existential_locatives']}({name}{{2,12}}){alt['existential_particles']}"
            rf"{alt['existential_predicates']}"
        ),
        past_request=re.compile(rf"\s*{alt['past_markers']}.+?(?:{alt['contrast_particles']}{comma}|{sentence}|\n)\s*(.+)", re.S),
        changed_field=re.compile(
            rf"(?:^|[\s{bracket_opens}{comma_chars}{sentence_chars}])({field_chars}{{2,32}})"
            rf"{alt['changed_field_particles']}?{alt['change_words']}(?:{alt['changed_field_endings']}|$)"
        ),
        clause_split=re.compile(
            rf"{char_class(words['clause_split_chars'])}|\n" + (rf"|(?:{contrast}){alt['contrast_particles']}" if contrast else "")
        ),
        sentence_split=re.compile(rf"{sentence}|\n"),
        unit=re.compile(rf"[^{unit_chars}\n]+[{unit_chars}\n]*"),
        # 「取消済み伝票」のように名詞を修飾する「〜済」はデータの状態。済の印の後に名詞が続けば除く（atomic で
        # 「済み」から「済」へ戻って「み」を後ろの字にしない）。
        tried_action=re.compile(rf"{alt['tried_action_cues']}|{alt['done_state_verbs']}(?>{done})(?!{kana_kanji})"),
        observed_state=re.compile(rf"{kana_kanji}{{2,}}{alt['passive_state_endings']}|{alt['observed_state_cues']}"),
        # 要求文の操作対象「X を〔一括で〕<操作語>(する|したい|…)」の X（数字で始まらない 2〜20 文字の名詞句）。
        action_target=re.compile(
            rf"({name_head}{name}{{1,19}}){alt['object_particles']}{alt['manner_words']}?"
            rf"{alt['action_target_verbs']}{alt['action_request_endings']}"
        ),
        error_quoted=re.compile(rf"{opens}([^{close_chars}\n]{{3,80}}){closes}"),
        error_phrase=re.compile(rf"([^{sentence_chars}{quote_chars}\n]{{4,80}}?){alt['error_connectors']}{alt['error_words']}"),
        # 「…で」「…、」の区切り。「では」「でも」「できない」の「で」は区切りにしない。
        error_clause_split=re.compile(
            rf"{alt['error_clause_particles']}(?!{char_class(words['error_clause_particle_exceptions'])})|{comma}"
        ),
        request_marker=re.compile(rf"{alt['question_final_particles']}[{unit_chars}\s]*$|{alt['request_cues']}"),
        # 原因・理由の名詞が質問の的になっている形（#3491）: 名詞の直後（並べた語を挟んでもよい）に問う語尾が続くか、
        # 疑問の語が名詞の前に付く（目的語の「を」が続く「どんな理由を入力」は項目の名前として除く）。
        # 「理由は何文字以上」「理由の欄」「理由を入力する手順」のように数量・入力・形式の対象の名前のときは当たらない。
        cause_target=re.compile(
            rf"{alt['cause_nouns']}(?:{alt['parallel_connectors']}[^{sentence_chars}\n]{{0,20}}?)?{alt['cause_noun_question_endings']}"
            rf"|{alt['cause_noun_question_leads']}{alt['cause_nouns']}(?!{alt['object_particles']})"
        ),
        parallel_objects=re.compile(
            rf"({name}{{1,20}}){alt['parallel_connectors']}({name}{{1,20}}){alt['target_particles']}"
            rf"({alt['split_operation_words']})"
        ),
        chained_operations=re.compile(
            rf"({alt['split_operation_words']}){alt['chain_connectors']}{comma}?"
            rf"(?:[^{comma_chars}{sentence_chars}]{{0,20}}?{alt['target_particles']})?({alt['split_operation_words']})"
        ),
        circled="".join(_expand(pack.words("markers", "circled_numbers"))),
        subquestion_circled=tuple("".join(_expand((item,))) for item in words["subquestion_circled_numbers"]),
        # 記号で始まる型（「(N)」）は文中のどこでも、数・字で始まる型（「N)」「問N」）は区切りの後だけを印にする。
        subquestion_numbers=tuple(
            (re.compile(re.escape(pattern).replace("N", r"([0-9０-９]{1,2})")), not pattern[0].isalnum())
            for pattern in words["subquestion_number_patterns"]
            if "N" in pattern
        ),
        subquestion_reference=re.compile(rf"\s*{alt['subquestion_reference_followers']}"),
        subquestion_boundary="".join(
            (*words["unit_split_chars"], *words["comma_chars"], *pack.words("evidence_cues", "label_colon_chars"))
        ),
    )


def _expand(items: tuple[str, ...]) -> list[str]:
    """1 文字か「最初-最後」の範囲の並びを字の並びにする。"""
    chars: list[str] = []
    for item in items:
        if len(item) == 3 and item[1] == "-":
            chars.extend(chr(code) for code in range(ord(item[0]), ord(item[2]) + 1))
        elif len(item) == 1:
            chars.append(item)
    return chars


_cues = per_pack(_build_cues)


def object_pattern() -> re.Pattern[str]:
    """質問や item が名指しする業務上の対象（画面・帳票・文書。種別語で終わる名詞句）。"""
    return _cues().object_pattern


def generic_target_words() -> frozenset[str]:
    """総称語だけの操作対象（「データを削除」「情報を変更」）。資料の具体名と照合できない (#696, #700)。"""
    return frozenset(_cues().words["generic_target_words"])


def _requester_terms_pattern() -> str:

    terms = _cues().words["requester_terms"]
    return "|".join(re.escape(term) for term in dict.fromkeys(term for term in terms if term))


def _text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower()


def current_request_text(question: str) -> str:
    """明示された過去の操作経験に続く今回の要求を返す。原質問は別途保持する。

    「以前…が、今回…」のように境界がある場合だけ補助的に絞る。
    後半に記された方法制約は保持し、不明な時制や対象は推測しない。
    """
    match = _cues().past_request.match(question)
    return match.group(1).strip() if match else question


def changed_field_conditions(text: str) -> list[str]:
    """明示された「項目が変更になった／項目変更時」の項目名だけを返す。

    文境界直後の名詞に限定し、曖昧な言い換えや単なる変更希望は推測しない。
    """
    return list(dict.fromkeys(_cues().changed_field.findall(unicodedata.normalize("NFKC", text))))


def question_intent_parts(question: str) -> dict[str, list[str]]:
    """明示的な希望・試行・観測・仮説を原文の節として返す。

    検索補助用の保守的な抽出であり、推測した解決策や省略された対象を補わない。
    分類は排他的ではなく、検出できない節も原質問側に保持する。
    """
    cues = _cues()
    clauses = [s.strip() for s in cues.clause_split.split(question) if s.strip()]
    hypothesis = f"{cues.alt['hypothesis_cues']}|{cues.alt['means_cues']}"
    # 同一文で撤回された希望を、接続助詞の分割で復活させない。
    withdrawn = [m.group() for m in cues.unit.finditer(question) if cues.has("withdrawn_cues", m.group())]
    conditional = f"{cues.alt['condition_cues']}"
    return {
        "desired_outcomes": [
            s
            for s in clauses
            if cues.has("wish_endings", s) and not re.search(hypothesis, s) and not any(s in sentence for sentence in withdrawn)
        ],
        # 「取消済み伝票」のように名詞を修飾する「〜済」はデータの状態であり、利用者の試行ではない。
        # 文末や助詞が続く「取消済みです」だけを試行操作とする。
        "tried_actions": [s for s in clauses if cues.tried_action.search(s) and not re.search(conditional, s)],
        "observed_states": [
            s
            for s in clauses
            if cues.observed_state.search(s) and not re.search(f"{hypothesis}|{conditional}|{cues.alt['uncertainty_cues']}", s)
        ],
        "hypothesized_means": [s for s in clauses if re.search(hypothesis, s)],
    }


def goal_retrieval_queries(question: str) -> tuple[str, ...]:
    """経緯や仮説が混在する場合、明示目的を最大2件の補助検索文として返す。

    原質問を置き換えず、固有の業務名・操作名・値は追加しない。
    """
    parts = question_intent_parts(question)
    if not any(parts[key] for key in ("tried_actions", "observed_states", "hypothesized_means")):
        return ()
    return tuple(dict.fromkeys(parts["desired_outcomes"]))[:2]


def task_contract(question: str) -> dict[str, Any]:
    """質問契約を返す。解析は質問ごとに1回だけ行い、呼出側が書き換えても共有されない写しを返す。"""
    # 言語資源の手がかりの語で結果が変わるので、言語資源の hash も cache の鍵にする（#2181）。
    return copy.deepcopy(_task_contract(question, current_pack().hash))


# 質問に明示された要求の種類（`explicit_requests`）。
STEPS_REQUEST = "steps"
LIST_REQUEST = "list"
COUNT_REQUEST = "count"


def explicit_requests(question: str) -> frozenset[str]:
    """今回の要求文に手がかりの語がある要求の種類（手順 steps・一覧 list・件数 count）を返す。

    質問を 1 つの目的に分類するのではない。複数が同時に立ち、どれの手がかりも無ければ空（既定値を持たない。#3498）。
    """
    return _explicit_requests(question, current_pack().hash)


@lru_cache(maxsize=256)
def _explicit_requests(question: str, _pack_hash: str = "") -> frozenset[str]:
    cues = _cues()
    alt = cues.alt
    text = _text(current_request_text(question))
    listing = cues.has("member_cues", text) or bool(
        re.search(
            rf"{alt['recipient_subject_words']}.*{alt['recipient_action_words']}|{alt['recipient_list_words']}"
            rf"|{alt['list_words']}.{{0,8}}{alt['list_output_words']}",
            text,
        )
    )
    # 「変更履歴」のような説明要求と、値を変えたい操作要求を区別する。
    change_request = bool(re.search(rf"{alt['change_verbs']}{alt['change_verb_endings']}|{alt['change_wish_cues']}", text))
    # 意味を尋ねる項目の名前（「登録」列など）の中の語は、操作の手がかりにしない。
    outside_items = text
    for target in definition_targets(question):
        outside_items = outside_items.replace(_text(target["label"]), "")
    steps = change_request or cues.has("method_cues", text) or cues.has("procedure_cues", outside_items)
    kinds = {STEPS_REQUEST: steps, LIST_REQUEST: listing, COUNT_REQUEST: cues.has("count_cues", text)}
    return frozenset(kind for kind, present in kinds.items() if present)


@lru_cache(maxsize=256)
def _task_contract(question: str, _pack_hash: str = "") -> dict[str, Any]:
    """外部I/Oなしで明示的な要求・形式・条件・仮説を抽出する。

    日本語の代表的な言い回しを認識する補助規則であり、未知表現の意味理解を保証しない。
    原文も保持し、検出できなかった値を推測で補わない。質問を 1 つの目的に分類しない（#3498）。
    """
    cues = _cues()
    alt = cues.alt
    current = current_request_text(question)
    text = _text(current)
    clauses = [p.strip() for p in cues.sentence_split.split(question) if p.strip()]
    missing_choice = bool(re.search(rf"{alt['choice_words']}.*{alt['absence_words']}", text))
    requester = re.findall(
        rf"(?:{_requester_terms_pattern()}){alt['requester_sides']}?{alt['requester_particles']}.{{0,25}}?"
        rf"{alt['requester_verbs']}",
        question,
    )
    wants_count = cues.has("count_cues", text)
    wants_members = cues.has("member_cues", text)
    wants_basis = cues.has("basis_cues", text)
    requests = explicit_requests(question)
    definitions = definition_targets(question)
    intent_parts = question_intent_parts(question)
    # 明示目的の操作を先に記録する。経緯中の「登録」だけを要求操作とすると、
    # 追加・削除等の本来の目的が契約から脱落する。原質問の他の要求も保持する。
    action_text = "\n".join([*intent_parts["desired_outcomes"], current])
    enumeration = enumeration_request(question)
    # 手順の観点は、手順か一覧（件数ではない）の要求、選択肢が無いと述べた質問で足す。
    procedure_aspect = STEPS_REQUEST in requests or requests & {LIST_REQUEST, COUNT_REQUEST} == {LIST_REQUEST} or missing_choice
    return {
        "original_question": question,
        "current_request": current,
        "historical_context": question[: question.rfind(current)].strip() if current != question else "",
        "intent_parts": intent_parts,
        "request_units": request_units(question),
        "definition_targets": definitions,
        "asks_cause": asks_cause(question),
        "requested_granularity": [
            kind for kind, wanted in [("count", wants_count), ("members", wants_members), ("calculation_basis", wants_basis)] if wanted
        ],
        # 存在構文の対象を先に置く。`target_text_queries` は上限 6 件で、画面名の部分語だけで
        # 埋まると質問の対象が検索語に入らない (#1068)。
        "business_objects": list(dict.fromkeys([*cues.existential.findall(question), *cues.object_pattern.findall(question)])),
        "error_messages": error_messages(question),
        "action_targets": action_targets(question),
        # 「取消済み」「登録済み」の操作語は状態の一部で、要求された操作ではない（直後の「済」で除く）。
        "requested_actions": list(dict.fromkeys(re.findall(rf"{alt['requested_action_words']}(?!{alt['done_marks']})", action_text))),
        "user_assertions": [c for c in clauses if not cues.has("hypothesis_cues", c)],
        "requester_mentions": requester,
        "output_formats": [v for v in cues.words["output_format_words"] if v in text],
        "changed_fields": changed_field_conditions(current),
        "explicit_conditions": [c for c in clauses if cues.has("explicit_condition_cues", c)],
        "hypotheses": [c for c in clauses if cues.has("hypothesis_cues", c)],
        "missing_choice": missing_choice,
        # 列挙・網羅の要求（件数・章の範囲・読む範囲。#1503）。CRAG の評価器は項目ごとの根拠を出し、件数・範囲の欠けは
        # プログラムで数える（`enumeration.enumeration_check`）。
        "enumeration": enumeration.to_payload() if enumeration is not None else None,
        "required_aspects": ["business_object", "requested_result", "applicability"]
        + (["procedure"] if procedure_aspect else [])
        + (["member_details", "population_membership"] if wants_members and cues.has("aggregation_words", text) else [])
        + ([ENUMERATION_ASPECT] if enumeration is not None else []),
    }


def asks_cause(question: str) -> bool:
    """今回の要求文が原因・理由を尋ねているか (#713)。原因の推測を summary で列挙させないための材料。

    「なぜ」のような原因を問う語があるか、原因・理由の名詞が質問の的になっているとき（`cause_noun_targeted`）だけ真。
    """
    current = current_request_text(question)
    return _cues().has("cause_cues", current) or cause_noun_targeted(current)


def cause_noun_targeted(text: str) -> bool:
    """原因・理由の名詞が、問う形（「理由は何ですか」「原因を教えて」「何が原因か」）の的になっているか (#3491)。

    「理由」は入力・記載する項目の名前（「理由は何文字以上」「理由の欄」）にも使うため、語の有無では判定しない。
    """
    return bool(_cues().cause_target.search(text))


def action_targets(question: str) -> list[str]:
    """質問が要求する操作の対象（「社印を変更するには」の「社印」）を原文の語で返す。

    種別語（画面・帳票…）で終わらない対象も拾い、根拠のどこにもない対象への強行回答（別対象の手順の適用）を
    検査する材料にする (#700)。総称語だけの対象は返さない。対象の意味や同義語は推測しない。
    """
    text = unicodedata.normalize("NFKC", question)
    generic = generic_target_words()
    return [t for t in dict.fromkeys(_cues().action_target.findall(text)) if t not in generic]


# 質問が示すエラー文。「…」というエラー / 「…」のエラー のように引用された文と、「…のエラーが出ている」の「…」。


def error_messages(question: str) -> list[str]:
    """質問が示すエラー文（引用された文、または「…のエラー」の「…」）を返す (#686)。

    エラーを含む文だけを対象にし、文に引用（「」『』）があればそれを、なければ「…のエラー」の
    直前の句を採る。回答生成では、これが根拠のどこにもない場合に別のエラーの手順を適用扱いにしない。
    """
    cues = _cues()
    found: list[str] = []
    for sentence in cues.sentence_split.split(question):
        if not cues.has("error_words", sentence):
            continue
        quoted = [q.strip() for q in cues.error_quoted.findall(sentence) if len(q.strip()) >= 3]
        if quoted:
            found.extend(quoted)
            continue
        for phrase in cues.error_phrase.findall(sentence):
            # 文頭からの句は対象名や文脈（「出荷検品エラーリストで定期出荷契約の」）を含むので、区切りの最後の句に絞る (#694)。
            clauses = [c.strip(cues.circled + " ") for c in cues.error_clause_split.split(phrase)]
            clauses = [c for c in clauses if len(c) >= 4]
            if clauses:
                found.append(clauses[-1])
    return list(dict.fromkeys(v for v in found if v))


# 疑問・依頼・可否・方法を尋ねる標識は言語資源（request_cues）。含まない文（「課名が変更になった。」）は経緯・背景。


def _answer_instruction_pattern() -> re.Pattern[str]:
    """回答指示と依頼の語尾が連続する文末。"""
    cues = _cues()
    return re.compile(
        rf"{cues.alt['answer_instruction_cues']}{cues.alt['answer_instruction_endings']}"
        rf"[{re.escape(''.join(cues.words['unit_split_chars']))}\s]*$"
    )


def answer_instruction_texts(question: str) -> list[str]:
    """根拠の要求にしない回答指示の原文。表記の解決でも同じ判定を使う。"""
    pattern = _answer_instruction_pattern()
    return [unit["text"] for unit in _base_units(question) if pattern.search(unit["text"])]


def _with_request_kind(units: list[dict[str, str]]) -> list[dict[str, str]]:
    """各文に kind=request / context を付ける。背景文は単位として残すが、答える対象ではない (#650)。

    要求の標識を持つ文が 1 つもない質問（「〇〇の件。」だけ等）は、全文を request として扱う。
    """
    cues = _cues()
    marker = cues.request_marker
    instructions = [bool(_answer_instruction_pattern().search(unit["text"])) for unit in units]
    kinds = [
        "request" if marker.search(unit["text"]) and not instruction else "context"
        for unit, instruction in zip(units, instructions, strict=True)
    ]
    if "request" not in kinds and not any(instructions):
        kinds = ["request"] * len(units)
    return [{**unit, "kind": kind} for unit, kind in zip(units, kinds, strict=False)]


def _sentence_units(question: str) -> list[dict[str, str]]:
    parts = [m.group().strip() for m in _cues().unit.finditer(question)]
    return [{"id": f"Q{i}", "text": part} for i, part in enumerate(parts, 1) if part]


def subquestion_markers(question: str) -> list[tuple[int, str]]:
    """質問に並べた小問の印の (位置, 印の原文) を返す (#2399)。小問として並んでいなければ空。

    印は言語資源の小問の印（丸数字の範囲・「(N)」などの型）。1 から始まり 1 ずつ増える同じ種類の印が 2 つ以上
    あるときだけ小問とみなす（1 つだけの「③」や「①と②の違い」は資料の番号への言及）。印の直後に言及の語
    （「の」「と」「～」など）が続く印は数えない。数・字で始まる型（「1)」）は文頭・空白・文末・読点・コロンの後だけ。
    種類が混ざるときは、最初の小問が前にある並びを使う。
    """
    cues = _cues()
    candidates: dict[str, list[tuple[int, int, str]]] = {}

    def add(style: str, start: int, end: int, ordinal: int) -> None:
        if not cues.subquestion_reference.match(question, end):
            candidates.setdefault(style, []).append((start, ordinal, question[start:end]))

    for index, chars in enumerate(cues.subquestion_circled):
        if len(chars) < 2:
            continue
        for match in re.finditer(f"[{re.escape(chars)}]", question):
            add(f"c{index}", match.start(), match.end(), chars.index(match.group()) + 1)
    for index, (pattern, anywhere) in enumerate(cues.subquestion_numbers):
        for match in pattern.finditer(question):
            start = match.start()
            before = question[start - 1] if start else ""
            if not anywhere and before and not before.isspace() and before not in cues.subquestion_boundary:
                continue
            add(f"n{index}", start, match.end(), int(unicodedata.normalize("NFKC", match.group(1))))
    best: list[tuple[int, str]] = []
    for found in candidates.values():
        sequence: list[tuple[int, str]] = []
        for start, ordinal, label in sorted(found):
            if ordinal == len(sequence) + 1:
                sequence.append((start, label))
        if len(sequence) >= 2 and (not best or sequence[0][0] < best[0][0]):
            best = sequence
    return best


def _subquestion_units(question: str, markers: Sequence[tuple[int, str]]) -> list[dict[str, str]]:
    """小問ごとの要求単位。前置きの文は背景（context）にし、各小問に preamble として付ける (#2399)。"""
    preamble_text = question[: markers[0][0]]
    preamble = " ".join(preamble_text.split())
    units = [{**unit, "kind": "context"} for unit in _sentence_units(preamble_text)]
    ends = [start for start, _ in markers[1:]] + [len(question)]
    for (start, label), end in zip(markers, ends, strict=True):
        text = " ".join(question[start:end].split())
        unit = {"id": f"Q{len(units) + 1}", "text": text, "kind": "request", "subquestion": label}
        if preamble:
            unit["preamble"] = preamble
        units.append(unit)
    return units


def _base_units(question: str) -> list[dict[str, str]]:
    """原文の要求単位（子要求を付ける前）。小問の印が並ぶ質問は小問ごと、それ以外は文ごと。"""
    markers = subquestion_markers(question)
    return _subquestion_units(question, markers) if markers else _with_request_kind(_sentence_units(question))


def definition_targets(question: str) -> list[dict[str, str]]:
    """原要求IDを維持して定義項目に子IDを付ける。記号は照合用にだけ正規化する。"""
    return [
        dict(
            id=f"{unit['id']}.D{n}",
            request_id=unit["id"],
            label=label,
            marker="*" if "*" in unicodedata.normalize("NFKC", unit["text"]) else "",
        )
        for unit in _base_units(question)
        if unit.get("kind") != "context"
        for n, label in enumerate(definition_labels(unit["text"].removeprefix(unit.get("subquestion", "")).lstrip()), 1)
    ]


# 「業務と種別を追加」「氏名、住所の変更」: 助詞を含まない 2 つの名詞句と、それに続く 1 つの操作語。区切りは「と」の
# ほか読点・「や」「及び」も受ける (#702)。「変更して再発行」: 前の操作の完了を前提にした別の操作 (#622)。
# 語は言語資源（parallel_connectors・split_operation_words・chain_connectors）。


def request_units(question: str) -> list[dict[str, str]]:
    """質問原文を句点・疑問符・改行で固定ID化する。背景文も捨てず判定側へ渡す。

    丸数字・(1) などで小問を並べた質問は、小問ごとに 1 つの要求単位にする（`subquestion` に印、`preamble` に
    小問の前の共通の前置き。前置きの文は背景）。小問の印の無い質問の単位は変わらない (#2399)。
    長い入力も切り捨てず、定義項目と経緯に埋もれた明示目的を子要求にする。
    原文単位のIDは維持し、既に独立した目的文は重複させない。
    """
    cues = _cues()
    withdrawn = f"{cues.alt['facet_withdrawal_objects']}?{cues.alt['topic_particles']}{cues.alt['facet_withdrawal_words']}"
    units = _base_units(question)
    goals = question_intent_parts(question)["desired_outcomes"] if goal_retrieval_queries(question) else []
    goal_units = [
        dict(id=f"{unit['id']}.G{n}", text=goal, kind="desired_outcome", request_id=unit["id"])
        for unit in units
        for n, goal in enumerate(goals, 1)
        if unit.get("kind") != "context" and goal in unit["text"] and goal != unit["text"].strip("。！？? \n")
    ]
    facets = []
    for unit in units:
        if unit.get("kind") == "context":
            continue
        # 最終集計の構成員を尋ねる要求では、候補の閲覧と集計への採否を
        # 別々に検証する。集計値だけの質問へ明細要求を追加しない。
        if cues.has("aggregation_words", unit["text"]) and cues.has("member_subject_words", unit["text"]):
            facets.extend(
                [
                    dict(
                        id=f"{unit['id']}.M1",
                        request_id=unit["id"],
                        kind="explicit_facet",
                        text=f"原要求 {unit['id']} の構成明細の確認方法。集計値の出力だけでは充足しない。原文: {unit['text']}",
                    ),
                    dict(
                        id=f"{unit['id']}.M2",
                        request_id=unit["id"],
                        kind="explicit_facet",
                        text=(
                            f"原要求 {unit['id']} の明細候補と最終集計対象との対応。"
                            f"採用・除外条件が未確認なら一致を保証せず、その不足を明示する。原文: {unit['text']}"
                        ),
                    ),
                ]
            )
        # 「AとBを追加」のような同一操作の複数対象、「変更して再発行」のような操作の連結は、
        # 一文のままだと監査が一方だけで addressed にする。原文の語だけで対象・操作ごとの子要求にする (#622)。
        objects = cues.parallel_objects.search(unit["text"])
        if objects:
            first, second, operation = objects.groups()
            facets.extend(
                dict(
                    id=f"{unit['id']}.R{n}",
                    request_id=unit["id"],
                    kind="explicit_facet",
                    text=f"原要求 {unit['id']} の対象「{target}」の{operation}を個別に回答する。原文: {unit['text']}",
                )
                for n, target in enumerate((first, second), 1)
            )
        chained = cues.chained_operations.search(unit["text"])
        if chained:
            facets.extend(
                dict(
                    id=f"{unit['id']}.O{n}",
                    request_id=unit["id"],
                    kind="explicit_facet",
                    text=f"原要求 {unit['id']} の操作「{operation}」を個別に回答する。原文: {unit['text']}",
                )
                for n, operation in enumerate(chained.groups(), 1)
            )
        # 一文内の明示的な複数論点だけを子要求化する。原文を保持し対象や効果を推測しない。
        found = [
            label
            for label, pattern in (
                ("操作方法", cues.alt["facet_method_cues"]),
                ("影響", cues.alt["facet_impact_cues"]),
                ("出力可否", cues.alt["facet_output_cues"]),
                ("必要性", cues.alt["facet_necessity_cues"]),
            )
            # 「方法の説明は不要」のように撤回された論点は子要求にしない。
            if re.search(pattern, unit["text"]) and not re.search(pattern + withdrawn, unit["text"])
        ]
        if len(found) > 1:
            facets.extend(
                dict(
                    id=f"{unit['id']}.F{n}",
                    request_id=unit["id"],
                    kind="explicit_facet",
                    text=f"原要求 {unit['id']} の「{label}」を個別に回答する。原文: {unit['text']}",
                )
                for n, label in enumerate(found, 1)
            )
    return (
        units
        + goal_units
        + facets
        + [
            dict(
                id=t["id"],
                kind="definition",
                request_id=t["request_id"],
                label=t["label"],
                marker=t["marker"],
                text=f"項目「{t['label']}」{t['marker']}への回答",
                original_question=next(unit["text"] for unit in units if unit["id"] == t["request_id"]),
            )
            for t in definition_targets(question)
        ]
    )


def query_rejection_reason(question: str, query: str, *, grounded_text: str = "") -> str:
    """目的の逸脱と未裏付けの業務IDを拒否する。grounded_textは検証済み別名のみ。

    日付や一文字の版名はIDと扱わず、一般的な言い換えは保持する。
    モデル自身が生成した説明をgrounded_textへ渡してはいけない。
    """
    source, value = _text(question), _text(query)
    if source == value:
        return ""
    # 日付・版ではなく業務IDを対象にする。明示別名以外の新しいIDは推測で追加しない。
    identifiers = re.findall(r"(?<![a-z0-9_])[a-z]{2,}[a-z0-9_-]*\d[a-z0-9_-]*", value)
    support = _text(question + " " + grounded_text)
    if any(not re.search(r"(?<![a-z0-9_])" + re.escape(identifier) + r"(?![a-z0-9_])", support) for identifier in identifiers):
        return "原質問・明示別名にない業務識別子"
    contract = task_contract(question)
    requests = explicit_requests(question)
    cues = _cues()
    alt = cues.alt
    if (
        LIST_REQUEST in requests
        and COUNT_REQUEST not in requests
        and cues.has("count_query_words", value)
        and not cues.has("list_query_words", value)
    ):
        return "対象者を確認する目的が人数・件数に変化"
    if (
        contract["missing_choice"]
        and re.search(rf"{alt['hidden_field_words']}.{{0,8}}{alt['hidden_field_states']}|{alt['fetch_failure_cues']}", value)
        and not cues.has("choice_words", value)
    ):
        return "選択肢に該当しない状態が項目非表示・取得失敗に変化"
    # 問い合わせ元の語（profile の requester_terms + 一般語）や地域が「〜別」「〜ごと」の絞込条件に転じた検索文を検出する (#849)。
    grouping = rf"(?:{_requester_terms_pattern()}|{alt['grouping_extra_words']})"
    geographic_filter = rf"{grouping}{alt['grouping_suffixes']}|{grouping}.{{0,8}}{alt['filter_words']}"
    if contract["requester_mentions"] and re.search(geographic_filter, value) and not re.search(geographic_filter, source):
        return "問い合わせ元が地域の絞込条件に変化"
    # 「<業務対象>の更新」（申請・台帳・伝票など、語彙は問わない）を尋ねる質問が、検索文でソフトウェアの版更新に
    # 変わっていないか。以前は福祉語彙（手帳・支給・手当）の列挙で判定していた (#849)。
    business_update = bool(re.search(rf"{cues.kana_kanji}{{2,}}{alt['target_particles']}?{alt['update_words']}", source)) or any(
        obj in source for obj in contract["business_objects"]
    )
    if (
        business_update
        and cues.has("update_words", source)
        and not cues.has("software_version_words", source)
        and cues.has("software_version_words", value)
    ):
        return "業務更新がソフトウェア版変更に変化"
    return ""


def filter_queries(question: str, queries: Sequence[str], *, grounded_text: str = "") -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    """採用検索文と、理由付きの不採用検索文を返す。入力は変更しない。"""
    accepted, rejected = [], []
    for query in queries:
        reason = query_rejection_reason(question, query, grounded_text=grounded_text)
        if reason:
            rejected.append({"query": query, "reason": reason})
        else:
            accepted.append(query)
    return tuple(accepted), tuple(rejected)
