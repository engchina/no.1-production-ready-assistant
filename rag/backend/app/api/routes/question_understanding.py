"""プロファイル単位の質問理解。全体の既定・取込設定を変更しない。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions
from rag_engine.retrieval.task_contract import task_contract
from rag_parser_core.language import bind_pack, builtin_pack, overrides_from, resolve

from app.clients.oracle import OracleClient
from app.rag.language_pack_releases import active_release, pack_from_release
from app.rag.question_understanding import (
    QUESTION_UNDERSTANDING_FIELDS,
    QUESTION_UNDERSTANDING_KEYS,
    question_understanding_overrides,
    replace_question_understanding,
)
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.common import ApiResponse
from app.schemas.search_answer_profile import SearchAnswerProfileDetail
from app.schemas.settings import LanguagePackData, LanguagePackFieldData

router = APIRouter()


class QuestionUnderstandingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overrides: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def validate_overrides(
        cls, value: dict[str, dict[str, list[str]]]
    ) -> dict[str, dict[str, list[str]]]:
        for section, fields in value.items():
            for key in fields:
                if (section, key) not in QUESTION_UNDERSTANDING_KEYS:
                    raise ValueError(f"{section}.{key}: 質問理解で変更できない項目です。")
        # 記号の型・一覧数・語の長さは既存の言語資源の契約で検証する。
        SearchAnswerProfileConfig.model_validate({"language_pack": value})
        return value


class QuestionUnderstandingPreview(QuestionUnderstandingUpdate):
    text: str = Field(min_length=1, max_length=5000)


async def _view(oracle: OracleClient, profile_id: str) -> SearchAnswerProfileDetail:
    view = await oracle.get_search_answer_profile(profile_id)
    if view is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    return view


def _data(view: SearchAnswerProfileDetail) -> LanguagePackData:
    base = pack_from_release(release) if (release := active_release()) else builtin_pack()
    effective = resolve(base, view.config.language_pack)
    return LanguagePackData(
        fields=[
            LanguagePackFieldData(
                section=spec.section,
                key=spec.key,
                kind=spec.kind.value,
                choices=list(spec.choices),
                ingest=spec.ingest,
                max_chars=spec.max_chars,
                min_value=spec.min_value,
            )
            for spec in QUESTION_UNDERSTANDING_FIELDS
        ],
        builtin=question_understanding_overrides(base.to_dict()),
        overrides=question_understanding_overrides(view.config.language_pack),
        effective=question_understanding_overrides(effective.to_dict()),
        hash=effective.hash,
    )


@router.get(
    "/{search_answer_profile_id}/question-understanding",
    response_model=ApiResponse[LanguagePackData],
)
async def get_question_understanding(
    search_answer_profile_id: str,
) -> ApiResponse[LanguagePackData]:
    return ApiResponse(data=_data(await _view(OracleClient(), search_answer_profile_id)))


@router.put(
    "/{search_answer_profile_id}/question-understanding",
    response_model=ApiResponse[LanguagePackData],
)
async def put_question_understanding(
    search_answer_profile_id: str, request: QuestionUnderstandingUpdate
) -> ApiResponse[LanguagePackData]:
    oracle = OracleClient()
    await _view(oracle, search_answer_profile_id)
    base = pack_from_release(release) if (release := active_release()) else builtin_pack()
    values = resolve(base, request.overrides)
    overrides = question_understanding_overrides(overrides_from(values, base))
    try:
        view = await oracle.update_question_understanding(search_answer_profile_id, overrides)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="検索・回答プロファイルが見つかりません。"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=_data(view))


@router.post("/{search_answer_profile_id}/question-understanding/preview")
async def preview_question_understanding(
    search_answer_profile_id: str, request: QuestionUnderstandingPreview
) -> ApiResponse[dict[str, object]]:
    view = await _view(OracleClient(), search_answer_profile_id)
    base = pack_from_release(release) if (release := active_release()) else builtin_pack()
    draft = replace_question_understanding(view.config.language_pack, request.overrides)
    results: dict[str, object] = {"original_text": request.text}
    for name, values in (("saved", view.config.language_pack), ("draft", draft)):
        pack = resolve(base, values)
        with bind_pack(pack):
            results[name] = {
                "inquiry": parse_inquiry_conditions(request.text).to_payload(),
                "task_contract": task_contract(request.text),
                "hash": pack.hash,
            }
    return ApiResponse(data=results)
