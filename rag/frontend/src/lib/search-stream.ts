/**
 * RAG 検索の SSE ストリーミングクライアント（POST /api/search/stream）。
 * バックエンドは run / stage(複数) / metadata / delta(複数) / citations / done のイベントを送る。
 * `run` は最初に送る実行の id（trace_id）。処理の経過は配信では送らず、画面が実行の trace の API
 * （`GET /api/search/runs/{trace_id}/trace`。共通の span の表の snapshot。#3226）で読む。`stage` は工程の開始・
 * 終了だけ（状態の 1 行に使う）。
 * stream を始めた後の失敗（timeout・回答形式の検証の失敗・内部エラー）は `error` event で届く。
 */

import { appPath } from "./base-path";
import {
  ApiError,
  apiErrorFromEnvelope,
  apiErrorFromFetchFailure,
  notifyResponseAuthStatus,
  withCsrfHeaders,
  type RetrievedChunk,
  type SearchDiagnostics,
  type SearchRequestBody,
} from "./api";
import { t } from "./i18n";

export interface SearchStageEvent {
  occurred_at_ms?: number;
  trace_id: string;
  stage: string;
  outcome: "started" | "success" | "error" | "cancelled" | "skipped";
  elapsed_ms: number;
}

export interface SearchStreamHandlers {
  onStage?: (stage: SearchStageEvent) => void;
  /** 実行の id（trace_id。処理の経過を実行の trace の API で読む。#3226）。最初に 1 回届く。 */
  onRun?: (run: { trace_id: string }) => void;
  onMetadata?: (meta: {
    trace_id: string;
    elapsed_ms: number;
    guardrail_warnings: string[];
    diagnostics: SearchDiagnostics;
    /** 回答の注意書き（回答の本文と根拠の後に出す。無いときは null / 未指定。#2405）。 */
    notice?: string | null;
  }) => void;
  onDelta?: (text: string) => void;
  /** realtime stream 後にガードレールがマスク/差し替えした本文で累積回答を置換する。 */
  onReplace?: (text: string) => void;
  onCitations?: (citations: RetrievedChunk[]) => void;
  onDone?: (meta: { trace_id: string }) => void;
}

/** backend の `error` event の payload（`app/api/routes/search.py`）。 */
interface SearchStreamErrorPayload {
  trace_id?: string;
  message?: string;
  error_type?: string;
  validation_codes?: string[];
  /** 失敗した工程（時間切れは工程の ID、それ以外は工程の名前。#2023）。 */
  stage?: string | null;
  /** LLM の応答が未完了の理由（`incomplete_details.reason`。#2023）。 */
  reason?: string;
  /** 未完了の出力の上限・使った token・推論の量（「詳細」に出す。#2023）。 */
  detail?: string;
}

const SEARCH_STREAM_PATH = "/api/search/stream";

/** `error` event の error_type を、同じ失敗を REST（`POST /api/search`）が返す HTTP status に揃える。 */
const STREAM_ERROR_STATUS: Record<string, number> = {
  TimeoutError: 504,
  GenerationContractError: 502,
  // LLM の応答が送り直しても未完了（REST は 502。#2023）。
  IncompleteResponse: 502,
};

/**
 * `error` event の技術的な項目を「詳細」の項目にする（messaging.md §10.3。#2023）。
 * 時間切れの工程は文言に名前が入っているので重ねない。
 */
export function streamErrorDetails(outcome: SearchStreamErrorPayload) {
  const stage = outcome.error_type === "TimeoutError" ? "" : (outcome.stage ?? "");
  return [
    { label: t("search.stream.detail.traceId"), value: outcome.trace_id ?? "" },
    { label: t("search.stream.detail.errorType"), value: outcome.error_type ?? "" },
    { label: t("search.stream.detail.stage"), value: stage },
    { label: t("search.stream.detail.reason"), value: outcome.reason ?? "" },
    { label: t("search.stream.detail.output"), value: outcome.detail ?? "" },
  ].filter((item) => item.value.trim());
}

