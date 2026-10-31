"""Vision の原文保持・文脈帰属・構造上限を提示詞と保存処理で共有する。"""

from __future__ import annotations

import re

from rag_engine.parsing.content_labels import content_labels
from rag_engine.resources.runtime import resolve_prompt

MAX_DIAGRAM_NODES = 30
MAX_DIAGRAM_EDGES = 40
MAX_CHART_AXES = 12
MAX_CHART_LEGENDS = 20
MAX_CHART_SERIES = 12
MAX_CHART_SERIES_VALUES = 20
MAX_TABLE_HEADERS = 20
MAX_TABLE_ROWS = 20
MAX_TABLE_COLUMNS = 20
MAX_FORM_FIELDS = 50
VISION_LIST_FIELD_LIMITS = {
    "diagram_nodes": MAX_DIAGRAM_NODES,
    "diagram_edges": MAX_DIAGRAM_EDGES,
    "chart_axes": MAX_CHART_AXES,
    "chart_legends": MAX_CHART_LEGENDS,
    "chart_series": MAX_CHART_SERIES,
    "table_headers": MAX_TABLE_HEADERS,
    "table_rows": MAX_TABLE_ROWS,
    "form_fields": MAX_FORM_FIELDS,
}

# 語の列挙だけの行の field (#773)。根拠の判定・監査（result_granularity / operation_audit）はこれらの行と
# グループ見出しを本文として使わない。ラベルは言語資源（content_labels。#2181）。
VISION_ENUMERATION_FIELDS = (
    "main_topic",
    "menu_route",
    "visible_screen_names",
    "visible_buttons",
    "visible_fields",
    "visible_values",
    "codes_and_errors",
    "search_keywords",
    "query_rewrites",
    "answerable_questions",
)


# 画面に写っている値そのものを載せる行の field。項目名は「項目:」に残るため、検索用テキストからは
# この行とその箇条書きを落とす。prompt の接頭辞（「画面例: 」）はモデルが守るとは限らず、
# 実文書で守られない例が出たため、アプリが決定的に判別できる行ラベルで除く (#778)。
VISION_SCREEN_VALUE_FIELDS = ("visible_values", "form_fields")
_BULLET_LINE = re.compile(r"\s*- ")


def without_vision_screen_values(text: str) -> str:
    """生成説明から画面の値の行（ラベル行とその箇条書き）を除いた text を返します。

    回答用の text には残すため、検索用テキストを作る側だけで使います。
    """
    screen_value_line = re.compile(content_labels().field_line(*VISION_SCREEN_VALUE_FIELDS))
    kept: list[str] = []
    dropping = False
    for line in (text or "").splitlines():
        if screen_value_line.match(line):
            # 「ラベル:」だけの行に続く箇条書きも同じ field の値なので一緒に落とす。
            dropping = line.rstrip().endswith(":")
            continue
        if dropping and _BULLET_LINE.match(line):
            continue
        dropping = False
        kept.append(line)
    return "\n".join(kept)


def is_vision_enumeration_line(line: str) -> bool:
    """生成説明のうち、語の列挙とグループ見出しだけの行かを返します。

    本文として引用・判定に使うと、ボタン名や検索語が根拠の記述として扱われるため除きます。
    """
    labels = content_labels()
    return line.lstrip().startswith(labels.group_prefix) or bool(re.match(r"\s*" + labels.field_line(*VISION_ENUMERATION_FIELDS), line))


RULES_START = "[Vision extraction rules v1]"
RULES_END = "[/Vision extraction rules]"
# 版を上げても保存済みテンプレート内の旧ブロックを 1 つに正規化できるよう、版番号は正規表現で受ける。
# 開始マーカーを現在の版で固定すると、v2 では v1 ブロックに一致せず本文が残り、規則が二重に送られる (#766)。
# 終了マーカーは版を持たないため、そのまま対になる。
_ANY_RULES_START = r"\[Vision extraction rules v\d+\]"
# 開始マーカーの手前に孤立した開始マーカーがあっても、その間の独自文面を巻き込まない。
_MANAGED_BLOCK = re.compile(_ANY_RULES_START + rf"(?:(?!{_ANY_RULES_START}).)*?" + re.escape(RULES_END), re.S)
# ブロックを取り除いた後に残る、対になっていないマーカー。
_ORPHAN_MARKER = re.compile(_ANY_RULES_START + "|" + re.escape(RULES_END))

# 図・画像を説明するときに、テンプレートの後ろに足す対象の指示（表は TABLE_EXTRACTION_INSTRUCTION）。
PICTURE_EXTRACTION_INSTRUCTION = "## Step 6: 対象領域を読み取る\n\n実行対象: 画像1の独立した画像領域。"

