"""根拠・データのツールを呼ばずに作った回答の拒答（#1835）。

業務 Agent が、資料の根拠のツール（RAG）や業務データのツール（NL2SQL・登録した MCP 接続）を
1 つも呼ばずに、モデルの知識だけで最終の回答を作ることがある。社内の資料・業務データで確かめて
いない回答は作り話の可能性があるため、利用者には出さない（ユーザーの決定、2026-10-14）。
プロンプトの指示だけでは拒答は安定しない（AbstentionBench など）ため、組み込み Runtime が
回答を確定する前に決定的に確かめる（Amazon Bedrock Guardrails の contextual grounding check の
BLOCK と同じ考え方）。

- 対象: この Run でモデルに根拠・データのツールを渡した業務 Agent の Run（チャット・
  品質評価のどれも）。ツールを持たない Agent・利用者が資料の検索を取り消して根拠・データの
  ツールが残らない Run は対象外（ガードをすり抜けた回答は #1834 の「根拠を使わずに答えた」に出る）。
- 数える呼び出し: モデルが根拠・データのツールを呼んだ step（成功・失敗・承認の却下・評価の
  dry-run・無人の Run で承認が要るため呼ばなかったものも、モデルが確かめようとしたので数える）。
  Control Plane が自分で呼んだもの（確認の取り消し・業務ガイドの照合・最終の検証）は数えない。
- 除外: 同じ会話の前の Run（会話の履歴の上限 `PLATFORM_CHAT_HISTORY_MAX_TURNS` の中）が根拠・
  データのツールで答えた Run（前の回答の言い換え・整形。事実の確かめは #1837 の最終の検証）と、
  利用者が確認を取り消した Run（「資料の検索は不要」など。#1441・#1445）と、
  利用者への確認の質問だけの回答（`is_clarification_only`）と、NL2SQL の SQL の生成前の確認で
  止まった Run（`nl2sql_clarification_pending`）。曖昧で対象が分からない質問は拒答ではなく確認に
  する（ユーザーの決定）。拒答の対象は、範囲外の質問と資料・データで確かめられない質問に限る。
- 1 回だけ差し戻す: 「業務の事実はツールで確かめてから答える。確かめられないなら拒答する」を
  利用者のメッセージとしてモデルへ返して続けさせる（自己修正）。それでも呼ばなければ、本文を
  出さず、決まった形の拒答の文（`refusal_text`）に置き換える。元の回答は Run の経過に残す。
- 調べずに範囲外・答えられないと判断した拒答（回答の結論が範囲外・確かめられないと述べる。
  `declined`。#2608）は、範囲の内外と根拠の有無をツールの結果で判断させる文（`RETRY_HEAD_DECLINED`）で
  差し戻す。差し戻しの文の結びは「調べても確かめられないときは答えられないと答える」で、範囲外かを
  調べずに決めてよいとは書かない。Run の経過（イベントの `declined`）と拒答の回答の対応の basis
  （`declined_without_tools`）で、ツールで確かめずに答えた回答の拒答（`no_tool_grounding`）と分ける。
  調べた結果として根拠が無かった拒答は、ツールを呼んでいるのでここの対象ではない（basis は
  `answer_outcome` の `absence_conclusion` など）。品質評価は両者を `refusal_stage` で分けて数える。
- 差し戻しの文の曖昧な質問の扱いは、その Run でモデルに回答前の確認のツール
  （`ask_user_confirmation`）を渡したかで分ける（#1881）。渡したときは、まず資料・業務データの
  ツールで調べる（業務ガイド・NL2SQL が確認事項を返せば、それが確認のカードになる）か、
  `ask_user_confirmation` で確かめるよう伝え、本文に確認の質問を書かせない。渡していない（確認の
  上限）ときは、ツールを呼ばずに確認の質問だけを返して
  よいと伝える。差し戻しの後も、確認のツールを渡したのに本文で確認の質問だけを返した回答は、
  拒答にはせず、改善の手がかりとして Run の経過に残す（`clarified_in_text`）。
- 確認のツールを渡した Run（#1881 の追加）でも、ツールを 1 つも呼ばずに本文で確認の質問だけを返した
  回答は 1 回差し戻す（確認のツールを渡したときの文。差し戻しの後も本文の聞き返しなら、拒答に
  せずそのまま出し、`text_clarification` を経過に残す）。あいさつへの返事（「こんにちは。ご用件を
  お知らせください。」。#1879）と、お礼・質問でない入力に用件を聞く返事（#1929）は確認の質問ではないので
  差し戻さない（`is_greeting_reply`）。
- 利用者に確認しない Run（業務 Agent の方針「前提を示して回答」と、利用者が「確認せずに回答」を
  選んだ Run と、無人の Run〔品質評価。#1959〕。#1903）では、確認の質問だけの回答も
  差し戻しの対象にし、差し戻しの文は「確認の質問はせず、資料・業務データのツールで調べ、決まらない点は前提を示して答える
  （条件ごとに分ける）」にする（`RETRY_MESSAGE_ASSUME`）。差し戻しの後も本文の聞き返しだけなら、
  拒答にはせずそのまま出し、Run の経過に残す（拒答は範囲外・確かめられない質問のためのもので、
  対象が曖昧な質問の聞き返しを「お答えできません」にすると利用者が次に何をすればよいか分からない）。
- 回答の対応は `insufficient_evidence`（basis `no_tool_grounding`。調べずに範囲外・答えられないと
  判断した拒答は `declined_without_tools`）。最終の検証の成果物は
  `skipped`（reason `no_tool_grounding`）にする（資料の根拠を使わずに答えた `no_rag_evidence`
  とは分ける。#1834 のシグナルは拒答した Run を「答えられなかった」に数える）。

ここは判定と文だけを持つ（差し戻し・保存は `builtin_runtime`）。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from app.features.agent.answer_outcome import INSUFFICIENT_EVIDENCE, OUTCOME_SCHEMA_VERSION
from app.features.agent.answer_passages import (
    asks_for_request,
    concludes_decline,
    greets,
    is_clarification_only,
    is_greeting_only,
)
from app.features.agent.chat_attachments import ATTACHMENT_READ_TOOLS
from app.features.agent.clarification_cancel import CANCEL_TRACE_PREFIX
from app.features.agent.confirmation import CONFIRMATION_TOOL
from app.features.agent.job_cancel import RUN_CANCEL_TRACE_PREFIX
from app.features.agent.support_task import (
    GUIDE_CHECK_TRACE_PREFIX,
    nl2sql_clarification_pending,
)
from app.features.agent.tools import MCP_TOOL_SEPARATOR, mcp_base_tool_name

if TYPE_CHECKING:
    from app.features.agent.runtime import RunStep

JsonObject = dict[str, Any]

# 回答の対応の basis と、最終の検証の成果物の reason。ツールで確かめずに答えた回答を拒答に
# 置き換えたものと、ツールで調べずに範囲外・答えられないと判断した拒答（#2608）を分ける（最終の
# 検証の成果物の reason はどちらも `no_tool_grounding`）。
BASIS_NO_TOOL_GROUNDING = "no_tool_grounding"
BASIS_DECLINED_WITHOUT_TOOLS = "declined_without_tools"
REASON_NO_TOOL_GROUNDING = "no_tool_grounding"
# Run の metadata に残す、差し戻しの記録（承認待ちから再開しても 2 回目を差し戻さない）。
METADATA_KEY = "tool_grounding"
# 差し戻しの回数（ユーザーの確認待ち。#1835）。
MAX_RETRIES = 1
# 拒答が、根拠・データのツールで調べる前か後か（`refusal_stage`。品質評価の集計。#2608）。
REFUSAL_BEFORE_TOOLS = "before_tools"
REFUSAL_AFTER_TOOLS = "after_tools"

# 根拠・データを確かめないツール（業務プロファイル・検索・回答プロファイルの一覧と推薦）と、
# Control Plane が自分で呼ぶツール（最終の検証・SQL の生成前の確認の取り消し）。
_NON_GROUNDING_TOOLS = frozenset(
    {
        "rag_list_search_answer_profiles",
        "rag_recommend_search_answer_profile",
        "rag_validate_answer",
        "nl2sql_list_profiles",
        "nl2sql_recommend_profile",
        "nl2sql_cancel_requirement",
        "nl2sql_cancel_job",
    }
)
# Control Plane が自分で呼んだ step の trace_id の接頭辞（最終の検証は回答の確定の後なので
# ここでは現れないが、念のため数えない）。
_CONTROL_PLANE_TRACE_PREFIXES = (
    CANCEL_TRACE_PREFIX,
    RUN_CANCEL_TRACE_PREFIX,
    GUIDE_CHECK_TRACE_PREFIX,
    "answer_validation_",
)

_RETRY_HEAD = (
    "直前の回答は、業務の事実をツールで確かめずに作られています。そのままでは利用者に出せません。"
    "業務の事実（規程・手順・数値・状態・記録など）は、資料の根拠のツールと業務データのツールの"
    "結果だけで答えてください。質問に答えるのに要るツールを呼んで確かめてから、改めて回答してください。"
)
# ツールを呼ばずに、範囲外・答えられないと判断した回答（`declined`。#2608）の差し戻しの頭。範囲の
# 内外と根拠の有無を、モデルの判断ではなくツールの結果で決めさせる（権限・データの範囲の外は
# ツールが呼び先へ送らないので、調べられるのは利用者と業務 Agent が使える範囲だけ）。
RETRY_HEAD_DECLINED = (
    "直前の回答は、ツールで調べずに、質問をこの業務の範囲外・確かめられないと判断しています。"
    "そのままでは利用者に出せません。範囲の内外と、資料・業務データに根拠があるかは、ツールの結果で"
    "判断してください。質問に答えるのに要るツールを呼んで調べてから、改めて回答してください。"
)
# 根拠・データのツールを渡した Run の system の指示（#58。lesson06: 先に調べ、根拠が無いときだけ
# 拒答する）。範囲の内外と根拠の有無を、モデルがツールを呼ぶ前に決めず、ツールの結果で判断させる。
# 以前の指示「業務の範囲外の質問のときは…答えられないと答える」は、調べる前の範囲の判断を認めて
# おり、範囲内で索引済みの根拠がある質問も調べずに範囲外と拒答していた（差し戻しの文
# `RETRY_HEAD_DECLINED` と system の指示が食い違い、差し戻しの後も調べないことがあった）。
# 権限・データの範囲の外はツールが呼び先へ送らないので、調べられるのは利用者と業務 Agent に
# 許された範囲だけ（調べても権限は広がらない）。根拠が無いときの拒答（#1835）は変えない。
SEARCH_FIRST_INSTRUCTION = (
    "質問がこの業務の範囲内か、資料・業務データに根拠があるかは、ツールを呼ぶ前に決めず、"
    "ツールで調べた結果で判断してください。範囲外に見える質問も、まず資料・業務データのツールで"
    "調べてください。調べても根拠が見つからないときは、一般的な知識で答えず、"
    "確かめられないため答えられないと答えてください。"
)
# 差し戻しはツールを 1 つも呼んでいない回答にだけ行うので、範囲外かどうかを調べずに決めてよいとは
# 書かない（#2608。調べずに範囲外と判断した拒答を、差し戻しの文が認めていた）。
_RETRY_TAIL = (
    "ツールで調べても確かめられないときは、推測や一般的な知識で答えず、"
    "確かめられないため答えられないと短く答えてください。"
)
# 回答前の確認のツールを渡していない Run（確認の上限）の、曖昧な質問の扱い。
_CLARIFY_PLAIN = (
    "質問が曖昧で対象（文書・システム・期間など）が分からないときは、ツールを呼ばずに、"
    "対象を確かめる確認の質問だけを返してかまいません（質問の文だけにし、例・説明・答えた後の"
    "案内は添えない）。"
)
# 利用者に確認しない Run（方針「前提を示して回答」・利用者が「確認せずに回答」を選んだ。#1903）。
# 確認の質問をさせず、ツールで調べてから、決まらない点は前提を示して答えさせる。
_CLARIFY_ASSUME = (
    "この実行では利用者に確認しません。質問が曖昧に見えるときも、確認の質問はせず"
    "（本文でも聞き返さない）、まず資料・業務データのツールで調べてください。ツールの結果を見ても"
    "決まらない点は、何を前提にしたかを回答の中で示して答えてください（条件によって答えが変わる"
    "ときは、条件ごとに分けて示してください）。業務プロファイル・検索の対象の推薦が無い（null の）"
    "ときも聞き返さず、推薦の候補（score の高い順）か一覧の説明から質問に最も合うものを選んで"
    "調べてください。"
)
# 回答前の確認のツール（ask_user_confirmation）を渡した Run（#1881）。本文で聞き返させず、資料・
# 業務データのツールの確認事項か、確認のツールのカードで確かめさせる。
_CLARIFY_CONFIRMABLE = (
    "質問が曖昧に見えるときも、まず資料・業務データのツールで調べてください（業務ガイド・資料・"
    "業務データのツールが確認事項を返せば、それが利用者への確認のカードになります）。"
    "ツールの結果を見ても対象（文書・システム・期間など）が決まらないときと、プロファイルの推薦の"
    "候補が割れたときだけ、"
    f"{CONFIRMATION_TOOL} で確認してください（選択肢は候補の名前と説明）。"
    "本文に確認の質問や選ばせる表を書かないでください。"
)
RETRY_MESSAGE = _RETRY_HEAD + _CLARIFY_PLAIN + _RETRY_TAIL
RETRY_MESSAGE_ASSUME = _RETRY_HEAD + _CLARIFY_ASSUME + _RETRY_TAIL
RETRY_MESSAGE_CONFIRMABLE = _RETRY_HEAD + _CLARIFY_CONFIRMABLE + _RETRY_TAIL

REFUSAL_HEAD = (
    "社内の資料・業務データで確かめられなかったため、お答えできません。推測ではお答えしていません。"
)
_RAG_EXAMPLE = "「経費精算規程の締め日は？」"
_DATA_EXAMPLE = "「先月の部門別の受注金額は？」"
_SCOPE_DESCRIPTION_MAX_CHARS = 200


# 結果が空なら根拠・データを調べた呼び出しに数えないツールと、その結果の一覧の項目（#2504）。業務
# ガイドの照合は、当たる業務ガイドを返したときだけ数える（業務ガイドが無いことは資料が無いことでは
# ないので、それだけで「資料に無い」と答えた回答を差し戻す）。
_EMPTY_RESULT_ITEMS = {"rag_lookup_guides": "guides"}


def _empty_result(step: RunStep) -> bool:
    """結果が空なら数えないツールで、成功して空を返した呼び出しか（#2504）。"""
    call, result = step.tool_call, step.tool_result
    key = _EMPTY_RESULT_ITEMS.get(mcp_base_tool_name(call.name)) if call is not None else None
    if key is None or result is None or not result.success:
        return False
    output = result.output
    items = output.get(key) if isinstance(output, dict) else None
    return isinstance(items, list) and not items


