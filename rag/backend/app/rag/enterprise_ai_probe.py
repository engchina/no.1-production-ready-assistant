"""モデルの API の LLM/VLM endpoint 契約の疎通 probe CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Literal

from pr_backend_core.observability.model_purpose import async_call_with_model_purpose

from app.clients.enterprise_ai import DEFAULT_MIME_TYPE, EnterpriseAiClient
from app.config import Settings, get_settings

ProbeSurface = Literal["llm", "vlm", "both"]

DEFAULT_LLM_PROMPT = "Enterprise AI LLM contract probe の確認用キーワードを答えてください。"
DEFAULT_LLM_CONTEXT = (
    "[enterprise-ai-probe.txt#probe:0]\n"
    "確認用キーワード: ENTERPRISE_AI_CONTRACT_PROBE\n"
    "この文は Enterprise AI LLM endpoint の staging 契約確認にだけ使います。"
)
DEFAULT_VLM_PROMPT = "Enterprise AI VLM contract probe として本文を構造化抽出してください。"
DEFAULT_VLM_TEXT = (
    "文書種別: Enterprise AI contract probe\n"
    "確認用キーワード: ENTERPRISE_AI_CONTRACT_PROBE\n"
    "この文は VLM endpoint の structured extraction 契約確認にだけ使います。"
)


@dataclass(frozen=True)
class EnterpriseAiProbeResult:
    """1 surface 分の非機密 probe 結果。"""

    ok: bool
    surface: str
    dry_run: bool
    stage: str
    request: dict[str, object] = field(default_factory=dict)
    parsed_output: dict[str, object] = field(default_factory=dict)
    error_type: str | None = None


@dataclass(frozen=True)
class EnterpriseAiProbeReport:
    """Enterprise AI probe CLI の JSON 出力。"""

    ok: bool
    results: list[EnterpriseAiProbeResult]


def main() -> int:
    """CLI entrypoint。"""
    parser = argparse.ArgumentParser(description="Probe model API LLM/VLM contracts.")
    parser.add_argument(
        "--surface",
        choices=("llm", "vlm", "both"),
        default="both",
        help="probe 対象。既定では LLM と VLM を両方確認する。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="外部 endpoint へ送信せず、request preview だけを JSON 出力する。",
    )
    parser.add_argument("--prompt", default=DEFAULT_LLM_PROMPT, help="LLM probe 用 query。")
    parser.add_argument("--context", default=DEFAULT_LLM_CONTEXT, help="LLM probe 用 context。")
    parser.add_argument(
        "--vlm-prompt",
        default=DEFAULT_VLM_PROMPT,
        help="VLM probe 用 prompt。",
    )
    parser.add_argument(
        "--vlm-text",
        default=DEFAULT_VLM_TEXT,
        help="VLM probe 用 text/plain 文書本文。",
    )
    parser.add_argument(
        "--mime-type",
        default="text/plain",
        help=f"VLM probe 文書の MIME type。既定は text/plain、空なら {DEFAULT_MIME_TYPE}。",
    )
    args = parser.parse_args()

    report = asyncio.run(
        run_enterprise_ai_probe(
            surface=args.surface,
            dry_run=args.dry_run,
            prompt=args.prompt,
            context=args.context,
            vlm_prompt=args.vlm_prompt,
            vlm_text=args.vlm_text,
            mime_type=args.mime_type,
        )
    )
    print(json.dumps(_report_payload(report), ensure_ascii=False))
    return 0 if report.ok else 1


async def run_enterprise_ai_probe(
    *,
    surface: ProbeSurface = "both",
    dry_run: bool = False,
    prompt: str = DEFAULT_LLM_PROMPT,
    context: str = DEFAULT_LLM_CONTEXT,
    vlm_prompt: str = DEFAULT_VLM_PROMPT,
    vlm_text: str = DEFAULT_VLM_TEXT,
    mime_type: str = "text/plain",
    settings: Settings | None = None,
    client: EnterpriseAiClient | None = None,
) -> EnterpriseAiProbeReport:
    """Enterprise AI の LLM/VLM endpoint 契約を個別に確認する。"""
    resolved_settings = settings or get_settings()
    resolved_client = client or EnterpriseAiClient(settings=resolved_settings)
    surfaces = _selected_surfaces(surface)
    results = [
        await _probe_surface(
            target,
            dry_run=dry_run,
            prompt=prompt,
            context=context,
            vlm_prompt=vlm_prompt,
            vlm_text=vlm_text,
            mime_type=mime_type,
            client=resolved_client,
        )
        for target in surfaces
    ]
    return EnterpriseAiProbeReport(
        ok=all(result.ok for result in results),
        results=results,
    )


async def _probe_surface(
    surface: Literal["llm", "vlm"],
    *,
    dry_run: bool,
    prompt: str,
    context: str,
    vlm_prompt: str,
    vlm_text: str,
    mime_type: str,
    client: EnterpriseAiClient,
) -> EnterpriseAiProbeResult:
    """1 surface の preview と任意の実 endpoint 呼び出しを行う。"""
    try:
        request = _request_preview(
            surface,
            client,
            prompt,
            context,
            vlm_prompt,
            vlm_text,
            mime_type,
        )
    except Exception as exc:
        return EnterpriseAiProbeResult(
            ok=False,
            surface=surface,
            dry_run=dry_run,
            stage="payload",
            error_type=type(exc).__name__,
        )

    if dry_run:
        return EnterpriseAiProbeResult(
            ok=True,
            surface=surface,
            dry_run=True,
            stage="preview",
            request=request,
        )

    try:
        parsed_output = await _invoke_surface(
            surface,
            client,
            prompt,
            context,
            vlm_prompt,
            vlm_text,
            mime_type,
        )
    except Exception as exc:
        return EnterpriseAiProbeResult(
            ok=False,
            surface=surface,
            dry_run=False,
            stage="invoke_or_parse",
            request=request,
            error_type=type(exc).__name__,
        )
    return EnterpriseAiProbeResult(
        ok=True,
        surface=surface,
        dry_run=False,
        stage="parsed",
        request=request,
        parsed_output=parsed_output,
    )


def _request_preview(
    surface: Literal["llm", "vlm"],
    client: EnterpriseAiClient,
    prompt: str,
    context: str,
    vlm_prompt: str,
    vlm_text: str,
    mime_type: str,
) -> dict[str, object]:
    """client の request preview を JSON 化する。"""
    if surface == "llm":
        preview = client.preview_llm_request(prompt, context)
    else:
        preview = client.preview_vlm_request(
            vlm_text.encode("utf-8"),
            vlm_prompt,
            mime_type=mime_type,
        )
    return asdict(preview)


async def _invoke_surface(
    surface: Literal["llm", "vlm"],
    client: EnterpriseAiClient,
    prompt: str,
    context: str,
    vlm_prompt: str,
    vlm_text: str,
    mime_type: str,
) -> dict[str, object]:
    """実 endpoint を呼び、raw content なしの parse 結果 summary を返す。"""
    if surface == "llm":
        answer = await async_call_with_model_purpose(
            "connection.check", client.generate, prompt, context
        )
        return {"text_chars": len(answer)}
    extraction = await async_call_with_model_purpose(
        "connection.check",
        client.extract_with_vlm_endpoint,
        vlm_text.encode("utf-8"),
        vlm_prompt,
        mime_type=mime_type,
    )
    raw_text = extraction.get("raw_text")
    elements = extraction.get("elements")
    return {
        "raw_text_chars": len(raw_text) if isinstance(raw_text, str) else 0,
        "element_count": len(elements) if isinstance(elements, list) else 0,
        "document_type_present": bool(extraction.get("document_type")),
    }


def _selected_surfaces(surface: ProbeSurface) -> tuple[Literal["llm", "vlm"], ...]:
    """CLI surface 指定を内部 surface tuple にする。"""
    if surface == "both":
        return ("llm", "vlm")
    return (surface,)


def _report_payload(report: EnterpriseAiProbeReport) -> dict[str, object]:
    """dataclass report を JSON serializable dict に変換する。"""
    return asdict(report)


if __name__ == "__main__":
    raise SystemExit(main())