TABLE_EXTRACTION_INSTRUCTION = (
    "## Step 6: 対象領域を読み取る\n\n"
    "実行対象: 表全体。\n\n### 表内画像の照合\n\n1. 独立 Picture として抽出されなかった表内画像の文字、ダイアログ、"
    "ボタン、条件と操作を読み取り、対応する行の説明と関連付けてください。\n2. "
    "existing_table_html は照合用の根拠データです。画像で確認できる既存セル本文を保ち、"
    "missing_picture_regions の画像セルを原本の列位置に補って table_rows を作成してください。\n3. "
    "画像部分の補足だけを返すのではなく、照合に必要な同じ行の既存セルも含めてください。\n4. "
    "既存 HTML と画像が食い違う場合は画像を優先し、変更箇所と根拠を correction_notes に残してください。\n5. "
    # 行の補完は table_rows だけを照合に使うため、先頭行で上限を使い切ると後方の画像セルが失われる。
    "表が出力契約の行数上限を超える場合は、missing_picture_regions の画像セルを含む行を優先して "
    "table_rows に入れ、残りの枠を先頭の行で埋めてください。"
)


VISION_EXTRACTION_INSTRUCTION = """### 読み取りの方針
以下は field ごとの抽出契約です。上記の方針は維持し、原文と検索用の言い換えを分離します。

### 1. 原文保持
- table_headers / table_rows / visible_* / codes_and_errors / visible_text は原文の正式名称、否定、条件、数値、単位、句読点を保持し、要約・同義語置換をしない。個人情報の扱いは上記の規則に従う。
- table_rows は見出しを除く行を上から下、セルを左から右の順で返す。画像セルもその位置に入れ、空セルは空文字として残す。複数行を一行に統合せず、結合セルや行列の帰属を確認できない場合は推測しない。
- table_summary / retrieval_text は要約用、query_rewrites / search_keywords は検索表現用。table_rows の原文を検索向けに書き換えない。画像と OCR が食い違う場合は読める画像を優先し、読めない箇所は空欄にして correction_notes に残す。

### 2. field の意味
- visual_kind は flowchart / architecture_diagram / chart / table / form / screenshot / photo / logo / watermark / background / decorative / other から一つ。不明なら other。logo 等は純粋な装飾の場合に限り、業務タイトル・画面名・操作情報を含む対象は見た目だけで装飾にせず、その情報を main_topic / visible_screen_names / retrieval_text に保持する。
- screenshot は操作の順序、クリック/入力/選択/確認の対象、表示条件、有効/無効状態、警告や確認ダイアログの結果を分けて書き、画面名、メニュー経路、ボタン、入力欄、選択肢、コード、エラー、パラメータ、制約、注意書きを優先して残す。
- diagram_* は図のタイトル・読めるノード・可視の接続・経路要約（開始点、分岐条件、順序、戻り先、完了条件）。edges の source / target は nodes の名称と一致させ、方向や条件 label が不明なら接続を創作しない。
- chart_* はグラフのタイトル・種別・軸と単位・凡例・系列・傾向。series の values は確実に読める category=value だけで、目測値を確定値にしない。
- table_headers / table_rows / table_summary は列見出し・同じ行のセル配列・表の要約。Q&A、仕様表、変更履歴、比較表では質問と回答、変更前後、条件と結果、対象と対象外の対応関係を崩さない。対象が表全体なら表内画像も含め、独立した小画像なら周囲の表全体を転記しない。
- form_fields は name / value / state / kind（項目名・表示値・選択/有効状態・項目の種類）、form_layout は配置とグループ。対象外の構造は空文字または空配列にする。項目の説明例を抽出結果としてコピーしない。
- form_fields の kind は画像で見える部品の形だけで決める: ▼ などの開くボタンが付いた欄は select、文字を入れる枠は text、四角いチェックの枠は checkbox、丸い選択の印は radio、カレンダーのアイコンが付いた日付の欄は date、押す部品は button。形が読めない・どれにも当たらない場合は unknown にし、項目名や値から推測しない。

### 3. 文脈の帰属
3.1 対象の特定
- 画像1と metadata.bbox が主対象。context 画像では、マゼンタ（明るい紫）の矩形（システムが付けた target_highlight）だけを対象マークとして扱い、その枠は画像1と同じ内容を囲む。原文に元からある赤枠・選択枠・強調色は文書の内容であり、そこから主対象を変更しない。判別に迷う場合は、画像1と内容が一致する枠を対象とする。
- context_source_record_refs の visible_bbox は実際に写る範囲。partially_visible=true のレコードの全内容が写っているとは扱わない。metadata の推定位置や cell_hint は補助情報であり確定した行対応ではない。

3.2 可視情報の帰属
- visible_* と visible_text は主対象内で確認できる情報だけ。周辺だけに見える画面名・値・手順を主対象の可視情報に入れない。
- visible_buttons は主対象内の操作部品（ボタン・リンク・タブ・メニュー項目）を、赤枠・色・矢印などの強調の有無に関係なくすべて列挙する。ボタン自体に印字された文字列だけを使い、無効化された部品もラベルが読めれば含める。アイコンの意味を推測した名称、チェック項目、周辺本文で言及されるだけのボタン名は含めない。原文が説明のために画面へ重ねた注記（丸数字・番号・矢印・吹き出し・囲み線）は操作部品ではないので含めず、operation_steps の手順番号との対応にだけ使う。
- キー・ショートカット・番号などの識別表記が読める場合は「識別表記 部品名」の形で一緒に残す（例: (F3) 検索、Ctrl+S 保存）。部品名が読めなければ識別表記のみ。

3.3 手順と操作部品
- ボタンの名称・操作は画面内の位置、所属パネル、隣接する入力欄と結び付け、同じ形や色のアイコンでも別位置のラベルを借用しない。operation_steps は操作ごとに根拠の場所（主画像 / 周辺説明）、操作部品の位置、読めるラベル、操作と結果を分けて書き、ラベルのないものは「無記名アイコン」と明記して位置・外観で特定する。読めなければ未確認とする。

3.4 文脈と補足
- surrounding_context は所属見出し・表・同行説明などの関係。owning_section は所属節の候補で画像との整合を確認する。next_record の見出しは次節の境界であり所属節ではない（section_role=section_boundary）。section_role=subheading_in_owning_section の見出しは同じ節の小見出しで、その本文は主対象の説明に使える。
- 周辺の説明を retrieval_text に使う場合は「周辺の同行説明では」等と出所を明示し、別行・別節の条件や結果を結び付けない。

3.5 画面の値の扱い
- 画面キャプチャの入力欄・一覧・ダイアログに見える値（年月・番号・コード・氏名・金額・選択状態）は原則として文書の例示であり、実運用システムの値、現在の顧客データ、顧客固有の設定、既定値、推奨の固定値のいずれとしても扱わない。周辺本文・注記・ラベルが「〜と入力します」「固定」「既定値」のように値を指定・限定している場合だけ、その根拠語と一緒に指定値として残す。
- operation_steps / condition_result_pairs / retrieval_text に画面の値を書くときは「画面例では」「入力例として」を付け、実際の値として書かない。visible_values の例示値は「画面例: 項目名=値」の形で項目名とセットにする。

### 4. 出力と長さ
- 長表は出力契約の上限まで原本の先頭の行を保持する。ただし実行対象が表全体で missing_picture_regions がある場合は、その画像セルを含む行を先に確保し、残りの枠を先頭の行で埋めて、上から下の順で返す。超過分は読める根拠に限って table_summary 等へ要約し、correction_notes に省略の有無と範囲を明示する。要約を全行の構造化抽出として扱わない。
- 同義語・質問例は重複を避け、見えている語から自然に導ける根拠のある表現だけ（特定コーパス専用の同義語を作らない）。必須キーを埋めるために情報を創作しない。出力は実行時の JSON schema に従う。"""


