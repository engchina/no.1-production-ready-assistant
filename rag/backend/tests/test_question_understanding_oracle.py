"""質問理解の部分更新と古い全体フォームの衝突を実 Oracle で確認する。"""

import asyncio
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from tests.test_security_scope import _scope


@pytest.mark.usefixtures("oracle_db")
async def test_question_settings_update_preserves_other_config_and_rejects_stale_form() -> None:
    client = OracleClient()
    profile = await client.create_search_answer_profile(
        name=f"synthetic-{uuid4().hex}",
        description="合成の設定",
        config=SearchAnswerProfileConfig(
            language_pack={"answer_labels": {"citation_prefix": "参照"}}
        ),
    )
    old_config = profile.config
    await asyncio.gather(
        client.update_question_understanding(
            profile.id, {"question_cues": {"cause_cues": ["理屈"]}}
        ),
        client.update_search_answer_profile(
            profile.id, description="合成の更新", update_fields={"description"}
        ),
    )
    saved = await client.get_search_answer_profile(profile.id)
    assert saved is not None
    assert saved.description == "合成の更新"
    assert saved.config.language_pack == {
        "answer_labels": {"citation_prefix": "参照"},
        "question_cues": {"cause_cues": ["理屈"]},
    }
    with pytest.raises(ValueError, match="質問の理解の設定が変わりました"):
        await client.update_search_answer_profile(
            profile.id, config=old_config, update_fields={"config"}
        )
    reset = await client.update_question_understanding(profile.id, {})
    assert reset.config.language_pack == {"answer_labels": {"citation_prefix": "参照"}}
    assert reset.description == "合成の更新"
    with _scope(search_answer_profile_ids={"synthetic-outside-scope"}), pytest.raises(KeyError):
        await client.update_question_understanding(profile.id, {})
    await client.archive_search_answer_profile(profile.id)
    with pytest.raises(ValueError, match="アーカイブ済み"):
        await client.update_question_understanding(profile.id, {})
