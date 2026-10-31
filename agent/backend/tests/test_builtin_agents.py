"""初期の業務 Agent（資料・データ・横断の 3 つ）と、資料とデータの横断調査の Skill（#1573）。"""

from __future__ import annotations

import re

import pytest
from checkpoint_support import export_state, load_state

from app.features.agent.builtin_runtime import agent_mcp_requirements
from app.features.agent.runtime import (
    BUILTIN_AGENT_IDS,
    AgentProfile,
    AgentRuntimeRepository,
    AgentRuntimeSnapshot,
    AgentVersion,
    RunCreateRequest,
)
from app.features.agent.skills import (
    RAG_AND_STRUCTURED_DATA_SKILL_ID,
    skill_registry,
)
from app.features.agent.templates import AGENT_TEMPLATES

OLD_SKILL_ID = "rag_then_structured_data"
LEGACY_DEFAULT_NAME = "汎用業務 Agent"
LEGACY_DEFAULT_SKILLS = [
    "business-rag-research",
    "structured-data-query",
    RAG_AND_STRUCTURED_DATA_SKILL_ID,
]


def _agent(repository: AgentRuntimeRepository, agent_id: str) -> AgentProfile:
    return next(item for item in repository.list_agents() if item.id == agent_id)


def test_new_environment_has_three_builtin_agents() -> None:
    repository = AgentRuntimeRepository()

    agents = repository.list_agents()
    assert [agent.id for agent in agents] == ["default", "rag-research", "data-query"]
    assert list(BUILTIN_AGENT_IDS) == ["default", "rag-research", "data-query"]
    assert [agent.name for agent in agents] == [
        "資料・データ横断アシスタント",
        "資料調査アシスタント",
        "データ照会アシスタント",
    ]
    assert [agent.skill_ids for agent in agents] == [
        [RAG_AND_STRUCTURED_DATA_SKILL_ID, "business-rag-research", "structured-data-query"],
        ["business-rag-research"],
        ["structured-data-query"],
    ]
    for agent in agents:
        # 公開した版があり、利用者がすぐ使える（チャット・自動実行で選べる）。
        assert agent.source == "builtin"
        assert agent.published_version == 1
        assert agent.unpublished_changes is False
        run = repository.create_builtin_run(RunCreateRequest(goal="質問", agent_id=agent.id))
        assert run.metadata["agent_version"] == 1


def test_builtin_agents_use_mcp_connections_of_their_skills() -> None:
    repository = AgentRuntimeRepository()

    def requirements(agent_id: str) -> dict[str, set[str] | None]:
        return agent_mcp_requirements(_agent(repository, agent_id).skill_ids)

    assert set(requirements("rag-research")) == {"rag"}
    assert set(requirements("data-query")) == {"nl2sql"}
    combined = requirements("default")
    assert set(combined) == {"rag", "nl2sql"}
    assert {"rag_retrieve_evidence"} <= (combined["rag"] or set())
    assert {"nl2sql_query", "nl2sql_get_job"} <= (combined["nl2sql"] or set())


