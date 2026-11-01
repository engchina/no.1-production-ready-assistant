"""回答の段落の分け方と、主張ではない段落の決定的な判定（#1277・#1306）。

最終の検証（`answer_validation`）は、RAG の `rag_validate_answer` が段落（`answer_quote`）ごとに
返す判定で、根拠で確かめられない段落を回答から外す。ところが主張ではない段落（見出し・出典の行・
利用者への質問・「資料に記載がありません」という不足の文・「資料で確認できず除いた内容」の節）まで
「確かめられない」として外すと、回答が読みにくくなり、拒答が拒答に見えなくなる（#1289 の評価）。

ここでは、そうした段落をモデルを使わずに判定する。判定は保守的にし、迷うものは主張として扱う
（確かめられない操作の手順・事実の主張は今までどおり外す）。

- 見出し: Markdown の見出し・太字だけの行・【…】・「…：」で終わる短いラベル。述語で終わる文や
  操作の指示を含むものは見出しにしない。句点で終わらない短い名詞句（「1. 人事評価システムの
  重要度」「補足（資料の記載）」「根拠」。項目・区分・資料の名前か「…とき」「…か」で終わり、
  述語・操作・数量・言い切りの「は」を含まないもの）と、以降の箇条書きを導くだけの導入の文
  （「根拠は次のとおりです。」）・礼の定型の文も見出しとして扱う（#1469）。規則は RAG の
  `operation_audit.is_heading` とそろえ、両方のテストが
  platform/contracts/answer-passages/headings.json の同じ事例で確かめる。
- 出典: 「出典：…」「根拠：…」「セクション：…」「ページ：…」「*Locator*: …」などの
  ラベルの行（ラベルの太字・斜体は許す）、文書名・頁・節を示す括弧だけの行、文書名だけの行、
  リンクだけの行、根拠の ID（【証拠 ID: …】・〔証拠 ID: …〕）と文の無い「…」の題・頁の括弧
  だけの行（表の区切りの `|` は許す）、文書名と場所の括弧だけの行（「**システム変更手順書**
  （第 2章 申請）」。#1370）、出典の節（見出しが「出典」「参考資料」など）の行（#1317）。
  規則は RAG の `operation_audit.is_citation_line` とそろえ、両方のテストが
  platform/contracts/answer-passages/citation-lines.json の同じ事例で確かめる。
- 表の形: Markdown の表の区切りの行（`|---|---|`）・区切り線（`---`）と、区切りの行の直前の
  見出しの行（短い語だけのセル）。表の本文の行は主張のまま（#1317）。
- 質問: 「？」で終わる文（後ろの「（はい／いいえ）」などの選択肢の括弧は許す）と、情報を
  求める依頼の文（「教えてください」など）、質問の直後の短い選択肢の箇条書き。主張や操作を抱き合わせた文は除く。
- 不足: 資料に記載が無い・資料からは確かめられないことだけを述べる文と、答えられないと言い切る
  拒答の文（「…示すことはできません」「回答はできません」。#1317）。数量・逆接・操作を含む文は除く。
- 確かめきれない点・除いた内容の節（最終の検証が足す「根拠と矛盾し除いた内容」「確認が必要な点」
  など）: その見出しと、その節の行（操作の指示を除く）。
- 作業の経過: 業務ガイドが見つからなかったことだけを述べる文（#1452。前置きに主張・数字を含む文と
  操作の指示を含む文は除く）。不足の印にはしない（業務ガイドが無いことは回答の不足ではない）。

補足の段落（見出しが「補足」「注記」「備考」などの節の行と、「なお、」「補足：」で始まる段落）は
`supplement_passages` で判定する（#1475）。主張かどうかは変えず、回答の対応（`answer_outcome`）が、
結論の段落で答えた後の補足の不足の文を不足の印にしないために使う。

別に、利用者に現場のデータ・記録の確認を求める段落（「認証ログでエラーの時刻の行を確認して
ください」など）を `environment_check_passages` で判定する（#1317）。こちらは主張のまま（検証で
確かめ、外す）で、回答の対応（`answer_outcome`）が「現場の確認が要る」の印に使う。

段落の分け方は Agent が自分の回答を組み立て直す（外す・印を付ける・番号を詰める）ための規則で、
括弧の中の「。」では分けない（#1455）。規則の事例は RAG と共有の契約
platform/contracts/answer-passages/sentence-splits.json（段落の見え方を 2 つの製品でそろえる）。
RAG の `rag_validate_answer` の段落の判定は、分け方の一致を前提にせず、判定の `answer_range`
（渡した回答の中の位置）で Agent の段落に結び付ける（`answer_validation.align_claims`。#3240）。
"""

from __future__ import annotations

import re
import unicodedata

from pr_backend_core.markdown import is_blank_text, normalize_heading_markup

from app.features.agent.answer_passage_vocabulary import alt as _v

KIND_HEADING = "heading"
KIND_CITATION = "citation"
KIND_QUESTION = "question"
KIND_ABSENCE = "absence"
KIND_UNVERIFIED = "unverified_section"
KIND_TABLE = "table"
# 作業の経過だけを述べる文（「業務ガイドは見つかりませんでした。」。#1452）。不足の印にはしない。
KIND_PROCESS = "process_note"

# ---- 段落の分け方（事例は RAG と共有の契約） ------------------------------------

_QUOTE_ONLY_LINE = re.compile(r"\s*(?:・|[0-9]+[.)]\s*)?「.*」\s*")
_PASSAGE_CHARS = 600
# 中の「。」で文を分けない括弧（開きと閉じ。#1455）。
_BRACKET_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】"}
_CLOSING_BRACKETS = frozenset(_BRACKET_PAIRS.values())
# 前の文に付ける断片（太字・強調の印と閉じ括弧・句点だけ。#1455）。
_MARKS_ONLY = re.compile(r"[\s*_`」』）)】。]*")


def sentence_ranges(line: str) -> list[tuple[int, int]]:
    """1 行を文に分けた範囲（開始・終了の位置。前後の空白を含む）。

    「。」で文を分けるが、開いた括弧（「」『』（）()【】）の中の「。」では分けない（括弧の深さを数える。
    #1455）。閉じない括弧は行の終わりまで続くとみなし、行の終わりで切る。開いていない閉じ括弧は数えない。
    文の区切りの「。」の直後の閉じ括弧は前の文に含め、太字の印（`**`）・閉じ括弧だけの断片は前の文に付ける。
    """
    ranges: list[tuple[int, int]] = []
    expected: list[str] = []
    start = index = 0
    while index < len(line):
        char = line[index]
        if char in _BRACKET_PAIRS:
            expected.append(_BRACKET_PAIRS[char])
        elif char in _CLOSING_BRACKETS and char in expected:
            # 対応する開きの括弧まで閉じる（その間の閉じていない括弧も閉じたとみなす）。
            while expected.pop() != char:
                pass
        elif char == "。" and not expected:
            end = index + 1
            while end < len(line) and line[end] in _CLOSING_BRACKETS:
                end += 1
            ranges.append((start, end))
            start = index = end
            continue
        index += 1
    if start < len(line):
        ranges.append((start, len(line)))
    merged: list[tuple[int, int]] = []
    for begin, end in ranges:
        if merged and _MARKS_ONLY.fullmatch(line[begin:end]):
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((begin, end))
    return merged


def passage_spans(line: str) -> list[tuple[int, int, str]]:
    """1 行の段落（行の中の開始・終了の位置と原文）。"""
    if is_blank_text(line):
        return []
    pieces = [(0, len(line))] if _QUOTE_ONLY_LINE.fullmatch(line) else sentence_ranges(line)
    spans: list[tuple[int, int, str]] = []
    for start, end in pieces:
        raw = line[start:end]
        value = raw.strip()
        offset = start + len(raw) - len(raw.lstrip())
        for index in range(0, len(value), _PASSAGE_CHARS):
            text = value[index : index + _PASSAGE_CHARS]
            spans.append((offset + index, offset + index + len(text), text))
    return spans


def answer_passage_positions(answer: str) -> list[tuple[int, int, str]]:
    """回答の段落の、回答の中の位置（開始・終了）と原文（行ごとの `passage_spans` の位置を回答の
    位置にしたもの）。"""
    positions: list[tuple[int, int, str]] = []
    offset = 0
    for line in answer.split("\n"):
        positions.extend(
            (offset + start, offset + end, text) for start, end, text in passage_spans(line)
        )
        offset += len(line) + 1
    return positions


# ---- 主張ではない段落の判定 --------------------------------------------------------

# 箇条書き・番号の印。
_BULLET = re.compile(r"^(?:[-*+・•]\s*|[0-9０-９]{1,3}[.)．）]\s*|[（(][0-9０-９]{1,3}[）)]\s*)")
# 操作の指示（RAG の `is_operation_instruction` と同じ考え方。これを含む段落は主張として確かめる）。
_OPERATION = re.compile(
    r"(?:開き|押し|選び|選択し|入力し|変更し|修正し|登録し|保存し|実行し|実施し|クリックし|削除し"
    r"|申請し|設定し|付与し|解除し|無効にし|有効にし)(?:ます|てください|て頂|ていただ|、|て)"
    r"|(?:を開く|を押す|を選択する|を入力する|を保存する|を削除する|に遷移する|に遷移します)"
    r"|(?:登録|選択|入力|変更|修正|保存|実行|削除|消去|申請|設定)(?:すれば|すると|することで|できます|可能)"
    r"|(?:押下|クリック|印刷|再発行)(?:する|すれば|すると|し|できます|可能)"
    r"|\b(?:click|press|navigate to|save changes|delete|erase)\b",
    re.IGNORECASE,
)
# 述語で終わる（見出し・ラベル・出典ではなく文）。
_PREDICATE_END = re.compile(
    r"(?:ます|ません|です|でした|ました|ください|ない|無い|ある|する|できる|れる|った|いた|した|だ)"
    r"[。．.!！]?$"
)
_MD_HEADING = re.compile(r"#{1,6}\s+(?P<title>[^\n]{1,60})")
_BOLD_LINE = re.compile(r"\*\*(?P<title>[^*\n]{1,40})\*\*\s*[:：]?")
_BRACKET_HEADING = re.compile(r"【(?P<title>[^】。\n]{1,30})】\s*[:：]?")
_LABEL_HEADING = re.compile(r"(?P<title>[^。！？!?\n:：]{1,30})[:：]")
# ラベルの「：」の直前の注記の括弧（「補足（資料の注記。同シート A1:A2）：」。#1455）。
_LABEL_NOTE = re.compile(r"[（(](?P<note>[^（）()\n]{1,60})[）)](?=\s*[:：]\s*$)")

