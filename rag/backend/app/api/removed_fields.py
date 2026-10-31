"""改名後の旧 scope 指定を拒否し、読み捨てによる検索範囲の拡大を防ぐ。"""

from fastapi import HTTPException, Request

_REMOVED_SCOPE_FIELDS = frozenset(
    {"business_view_id", "business_view_ids", "allowed_business_view_ids"}
)


async def reject_removed_profile_fields(request: Request) -> None:
    keys = set(request.query_params)
    if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
        try:
            body = await request.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            keys.update(body)
    if keys & _REMOVED_SCOPE_FIELDS:
        raise HTTPException(
            422, "対象の旧指定は使えません。search_answer_profile_id を指定してください。"
        )
