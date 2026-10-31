"""サンプル業務ルーター。実際の feature はここを置き換える。"""

from fastapi import APIRouter
from pr_backend_core import ApiResponse
from pydantic import BaseModel

router = APIRouter(prefix="/example", tags=["example"])


class PingData(BaseModel):
    message: str


@router.get("/ping", response_model=ApiResponse[PingData])
async def ping() -> ApiResponse[PingData]:
    """疎通確認用。共通 envelope（ApiResponse）で返す。"""
    return ApiResponse(data=PingData(message="pong"))
