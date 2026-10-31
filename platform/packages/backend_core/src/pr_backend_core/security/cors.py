"""CORS 設定（サービス横断で共通）。"""

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def configure_cors(
    app: FastAPI,
    *,
    origins: Sequence[str],
    allow_credentials: bool = True,
) -> None:
    """CORS ミドルウェアを既定方針で追加する。"""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
