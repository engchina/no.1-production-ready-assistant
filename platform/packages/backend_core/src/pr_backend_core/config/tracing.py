"""3 製品共通の trace の設定。exporter の変更はプロセスの再起動で反映する。"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, model_validator


class TracingSettings(BaseModel):
    trace_enabled: bool = True
    trace_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    trace_otlp_endpoint: str = ""
    trace_otlp_api_key: SecretStr | None = None
    trace_queue_size: int = Field(default=2048, ge=1, le=100000)
    trace_batch_size: int = Field(default=256, ge=1, le=10000)
    trace_export_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    trace_shutdown_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    trace_ui_max_segments: int = Field(default=16, ge=1, le=1000)
    trace_ui_max_spans: int = Field(default=1000, ge=1, le=10000)
    # 共通の span の表（`PLATFORM_TRACE_SPANS`。#3226）。保存期限の 0 は無期限（既定。
    # 自動で消さない）。
    trace_retention_days: int = Field(default=0, ge=0, le=36500)
    trace_store_flush_seconds: float = Field(default=0.5, gt=0, le=60)
    trace_store_batch_size: int = Field(default=100, ge=1, le=10000)
    trace_store_queue_size: int = Field(default=5000, ge=1, le=1000000)

    @model_validator(mode="after")
    def validate_trace_batch(self) -> TracingSettings:
        if self.trace_batch_size > self.trace_queue_size:
            raise ValueError("trace の batch 件数は queue 件数以下にしてください。")
        return self
