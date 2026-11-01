"""業務の説明から業務 Agent の下書きを提案する（#2519）。

編集画面の「説明から下書きを作る」が呼ぶ。モデル（既定のテキストモデル）に、業務の説明と、
選べるスキル・プロファイルの候補を渡し、名前・説明・指示・スキル・データの範囲・出力の項目
（#2561）・評価の問いを構造化出力で受ける。候補に無い ID・形の正しくない出力の項目・上限を
超える件数と文字数は、ここ（コード）で外す。提案はフォームに入れるだけで、業務 Agent は作らない
（保存は利用者がいつもの「作成」で行う）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace

from agents import Agent, Runner
from pr_backend_core.bounded_calls import Attempt, BoundedCallFailed, ErrorKind
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.features.agent import builtin_runtime
from app.features.agent.ai_calls import make_ai_caller, retry_model_target
from app.features.agent.data_scope import (
    MAX_SCOPE_PROFILES,
    AgentDataScope,
    AgentDataScopes,
    DataScopeCandidate,
)
from app.features.agent.output_fields import (
    MAX_OUTPUT_FIELDS,
    OUTPUT_KEY_PATTERN,
    AgentOutputField,
    OutputFieldType,
)
from app.features.agent.skills import AgentSkillDefinition
from app.features.agent.templates import TemplateEvaluationCase

logger = logging.getLogger(__name__)

# 業務の説明の文字数の上限。
MAX_DESCRIPTION_CHARS = 4000
# 提案の上限（コードで切り詰める）。
MAX_NAME_CHARS = 80
MAX_AGENT_DESCRIPTION_CHARS = 300
MAX_INSTRUCTIONS_CHARS = 6000
MAX_SUGGESTED_SKILLS = 8
MIN_EVALUATION_CASES = 3
MAX_EVALUATION_CASES = 8
MAX_CASE_TEXT_CHARS = 1000
# モデルを呼ぶ回数の上限（最初の 1 回 + 出力の上限で止まったときの再試行 1 回。判定と同じ）。
DRAFT_MAX_ATTEMPTS = 2


class AgentDraftRequest(BaseModel):
    """「説明から下書きを作る」の入力。"""

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("description")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("業務の説明を入力してください。")
        return stripped


class _ProposedScope(BaseModel):
    connection: str
    profile_ids: list[str]


_NO_INVENTED_FACTS = "文書の章・条・節・頁、数値、日付、人や部署の名前を書かない"


class _ProposedCase(BaseModel):
    question: str = Field(description=f"利用者が実際に聞きそうな質問。{_NO_INVENTED_FACTS}。")
    expected: str = Field(
        description=f"回答が満たすべき要点（何を答え、何を根拠に示すか）。{_NO_INVENTED_FACTS}。"
    )


class _ProposedOutputField(BaseModel):
    key: str
    label: str
    type: OutputFieldType
    required: bool
    description: str = Field(description="値の書き方（無ければ空）")


class _ModelProposal(BaseModel):
    """モデルの出力（structured output。範囲の制約は schema に入れず、読んだ後にコードで外す）。"""

    name: str = Field(description="日本語の短い名前")
    description: str
    instructions: str = Field(description=f"業務 Agent への指示。{_NO_INVENTED_FACTS}。")
    skill_ids: list[str]
    data_scopes: list[_ProposedScope]
    output_fields: list[_ProposedOutputField]
    evaluation_cases: list[_ProposedCase]


class AgentDraftSuggestion(BaseModel):
    """画面へ返す提案（候補の中の ID と上限の中の件数・文字数だけ）。"""

    name: str
    description: str = ""
    instructions: str = ""
    skill_ids: list[str] = Field(default_factory=list)
    data_scopes: AgentDataScopes = Field(default_factory=dict)
    output_fields: list[AgentOutputField] = Field(default_factory=list)
    evaluation_cases: list[TemplateEvaluationCase] = Field(default_factory=list)
    # 候補を取得できなかった接続など、提案に使えなかったもの（画面が知らせる）。
    notices: list[str] = Field(default_factory=list)


class AgentDraftError(Exception):
    """提案を作れない（画面へ返す文言）。"""


DRAFT_INSTRUCTIONS = f"""あなたは業務 Agent の設計を手伝う担当者です。
利用者が書いた業務の説明を読み、その業務に答える業務 Agent の下書きを提案します。
業務 Agent は、社内の資料（RAG）と業務データ（NL2SQL）を、スキルの手順に沿って調べて答えます。