_CITATION_LABEL = re.compile(
    rf"[（(【〔\[]?\s*{_v('citation_labels')}\s*[:：]\s*(?P<rest>.+?)\s*[）)】〕\]]?",
    re.IGNORECASE,
)
# 出典の場所のラベル（#1833。「- 場所：第 2 章「締め日と支払日」」「- 該当箇所：p.3」）。「場所」
# 「位置」は業務の値にも使う語（「場所：本社 3 階の会議室」は主張）なので、値が文書の場所だけのとき
# （`_is_location_value`）に限って出典にする。
_LOCATION_LABEL = re.compile(
    rf"[（(【〔\[]?\s*{_v('location_labels')}\s*[:：]\s*(?P<rest>.+?)\s*[）)】〕\]]?"
)
# Markdown の強調（「**根拠**：…」「*Locator*:」のラベル。出典の判定だけで外す。
# #1370 で斜体の `*` も外す）。
_EMPHASIS = re.compile(r"\*{1,2}|__|`")
# 質問の直後の、選択肢を並べた行（「選択肢: **個別**、**グループ**」。#1322 の確認の質問の次の手は
# 選択肢を添えさせる）。
_OPTIONS_LINE = re.compile(r"^(?:選択肢|候補)\s*[:：]\s*\S")
# 根拠の ID の括弧（【証拠 ID: 03ac…:1】・〔証拠 ID: 07e8…:5〕・【証拠ID cb0a…:2】・【証拠1】・
# 【evidence_id: …】。〔〕［］[] は #1370）。
_EVIDENCE_REF = re.compile(
    r"[【〔［\[]\s*(?:証拠|根拠|出典|evidence|source)(?:\s*_?(?:ID|id))?\s*[:：]?"
    r"[\s0-9A-Za-z:_\-,，、…①-⑳]{0,200}[】〕］\]]",
    re.IGNORECASE,
)
# 「…」の題（文ではないもの）。
_TITLE_QUOTE = re.compile(r"[「『]([^「」『』\n]{1,80})[」』]")
# 節の参照（「6. アカウントの削除」「第 3 章」）。
_SECTION_REF = re.compile(
    r"(?:第\s*[0-9０-９]+\s*[章節条項]|[0-9０-９]+(?:\.[0-9０-９]+)*[.．]\s*)[^。\n]{0,30}"
)
# 出典の行に残ってよい記号（表の区切り・矢印・句読点・「同上」・頁の数字）。
_REFERENCE_FILLER = re.compile(
    r"[|\s→>＞\-–—‑―、,，:：/／]+|同上|(?:ページ|頁|p\.|page)\s*[0-9０-９]+",
    re.IGNORECASE,
)
# Markdown の表の区切りの行（`|---|:---:|`）と区切り線（`---`・`***`）。
_TABLE_SEPARATOR = re.compile(r"\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?|\|?\s*:?-{3,}:?\s*")
_RULE = re.compile(r"(?:-{3,}|\*{3,}|_{3,})")
_TABLE_ROW = re.compile(r"\|.*\|")
_DOCUMENT_MARK = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|md|txt|html?|csv)\b|(?:\bpage\b|\bp\.\s*\d|\bpp\.|ページ|頁"
    r"|\bsection\b|\bsheet\b|シート|第\s*[0-9０-９]+\s*(?:章|節|条|項))",
    re.IGNORECASE,
)
_BRACKETED = re.compile(r"(?:[【\[（(][^】\]）)\n]{1,200}[】\]）)][\s、,，]*)+")
_BRACKET_PART = re.compile(r"[【\[（(]([^】\]）)\n]{1,200})[】\]）)]")
_FILE_LINE = re.compile(
    r"\S[^。\n]{0,160}\.(?:pdf|docx?|xlsx?|pptx?|md|txt|html?|csv)\b[^。\n]{0,80}", re.IGNORECASE
)
_FILE_NAME = re.compile(
    r"[^\s「」『』（）()【】\[\]、,，|]+\.(?:pdf|docx?|xlsx?|pptx?|md|txt|html?|csv)\b", re.I
)
_LINK_LINE = re.compile(r"(?:\[[^\]\n]{1,200}\]\([^)\s]{1,500}\)[\s、,，]*)+|https?://\S+")
# 出典の節の見出し。
_CITATION_SECTION = re.compile(
    r"出典|参照|参考資料|参考|根拠資料|引用元|sources?|references?", re.IGNORECASE
)
# 確かめきれない点・除いた内容の節の見出し（Agent の最終の検証が足す「根拠と矛盾し除いた内容」
# 「確認が必要な点」〔#2738。answer_validation.SECTION_HEADINGS。後者は語の一部に当たらないよう
# 見出しの全体で比べる〕と、RAG・モデルが付ける同じ意味の見出し）。RAG の回答（`rag_search` の
# answer）の末尾の節の見出しは、共有の語彙の `unverified_section_titles`（RAG の言語資源を写さない。
# #2040・#3240）。
_UNVERIFIED_SECTION = re.compile(
    r"根拠と矛盾し除いた内容|^確認が必要な点$"
    rf"|資料で確認できず除いた内容|{_v('unverified_section_titles')}"
    r"|確かめられていない点|確かめられない点|確認できていない点|確認できない点|未確認の(?:点|事項)"
    r"|資料からは確認できない点|資料で確かめられない点|不明な点"
)

# 「？」で終わる質問（後ろの選択肢の括弧は許す）。
# 後ろの注記の括弧（「（はい／いいえ）」「【clarification: target】」。#1317）は許す。
_QUESTION = re.compile(r".*[？?]\s*(?:[（(【][^）)】\n]{1,40}[）)】]\s*)*[。]?")
# 情報を求める依頼の文（利用者に答えてもらう）。操作の依頼（「確認してください」など）は含めない。
_REQUEST = re.compile(
    r".*(?:教えてください|教えていただけますか|お知らせください|お教えください|お聞かせください"
    r"|ご回答ください|ご返答ください|お答えください|確認させてください|お伺いします|伺います"
    # 確認の質問に添える回答の依頼（「ご回答をお願いします。」。#1322）。
    r"|(?:ご回答|ご返答|お答え|お返事|ご返信)を?お願い(?:します|いたします|致します)"
    # 選択肢から選んでもらう依頼（「ご希望の方をお選びください。」。#1322）。
    r"|(?:どちらか|いずれか|ご希望の(?:方|もの))を?お選びください)[。．!！]?"
)
# 質問に抱き合わせた主張（「〜できますが、よろしいですか？」）。
_ADVERSATIVE = re.compile(r"(?:ますが|ですが|ましたが|ませんが|ものの|けれど|けど|ただし)")

_ABSENCE_SUBJECT = re.compile(
    r"資料|文書|マニュアル|ドキュメント|規程|規定|ガイド|ナレッジ|検索|記載|記述|根拠|証拠|手順書"
)
_ABSENCE_END = re.compile(
    r"(?:記載|記述|説明|情報|言及|該当(?:する)?(?:箇所|内容|資料|記載)?|根拠|定め|規定)"
    r"[^。]{0,24}?(?:ありません|見つかりません|見当たりません|されていません|含まれていません|無く|なく"
    r"|ない|無い)(?:でした)?[。．]?$"
    r"|(?:確認|特定|判断|判別|把握)(?:でき|出来)ません(?:でした)?[。．]?$"
    r"|確かめられません(?:でした)?[。．]?$"
    r"|(?:分かり|わかり)ません(?:でした)?[。．]?$"
    r"|(?:お答え|回答|ご案内|案内)(?:でき|出来)ません(?:でした)?[。．]?$"
    # 資料に記載が無いので分からない（#1317。「…資料に記載がないため、不明です。」）。
    r"|不明(?:です|でした)?[。．]?$"
)
# 文末の補足の括弧（「…含まれていませんでした（取得した全証拠の抜粋を参照）。」。#1317）。
_TRAILING_NOTE = re.compile(r"\s*[（(][^（()）\n]{1,60}[）)](?=\s*[。．]?\s*$)")
# 検索の結果・資料に該当が無かった文（#1317。「検索結果でも、該当する業務ガイドは返ってきません
# でした。」）。画面の検索の振る舞い（「利用者を検索してもヒットしません」）と区別するため、主語を
# 検索の結果・資料に限る。
_NOT_FOUND_SUBJECT = re.compile(r"検索結果|業務ガイド|資料|文書|根拠|証拠|ナレッジ")
_NOT_FOUND_END = re.compile(r"(?:返って(?:き|こ)|返され|ヒットし|得られ)ません(?:でした)?[。．]?$")
# 答えられないと言い切る拒答の文（#1317。「推測で金額を示すことはできません。」「根拠のある回答は
# できません。」）。答える・示す動詞に限る（「削除することはできません」のような事実の主張は除く）。
_REFUSAL_END = re.compile(
    r"(?:(?:示す|お示しする|提示する|明示する|答える|お答えする|回答する|案内する|ご案内する"
    r"|伝える|お伝えする|断定する)こと(?:は|が|も)?"
    r"|(?:回答|ご回答|お答え|ご案内|案内|お示し|提示|明示|お伝え|断定)(?:は|を|も)?)"
    r"(?:でき|出来)(?:ません|かねます)(?:でした)?[。．]?$"
    r"|(?:示せ|答えられ)ません(?:でした)?[。．]?$"
)
# 不足の文に混ざった主張（数量・逆接・「〜は〜で、」の言い切り）。
_QUANTITY = r"[0-9０-９]+\s*(?:円|日|時間|分|秒|件|%|％|回|か月|ヶ月|カ月|年|名|人|GB|MB)"
_ABSENCE_CLAIM = re.compile(
    _QUANTITY + r"|ますが|ですが|ものの|けれど|けど|ただし"
    r"|(?:は|が)[^。、]{1,20}(?:で|であり|となり)、"
)
# 業務ガイドが見つからなかったことだけを述べる文（#1452。「補足：この内容に当たる公開の
# 業務ガイドは見つかりませんでした（`rag_lookup_guides` の結果は0件）。」）。業務ガイドの検索の
# 結果は検証の根拠に無いので、主張として確かめると必ず外れる。前置きは、業務ガイドを修飾する
# だけの語（は・が・を・数字・引用を含まない）に限る（「部門長の承認が要る業務ガイドはありません」は
# 主張）。RAG の `operation_audit.is_guide_not_found` と同じ規則で、両方のテストが
# platform/contracts/answer-passages/process-notes.json の同じ事例で確かめる。
_GUIDE_NOT_FOUND = re.compile(
    rf"(?:[^。:：\n]{{1,8}}[:：]\s*)?(?:なお|また|ただし)?[、,]?\s*(?P<lead>[^。\n]{{0,30}}?){_v('guide_words')}"
    rf"(?:は|が|も)[、,]?(?:まだ)?{_v('guide_not_found_endings')}(?:でした)?[。．]?"
)
_GUIDE_NOT_FOUND_LEAD = re.compile(r"[はがを0-9０-９「」『』]")
# 文末の、ツールの名前を挙げた作業の経過の括弧（「（rag_lookup_guides の結果は0件）」）。
_TOOL_NOTE = re.compile(r"\s*[（(][^（()）\n]*\brag_[a-z_]+[^（()）\n]*[）)](?=\s*[。．]?\s*$)")
# 分岐の見出し・ラベル（#1317。「個別利用者に付与する場合」「原因 A の場合の対処：」）。
_CASE_LABEL = re.compile(
    r"(?P<label>[^。\n]{1,40}?(?:場合|とき)(?:の(?:手順|対処|操作|対応|方法))?)\s*(?:は)?\s*[:：]?"
)
_CASE_LEAD = re.compile(
    r"(?P<label>[^。:：\n]{1,40}?(?:場合|とき)(?:の(?:手順|対処|操作|対応|方法))?)\s*[:：]"
)
# 現場のデータ・記録の確認を求める段落（#1317）の、確かめる対象と確かめる依頼の語。
# 「ログイン」「ログアウト」はログではない。文書・資料を読むよう促す文（知識の不足）は含めない。
_ENVIRONMENT_TARGET = re.compile(
    r"ログ(?!イン|アウト|ラム)|\.log\b|記録|履歴|実データ|実際の(?:値|データ|件数|合計|明細|設定)"
    r"|設定値|(?:現在|今)の(?:値|設定|状態)|件数|明細|ステータス|画面で",
    re.IGNORECASE,
)
_ENVIRONMENT_CHECK = re.compile(
    r"ご確認ください|(?:確認|照合|比較|突き合わせ|チェック)(?:して(?:ください|下さい|いただ)|します|し[、て])"
    r"|確か(?:めてください|めます|め[、て])"
    r"|(?:確認|照合|比較|チェック)(?:する必要が(?:あります|ある)|が必要(?:です)?)"
    r"|確かめる必要が(?:あります|ある)"
)
# 利用者に聞けば分かる条件（自分のロール・権限・承認、質問で特定されていない対象）を
# 確かめる語（#1453）。これは現場のデータ・記録ではないので、記録そのもの（ログ・履歴・
# 設定値・件数・明細）を確かめる語が無ければ、現場の確認を求める段落にしない。
_USER_CONDITION = re.compile(
    r"ロール(?:を|が)(?:持|お持|付与され)|権限(?:を|が)(?:持|お持)|承認(?:を|が)(?:得|取|受け|もら|あ|な)"
    r"|(?:どの|対象の?)(?:システム|申請|アカウント|利用者|グループ)"
)
_RECORD_TARGET = re.compile(
    r"ログ(?!イン|アウト|ラム)|\.log\b|記録|履歴|設定値|件数|明細|合計", re.IGNORECASE
)
# 出典の行に無い述語（括弧の外にあれば文。「…は **毎月 10 日** です（…）【….pdf】.」。#1317）。
_PREDICATE_IN = re.compile(r"です|ます|ません|でした|ました|ください")
_BRACKETS_ANY = re.compile(r"[（(【〔\[「『][^（()）【】〔〕\[\]「」『』\n]*[）)】〕\]」』]")
# 括弧で囲まれた段落（「（根拠：検索結果に…記述は含まれていません）」。#1317）。
_ENCLOSED = re.compile(r"[（(](?P<inner>[^\n]+)[）)]")
# 記号だけの段落（「**」「---」「>」。強調の閉じが句点の後の段落に分かれたもの。#1317）。
_MARKUP_ONLY = re.compile(r"[*_`>#|~\-\s]+")