def vision_extraction_rules() -> str:
    """保存した自然言語の読取方針を返す。構造と件数の制約は実行時契約に置く。"""
    return resolve_prompt("picture_description.rules", VISION_EXTRACTION_INSTRUCTION)


def split_image_retrieval_prompt(template: str) -> tuple[str, str | None]:
    """旧テンプレートの共通規則を、本文を変更せず別の編集単位として取り出す。"""
    blocks = list(_MANAGED_BLOCK.finditer(template))
    rules = []
    for block in blocks:
        content = block.group(0)
        content = re.sub(r"^" + _ANY_RULES_START, "", content)
        rules.append(content.removesuffix(RULES_END).strip("\n"))
    body = _ORPHAN_MARKER.sub("", _MANAGED_BLOCK.sub("", template)).rstrip()
    return body, "\n\n".join(rules) if rules else None


def refine_image_retrieval_prompt(template: str, *, rules: str | None = None) -> str:
    """テンプレートと独立した読取方針を合成する。保存した方針の文は書き換えない。"""
    body, legacy = split_image_retrieval_prompt(template)
    effective = (
        rules
        if rules is not None
        else resolve_prompt("picture_description.rules", legacy if legacy is not None else VISION_EXTRACTION_INSTRUCTION)
    )
    return body + "\n\n" + RULES_START + "\n" + effective + "\n" + RULES_END


def managed_block_warning(template: str) -> str:
    """旧ブロックも独立した編集内容として保持するため、置換の警告は不要。"""
    return ""