def is_grounding_tool(name: str) -> bool:
    """根拠・データのツールか（MCP 接続のツール。一覧・推薦・Control Plane が呼ぶものを除く）。

    Control Plane のツール（`echo`・`skill_reference_read` など。名前に接続の
    区切りが無い）は数えない。例外は添付を読むツール（`ATTACHMENT_READ_TOOLS`。#2517）。
    """
    if name in ATTACHMENT_READ_TOOLS:
        # 利用者が添付したファイルを読むツール（#2517）は、その内容の根拠を調べたものに数える。
        return True
    if MCP_TOOL_SEPARATOR not in name:
        return False
    base = mcp_base_tool_name(name)
    return bool(base) and base not in _NON_GROUNDING_TOOLS


def grounding_tools(tool_names: Iterable[str]) -> list[str]:
    """モデルに渡したツールのうち、根拠・データのツール。"""
    return [name for name in tool_names if is_grounding_tool(name)]


def grounding_attempted(steps: list[RunStep]) -> bool:
    """モデルがこの Run で根拠・データのツールを呼んだ（呼ぼうとした）か。

    当たる業務ガイドが無かった業務ガイドの照合は数えない（#2504。`_empty_result`）。
    """
    for step in steps:
        call = step.tool_call
        if call is None or not is_grounding_tool(call.name) or _empty_result(step):
            continue
        if (call.trace_id or "").startswith(_CONTROL_PLANE_TRACE_PREFIXES):
            continue
        return True
    return False


