"""保存した指示を要求の処理・その子 task に固定する。"""

import logging
from collections.abc import AsyncIterator

from fastapi import Request
from rag_engine.resources.runtime import bind_prompt_overrides

from app.clients.oracle import OracleClient

logger = logging.getLogger(__name__)


async def published_prompt_dependency(request: Request) -> AsyncIterator[None]:
    """モデルを使う変更系 API の前に指示を読み、並行要求と分離する。"""
    if request.method not in {"POST", "PUT", "PATCH"} and not request.url.path.startswith(
        "/api/evaluation/calibration"
    ):
        yield
        return
    try:
        overrides = await OracleClient().answer_prompt_overrides()
    except Exception:  # noqa: BLE001 - 指示を読めなければ組み込みの指示で続ける。
        logger.warning("prompt overrides could not be loaded", exc_info=True)
        yield
        return
    with bind_prompt_overrides(overrides):
        yield