def test_combined_skill_does_not_fix_the_order() -> None:
    assert skill_registry.get(OLD_SKILL_ID) is None
    skill = skill_registry.get(RAG_AND_STRUCTURED_DATA_SKILL_ID)
    assert skill is not None
    assert skill.name == "資料とデータの横断調査"
    assert skill.source == "builtin"
    # 識別子は Markdown の `code`（#1795）。規則の文は記号を除いて確かめる。
    instructions = skill.instructions.replace("`", "")
    # 順序を固定しない・往復と繰り返しを許す・止めどきを書く。
    assert "先に rag_retrieve_evidence" not in instructions
    assert "順序は決めない" in instructions
    assert "nl2sql_query が先でもよい" in instructions
    assert "何度でも呼んでよい" in instructions
    assert "それ以上ツールを呼ばずに答える" in instructions
    # 今の指示の手順を引き継ぐ。
    for phrase in (
        # 資料の根拠だけでは確定できない現場の値は業務データで確かめる（#2651 で RAG の回答の
        # 対応から根拠の取得に移した）。
        "資料の根拠だけでは確定できない現場の値・記録",
        "nl2sql_recommend_profile",
        "nl2sql_get_job",
        "needs_clarification",
        "requirement_session_id",
        "出所として分けて示す",
        # 資料の台帳の値で挙げる・数える質問は資料、業務データの表の集計は NL2SQL（#1958）。
        "値がどこに載っているかで決める",
        "台帳・一覧・名簿・規程の表のような資料そのものに載っている質問",
        "そこで終えずに資料（rag_retrieve_evidence）を引く",
        # 資料と業務データの両方が要る質問（#1979）: 規程の名前で始まっても、業務の対象の値は
        # 業務データで引き、資料に無いと答える前に nl2sql_query を呼ぶ。
        "資料の値と業務データの値の両方が要ることがある",
        "業務の対象（受注・売上・商品・顧客・請求など）の値（単価・金額・件数・日付・状態）は、"
        "業務データ（nl2sql_query）で引く",
        "規程の名前で始まる質問でも",
        "「資料からは確認できない」と答える前に nl2sql_query で引く",
        "業務データのツールを呼ばずに、業務の対象の値",
        # NL2SQL には規程の言葉ではなく、資料で分かった基準を値の条件にして渡す（#1979）。
        "資料で分かった基準を値の条件に置き換えて書く",
    ):
        assert phrase in instructions
    tools = {
        requirement.server_id: requirement.tool_names for requirement in skill.mcp_requirements
    }
    # 検索・回答プロファイルは推薦で選ぶ（#1850）。
    assert tools["rag"] == [
        "rag_retrieve_evidence",
        "rag_recommend_search_answer_profile",
        "rag_list_search_answer_profiles",
    ]
    assert "nl2sql_query" in tools["nl2sql"]


BUILTIN_SKILL_IDS = [skill.id for skill in skill_registry.list() if skill.source == "builtin"]


# 組み込みの Skill で同じ見出しの並び（使う見出しだけを、この順に置く。#1795 / #1816。
# 「入力の種類を分ける」は #1929）。見出しはすべて処理順の `## Step N:`（docs/agents/prompts.md。
# #3830）で、手順の Step を前と後の節の間に置く。
# 「いつ使うか」は SKILL.md の frontmatter の description に書く（Agent Skills の best practices）。
# 使うツールの一覧は frontmatter の metadata とモデルに渡すツールの定義にあるので、
# 本文に節を置かない（1 つの規則は 1 層。#2269）。
SKILL_LEADING_SECTIONS = ["入力の種類を分ける"]
SKILL_TRAILING_SECTIONS = ["結果ごとの対応", "回答の書き方", "してはいけないこと"]


@pytest.mark.parametrize("skill_id", BUILTIN_SKILL_IDS, ids=BUILTIN_SKILL_IDS)
def test_builtin_skill_instructions_are_flow_markdown(skill_id: str) -> None:
    """組み込みの Skill の指示は、手順で進める Step 形式の Markdown（#1795 / #1816 / #3830）。

    本文は目的の文から始まり、見出しはすべて `## Step N:`（1 から順に。入れ子の Step を置かない）。
    節の並びは組み込みの Skill で同じ。本文の「Step N」（「→ Step N」の行き先など）は
    ある Step だけを指す。
    """
    skill = skill_registry.get(skill_id)
    assert skill is not None
    instructions = skill.instructions
    assert not instructions.splitlines()[0].startswith("#")
    headings = re.findall(r"^#+ .*$", instructions, re.M)
    steps = re.findall(r"^## Step (\d+): (\S.*)$", instructions, re.M)
    assert len(steps) == len(headings), headings
    numbers = [int(number) for number, _ in steps]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    titles = [title for _, title in steps]
    leading = [name for name in SKILL_LEADING_SECTIONS if name in titles]
    trailing = [name for name in SKILL_TRAILING_SECTIONS if name in titles]
    procedure = titles[len(leading) : len(titles) - len(trailing)]
    assert titles == leading + procedure + trailing, titles
    assert "結果ごとの対応" in trailing, titles
    assert procedure and not set(procedure) & {*SKILL_LEADING_SECTIONS, *SKILL_TRAILING_SECTIONS}
    targets = {int(number) for number in re.findall(r"Step (\d+)", instructions)}
    assert targets <= set(numbers), targets
    # 1 つの長い段落に戻さない（書き直しの前は 1 行が 4,000 字を超えていた）。
    lines = instructions.splitlines()
    assert all(len(line) <= 300 for line in lines), max(lines, key=len)