def tools_consulted(steps: list[RunStep]) -> bool:
    """モデルがこの Run で MCP 接続のツール（根拠・データのツールと、プロファイルの一覧・推薦）を
    呼んだ（呼ぼうとした）か（#1917）。

    回答前の確認（`ask_user_confirmation`）の前に資料・業務データを調べたかの判定に使う。
    プロファイルの推薦の候補が割れたときの確認は推薦を呼んだ後なので、推薦も数える。
    Control Plane が自分で呼んだもの（確認の取り消し・業務ガイドの照合・最終の検証）は数えない。
    """
    for step in steps:
        call = step.tool_call
        if call is None or MCP_TOOL_SEPARATOR not in call.name:
            continue
        if (call.trace_id or "").startswith(_CONTROL_PLANE_TRACE_PREFIXES):
            continue
        return True
    return False


def last_tool_failed(steps: list[RunStep]) -> bool:
    """モデルがこの Run で最後に呼んだ MCP 接続のツールが失敗したか（#2234）。

    ツールのエラー（呼び先の LLM のタイムアウト・接続の失敗など）の直後の回答前の確認を差し戻すのに
    使う（確認に答えてもツールの失敗は直らない）。Control Plane が自分で呼んだものは数えない。
    """
    for step in reversed(steps):
        call = step.tool_call
        if call is None or MCP_TOOL_SEPARATOR not in call.name:
            continue
        if (call.trace_id or "").startswith(_CONTROL_PLANE_TRACE_PREFIXES):
            continue
        if step.status not in {"completed", "failed"}:
            continue
        return step.status == "failed" or step.tool_result is None or not step.tool_result.success
    return False