def _strip_bullet(text: str) -> str:
    return _BULLET.sub("", text.strip(), count=1).strip()


def _is_sentence(title: str) -> bool:
    return bool(_PREDICATE_END.search(title.strip().rstrip("*").strip()))


def _heading_title(text: str) -> str | None:
    """見出しなら見出しの語（見出しでなければ None）。"""
    value = normalize_heading_markup(text).strip()
    for pattern in (_MD_HEADING, _BOLD_LINE, _BRACKET_HEADING):
        match = pattern.fullmatch(value)
        if match:
            title = match.group("title").strip().strip("*").strip()
            # 語に数量（「最長 90 日」）を含む行は、見出しの記法でも主張（#2625。RAG の
            # `operation_audit.is_value_heading` と同じ規則）。
            if (
                title
                and not _is_sentence(title)
                and not _OPERATION.search(title)
                and not _HEADING_QUANTITY.search(title)
                and "は" not in _HEADING_QUOTED.sub(" ", title)
            ):
                return title
            return None
    label = _strip_bullet(value)
    match = _LABEL_HEADING.fullmatch(label)
    note = _LABEL_NOTE.search(label) if match is None else None
    if note and not _is_sentence(note.group("note")) and not _OPERATION.search(note.group("note")):
        # ラベルの後ろの注記の括弧（「補足（資料の注記。同シート A1:A2）：」）は除いて確かめる。
        match = _LABEL_HEADING.fullmatch(label[: note.start()] + label[note.end() :])
    if match:
        title = match.group("title").strip().strip("*").strip()
        if (
            title
            and not _is_sentence(title)
            and not _OPERATION.search(title)
            and not _HEADING_QUANTITY.search(title)
            and "は" not in _HEADING_QUOTED.sub(" ", title)
        ):
            return title
    phrase = heading_phrase(value)
    if phrase is not None:
        return phrase
    return value if is_lead_in(value) else None


# ---- 見出しの名詞句と導入の文（#1469）。RAG の `operation_audit.heading_phrase` /
# `is_lead_in` と同じ規則で、両方のテストが platform/contracts/answer-passages/headings.json の
# 同じ事例で確かめる。
# 見出しの後ろの注記の括弧（「補足（資料の記載）」
# 「2. 略号の意味（組織規程 第 2 章 部署と略号／1 頁）」）。
_HEADING_NOTE = re.compile(r"[（(](?P<note>[^（）()\n]{1,60})[）)]\s*$")
# 「…」の語（「略号「運」の意味」）。中の「は」「が」は言い切りの助詞に数えない。
_HEADING_QUOTED = re.compile(r"[「『][^「」『』\n]{1,40}[」』]")
# 数量（「30 分」「10 年」。主張の値）。年度（「2026年度」）・行・頁の番号は数えない。
_HEADING_QUANTITY = re.compile(
    r"[0-9０-９]+\s*(?:円|日|時間|分|秒|件|%|％|回|か月|ヶ月|カ月|年(?!度)|名|人|GB|MB)"
)
# 見出しの語に残してよい番号（章・節の番号と年度）。ほかの数字（値の「3 点」「SYS-103」）を
# 含む行は主張のまま。
_HEADING_NUMBERED_NAME = re.compile(r"第\s*[0-9０-９]+\s*[章節条項]|[0-9０-９]+\s*年度")
# 見出しの名詞句の終わりの語（項目・区分・資料の名前。値の名前「運行管理課長」「極秘」では
# 終わらない）。
_HEADING_END = re.compile(rf"{_v('heading_end_words')}$")
# 番号（「1.」「（2）」）と箇条書きの印（「- 」「・」。強調の「**」は印に数えない）。番号つき・
# 箇条書きの行は段落だけでは決めず、次の行がその下の内容のときだけ見出し（`list_heading_lines`。
# #1481 / #1482 / #1486）。番号・箇条書きの行が続く一覧の項目は主張のまま。
_HEADING_NUMBER = re.compile(r"^(?:[0-9０-９]{1,3}[.)．）]|[（(][0-9０-９]{1,3}[）)])")
_HEADING_BULLET = re.compile(r"^(?:[-+・•]|\*(?=\s))")
# 「…の…」の名詞句（「重要度 B の復旧目標」「連携元のシステム」）。「ので」「のに」「のみ」は名詞を
# つなぐ「の」に数えない。
_HEADING_NO_PHRASE = re.compile(r"[^\sの]\s*の(?![でにみ])\s*[^\sの]")
# 番号つきの見出しの下の内容の行（箇条書き・「→」。字下げした行も、字下げした番号の行も下の内容）。
# 箇条書きの見出しの下の内容は、字下げした行と「→」の行だけ（同じ字下げの箇条書きの行は同じ一覧の
# 次の項目。#1486）。
_SUB_CONTENT = re.compile(r"[-*+]\s|[・•→]")
_BULLET_SUB_CONTENT = re.compile(r"→")
# 値の名前（人・組織・役職）の終わり。番号つきの「1. 情報システム部の課長」は見出しではなく値の
# 主張のまま。
_HEADING_VALUE_END = re.compile(rf"{_v('heading_value_end_words')}$")
# 番号・箇条書きの印の後が太字のラベルだけの行（「1. **実施権限**」「- **アカウント削除**：」。
# #1898）。終わりの語が一覧に無く「…の…」でもない短い名詞でも、太字だけで書いた番号つき・
# 箇条書きの行は見出しの形（次の行で決める）。
_HEADING_BOLD_LABEL = re.compile(r"\*\*[^*\n]{1,30}\*\*\s*[:：]?")
# 分岐・問いの形の見出しの終わり（この形だけ、中の「が」「を」を許す）。
_HEADING_CLAUSE_END = re.compile(r"(?:とき|場合|か)$")
# 見出しの中の、分岐の節（「承認者が不在のときの代理」の「が不在のとき」）。
_HEADING_CASE_CLAUSE = re.compile(r"[がを][^はがを、。]{1,15}?(?:とき|場合)")
# 導入の文の先頭の接続の語（「そのため、作業できる時間帯は次のとおりです。」。この後の「、」は
# 主語の区切りに数えない。#1481）。
_LEAD_IN_CONNECTIVE = re.compile(rf"^{_v('lead_in_connectives')}[、，,]\s*")
# 以降の箇条書きを導くだけの導入の文（「根拠は次のとおりです。」「以下に示します。」）。
_LEAD_IN = re.compile(
    rf"(?P<subject>[^。！？!?\n]{{1,24}}?){_v('lead_in_subject_particles')}、?\s*{_v('lead_in_next_words')}の"
    rf"{_v('lead_in_forms')}{_v('copulas')}?[。．:：]?"
    rf"|(?:(?P<object>[^。！？!?\n]{{1,24}}?)を)?{_v('lead_in_next_words')}に{_v('lead_in_verbs')}ます[。．:：]?"
)
# 利用者の答え・質問への礼の定型の文（「ご回答ありがとうございます。」）。
_COURTESY = re.compile(rf"{_v('courtesy_objects')}(?:いただき)?、?ありがとうございます[。．!！]?")
# 回答の締めくくりの定型の文（#1833。「以上、資料に基づく回答です。」「以上です。」）。
# 語の組み合わせを固定し、事実の語（値・名前・条件）が入る余地を置かない（検証の抜け道に
# しない）。RAG の `operation_audit._CLOSING` と同じ規則。
_CLOSING = re.compile(
    rf"以上{_v('copulas')}[。．]?"
    rf"|(?:以上[、，,]?\s*(?:が)?\s*)?(?:{_v('closing_source_verbs')}{_v('closing_source_verb_forms')}\s*)?"
    rf"{_v('closing_sources')}(?:の(?:内容|記載))?に{_v('closing_based_on')}"
    rf"{_v('closing_kinds')}{_v('copulas')}[。．]?"
)
# 前の内容をまとめて指すだけの結びの文（#1879。「以上が、締め日を過ぎた場合の取扱いです。」）と
# 結びのあいさつ（「ご参考になれば幸いです。」）。指す対象（topic）は質問の対象を名詞で言い直す
# 語に限り、言い切りの「は」・数量・操作・引用を含むもの（「以上が、締め日は 25 日という取扱い
# です。」）は主張のまま。RAG の `operation_audit` と同じ規則。
_CLOSING_SUMMARY = re.compile(
    r"以上(?:が|で)[、，,]?\s*(?P<topic>[^。！？!?\n「」『』]{0,40}?)"
    rf"{_v('closing_summary_nouns')}{_v('copulas')}[。．]?"
)
_CLOSING_WISH = re.compile(
    rf"(?:少しでも)?{_v('closing_wish_objects')}{_v('closing_wish_conditions')}幸いです[。．!！]?"
)


