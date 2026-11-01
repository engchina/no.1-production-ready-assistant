"""取込の各段でモデルの API へ渡す固定のプロンプト。

回答プロンプトの画面の「各段のプロンプト」(``answer_prompts.readonly_prompt_stages``)に
そのまま載せるため、呼び出しの中に文字列を書かず、ここに定数として置く(#2054)。
"""

from __future__ import annotations

# 見出しの階層の推定(#2277)。続けて見出しの JSON(``[{"i": 番号, "text": 見出し}]``)を渡す。
HEADING_LEVEL_PROMPT = (
    "## Step 1: 見出しの入力を読む\n"
    "次の JSON は、1 つの文書の見出しを読み順に並べたものです(i "
    "は見出しの番号、text は見出しの文字列)。\n"
    "\n"
    "## Step 2: 階層を決めて原文を保つ\n"
    "- 文書の章・節の構成として自然になるように、各見出しの階層を 1(最"
    "上位)から 6(最も深い)の整数で決めてください。\n"
    "- 見出しを足したり省いたり、文字列を書き換えたりしないでください。\n"
    "\n"
    "## Step 3: 結果を返す\n"
    '- {"levels": [{"i": 見出しの番号, "level"'
    ": 階層}]} の JSON だけを出力し、すべての見出しを1 回ずつ"
    "含めてください。\n"
)

# 項目抽出(メタデータ/項目抽出)。末尾に「抽出する field 定義: …」を足して渡す。応答の形は
# JSON Schema(``FIELD_EXTRACTION_RESPONSE_SCHEMA``)でも渡す(対応しないモデルはこの文だけで決める)。
FIELD_EXTRACTION_PROMPT = (
    "## Step 1: 抽出対象を確認する\n"
    "次の文書から、指定された field を抽出してください。\n"
    "\n"
    "## Step 2: 値を整える\n"
    "- value は、value_type が date なら YYYY"
    "-MM-DD、number なら桁区切りと単位のない数字、bool な"
    "ら true か false で出力してください。\n"
    "\n"
    "## Step 3: 結果を返す\n"
    '- {"fields": [{"name", "value", "va'
    'lue_type", "confidence"}]} の JSON だ'
    "けを出力し、見つからない field は省略してください。\n"
)
# 項目抽出で本文を分けて渡すとき(#2720)の、各部分の頭書き。位置(i/n)と、その部分の最初の
# 要素の見出しの位置を付ける(文書の冒頭ではないことと、どの章の中かをモデルに示す)。
FIELD_EXTRACTION_PART_HEADER = "[文書の一部 {index}/{total}]"
FIELD_EXTRACTION_PART_PATH = " 見出し: {path}"

# ナビゲーション要約(章節木): 章節ごとの要約。
NAVIGATION_SUMMARY_PROMPT = (
    "## Step 1: 章節を要約する\n"
    "次の章節の内容を日本語で1〜2文に要約してください。\n"
    "\n"
    "## Step 2: 原文との一致を確かめる\n"
    "\n"
    "- 原文にない情報は補わないでください。\n"
)

# RAPTOR 再帰要約索引: chunk の群の要約。
RAPTOR_SUMMARY_SYSTEM_PROMPT = (
    "## Step 1: テキスト群を要約する\n"
    "\n"
    "あなたは文書を階層的に要約するアシスタントです。\n"
    "次のテキスト群の要点を簡潔な日本語で要約してください。"
)

__all__ = [
    "FIELD_EXTRACTION_PART_HEADER",
    "FIELD_EXTRACTION_PART_PATH",
    "FIELD_EXTRACTION_PROMPT",
    "HEADING_LEVEL_PROMPT",
    "NAVIGATION_SUMMARY_PROMPT",
    "RAPTOR_SUMMARY_SYSTEM_PROMPT",
]