/**
 * SSE の `event:`/`data:` ブロックを解析しながらハンドラへ流す。
 *
 * `error` event を受けたら、または `done` を受けずに stream が終わったら ApiError を投げる
 * （途中の失敗を「該当なし」の完了として見せない。#285）。
 */
export async function streamSearch(
  body: SearchRequestBody,
  handlers: SearchStreamHandlers,
  signal?: AbortSignal
): Promise<void> {
  const request = { method: "POST", path: SEARCH_STREAM_PATH };
  let res: Response;
  try {
    // 送る URL には配信の base を付ける（#1316）。エラーの表示の path は付けない。
    res = await fetch(appPath(SEARCH_STREAM_PATH), {
      method: "POST",
      // Cookie セッションの CSRF（#214）。
      headers: withCsrfHeaders("POST", {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      }),
      credentials: "same-origin",
      body: JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    // 通信断（`TypeError: Failed to fetch`）は利用者向けの文の ApiError にする。中止はそのまま（#906）。
    throw apiErrorFromFetchFailure(cause, request) ?? cause;
  }

  if (!res.ok || !res.body) {
    // 401 はログインへ。範囲外の 403（RAG_SCOPE_FORBIDDEN）は検索結果の位置で理由を見せる（#224）。
    notifyResponseAuthStatus(res);
    let envelope: unknown = null;
    try {
      envelope = await res.json();
    } catch {
      // SSE エラー時に JSON でない場合は既定メッセージを使う
    }
    throw apiErrorFromEnvelope(res.status, envelope, res.headers.get("X-Request-ID"));
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const requestId = res.headers.get("X-Request-ID") ?? undefined;
  let buffer = "";
  let outcome: StreamOutcome = null;

  while (outcome === null) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (cause) {
      // 受信の途中で接続が切れた（`TypeError: network error` など）。中止はそのまま（#906）。
      throw apiErrorFromFetchFailure(cause, request) ?? cause;
    }
    const { value, done } = chunk;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");

    let separator: number;
    while (outcome === null && (separator = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      outcome = dispatchEvent(block, handlers);
    }
  }

  if (outcome === null) {
    buffer += decoder.decode();
    buffer = buffer.replace(/\r\n/g, "\n").trim();
    if (buffer) {
      outcome = dispatchEvent(buffer, handlers);
    }
  } else {
    // error / done の後は読まない（backend はその後に event を送らない）。
    void reader.cancel().catch(() => undefined);
  }

  if (outcome === "done") return;
  if (outcome === null) {
    // proxy の timeout・接続断などで done を受けずに終わった。
    throw new ApiError(502, [t("search.stream.incomplete")], { requestId });
  }
  throw new ApiError(
    STREAM_ERROR_STATUS[outcome.error_type ?? ""] ?? 500,
    [outcome.message?.trim() || t("search.stream.failed")],
    { requestId, extraDetails: streamErrorDetails(outcome) }
  );
}

/** stream の終わり方。null はまだ終わっていない（done も error も受けていない）。 */
type StreamOutcome = "done" | SearchStreamErrorPayload | null;

function dispatchEvent(block: string, handlers: SearchStreamHandlers): StreamOutcome {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;

  let payload: unknown;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }

  switch (event) {
    case "stage":
      handlers.onStage?.(payload as SearchStageEvent);
      break;
    case "run": {
      const traceId = (payload as { trace_id?: unknown } | null)?.trace_id;
      if (typeof traceId === "string" && traceId) handlers.onRun?.({ trace_id: traceId });
      break;
    }
    case "metadata":
      handlers.onMetadata?.(payload as Parameters<NonNullable<SearchStreamHandlers["onMetadata"]>>[0]);
      break;
    case "delta":
      handlers.onDelta?.((payload as { text: string }).text);
      break;
    case "replace":
      handlers.onReplace?.((payload as { text: string }).text);
      break;
    case "citations":
      handlers.onCitations?.(payload as RetrievedChunk[]);
      break;
    case "done":
      handlers.onDone?.(payload as { trace_id: string });
      return "done";
    case "error":
      return (payload && typeof payload === "object" ? payload : {}) as SearchStreamErrorPayload;
  }
  return null;
}