def _heading_note_ok(note: str) -> bool:
    """見出しの注記の括弧が、言い切り・操作・数量を含まない補足か（「（承認者は人事部長です）」は主張）。"""
    outside = _HEADING_QUOTED.sub(" ", note)
    return (
        "。" not in note
        and not _is_sentence(note)
        and not _OPERATION.search(note)
        and not _HEADING_QUANTITY.search(note)
        and "は" not in outside
    )


def heading_phrase(text: str) -> str | None:
    """主張でない見出しの名詞句なら、注記の括弧・番号・強調を除いた見出しの語（#1469）。

    見出しでなければ None。「補足（資料の記載）」「根拠」「回答に使った資料」「**重要度 C の
    システムで障害が起きたとき**」のように、句点で終わらない短い名詞句で、項目・区分・資料の名前
    （`_HEADING_END`）か分岐・問いの形（「…とき」「…か」）で終わるもの。述語（です・ます・する…）・
    操作・数量・言い切りの「は」を含む行（「重要度は A です」「承認者は管理本部長」）は主張のまま。
    番号つき・箇条書きの行（「1. 人事評価システムの重要度」「- 組織規程（ご担当部署の承認者）」）は
    段落だけでは見出しにせず、回答の中の次の行で決める（番号・箇条書きの行が続く一覧の項目は主張。
    `list_heading_lines`。#1481 / #1482 / #1486）。
    """
    found = _heading_value(text)
    if not found or _is_list_item(text):
        return None
    return found[0] if _HEADING_END.search(found[0]) else None


def _is_list_item(text: str) -> bool:
    """番号つきか箇条書きの行か（「1. …」「（2）…」「- …」「・…」。#1482 / #1486）。"""
    value = text.strip()
    return bool(_HEADING_NUMBER.match(value) or _HEADING_BULLET.match(value))


def _list_heading_phrase(text: str) -> str | None:
    """番号つき・箇条書きの見出しの語（「1. 人事評価システムの重要度」。#1481 / #1482 / #1486）。

    終わりの語が一覧（`_HEADING_END`）にあるか、一覧に無くても「…の…」の名詞句か
    （「2. 重要度 B の復旧目標」「- 重要度 B の復旧目標」）、印の後が太字のラベルだけの行
    （「1. **実施権限**」「3. **アカウント削除**」。#1898）のもの。人・組織・役職の名前で終わる
    「…の…」・太字の行（「1. 情報システム部の課長」「1. **人事部長**」）と、述語・操作・数量・
    言い切りの「は」を含む行（「1. **ID 管理システムの重要度は A**」）は値の主張のまま。見出しか
    どうかは次の行で決める（`list_heading_lines`）。
    """
    found = _heading_value(text)
    if not found or not _is_list_item(text):
        return None
    value, outside = found
    if _HEADING_END.search(value):
        return value
    if _HEADING_VALUE_END.search(value):
        return None
    bold = _HEADING_BOLD_LABEL.fullmatch(_strip_bullet(text))
    if bold or _HEADING_NO_PHRASE.search(outside):
        return value
    return None


def list_heading_lines(text: str) -> set[str]:
    """回答の中の、番号つき・箇条書きの見出しの行（前後の空白を除いた行。#1481 / #1482 / #1486）。

    番号つき・箇条書きの見出しの語（`_list_heading_phrase`）の行で、次の（空でない）行がその下の内容
    のもの。番号つきの行の下の内容は、字下げした行か、箇条書き・「→」の行。箇条書きの行の下の内容は、
    字下げした行（下位の箇条書き）か「→」の行で、同じ字下げの箇条書きの行は同じ一覧の次の項目。
    番号・箇条書きの行が続く一覧（「申請書に書く 4 項目」の「1. 変更の内容と理由」〜
    「4. 作業の日時」、「ご確認いただきたい点」の「- 組織規程（ご担当部署の承認者）」）の項目と、
    下に内容の無い行は、終わりの語が一覧にあっても主張のまま（段落だけで決めると、一覧の項目を
    見出しとして監査から外す。#1482 / #1486）。RAG の `operation_audit.list_heading_lines` と
    同じ規則。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    found = set()
    for line, following in zip(lines, lines[1:], strict=False):
        indent = len(line) - len(line.lstrip())
        sub_content = _BULLET_SUB_CONTENT if _HEADING_BULLET.match(line.strip()) else _SUB_CONTENT
        below = len(following) - len(following.lstrip()) > indent or bool(
            sub_content.match(following.strip())
        )
        if below and _list_heading_phrase(line):
            found.add(line.strip())
    return found


def _heading_value(text: str) -> tuple[str, str] | None:
    """見出しの名詞句の共通の確かめ（注記の括弧・番号・強調を除いた語と、「…」の語を除いた残り）。"""
    if is_citation(text):
        # 文書名と場所だけの行（「1. **システム変更手順書**（第 2章 申請）」）は出典の行（#1370）。
        return None
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip().rstrip(":：").strip()
    while match := _HEADING_NOTE.search(value):
        if not _heading_note_ok(match.group("note")):
            return None
        value = value[: match.start()].strip()
    if not value or len(value) > 30 or re.search(r"[。．！？!?、,，:：]", value):
        return None
    if (
        _is_sentence(value)
        or _OPERATION.search(value)
        or re.search(r"[0-9０-９]", _HEADING_NUMBERED_NAME.sub(" ", value))
    ):
        return None
    outside = _HEADING_QUOTED.sub(" ", value)
    if "は" in outside:
        return None
    # 「が」「を」は、分岐・問いの形の見出しか、「…が不在のとき」の節の中だけに許す。
    if not _HEADING_CLAUSE_END.search(value) and re.search(
        r"[がを]", _HEADING_CASE_CLAUSE.sub(" ", outside)
    ):
        return None
    return value, outside


# 表題を示すだけで、表の値を述べない導入行。
_TABLE_LEAD_IN = re.compile(
    rf"{_v('table_lead_in_labels')}[「『][^「」『』。\n]{{1,80}}[」』](?:では|は)[、，,:：]?"
)


def is_lead_in(text: str) -> bool:
    """以降の箇条書きを導くだけの導入の文（「根拠の積み上げは次のとおりです。」）と礼・締めくくりの
    定型の文か（#1833。「以上、資料に基づく回答です。」）。

    先頭の接続の語の後の「、」（「そのため、作業できる時間帯は次のとおりです。」）は許す（#1481）。
    主語・目的語に数量・操作・言い切りの「は」・「、」を含む文（「保管期間は 10 年で、内訳は
    次のとおりです。」）は主張のまま（#1469）。
    """
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    if (
        _TABLE_LEAD_IN.fullmatch(value)
        or _COURTESY.fullmatch(value)
        or _CLOSING.fullmatch(value)
        or _CLOSING_WISH.fullmatch(value)
    ):
        return True
    summary = _CLOSING_SUMMARY.fullmatch(value)
    if summary:
        topic = summary.group("topic")
        return not (_HEADING_QUANTITY.search(topic) or _OPERATION.search(topic) or "は" in topic)
    match = _LEAD_IN.fullmatch(_LEAD_IN_CONNECTIVE.sub("", value, count=1))
    if not match:
        return False
    head = match.group("subject") or match.group("object") or ""
    return not (
        _HEADING_QUANTITY.search(head) or _OPERATION.search(head) or "は" in head or "、" in head
    )


# 利用者に行わせる指示の文末（「…を訂正する。」「…してください。」「…を確認します。」。#2604）。
# 述語の形だけで決め、業務の語は見ない。「です」で言い切る事実の文と、「…できます」「…あります」
# 「…が発生します」（目的語の「を」の無い「します」）の状態の文は含めない。
_INSTRUCTION_END = re.compile(
    r"(?:する|しない|しておく|ておく|てください|でください|て下さい|すること|しましょう"
    r"|を[^。、]{1,40}?(?:します|しておきます)|を行(?:う|います))[。．!！]?$"
)


def is_operation_step(text: str) -> bool:
    """利用者に操作・作業を行わせる段落か（番号つきの手順の項目の判定。#2604）。

    操作の指示（`_OPERATION`）を含むか、文末が指示の形（`_INSTRUCTION_END`）のもの。箇条書き・
    番号の印・強調と、文末の補足の括弧は除いて確かめる。事実を述べる文（「上限は 90 日です。」）は
    含めない。
    """
    value = _TRAILING_NOTE.sub("", _EMPHASIS.sub("", _strip_bullet(text))).strip()
    return bool(_OPERATION.search(value) or _INSTRUCTION_END.search(value))


def list_item_label(text: str) -> str | None:
    """番号つきの項目の段落が名詞句の名前だけなら、番号・強調・注記の括弧を除いた語（#2692）。

    「1. **対象 A の属性訂正（記録修正）**」→「対象 A の属性訂正」。文・操作の指示・数量・
    言い切りの「は」を含む項目（「1. 対象 A を訂正する。」）と、番号の無い行は None。
    """
    if not _HEADING_NUMBER.match(text.strip()):
        return None
    found = _heading_value(text)
    return found[0] if found else None


# 名前の語（漢字・片仮名・英数字の並び。仮名の助詞・送り仮名と記号で切る。#2692）。
_NAME_RUN = re.compile(r"[一-鿿々〆ヶァ-ヺー0-9A-Za-z]+")
# 名前が段落と同じ操作を指すとみなす、名前の語の最小の字数と、段落の語に占める最小の割合。
# 短い名前（「申請」）と、長い段落の一部の語だけに当たる名前は対応させない。
_LABEL_MIN_CHARS = 4
_LABEL_MIN_SHARE = 0.4


def _name_runs(text: str) -> list[str]:
    return _NAME_RUN.findall(re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)))


def _covered_end(run: str, text: str, pieces: list[str]) -> int | None:
    """名前の語 `run` を段落の語 `pieces` の 2 字以上の部分（1 字の語はそのまま）で書けるとき、
    最後の部分が段落 `text` の中で終わる位置（書けなければ None）。"""
    if len(run) == 1:
        return text.rfind(run) + 1 if any(run in piece for piece in pieces) else None
    # reach[i]: run[:i] を書ける最後の部分（無ければ None）。
    reach: list[str | None] = [None] * (len(run) + 1)
    reach[0] = ""
    for start in range(len(run)):
        if reach[start] is None:
            continue
        for end in range(start + 2, len(run) + 1):
            part = run[start:end]
            if any(part in piece for piece in pieces):
                reach[end] = part
    last = reach[len(run)]
    return text.rfind(last) + len(last) if last else None


def label_operation_end(label: str, text: str) -> int | None:
    """名前 `label`（`list_item_label`）が段落 `text` と同じ操作を名前だけで指すとき、名前の
    最後の語が段落の中で終わる位置（指さなければ None。#2692）。

    名前の語（漢字・片仮名・英数字の並び）がすべて段落の語の部分で書け（「属性訂正」は「属性を
    先に訂正する」の「属性」と「訂正」）、名前が段落の語の一定の割合を占めるとき対応させる。業務の
    語は見ない。位置は、1 つの段落が複数の名前を含むとき、文末の述語に近い名前（主の操作）を
    選ぶのに使う。
    """
    runs = _name_runs(label)
    size = sum(len(run) for run in runs)
    flat = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    pieces = _NAME_RUN.findall(flat)
    if size < _LABEL_MIN_CHARS or size < _LABEL_MIN_SHARE * sum(len(piece) for piece in pieces):
        return None
    end: int | None = None
    for run in runs:
        end = _covered_end(run, flat, pieces)
        if end is None:
            return None
    return end


def is_heading(text: str) -> bool:
    """見出しだけの段落か。"""
    return _heading_title(text) is not None


def is_structure(text: str) -> bool:
    """見出し・出典・表の区切りだけの段落か（本文から内容を外した後に、それだけ残っても意味が無いもの）。"""
    return is_heading(text) or is_citation(text) or is_table_rule(text)


def is_table_rule(text: str) -> bool:
    """表の区切りの行（`|---|---|`）・区切り線（`---`）・記号だけの段落（`**`）か（#1317）。"""
    value = text.strip()
    return bool(
        _TABLE_SEPARATOR.fullmatch(value) or _RULE.fullmatch(value) or _MARKUP_ONLY.fullmatch(value)
    )


