"""provider が明示した usage だけを正規化する。cache / reasoning は合計の内数。"""

from collections.abc import Mapping

from .tracing import ObservedSpan


def record_genai_usage(span: ObservedSpan, usage: object) -> None:
    if not isinstance(usage, Mapping):
        return

    def tokens(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    input_tokens = tokens(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = tokens(usage.get("output_tokens", usage.get("completion_tokens")))
    input_details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
    output_details = usage.get("output_tokens_details", usage.get("completion_tokens_details"))
    cached = (
        tokens(input_details.get("cached_tokens")) if isinstance(input_details, Mapping) else None
    )
    reasoning = (
        tokens(output_details.get("reasoning_tokens"))
        if isinstance(output_details, Mapping)
        else None
    )
    values: dict[str, object] = {}
    if input_tokens is not None:
        values["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        values["gen_ai.usage.output_tokens"] = output_tokens
    if cached is not None and (input_tokens is None or cached <= input_tokens):
        values["gen_ai.usage.cache_read.input_tokens"] = cached
    if reasoning is not None and (output_tokens is None or reasoning <= output_tokens):
        values["gen_ai.usage.reasoning.output_tokens"] = reasoning
    span.set_attributes(values)