def test_templates_use_registered_skills() -> None:
    used = {skill_id for template in AGENT_TEMPLATES for skill_id in template.skill_ids}
    assert OLD_SKILL_ID not in used
    assert RAG_AND_STRUCTURED_DATA_SKILL_ID in used
    assert all(skill_registry.get(skill_id) is not None for skill_id in used)


def _legacy_default() -> AgentProfile:
    content = {
        "name": LEGACY_DEFAULT_NAME,
        "description": "RAG / NL2SQL の MCP 接続と承認フローを使う既定 Agent。",
        "instructions": "業務データは外部ツール経由で取得し、根拠と監査情報を残す。",
        "skill_ids": LEGACY_DEFAULT_SKILLS,
    }
    return AgentProfile(
        id="default",
        source="builtin",
        versioned=True,
        versions=[AgentVersion(version=1, **content)],
        published_version=1,
        **content,
    )


def test_saved_snapshot_is_migrated_to_three_builtin_agents() -> None:
    custom = AgentProfile(
        id="custom-1573",
        name="経理",
        skill_ids=[RAG_AND_STRUCTURED_DATA_SKILL_ID, "business-rag-research"],
        versioned=True,
        versions=[
            AgentVersion(version=1, name="経理", skill_ids=[RAG_AND_STRUCTURED_DATA_SKILL_ID])
        ],
        published_version=1,
    )
    repository = AgentRuntimeRepository()
    load_state(repository, AgentRuntimeSnapshot(agents=[_legacy_default(), custom]))

    # 無かった組み込みの Agent は足す（保存済みの Agent の後ろ）。
    assert [agent.id for agent in repository.list_agents()][-2:] == ["rag-research", "data-query"]
    assert {agent.id for agent in repository.list_agents()} == {
        "default",
        "custom-1573",
        "rag-research",
        "data-query",
    }
    # 名前を変えていない既定の Agent は、新しい内容を次の版として公開する（前の版は残す）。
    default = _agent(repository, "default")
    assert default.name == "資料・データ横断アシスタント"
    assert default.skill_ids[0] == RAG_AND_STRUCTURED_DATA_SKILL_ID
    assert default.published_version == 2
    assert [item.version for item in default.versions] == [1, 2]
    assert default.versions[0].name == LEGACY_DEFAULT_NAME
    assert default.versions[0].skill_ids == [
        "business-rag-research",
        "structured-data-query",
        RAG_AND_STRUCTURED_DATA_SKILL_ID,
    ]
    assert default.versions[1].name == "資料・データ横断アシスタント"
    assert default.unpublished_changes is False
    # 利用者の Agent はそのまま（旧 ID の置き換えはシステムテーブルの migration 013。#1816）。
    migrated = _agent(repository, "custom-1573")
    assert migrated.name == "経理"
    assert migrated.skill_ids == [RAG_AND_STRUCTURED_DATA_SKILL_ID, "business-rag-research"]
    assert migrated.versions[0].skill_ids == [RAG_AND_STRUCTURED_DATA_SKILL_ID]
    # 保存し直して読み込んでも、版は増えない。
    load_state(repository, export_state(repository))
    assert _agent(repository, "default").published_version == 2
    exported = export_state(repository).model_dump_json()
    assert OLD_SKILL_ID not in exported


def test_renamed_default_keeps_user_content() -> None:
    renamed = _legacy_default().model_copy(update={"name": "経理の既定"})
    repository = AgentRuntimeRepository()
    load_state(repository, AgentRuntimeSnapshot(agents=[renamed]))

    default = _agent(repository, "default")
    assert default.name == "経理の既定"
    assert default.published_version == 1
    assert default.skill_ids == [
        "business-rag-research",
        "structured-data-query",
        RAG_AND_STRUCTURED_DATA_SKILL_ID,
    ]


@pytest.mark.parametrize("agent_id", BUILTIN_AGENT_IDS)
def test_builtin_agents_cannot_be_deleted(agent_id: str) -> None:
    repository = AgentRuntimeRepository()
    with pytest.raises(ValueError, match="組み込みの業務 Agent は削除できません。"):
        repository.delete_agent(agent_id)