def tool_output_texts(steps: list[RunStep]) -> list[str]:
    """この Run でモデルが呼んで成功した MCP 接続のツールの結果（JSON の文。#1934）。

    回答前の確認の選択肢がツールの結果にあるかを確かめるのに使う。Control Plane が自分で
    呼んだもの（確認の取り消し・業務ガイドの照合・最終の検証）は入れない。
    """
    texts: list[str] = []
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or MCP_TOOL_SEPARATOR not in call.name:
            continue
        if (call.trace_id or "").startswith(_CONTROL_PLANE_TRACE_PREFIXES):
            continue
        if result.success and result.output is not None:
            texts.append(json.dumps(result.output, ensure_ascii=False, default=str))
    return texts


def grounding_used(steps: list[RunStep]) -> bool:
    """モデルが根拠・データのツールを呼んで結果を得たか（成功した呼び出しだけ。当たる業務ガイドが
    無かった業務ガイドの照合は数えない。#2504）。"""
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not is_grounding_tool(call.name):
            continue
        if _empty_result(step):
            continue
        if (call.trace_id or "").startswith(_CONTROL_PLANE_TRACE_PREFIXES):
            continue
        if result.success and step.status == "completed":
            return True
    return False


# 根拠・データのツールの実行の失敗（呼び先の障害・接続・応答の誤り）の error code（#2475）。承認の
# 却下・評価の dry-run・無人の Run で呼ばなかったもの・予算の超過・データの範囲の拒否は数えない。
GROUNDING_TOOL_FAILED_CODE = "runtime.grounding_tool_failed"
_EXECUTION_FAILURE_PREFIXES = ("mcp.", "external_mcp.")
_EXECUTION_FAILURE_CODES = frozenset({"tool.unhandled_error"})
_MCP_TOOL_ERROR = "mcp.tool_error"