def _outside_brackets(value: str) -> str:
    """括弧（（）【】「」[]）の中を除いた文字列。"""
    while True:
        stripped = _BRACKETS_ANY.sub(" ", value)
        if stripped == value:
            return value
        value = stripped


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_header_cell(cell: str) -> bool:
    value = _EMPHASIS.sub("", cell).strip()
    return (
        len(value) <= 20
        and "。" not in value
        and not _is_sentence(value)
        and not _OPERATION.search(value)
    )


def table_header_lines(answer: str) -> set[str]:
    """Markdown の表の見出しの行（区切りの行の直前の、短い語だけのセルの行。#1317）。

    見出しかどうかは次の行で決まるので、回答全体から判定する。表の本文の行は主張のまま。
    """
    lines = [line.strip() for line in answer.split("\n")]
    return {
        line
        for line, following in zip(lines, lines[1:], strict=False)
        if _TABLE_ROW.fullmatch(line)
        and "|" in following
        and is_table_rule(following)
        and any(_EMPHASIS.sub("", cell).strip() for cell in _table_cells(line))
        and all(_is_header_cell(cell) for cell in _table_cells(line))
    }


def _document_title(title: str) -> bool:
    return bool(
        re.search(r"手順書|マニュアル|ガイド|規程|規定|資料|文書|メモ|第\s*[0-9０-９]+\s*版", title)
    )


def _is_reference_only(value: str) -> bool:
    """根拠の ID・文書名・頁の括弧・文の無い「…」の題（と節の参照）だけの段落か（#1317）。

    「| 「1. 前提」 （ページ1）【証拠 ID: 03ac…:1】 |」（表のセルに分かれた出典）や
    「- 「サンプル業務ポータル 運用手順書 第3版」 6. アカウントの削除」。文書を示す印（根拠の ID・
    文書名・頁の括弧・文書の題）が 1 つも無ければ出典にしない（短い「…」だけの行は主張のまま）。
    """
    if _OPERATION.search(value) or _is_sentence(value.strip("|").strip()):
        return False
    found = False

    def drop_document(match: re.Match[str]) -> str:
        nonlocal found
        if _DOCUMENT_MARK.search(match.group(1)):
            found = True
            return " "
        return match.group(0)

    def drop_title(match: re.Match[str]) -> str:
        nonlocal found
        title = match.group(1)
        if "。" in title or _is_sentence(title) or _OPERATION.search(title):
            return match.group(0)
        found = found or _document_title(title)
        return " "

    rest, count = _EVIDENCE_REF.subn(" ", value)
    found = count > 0
    rest = _BRACKET_PART.sub(drop_document, rest)
    rest = _TITLE_QUOTE.sub(drop_title, rest)
    rest, count = _FILE_NAME.subn(" ", rest)
    found = found or count > 0
    rest = _REFERENCE_FILLER.sub(" ", rest).strip()
    if not found:
        return False
    return not rest or bool(_SECTION_REF.fullmatch(rest) and not _is_sentence(rest))


# 行末の場所の括弧（「（第 2章 申請）」「（p.12）」。#1370）。
_LOCATION_TAIL = re.compile(r"[（(〔\[]([^（()）〔〕\[\]\n]{1,80})[）)〕\]]\s*$")
# 文書名に無い、言い切りの助詞（「承認者は課長（第 3 章）」「申請先は規程が定める承認者」は主張）。
_STATEMENT_PARTICLE = re.compile(r"[はがを]")


def _is_document_location(value: str) -> bool:
    """文書名と場所の括弧だけの段落か（#1370）。

    「システム変更手順書（第 2章 申請）」「運用マニュアル（p.12）」。

    行末の場所の括弧（章・節・頁・シートなどの印を含むもの）を外した残りが、文書を示す語（手順書・規程・
    ファイル名など）を含む短い名前で、述語・操作・言い切りの助詞（は・が・を）を含まないときだけ出典にする。
    場所の後に本文が続く行（「…（第 2章）では、…」）は主張のまま。RAG の
    `operation_audit._is_document_location` と同じ規則。
    """
    rest = value.strip().rstrip("。．").strip()
    found = False
    while match := _LOCATION_TAIL.search(rest):
        location = match.group(1)
        if (
            not _DOCUMENT_MARK.search(location)
            or _is_sentence(location)
            or _OPERATION.search(location)
        ):
            break
        rest, found = rest[: match.start()].strip(), True
    if not found or not rest or len(rest) > 80 or "。" in rest:
        return False
    if _is_sentence(rest) or _OPERATION.search(rest):
        return False
    if _STATEMENT_PARTICLE.search(_outside_brackets(rest)):
        return False
    return bool(_document_title(rest) or _FILE_NAME.search(rest))


# 行末に続く章・節・条・項の番号（「第12条第5項」「第 8 章 第105条」「第6条の2」。#2225）。
_TRAILING_SECTIONS = re.compile(r"(?:\s*第\s*[0-9０-９]+\s*[章節条項](?:\s*の\s*[0-9０-９]+)?)+$")
# 文書名に数えない文字（数量・値の番号・句読点・引用）。
_SECTION_NAME_EXCLUDED = re.compile(r"[0-9０-９。．、,，:：「」『』]")


def _is_document_section(value: str) -> bool:
    """文書名と条・項・章などの番号（と見出しの括弧）だけの段落か（#2225）。

    「サンプル法 第12条第5項（届出）」「第 8 章 第105条第1項（届出等）」。行末の括弧（条の見出し）を
    外した残りが章・節・条・項の番号で終わり、その前の名前が短く、数字・句読点・言い切りの助詞
    （は・が・を）・述語・操作を含まないときだけ出典にする。見出しの括弧に文・数量があるもの
    （「第12条（14 日以内）」）、番号の後に本文が続くもの（「第12条により…」）は主張のまま。RAG の
    `operation_audit._is_document_section` と同じ規則。
    """
    rest = value.strip().rstrip("。．").strip()
    if match := _LOCATION_TAIL.search(rest):
        caption = match.group(1)
        if "。" in caption or _is_sentence(caption) or _OPERATION.search(caption):
            return False
        if _HEADING_QUANTITY.search(caption):
            return False
        rest = rest[: match.start()].strip()
    sections = _TRAILING_SECTIONS.search(rest)
    if not sections:
        return False
    name = rest[: sections.start()].strip()
    if len(name) > 40 or _SECTION_NAME_EXCLUDED.search(name):
        return False
    return not name or not (
        _STATEMENT_PARTICLE.search(name) or _is_sentence(name) or _OPERATION.search(name)
    )


def _is_location_value(value: str) -> bool:
    """場所のラベルの値が文書の場所だけか（#1833。「第 2 章「締め日と支払日」」「p.3」）。

    章・節・頁・シート・ファイル名の印を含み、節の題（文でない「…」）・場所の括弧・ファイル名・頁を
    外した残りが、空か、節の参照か、文書の名前（手順書・マニュアル・規程など）だけのもの。数量・言い切りの
    助詞（は・が・を）・述語・操作を含む値（「本社 3 階の会議室（第 2 章）」）は主張のまま。RAG の
    `operation_audit._is_location_value` と同じ規則。
    """
    if not _DOCUMENT_MARK.search(value) or _is_sentence(value) or _OPERATION.search(value):
        return False

    def drop_title(match: re.Match[str]) -> str:
        title = match.group(1)
        if "。" in title or _is_sentence(title) or _OPERATION.search(title):
            return match.group(0)
        return " "

    def drop_document(match: re.Match[str]) -> str:
        return " " if _DOCUMENT_MARK.search(match.group(1)) else match.group(0)

    rest = _TITLE_QUOTE.sub(drop_title, _BRACKET_PART.sub(drop_document, value))
    rest = _REFERENCE_FILLER.sub(" ", _FILE_NAME.sub(" ", rest)).strip()
    if not rest:
        return True
    if (
        _STATEMENT_PARTICLE.search(rest)
        or _HEADING_QUANTITY.search(rest)
        or "「" in rest
        or "『" in rest
    ):
        return False
    return bool(_SECTION_REF.fullmatch(rest) or (len(rest) <= 60 and _document_title(rest)))


def is_citation(text: str) -> bool:
    """出典の行か。

    出典・定位子・場所のラベル・文書名・頁・節の括弧・リンク・根拠の ID・文書名と場所だけの段落。
    RAG の `operation_audit.is_citation_line` と同じ規則で、両方のテストが
    platform/contracts/answer-passages/citation-lines.json の同じ事例で確かめる（#1370・#1833）。
    """
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    if not value or "。" in value.rstrip("。"):
        return False
    # 括弧の外に述語があれば文（出典を添えた主張）。
    if _PREDICATE_IN.search(_outside_brackets(value)):
        return False
    label = _CITATION_LABEL.fullmatch(value)
    if label:
        return not _is_sentence(label.group("rest")) and not _OPERATION.search(label.group("rest"))
    # 場所のラベルは値が文書の場所のときだけ出典にし、そうでなければ他の出典の形（文書名と
    # 場所・題と頁など）で決める。
    location = _LOCATION_LABEL.fullmatch(value)
    if location and _is_location_value(location.group("rest")):
        return True
    titles = _TITLE_QUOTE.findall(value)
    if (
        titles
        and not any(
            _STATEMENT_PARTICLE.search(title) or _HEADING_QUANTITY.search(title) for title in titles
        )
        and _is_location_value(value)
    ):
        return True
    if _LINK_LINE.fullmatch(value):
        return True
    if _BRACKETED.fullmatch(value) and all(
        _DOCUMENT_MARK.search(part) for part in _BRACKET_PART.findall(value)
    ):
        return True
    if _FILE_LINE.fullmatch(value) and not _is_sentence(value):
        return True
    return _is_reference_only(value) or _is_document_location(value) or _is_document_section(value)


def is_question(text: str) -> bool:
    """利用者への質問・情報を求める依頼の文か（主張や操作を抱き合わせた文は除く）。"""
    value = _strip_bullet(text)
    if not (_QUESTION.fullmatch(value) or _REQUEST.fullmatch(value)):
        return False
    return not _ADVERSATIVE.search(value)


