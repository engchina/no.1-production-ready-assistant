"""業務ルーターの集約（/api 配下に include される）。"""

from fastapi import APIRouter

from app.features.example.router import router as example_router

api_router = APIRouter()
api_router.include_router(example_router)