def _execution_failed(step: RunStep) -> bool:
    """ツールの呼び出しが、呼び先で実行できずに失敗したか（引数の誤りなど呼び出し側の誤りは除く）。"""
    result = step.tool_result
    if step.status != "failed" or result is None or result.success:
        return False
    code = result.error_code or ""
    if code == _MCP_TOOL_ERROR:
        # 呼び先が 4xx（引数・権限・対象が無い）で返したエラーは、呼び出しを直せば答えられる。
        status = (result.error_details or {}).get("status")
        return not (isinstance(status, int) and 400 <= status < 500)
    return code.startswith(_EXECUTION_FAILURE_PREFIXES) or code in _EXECUTION_FAILURE_CODES


def grounding_failure(steps: list[RunStep]) -> RunStep | None:
    """根拠・データのツールを呼んだが 1 つも結果を得られず、実行に失敗した呼び出しがあれば、
    その最後の step（#2475）。

    検索・照会を終えられなかった Run の回答は、資料・業務データを調べた結果ではない。そのまま
    出すと「資料に根拠が無い」と読まれるため、`builtin_runtime` は Run を失敗にする。
    Control Plane が自分で呼んだものは数えない。
    """
    if grounding_used(steps):
        return None
    failed: RunStep | None = None
    for step in steps:
        call = step.tool_call
        if call is None or not is_grounding_tool(call.name):
            continue
        if (call.trace_id or "").startswith(_CONTROL_PLANE_TRACE_PREFIXES):
            continue
        if _execution_failed(step):
            failed = step
    return failed