def is_absence(text: str) -> bool:
    """資料に記載が無い・資料からは確かめられないことだけを述べる文か、答えられないと言い切る拒答の文か。

    強調（`**…**`）・段落を囲む括弧・文末の補足の括弧は判定の前に外す（#1317）。
    """
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    enclosed = _ENCLOSED.fullmatch(value)
    if enclosed:
        value = enclosed.group("inner").strip()
    value = _TRAILING_NOTE.sub("", value)
    absent = (
        (_ABSENCE_SUBJECT.search(value) and _ABSENCE_END.search(value))
        or (_NOT_FOUND_SUBJECT.search(value) and _NOT_FOUND_END.search(value))
        or _REFUSAL_END.search(value)
    )
    if not absent:
        return False
    return not _ABSENCE_CLAIM.search(value) and not _OPERATION.search(value)


def is_guide_not_found(text: str) -> bool:
    """業務ガイドが見つからなかったことだけを述べる文か（#1452。作業の経過で、資料の主張ではない）。

    文末のツールの名前を挙げた括弧は外して照らす。前置きに主張（は・が・を・数字・引用）を含む文と、
    操作の指示を含む文は除く。
    """
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    value = _TOOL_NOTE.sub("", value)
    match = _GUIDE_NOT_FOUND.fullmatch(value)
    if not match or _GUIDE_NOT_FOUND_LEAD.search(match.group("lead")):
        return False
    return not _OPERATION.search(value)


# 調べ方の行（#2328）の内部の識別子: 「ID: …」のラベル、英字（a〜f）と数字を含む 8 文字以上の 16 進
# （UUID の区切りを含む）、ツールの名前（`rag_search`・`<接続>__<ツール>`）。
_INTERNAL_ID = re.compile(
    r"(?<![A-Za-z])(?:ID|Id|id)\s*[:：=]\s*\S"
    r"|(?<![0-9A-Za-z])(?=[0-9A-Fa-f-]*[A-Fa-f])(?=[0-9A-Fa-f-]*[0-9])[0-9A-Fa-f]{8,}(?:-[0-9A-Fa-f]{4,})*"
    r"(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![0-9A-Za-z_])"
    r"|[0-9A-Za-z]__[a-z]"
)
# 調べ方の行の述語（「〜を用いて上記根拠を取得しました」「〜で検索しました」）。
_LOOKUP_ACTION = re.compile(
    r"(?:用いて|使って|使用して|利用して|により|によって|から|で)[、,]?[^。]{0,30}?"
    r"(?:取得|検索|参照|照会|抽出|収集|調査|確認)(?:しました|しています|します|した)[。．]?$"
)


# 調べるのに使ったツールの名前だけの行（「- `rag__rag_retrieve_evidence`」「- `rag__rag_recommend_
# search_answer_profile`（社内規程 Q&A を推薦）」。#104）。モデルに渡すツールの名前は
# `<接続>__<ツール>` なので、`__` を含む名前に限る（設定のキーなど snake_case の語の説明は
# 外さない）。
_TOOL_NAME = r"`?[a-z][a-z0-9_]*__[a-z][a-z0-9_]*`?(?:\s*[（(][^（）()]*[）)])?"
# 先頭の「使用したツール:」の見出し（同じ行にツール名を書く形。強調の印つき。#117）も許す。
_TOOL_NAME_LINE = re.compile(
    r"(?:(?:使用した|使った|利用した|呼び出した|使用|利用)?ツール(?:名)?\s*\**\s*[:：]\s*\**\s*)?"
    rf"{_TOOL_NAME}(?:\s*[、,，/／]\s*{_TOOL_NAME})*[。.]?"
)


# ツール名に、回数（「×2」）・括弧の補足・「→」「:」の後の結果の説明だけが続く行
# （「- `rag__rag_retrieve_evidence` ×2（「社員食堂 献立」で検索）」
# 「- `rag__…` → 推薦プロファイル: 社内規程 Q&A」。#129）。
_TOOL_REPORT_LINE = re.compile(
    rf"{_TOOL_NAME}(?:\s*[×xX]\s*[0-9]+)?(?:\s*[（(][^（）()]*[）)])?(?:\s*(?:→|->|⇒|[:：])\s*\S.*)?"
)
# 検索・回答プロファイルの選択の報告の行
# （「- 検索・回答プロファイル：社内規程 Q&A（ID: …）」。#129）。
_PROFILE_NOTE = re.compile(r"[^:：。]{0,20}プロファイル(?:名)?\s*\**\s*[:：]")


def is_lookup_note(text: str) -> bool:
    """調べるのに使ったツール・プロファイル・内部の ID だけを述べる行か（#2328）。

    「〈プロファイル名〉（ID: 0f3a9c2e…）を用いて上記根拠を取得しました。」のような
    作業の経過で、資料の主張ではない（使ったツールと根拠は画面と MCP の `sources` が
    別に示す）。内部の識別子（`_INTERNAL_ID`）と調べ方の述語（`_LOOKUP_ACTION`）の
    両方がある文に限る。操作の指示・質問は除く。使ったツールの名前（`<接続>__<ツール>`）だけを
    並べた行（「- `rag__rag_retrieve_evidence`」。括弧の補足つきを含む。#104）も作業の経過にする。
    """
    bare = _strip_bullet(text).strip("*").strip()
    if len(bare) <= 200 and (
        _TOOL_NAME_LINE.fullmatch(bare)
        or _TOOL_REPORT_LINE.fullmatch(bare)
        or _PROFILE_NOTE.match(bare)
    ):
        # 使ったツールの名前だけの行（#104）。回答の本文ではなく作業の経過（`__` を強調の印として
        # 外す前に見る）。
        return True
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    if not value or len(value) > 200 or is_question(value) or _OPERATION.search(value):
        return False
    return bool(_INTERNAL_ID.search(value) and _LOOKUP_ACTION.search(value))


# 結論の不足の文の前の、調べたことだけを述べる節（「資料を確認しましたが、」「確認できる根拠を
# 検索しましたが、」。#1454）。
_SEARCHED_LEAD = re.compile(
    r".*?(?:検索|確認|調査|調べ|探)(?:し|い)?(?:ましたが|たが|ましたところ|たところ)、\s*"
)
# 結論の不足の文の述語（「該当する設定・手順は見つかりませんでした」「該当する記載を見つけられません
# でした」）。主語は資料の記載・該当・設定・手順などに限る。
_ABSENCE_CONCLUSION = re.compile(
    r"(?:記載|記述|説明|情報|言及|該当|定め|規定|設定|手順|根拠|項目)[^。]{0,24}?"
    r"(?:見つかりません|見つけられません|見当たりません|ありません|確認できません|確かめられません)"
    r"(?:でした)?[。．]?$"
)
# 質問の対象を主語にして、確かめられなかったとだけ結ぶ文（#108。「社員食堂の今日の献立は確かめられ
# ませんでした。」）。「確かめられない」は資料・データで確かめた結果を述べる語で、業務の事実の主張
# （「…は変更できません」）にならないので、主語の語を限らない。
_UNCONFIRMED_CONCLUSION = re.compile(
    r"^[^。]{1,60}?(?:は|が)[、,]?\s*確かめられません(?:でした)?[。．]?$"
)


def is_absence_conclusion(text: str) -> bool:
    """回答の結論の段落が、質問の対象が資料から確かめられないと述べるか（#1454）。

    `is_absence` に加えて、前に調べたことだけを述べる節（「資料を確認しましたが、」）が付いた
    不足の文も数える（段落の判定〔主張ではない段落〕には使わない。回答の対応を決めるときに、
    最初の段落にだけ使う）。質問の対象を主語にして確かめられなかったとだけ結ぶ文（#108）も数える。
    文頭のお詫びの節（「申し訳ありませんが、」）は外して判定する。数量・逆接（前の節を除く）・
    操作を含む文は除く。
    """
    if is_absence(text):
        return True
    value = _APOLOGY_LEAD.sub("", _EMPHASIS.sub("", _strip_bullet(text)).strip())
    value = _TRAILING_NOTE.sub("", _SEARCHED_LEAD.sub("", value, count=1))
    if not (_ABSENCE_CONCLUSION.search(value) or _UNCONFIRMED_CONCLUSION.search(value)):
        return False
    return not _ABSENCE_CLAIM.search(value) and not _OPERATION.search(value)


def case_label(text: str) -> str | None:
    """分岐の見出し・ラベル（「2. **個別に付与する場合**」「- グループの場合: …」）の語（#1317）。

    見出し・ラベルだけの段落か、段落の先頭の「…場合：」のラベルで、「場合」「とき」で終わるもの。
    文（述語のあるもの）は分岐のラベルにしない。
    """
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    match = _CASE_LABEL.fullmatch(value) or _CASE_LEAD.match(value)
    if not match:
        return None
    label = match.group("label").strip()
    return None if _is_sentence(label) or _OPERATION.search(label) else label


def case_labels(answer: str) -> set[str]:
    """回答の中の、分岐の見出し・ラベルの語（異なる語の集合。#1317）。"""
    return {
        label
        for line in answer.split("\n")
        for _start, _end, text in passage_spans(line)
        if (label := case_label(text)) is not None
    }


def asks_environment_data(text: str) -> bool:
    """利用者に現場のデータ・記録（ログ・設定値・明細など）の確認を求める段落か（#1317）。

    「認証ログ（auth.log）でエラーの時刻の行を確認し、…」のように、確かめる対象と確かめる依頼の
    語の両方がある段落。操作の指示・質問は除く。主張かどうかとは別の印で、検証では主張として
    確かめる（資料に同じ見分け方があれば supported になる）。

    利用者に聞けば分かる条件（「ポータル管理者」ロールを持つか・承認を得たか・対象のシステム）を
    確かめるだけの段落は、記録そのもの（ログ・履歴・設定値など）を確かめる語が無ければ含めない（#1453）。
    """
    value = _strip_bullet(text)
    return (
        bool(_ENVIRONMENT_TARGET.search(value) and _ENVIRONMENT_CHECK.search(value))
        and not _OPERATION.search(value)
        and not is_question(value)
        and not (_USER_CONDITION.search(value) and not _RECORD_TARGET.search(value))
    )


def _is_option(text: str) -> bool:
    """質問の直後の選択肢（短い箇条書きか「選択肢: …」の行。文・操作の指示は除く）。"""
    if not _BULLET.match(text.strip()):
        value = _EMPHASIS.sub("", text).strip()
        return (
            bool(_OPTIONS_LINE.match(value))
            and len(value) <= 80
            and "。" not in value
            and not _OPERATION.search(value)
        )
    value = _strip_bullet(text)
    return (
        0 < len(value) <= 40
        and "。" not in value
        and not _OPERATION.search(value)
        and not re.search(r"(?:ます|です)$", value)
    )


def passage_kind(text: str) -> str | None:
    """行の前後に依らない、1 つの段落の判定（主張なら None）。"""
    if is_table_rule(text):
        return KIND_TABLE
    if _heading_title(text) is not None:
        return KIND_HEADING
    if is_citation(text):
        return KIND_CITATION
    if is_question(text):
        return KIND_QUESTION
    if is_absence(text):
        return KIND_ABSENCE
    if is_guide_not_found(text):
        return KIND_PROCESS
    return None