- name: 業務 Agent の名前。何に答えるかが分かる日本語の短い名前（{MAX_NAME_CHARS} 文字以内）。
- description: 利用者が業務 Agent を選ぶときに読む 1 文。
- instructions: 業務 Agent への指示。役割、答える範囲と範囲の外の質問への対応、答え方
  （根拠の示し方・確かめられなかったときの扱い）を書く。ツールの呼び方や調べる手順はスキルが持つので書かない。
- skill_ids: 「使えるスキル」から、この業務に要るものの id を選ぶ。
- data_scopes: 「使えるプロファイル」から、この業務に関係するものだけを接続ごとに選ぶ。関係する
  ものが無い接続は入れない。
- output_fields: 業務の説明に、結果として返す項目（書類から抜き出す項目・照合の結果・一覧の列）が
  書かれているときだけ、その項目を提案する。質問に文で答える業務では空にする。1 つ目は行を見分ける
  項目にする。label は日本語の短い表示名、key はその意味の英語の識別子（半角英小文字と _）。
- evaluation_cases: 公開の前に確かめる質問を {MIN_EVALUATION_CASES}〜{MAX_EVALUATION_CASES} 件。
  question は利用者が実際に聞きそうな質問、expected は回答が満たすべき要点。

資料と業務データの中身は分からない。文書の章・条・頁、数値、日付、人や部署の名前、表の列を
作らず、何を答えるか・何を根拠に示すかだけを書く（instructions と evaluation_cases の両方）。
例: expected は「上限の金額と、その根拠の規程の箇所を示す」と書き、「第3章第5条により
1 日 1 万円」とは書かない。
業務の説明に無い業務・組織・制度を作らない。文はすべて日本語で書く。"""


def _candidate_lines(items: Sequence[tuple[str, str, str]]) -> str:
    return "\n".join(
        f"- {item_id}: {name}" + (f"（{description}）" if description else "")
        for item_id, name, description in items
    )


def draft_input(
    description: str,
    skills: Sequence[AgentSkillDefinition],
    candidates: Mapping[str, tuple[str, Sequence[DataScopeCandidate]]],
) -> str:
    """モデルへ渡す入力（業務の説明と候補）。`candidates` は接続 → (表示名, プロファイル)。"""
    skill_lines = _candidate_lines([(skill.id, skill.name, skill.description) for skill in skills])
    scope_blocks = "\n\n".join(
        f"### 接続 {connection}（{label}）\n"
        + _candidate_lines([(item.id, item.name, item.description) for item in profiles])
        for connection, (label, profiles) in candidates.items()
        if profiles
    )
    return (
        f"## 業務の説明\n{description}\n\n"
        f"## 使えるスキル\n{skill_lines or '（なし）'}\n\n"
        f"## 使えるプロファイル\n{scope_blocks or '（なし）'}"
    )


def _clip(value: str, limit: int) -> str:
    return value.strip()[:limit]


def _output_key(raw: str, number: int) -> str:
    """提案の key を key の形にする（英小文字・数字・_ 以外は _。英字で始まらなければ field_N）。"""
    text = re.sub(r"[^a-z0-9_]+", "_", unicodedata.normalize("NFKC", raw).lower())
    text = re.sub(r"_+", "_", text).strip("_")[:40].rstrip("_")
    return text if OUTPUT_KEY_PATTERN.fullmatch(text) else f"field_{number}"


def _sanitize_output_fields(
    proposed: Sequence[_ProposedOutputField],
) -> tuple[list[AgentOutputField], int, int]:
    """形の正しくない項目・重なる項目を外し、上限で切る（残した項目・外した数・key を付けた数）。

    key の形だけが違う項目は外さず、key を形に合わせて付ける（表示名が項目の中身。モデルは key を
    日本語で書くことがある）。
    """
    fields: list[AgentOutputField] = []
    dropped = 0
    renamed = 0
    for number, item in enumerate(proposed, start=1):
        key = item.key.strip()
        if not OUTPUT_KEY_PATTERN.fullmatch(key):
            key = _output_key(key, number)
            if any(existing.key == key for existing in fields):
                key = f"field_{number}"
            renamed += 1
        try:
            field = AgentOutputField.model_validate(
                {
                    "key": key,
                    "label": _clip(item.label, 40),
                    "type": item.type,
                    "required": item.required,
                    "description": _clip(item.description, 200),
                }
            )
        except ValidationError:
            dropped += 1
            continue
        if any(existing.key == field.key for existing in fields):
            dropped += 1
            continue
        fields.append(field)
    dropped += max(0, len(fields) - MAX_OUTPUT_FIELDS)
    return fields[:MAX_OUTPUT_FIELDS], dropped, renamed


def sanitize_proposal(
    proposal: _ModelProposal,
    skills: Sequence[AgentSkillDefinition],
    candidates: Mapping[str, tuple[str, Sequence[DataScopeCandidate]]],
) -> AgentDraftSuggestion:
    """候補に無い ID・形の正しくない出力の項目を外し、件数と文字数を上限に収める。"""
    known_skills = {skill.id for skill in skills}
    skill_ids = list(dict.fromkeys(item for item in proposal.skill_ids if item in known_skills))
    scopes: AgentDataScopes = {}
    for scope in proposal.data_scopes:
        entry = candidates.get(scope.connection)
        if entry is None:
            continue
        known = {item.id for item in entry[1]}
        ids = list(dict.fromkeys(item for item in scope.profile_ids if item in known))
        if ids:
            ids = ids[:MAX_SCOPE_PROFILES]
            previous = scopes.get(scope.connection)
            merged = list(dict.fromkeys([*(previous.profile_ids if previous else []), *ids]))
            scopes[scope.connection] = AgentDataScope(
                profile_ids=merged[:MAX_SCOPE_PROFILES],
                # 1 つだけなら既定にする（複数なら、モデルが質問ごとに選ぶ）。
                default_profile_id=merged[0] if len(merged) == 1 else "",
            )
    cases = [
        TemplateEvaluationCase(
            question=_clip(case.question, MAX_CASE_TEXT_CHARS),
            expected=_clip(case.expected, MAX_CASE_TEXT_CHARS),
        )
        for case in proposal.evaluation_cases
        if case.question.strip() and case.expected.strip()
    ][:MAX_EVALUATION_CASES]
    output_fields, dropped_fields, renamed_fields = _sanitize_output_fields(proposal.output_fields)
    name = _clip(proposal.name, MAX_NAME_CHARS)
    if not name:
        raise AgentDraftError(
            "業務 Agent の名前を提案できませんでした。説明を詳しくしてやり直してください。"
        )
    notices: list[str] = []
    if output_fields:
        # 項目を返さない業務にも提案することがあるので、入れたことを必ず知らせる。
        notices.append(
            f"出力の項目を {len(output_fields)} 件入れました。回答を文だけで返す業務なら、"
            "「出力の項目」から外してください。"
        )
    if dropped_fields:
        notices.append(f"形の正しくない出力の項目を {dropped_fields} 件外しました。")
    if renamed_fields:
        notices.append(
            f"出力の項目 {renamed_fields} 件の key を付けました。key を確かめてください。"
        )
    return AgentDraftSuggestion(
        name=name,
        description=_clip(proposal.description, MAX_AGENT_DESCRIPTION_CHARS),
        instructions=_clip(proposal.instructions, MAX_INSTRUCTIONS_CHARS),
        skill_ids=skill_ids[:MAX_SUGGESTED_SKILLS],
        data_scopes=scopes,
        output_fields=output_fields,
        evaluation_cases=cases,
        notices=notices,
    )


async def _propose_once(text: str, target: builtin_runtime.ModelTarget) -> _ModelProposal:
    drafter = Agent(
        name="agent_drafter",
        instructions=DRAFT_INSTRUCTIONS,
        model=builtin_runtime.model_factory(target),
        model_settings=builtin_runtime.model_settings_for(target),
        output_type=_ModelProposal,
    )
    result = await Runner.run(drafter, text, max_turns=1)
    output = result.final_output
    if isinstance(output, _ModelProposal):
        return output
    return _ModelProposal.model_validate(output)


def _has_digits(text: str) -> bool:
    """数字（半角・全角）を含むか。資料の中身を知らない提案の要点に数字があれば、作った値・箇所。"""
    return any(char.isdigit() for char in text)


def _invented_cases(proposal: _ModelProposal) -> list[_ProposedCase]:
    """期待する要点に数字（作った数値・章・条・頁）を含む評価の問い。"""
    return [case for case in proposal.evaluation_cases if _has_digits(case.expected)]


def _send_back_input(text: str, invented: Sequence[_ProposedCase]) -> str:
    """差し戻しの入力（前の提案の、直す要点を示す）。"""
    lines = "\n".join(f"- {case.expected}" for case in invented)
    return (
        f"{text}\n\n## 直してほしい点\n前の提案の evaluation_cases の expected に、資料から確かめて"
        f"いない数値・箇所がありました。数値・箇所を書かずに、何を答え、何を根拠に示すかで"
        f"書き直して、提案の全体をもう一度出してください。\n{lines}"
    )


async def suggest_agent_draft(
    description: str,
    *,
    skills: Sequence[AgentSkillDefinition],
    candidates: Mapping[str, tuple[str, Sequence[DataScopeCandidate]]],
    notices: Sequence[str] = (),
) -> AgentDraftSuggestion:
    """業務の説明から下書きを提案する（既定のテキストモデル。構造化出力）。

    呼び出しは品質評価の判定と同じ上限つきの実行器を通す（429・5xx・接続・時間切れは種類ごとに
    再試行し、出力の上限で止まったら上限を広げて 1 回だけ送り直す）。権限・設定の誤りは
    ``BoundedCallAborted`` のまま返し、それ以外の失敗は ``AgentDraftError``。

    評価の問いの期待する要点に数字（作った数値・箇所）があれば、その要点を示して 1 回だけ差し戻す。
    それでも残る問いは外す（作った値で品質評価が正しい回答を誤りと判定しないように）。
    """
    caller = make_ai_caller("agent_drafting", max_concurrency=1)
    text = draft_input(description, skills, candidates)

    async def propose(model_input: str) -> _ModelProposal:
        async def once(attempt: Attempt) -> _ModelProposal:
            target = replace(builtin_runtime.resolve_model_target(""), sdk_max_retries=0)
            for _ in range(min(attempt.adaptation, DRAFT_MAX_ATTEMPTS - 1)):
                target = retry_model_target(target)
            return await _propose_once(model_input, target)

        try:
            return await caller.call(once, label="agent_draft", adaptive=True)
        except BoundedCallFailed as exc:
            logger.warning(
                "agent_draft_suggestion_failed",
                extra={"warning_code": "AGENT_DRAFT_SUGGESTION_FAILED", "kind": exc.kind.value},
            )
            if exc.kind is ErrorKind.TRUNCATED:
                raise AgentDraftError(
                    "提案が長くなりすぎて作れませんでした。説明を短くしてやり直してください。"
                ) from exc
            raise AgentDraftError(
                "下書きを提案できませんでした。時間をおいてもう一度試してください。"
            ) from exc

    proposal = await propose(text)
    invented = _invented_cases(proposal)
    if invented:
        proposal = await propose(_send_back_input(text, invented))
    remaining = _invented_cases(proposal)
    result_notices = list(notices)
    if remaining:
        proposal = proposal.model_copy(
            update={
                "evaluation_cases": [
                    case for case in proposal.evaluation_cases if case not in remaining
                ]
            }
        )
        result_notices.append(
            f"資料から確かめていない数値・箇所を含む評価の問いを {len(remaining)} 件外しました。"
        )
    suggestion = sanitize_proposal(proposal, skills, candidates)
    return suggestion.model_copy(update={"notices": [*result_notices, *suggestion.notices]})