def grounding_failure_message(step: RunStep) -> str:
    """根拠・データのツールの実行の失敗で Run を止めたときの文（ツールの名前と返した理由）。"""
    call, result = step.tool_call, step.tool_result
    name = mcp_base_tool_name(call.name) if call is not None else ""
    reason = (result.error if result is not None else "") or ""
    head = f"資料・業務データのツール（{name}）の実行に失敗したため、回答を作れませんでした。"
    tail = "資料・業務データに根拠が無いという意味ではありません。"
    return f"{head}{reason.strip()}{tail}" if reason.strip() else f"{head}{tail}"


def grounded_in_thread(previous_steps: Iterable[list[RunStep]]) -> bool:
    """同じ会話の前の Run（会話の履歴の上限の中）のどれかが、根拠・データのツールで答えたか。

    そうなら、今の Run がツールを呼ばずに前の回答を言い換え・整形・要約しても拒答にしない
    （「前の回答を表にして」「もっと短く」など）。前のターンの事実の確かめは、前のターンで
    裏付けた根拠を今のターンの最終の検証に渡す仕組み（#1837）に任せる（今のターンに根拠が無くても、
    その根拠だけで検証する。#1879）。
    """
    return any(grounding_used(steps) for steps in previous_steps)


def is_exempt_answer(answer: str, steps: list[RunStep]) -> bool:
    """拒答の対象外の回答か（利用者への確認の質問だけ・NL2SQL の SQL の生成前の確認で止まった）。"""
    return is_clarification_only(answer) or nl2sql_clarification_pending(steps)


def is_greeting_reply(answer: str, question: str = "") -> bool:
    """あいさつ・質問でない入力への返事か（#1879・#1929）。

    あいさつ・お礼を含む回答か、利用者の質問があいさつ・お礼だけか、用件（何を知りたいか）を
    聞くだけの回答（`asks_for_request`。「テスト」への「社内の規程や手順について、知りたいことを
    お知らせください。」）。「こんにちは。ご用件をお知らせください。」のように用件を聞く回答は、
    質問の解釈を確かめる確認の質問ではないので、本文の確認の質問の差し戻し（#1881・#1903）の
    対象にしない。
    """
    return greets(answer) or is_greeting_only(question) or asks_for_request(answer)


def needs_grounding(
    answer: str,
    *,
    steps: list[RunStep],
    tool_names: Collection[str],
    clarification_exempt: bool = True,
    question: str = "",
) -> bool:
    """ツールで確かめていない回答として、差し戻し・拒答の対象か。

    `clarification_exempt` が False（確認のツールを渡した Run と、利用者に確認しない Run。
    #1881・#1903）なら、本文の確認の質問だけの回答も差し戻しの対象にする（あいさつへの返事は
    除く。`is_greeting_reply`。`question` は利用者の質問）。NL2SQL の SQL の生成前の確認で止まった
    Run は、ツールを呼んでいるので対象外。
    """
    if not grounding_tools(tool_names):
        return False
    if grounding_attempted(steps):
        return False
    if not clarification_exempt and not is_greeting_reply(answer, question):
        return not nl2sql_clarification_pending(steps)
    return not is_exempt_answer(answer, steps)


def clarified_in_text(answer: str, *, steps: list[RunStep], tool_names: Collection[str]) -> bool:
    """回答前の確認のツールを渡したのに、ツールを呼ばずに本文で確認の質問だけを返したか（#1881）。

    拒答にはしない（`is_exempt_answer`）。確認のカードが出なかった改善の手がかりとして、
    呼ぶ側が Run の経過に残す。
    """
    if CONFIRMATION_TOOL not in tool_names or not grounding_tools(tool_names):
        return False
    if grounding_attempted(steps) or nl2sql_clarification_pending(steps):
        return False
    return is_clarification_only(answer)


def retry_message(*, confirmable: bool, assume: bool = False, declined: bool = False) -> str:
    """差し戻しの文（回答前の確認のツールを渡したかで、曖昧な質問の扱いを分ける。#1881）。

    `assume` は利用者に確認しない Run（#1903）。確認のツールを渡していても（「確認せずに回答」で
    再開した Run は、答えた呼び出しの再開のためにツールが残る）、確認させない文にする。
    `declined` は、直前の回答がツールで調べずに範囲外・答えられないと判断した拒答（`declined`。
    #2608）。頭の文を、範囲の内外と根拠の有無をツールの結果で判断させる文にする。
    """
    clarify = _CLARIFY_CONFIRMABLE if confirmable else _CLARIFY_PLAIN
    if assume:
        clarify = _CLARIFY_ASSUME
    head = RETRY_HEAD_DECLINED if declined else _RETRY_HEAD
    return head + clarify + _RETRY_TAIL