def non_claim_passages(answer: str) -> dict[str, str]:
    """回答の中の、主張ではない段落（原文 → 判定）。

    同じ原文の段落が、ある場所では主張ではなく、別の場所では主張なら、主張として扱う（外すかどうかは
    原文で決めるため）。節（「資料で確認できず除いた内容」・出典）は、次の見出しか、空行の後の箇条書きでは
    ない行まで続く。表の見出しの行（区切りの行の直前の行）は表の形として扱う（#1317）。
    """
    kinds: dict[str, str] = {}
    claims: set[str] = set()
    headers = table_header_lines(answer)
    listed = list_heading_lines(answer)
    section: str | None = None
    options = False
    blank = False
    for line in answer.split("\n"):
        if is_blank_text(line):
            if line.strip():
                kinds[line.strip()] = KIND_HEADING
            # 質問と選択肢の間の空行では、選択肢の続きを切らない（#1322）。
            blank = True
            continue
        if blank and not _BULLET.match(line.strip()):
            section = None
        blank = False
        if any(
            pattern.fullmatch(normalize_heading_markup(line).strip())
            for pattern in (_MD_HEADING, _BOLD_LINE, _BRACKET_HEADING)
        ):
            # 新しい節で前の分類を終える。書式中の断定も主張として監査する。
            section = None
            options = False
        spans = passage_spans(line)
        if len(spans) == 1:
            title = _heading_title(spans[0][2])
            if title is None and spans[0][2] in listed:
                title = spans[0][2]
            if title is not None:
                if _UNVERIFIED_SECTION.search(title):
                    section = KIND_UNVERIFIED
                elif _CITATION_SECTION.search(title):
                    section = KIND_CITATION
                else:
                    section = None
                options = title.endswith(("？", "?"))
                kinds.setdefault(
                    spans[0][2], KIND_UNVERIFIED if section == KIND_UNVERIFIED else KIND_HEADING
                )
                continue
        line_question = False
        kind: str | None
        for _start, _end, text in spans:
            kind = KIND_TABLE if text in headers else passage_kind(text)
            if kind is None and options and _is_option(text):
                kind = KIND_QUESTION
            if kind is None and section == KIND_UNVERIFIED and not _OPERATION.search(text):
                kind = KIND_UNVERIFIED
            if kind is None and section == KIND_CITATION and not _is_sentence(text):
                kind = KIND_CITATION
            if kind is None:
                claims.add(text)
            else:
                kinds.setdefault(text, kind)
            line_question = kind == KIND_QUESTION
        # 質問の直後の箇条書き（空行を挟んでもよい）は選択肢として扱う。
        options = line_question or (options and bool(_BULLET.match(line.strip())))
    return {text: kind for text, kind in kinds.items() if text not in claims}


# 補足の節の見出しの語（「補足（資料の記載）」の注記の括弧は見出しの語から外れる。
# 「参考」は出典の節）。
_SUPPLEMENT_SECTION = re.compile(r"(?:補足|注記|備考|注意|留意|その他)(?:事項|点|情報)?")
# 補足を導く段落の頭（「なお、…」「補足：…」「注記（同シート A1:A2）：…」）。
_SUPPLEMENT_LEAD = re.compile(
    r"(?:なお|ちなみに)[、,]"
    r"|(?:補足|注記|備考|注意|留意)(?:事項|点|情報)?\s*(?:[（(][^（）()\n]{1,60}[）)])?\s*[:：]"
)


def supplement_passages(answer: str) -> set[str]:
    """回答の中の、補足の段落（補足の節の行と、「なお、」「補足：」で始まる段落。#1475）。

    回答の対応（`answer_outcome`）が、結論の段落で答えた後の補足の不足の文（「現在の各環境の
    設定値は資料からは分かりません。」）を不足の印にしないために使う。節（見出しが「補足」「注記」
    「備考」など）は、`non_claim_passages` の節と同じく、次の見出しか、空行の後の箇条書きではない行
    まで続く。同じ原文の段落が補足の外にもあれば、補足として扱わない（保守的にする）。
    """
    inside: set[str] = set()
    outside: set[str] = set()
    listed = list_heading_lines(answer)
    section = False
    blank = False
    for line in answer.split("\n"):
        if not line.strip():
            blank = True
            continue
        if blank and not _BULLET.match(line.strip()):
            section = False
        blank = False
        spans = passage_spans(line)
        if len(spans) == 1:
            title = _heading_title(spans[0][2])
            if title is None and spans[0][2] in listed:
                title = spans[0][2]
            if title is not None:
                section = bool(_SUPPLEMENT_SECTION.fullmatch(_EMPHASIS.sub("", title).strip()))
                continue
        for _start, _end, text in spans:
            value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
            if section or _SUPPLEMENT_LEAD.match(value):
                inside.add(text)
            else:
                outside.add(text)
    return inside - outside


def environment_check_passages(answer: str) -> set[str]:
    """回答の中の、利用者に現場のデータ・記録の確認を求める段落（主張ではない段落を除く。#1317）。"""
    kinds = non_claim_passages(answer)
    return {
        text
        for line in answer.split("\n")
        for _start, _end, text in passage_spans(line)
        if text not in kinds and asks_environment_data(text)
    }


# あいさつ・お礼の決まった文（#1879。「こんにちは。ご用件をお知らせください。」の
# 「こんにちは。」）。語を固定し、事実の語が入る余地を置かない。あいさつだけで質問・依頼の無い
# 回答は、質問が無いので確認の質問だけの回答にならない。
# お礼への返事（「どういたしまして。」「お役に立ててよかったです。」）も含める（#1929）。
_GREETING_PATTERN = (
    r"(?:こんにちは|こんばんは|おはよう(?:ございます)?|お疲れ(?:さま|様)です|はじめまして|初めまして"
    r"|いつもお世話になっております|(?:どうも)?ありがとう(?:ございます|ございました)?"
    r"|こちらこそ(?:[、,]?ありがとうございます)?|どういたしまして"
    r"|お役に立て(?:て|たなら)(?:よかった|良かった|うれしい|嬉しい)です)[。．！!]?"
)
_GREETING = re.compile(_GREETING_PATTERN)
# 利用者の入力・回答の結びのくだけたあいさつ（「よろしく」「どうも」「お疲れさま」。#1929）。
# 利用者の入力があいさつだけか（`is_greeting_only`）と、確認の質問に添える前置きにだけ使う
# （回答があいさつを含むか（`greets`）には数えない。確認の質問の結びの「よろしくお願いします。」で
# 本文の聞き返しの差し戻しを外さない）。
_CASUAL_GREETING_PATTERN = (
    r"(?:どうも|お疲れ(?:さま|様)|(?:こちらこそ[、,]?)?(?:どうぞ)?よろしく"
    r"(?:お願い(?:します|いたします|致します))?)[。．！!]?"
)
_ANY_GREETING = re.compile(_GREETING_PATTERN + r"|" + _CASUAL_GREETING_PATTERN)
# 確認の質問に添える前置きと答え方の案内（#1454）。確認の質問だけの回答かの判定にだけ使い、
# 段落の判定（`non_claim_passages`）は変えない。
_CLARIFY_NOTE = re.compile(
    _GREETING_PATTERN + r"|" + _CASUAL_GREETING_PATTERN + r"|"
    # 前置き（「条件を 1 つ確かめさせてください。」「その点がまだ分かっていません。」）。
    r".*(?:確認|確かめ)させて(?:ください|下さい|いただきます|いただけますか)[。．]?"
    r"|.*(?:が|は)(?:まだ)?(?:分かって|わかって|決まって|伺って|お聞きして)いません[。．]?"
    # 答え方の案内（「「個別」または「グループ」でお答えください。」）。
    r"|.*(?:お答え|ご回答|ご返答|お選び|お知らせ)(?:ください|下さい)[。．]?"
    # 答えた後の案内（「お答えいただいた内容に応じて、手順と必要な承認をご案内します。」）。
    r"|(?:お答え|ご回答|ご返答|回答)(?:いただいた|いただければ|いただき次第|の内容|に応じて)"
    r".*(?:ご案内|案内|お伝え|お知らせ|お答え|回答)(?:します|いたします|できます)[。．]?"
    # 答えが分かった後の案内の結び（「付与先が分かりましたら、その場合の手順をご案内します。」。
    # 回答が自ら案内する敬語の述語だけ。資料の主張の「案内します」は数えない）。
    r"|[^。]{0,30}?(?:分かり|わかり|決まり|確認でき|お分かりになり)(?:ましたら|次第)"
    r"[、,]?[^。]*(?:ご案内|ご説明|お伝え|お知らせ|お答え)(?:します|いたします|できます)[。．]?"
    r"|[^。]{0,30}?(?:分かれば|わかれば|決まれば|伺えれば|お聞きできれば"
    r"|(?:教えて|お知らせ|ご確認)(?:いただければ|いただけましたら|いただけると))"
    r"[、,]?[^。]*(?:ご案内|ご説明|お伝え|お知らせ|お答え)(?:します|いたします|できます)[。．]?"
    # 確かめる理由（「この条件によって手順・答えが変わるため、まず確認しています。」）。
    r"|.*(?:によって|に応じて|次第で)[^。]*(?:変わる|異なる|分かれる)ため、?[^。]*"
    r"(?:確認|確かめ|お伺い|お聞き)(?:しています|します|いたします|させてください)[。．]?"
)
_CLARIFY_NOTE_EDGES = "（()）"
# 前置き・案内に含まれると資料の主張とみなす数量（「3 営業日以内の手順」。`_ABSENCE_CLAIM` の
# 数量に「営業日」などの複合の単位を足したもの。「条件を 1 つ」は数えない）。
_CLARIFY_QUANTITY = re.compile(
    r"[0-9０-９]+\s*(?:営業日|営業時間|円|日|時間|分|秒|件|%|％|回|か月|ヶ月|カ月|年|名|人|GB|MB)"
)


def _is_clarification_question(text: str) -> bool:
    """確認の質問の段落か（太字の質問「**…ですか？**（はい／いいえ）」も含める。#1454）。"""
    return is_question(_EMPHASIS.sub("", text).strip())


def _is_clarification_note(text: str) -> bool:
    """確認の質問に添える前置き・答え方の案内か（#1454。操作・数量・逆接を含む文は除く）。"""
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip().strip(_CLARIFY_NOTE_EDGES).strip()
    # 括弧の中の「。」では段落を分けないので（#1455）、括弧の中の文ごとに確かめる。
    sentences = [value[start:end].strip() for start, end in sentence_ranges(value)]
    return (
        bool(value)
        and all(_CLARIFY_NOTE.fullmatch(sentence) for sentence in sentences if sentence)
        and not _OPERATION.search(value)
        and not _ABSENCE_CLAIM.search(value)
        and not _CLARIFY_QUANTITY.search(value)
    )


def _sentences(text: str) -> list[str]:
    return [
        sentence
        for line in text.split("\n")
        for start, end in sentence_ranges(line)
        if (sentence := _EMPHASIS.sub("", line[start:end]).strip())
    ]