# 差し戻しに添える元の回答の長さの上限（モデルへの入力を増やしすぎない）。
_RETRY_ANSWER_MAX_CHARS = 4000


def retry_input(
    items: list[Any],
    answer: str,
    *,
    confirmable: bool = False,
    assume: bool = False,
    declined: bool = False,
    head: str | None = None,
) -> list[Any]:
    """差し戻して Run を続ける入力（前の入力・ツールの呼び出し・差し戻しの利用者のメッセージ）。

    前の応答の assistant のメッセージ（`output_text`）は入力に入れない。モデルの API の
    `openai.gpt-oss-120b` は、`input` の `output_text` を「output_text is not supported by model」の
    400 で拒否する（2026-10-14 に実環境で確認）。その直前の reasoning item も、続く出力が無くなる
    ため外す。元の回答は差し戻しのメッセージの中に引用として添える（何を直すかをモデルに示す）。
    `confirmable` は、この Run でモデルに `ask_user_confirmation` を渡したか、`assume` は利用者に
    確認しない Run か、`declined` は調べずに範囲外・答えられないと判断した拒答か
    （`retry_message`）。
    `head` を渡すと差し戻しの文をそれにする（多段の段の差し戻し。#1958）。
    """
    kept: list[Any] = []
    for item in items:
        data = item if isinstance(item, dict) else None
        if data is None and hasattr(item, "model_dump"):
            data = item.model_dump(exclude_none=True)
        if isinstance(data, dict) and data.get("role") == "assistant":
            while kept and _item_type(kept[-1]) == "reasoning":
                kept.pop()
            continue
        kept.append(item)
    quoted = answer.strip()
    if len(quoted) > _RETRY_ANSWER_MAX_CHARS:
        quoted = quoted[: _RETRY_ANSWER_MAX_CHARS - 1] + "…"
    head = head or retry_message(confirmable=confirmable, assume=assume, declined=declined)
    message = f"{head}\n\n直前の回答（利用者には出していません）:\n{quoted}"
    return [*kept, {"role": "user", "content": message}]


def _item_type(item: Any) -> object:
    if isinstance(item, dict):
        return item.get("type")
    return getattr(item, "type", None)


def hops_retried(metadata: object) -> bool:
    """この Run で、引いていない段（略号・別名）を差し戻したか（#1958。1 回だけ）。"""
    record = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    return isinstance(record, dict) and bool(record.get("hop_retries"))


def hop_original_answer(events: Sequence[Any]) -> str | None:
    """差し戻しの前の回答（経過に残したもの。無ければ None）。

    差し戻しは略号・別名（`hop_retry`。#1958）か業務データ（`data_retry`。#1979）か業務ガイドの
    手順（`guide_step_retry`。#2396）の 1 回だけ。章節の続きの差し戻し（`section_retry`。#2492）は
    含めない（前の回答は手順・条件の後半を欠くと分かっているので、前の回答に戻さない）。
    """
    for event in reversed(list(events)):
        payload = getattr(event, "payload", None)
        record = payload.get("tool_grounding") if isinstance(payload, dict) else None
        if isinstance(record, dict) and record.get("action") in {
            "hop_retry",
            "data_retry",
            "guide_step_retry",
        }:
            original = record.get("original_answer")
            return original if isinstance(original, str) else None
    return None


def retried(metadata: object) -> int:
    """この Run で差し戻した回数。"""
    record = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    count = record.get("retries") if isinstance(record, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _scope_line(description: str, skill_names: list[str]) -> str:
    text = " ".join(description.split())
    if len(text) > _SCOPE_DESCRIPTION_MAX_CHARS:
        text = text[: _SCOPE_DESCRIPTION_MAX_CHARS - 1] + "…"
    names = "、".join(dict.fromkeys(name.strip() for name in skill_names if name.strip()))
    if text and names:
        return f"この業務 Agent が扱う内容: {text}（スキル: {names}）"
    if text or names:
        return f"この業務 Agent が扱う内容: {text or names}"
    return ""


def refusal_text(
    *, description: str = "", skill_names: list[str] | None = None, tool_names: Collection[str] = ()
) -> str:
    """拒答の文（決まった形。何が無いか・扱う範囲・質問し直し方を示し、推測で補わない）。

    言い換えの例は、モデルに渡した根拠・データのツールの種類から選ぶ（資料なら規程の質問、
    業務データなら期間と集計の質問。どちらでもなければ両方）。
    """
    grounding = grounding_tools(tool_names)
    has_rag = any(mcp_base_tool_name(name).startswith("rag_") for name in grounding)
    has_data = any(not mcp_base_tool_name(name).startswith("rag_") for name in grounding)
    examples = [
        example
        for example, used in ((_RAG_EXAMPLE, has_rag), (_DATA_EXAMPLE, has_data))
        if used or not (has_rag or has_data)
    ]
    lines = [REFUSAL_HEAD]
    scope = _scope_line(description, list(skill_names or []))
    if scope:
        lines.append(scope)
    lines.append(
        "この業務 Agent が扱う内容について、文書名・システム名・期間などを入れて質問してください。"
        f"例: {''.join(examples)}"
    )
    return "\n\n".join(lines)


def declined(answer: str) -> bool:
    """回答の結論が、答えられない・資料から確かめられない・この業務の範囲外と述べるか（#2608）。

    根拠・データのツールを呼ばずに作った回答に使い、調べずに範囲外・答えられないと判断した拒答と、
    ツールで確かめずに業務の事実を答えた回答を分ける（差し戻しの文・Run の経過・回答の対応の
    basis）。判定は回答の最初の段落だけで決める（`answer_passages.concludes_decline`）。
    """
    return concludes_decline(answer)


def refusal_outcome(*, declined: bool = False) -> JsonObject:
    """拒答の回答の対応（`answer_outcome` と同じ形）。

    差し戻しの後もツールを呼ばずに、範囲外・答えられないと判断した拒答（`declined`。#2608）は
    basis を `declined_without_tools`、ツールで確かめずに答えた回答を置き換えたものは
    `no_tool_grounding` にする。
    """
    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "value": INSUFFICIENT_EVIDENCE,
        "basis": BASIS_DECLINED_WITHOUT_TOOLS if declined else BASIS_NO_TOOL_GROUNDING,
        "rag_outcome": None,
        "signals": [],
    }


def refusal_stage(outcome: str | None, tool_calls: Iterable[str]) -> str | None:
    """拒答（回答の対応が insufficient_evidence）が、ツールで調べる前か後か（#2608）。

    呼んだ（呼ぼうとした）ツールの名前に根拠・データのツールが 1 つも無ければ `before_tools`
    （調べずに拒答した。ツールを呼ばずに作った回答の拒答 #1835 を含む）、あれば `after_tools`
    （調べた結果として根拠が無かった）。拒答でなければ None。品質評価の集計に使う。
    """
    if outcome != INSUFFICIENT_EVIDENCE:
        return None
    if any(is_grounding_tool(name) for name in tool_calls):
        return REFUSAL_AFTER_TOOLS
    return REFUSAL_BEFORE_TOOLS


__all__ = [
    "BASIS_DECLINED_WITHOUT_TOOLS",
    "BASIS_NO_TOOL_GROUNDING",
    "GROUNDING_TOOL_FAILED_CODE",
    "MAX_RETRIES",
    "METADATA_KEY",
    "REASON_NO_TOOL_GROUNDING",
    "REFUSAL_AFTER_TOOLS",
    "REFUSAL_BEFORE_TOOLS",
    "REFUSAL_HEAD",
    "RETRY_HEAD_DECLINED",
    "RETRY_MESSAGE",
    "RETRY_MESSAGE_ASSUME",
    "RETRY_MESSAGE_CONFIRMABLE",
    "SEARCH_FIRST_INSTRUCTION",
    "clarified_in_text",
    "declined",
    "grounding_attempted",
    "grounding_failure",
    "grounding_failure_message",
    "grounded_in_thread",
    "grounding_tools",
    "grounding_used",
    "hop_original_answer",
    "hops_retried",
    "is_exempt_answer",
    "is_greeting_reply",
    "is_grounding_tool",
    "needs_grounding",
    "refusal_outcome",
    "refusal_stage",
    "refusal_text",
    "retried",
    "retry_input",
    "retry_message",
    "tool_output_texts",
    "last_tool_failed",
    "tools_consulted",
]