def greets(text: str) -> bool:
    """あいさつ・お礼の決まった文を含むか（#1879・#1881。「こんにちは。ご用件をお知らせください。」）。

    あいさつへの返事で用件を聞く回答は、質問の解釈を確かめる確認の質問ではない（差し戻さない）。
    """
    return any(_GREETING.fullmatch(sentence) for sentence in _sentences(text))


def is_greeting_only(text: str) -> bool:
    """あいさつ・お礼の決まった文だけか（利用者の質問が「こんにちは」だけなど）。"""
    sentences = _sentences(text)
    return bool(sentences) and all(_ANY_GREETING.fullmatch(sentence) for sentence in sentences)


# 用件（何を知りたいか）を聞く質問の決まった形（#1929。「社内の規程や手順について、知りたいことを
# お知らせください。」「何についてお調べしましょうか？」）。対象（文書・システム・期間など）を
# 確かめる確認の質問とは分ける。前に付く「〜について」はスキルの対象の範囲を示すだけで、数字・
# 逆接・操作を含む文は除く（`asks_for_request`）。
_ASK_REQUEST = re.compile(
    r"(?:[^。？?!！]{0,40}?について[、,]?\s*)?"
    r"(?:ほかに|他に)?[、,]?\s*(?:何か|なにか)?[、,]?\s*"
    r"(?:(?:どのような|どんな)?(?:ご用件|ご用|ご質問|お問い合わせ|ご相談|お調べしたいこと"
    r"|調べたいこと|知りたいこと|お知りになりたいこと|お困りのこと|お手伝いできること)"
    r"|(?:何|なに)(?:を|について)(?:お調べ|調べ|お手伝い)|どのようなこと|どんなこと)"
    r"[^。？?!！]{0,30}?(?:[？?]|(?:お知らせ|お聞かせ|教えて|お申し付け)(?:ください|下さい))"
    r"[。．!！]?"
)


def asks_for_request(answer: str) -> bool:
    """用件（何を知りたいか）を聞くだけの回答か（#1929）。

    あいさつ・お礼・質問でない入力（「テスト」）に、ツールを呼ばずに用件を聞いて返す回答
    （「社内の規程や手順について、知りたいことをお知らせください。」）。確認の質問だけの回答
    （`is_clarification_only`）で、質問の段落がすべて用件を聞く決まった形のもの。質問の解釈を
    確かめる確認の質問（「どのシステムの申請ですか？」）は含めない。
    """
    if not is_clarification_only(answer):
        return False
    kinds = non_claim_passages(answer)
    asked = False
    for line in answer.split("\n"):
        for _start, _end, text in passage_spans(line):
            if kinds.get(text) != KIND_QUESTION and not _is_clarification_question(text):
                continue
            value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
            sentences = [value[start:end].strip() for start, end in sentence_ranges(value)]
            for sentence in (item for item in sentences if item):
                if _CLARIFY_NOTE.fullmatch(sentence) and not is_question(sentence):
                    continue
                if (
                    not _ASK_REQUEST.fullmatch(sentence)
                    or _ADVERSATIVE.search(sentence)
                    or _OPERATION.search(sentence)
                    or re.search(r"[0-9０-９]", sentence)
                ):
                    return False
                asked = True
    return asked


def is_clarification_only(answer: str) -> bool:
    """利用者への確認の質問だけの回答か（見出しと質問・選択肢だけで、質問が 1 つ以上ある）。

    質問の前置き（「条件を 1 つ確かめさせてください。」「その点がまだ分かっていません。」）と、
    答え方の案内・確かめる理由の括弧（「（「個別」または「グループ」でお答えください。…）」）も
    質問の一部として扱う（#1454。これらが主張として残ると、確認の質問に「資料の根拠を使って
    いない」の注記が付く）。
    """
    passages = [text for line in answer.split("\n") for _s, _e, text in passage_spans(line)]
    if not passages:
        return False
    kinds = non_claim_passages(answer)
    asked = False
    for text in passages:
        kind = kinds.get(text)
        if kind == KIND_QUESTION or _is_clarification_question(text):
            asked = True
        elif kind != KIND_HEADING and not _is_clarification_note(text):
            return False
    return asked


# 拒答に添える結びの決まった文（#1879。「ご了承ください。」「申し訳ありません。」）。語を固定し、
# 事実の語が入る余地を置かない（`is_absence_only` にだけ使う）。
_ABSENCE_CLOSING = re.compile(
    r"(?:何卒|なにとぞ)?(?:ご了承|ご容赦)(?:ください(?:ませ)?|下さい|願います"
    r"|(?:を|のほど)?(?:お願い(?:します|いたします|致します))|いただけますと幸いです)[。．!！]?"
    r"|(?:お役に立てず[、,]?\s*)?(?:大変|誠に)?申し訳(?:ありません|ございません)(?:でした)?[。．!！]?"
)


def is_absence_only(answer: str) -> bool:
    """資料に無い・答えられないことだけを述べる回答か（#1879。資料の主張を含まない拒答）。

    すべての段落が、資料に記載が無い・答えられないと述べる文（`is_absence_conclusion`。前に
    調べたことだけを述べる節が付いたものを含む）か、拒答に添える結びの決まった文
    （「ご了承ください。」「申し訳ありません。」）か、見出しで、不足の文が 1 つ以上あるもの。
    数量・逆接・操作を含む不足の文は主張として扱う（`is_absence` と同じく保守的にする）。
    根拠の無い回答に「資料と照らし合わせて確かめていない」の注記を足さないかの判定にだけ使う。
    """
    passages = [text for line in answer.split("\n") for _s, _e, text in passage_spans(line)]
    if not passages:
        return False
    kinds = non_claim_passages(answer)
    absent = False
    for text in passages:
        if is_absence_conclusion(text):
            absent = True
        elif kinds.get(text) != KIND_HEADING and not _ABSENCE_CLOSING.fullmatch(
            _EMPHASIS.sub("", _strip_bullet(text)).strip()
        ):
            return False
    return absent


# 質問がこの業務 Agent の範囲外だと述べる文（#2608。「この質問は業務の範囲外です。」）。「範囲外」
# 「対象外」だけでは事実の主張（「交通費は課税の対象外です。」）と区別できないので、質問・依頼・
# この業務 Agent・扱う範囲を指す語と一緒に現れる文に限る。
_OUT_OF_SCOPE = re.compile(r"(?:範囲|対象|専門|担当|管轄)外")
_OUT_OF_SCOPE_SUBJECT = re.compile(
    r"質問|問い合わせ|ご依頼|ご相談|お尋ね|(?:この|本|当)\s*(?:業務|Agent|エージェント)"
    r"|業務の?範囲|扱(?:う|え|っ)|取り扱",
    re.IGNORECASE,
)
# 結論の段落を探すときに飛ばす段落の種類（見出し・出典・表の形）。
_DECLINE_SKIPPED_KINDS = frozenset({KIND_HEADING, KIND_CITATION, KIND_TABLE})
# 拒答の判定の前にそろえる、文頭のお詫びの節と謙譲の否定（#2608。実サービスの評価で
# 「申し訳ございませんが、…情報は含まれておりません。」が判定から漏れた）。
_APOLOGY_LEAD = re.compile(
    r"^(?:大変|誠に)?申し訳(?:ありません|ございません)(?:が|けれども)?[、,]\s*"
)
_HUMBLE_NEGATIVES = (("ておりません", "ていません"), ("ございません", "ありません"))
# 資料に無いことを理由の節にして、答えられないと結ぶ文（#2608。「…確認できる情報がありません
# ので、…についてはお答えできません。」）。理由の節の「…ので、」は `_ABSENCE_CLAIM` の言い切り
# （「〜が〜で、」）に当たるので、`is_absence` とは別に見る。結びは答える動詞に限る。
_DECLINE_REASON = re.compile(
    r"(?:ありません|含まれていません|見つかりません|見当たりません|確認できません|確かめられません"
    r"|ない|無い)(?:ので|ため)[、,]"
)
_DECLINE_END = re.compile(
    r"(?:お答え|回答|ご回答|ご案内|案内)(?:は|を|も)?(?:でき|出来)ません(?:でした)?[。．]?$"
    r"|(?:お答え|回答|ご回答|ご案内)(?:いたし|致し|し)かねます[。．]?$"
)


def is_out_of_scope(text: str) -> bool:
    """質問がこの業務 Agent の範囲外だと述べる文か（#2608。数量・逆接・操作を含む文は除く）。"""
    value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
    if not (_OUT_OF_SCOPE.search(value) and _OUT_OF_SCOPE_SUBJECT.search(value)):
        return False
    return not _ABSENCE_CLAIM.search(value) and not _OPERATION.search(value)


def concludes_decline(answer: str) -> bool:
    """回答の結論（最初の段落）が、答えられない・資料から確かめられない・範囲外と述べるか（#2608）。

    見出し・出典・表の形と、お詫び・了承を求める決まった文（「申し訳ありません。」）は飛ばして、
    最初の段落を見る。文頭のお詫びの節（「申し訳ございませんが、」）は外し、謙譲の否定
    （「含まれておりません」「ございません」）はそろえてから判定する。根拠・データのツールを呼ばずに作った回答が、調べずに範囲外・答えられないと
    判断した拒答か（`tool_grounding.declined`）の判定に使う。
    """
    kinds = non_claim_passages(answer)
    for line in answer.split("\n"):
        for _start, _end, text in passage_spans(line):
            value = _EMPHASIS.sub("", _strip_bullet(text)).strip()
            if not value or kinds.get(text) in _DECLINE_SKIPPED_KINDS:
                continue
            if _ABSENCE_CLOSING.fullmatch(value):
                continue
            value = _APOLOGY_LEAD.sub("", value)
            for humble, plain in _HUMBLE_NEGATIVES:
                value = value.replace(humble, plain)
            return (
                is_absence_conclusion(value)
                or is_out_of_scope(value)
                or _declines_for_absence(value)
            )
    return False


def _declines_for_absence(value: str) -> bool:
    """資料に無いことを理由の節にして、答えられないと結ぶ文か（#2608。数量・操作を含む文は除く）。"""
    if not (_ABSENCE_SUBJECT.search(value) and _DECLINE_REASON.search(value)):
        return False
    if not (_DECLINE_END.search(value) or _REFUSAL_END.search(value)):
        return False
    return not re.search(_QUANTITY, value) and not _OPERATION.search(value)


__all__ = [
    "KIND_ABSENCE",
    "KIND_CITATION",
    "KIND_HEADING",
    "KIND_PROCESS",
    "KIND_QUESTION",
    "KIND_TABLE",
    "KIND_UNVERIFIED",
    "asks_environment_data",
    "asks_for_request",
    "case_label",
    "case_labels",
    "concludes_decline",
    "environment_check_passages",
    "greets",
    "heading_phrase",
    "is_absence",
    "is_absence_conclusion",
    "is_absence_only",
    "is_citation",
    "is_clarification_only",
    "is_greeting_only",
    "is_guide_not_found",
    "is_heading",
    "is_lead_in",
    "is_operation_step",
    "is_out_of_scope",
    "is_question",
    "is_structure",
    "is_table_rule",
    "list_heading_lines",
    "non_claim_passages",
    "passage_kind",
    "passage_spans",
    "supplement_passages",
    "table_header_lines",
]
