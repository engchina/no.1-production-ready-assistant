/**
 * API の呼び出しの失敗を、利用者向けの文（何が起きたか + 次の操作）と「詳細」（技術的な情報）に分ける（#900 / #906）。
 * UX 契約 messaging.md §10.3。DOM に依存しない形にして 3 製品の API のラッパーと単体テストから使う。
 *
 * - 応答が届かなかった失敗（timeout・通信断）は `ApiTransportError` にする。ブラウザの英語の文
 *   （`signal timed out` / `Failed to fetch` / `NetworkError when attempting to fetch resource.` など）は
 *   `causeMessage` に分け、画面では「詳細」の「元のメッセージ」にだけ出す。
 * - 製品の `ApiError`（HTTP の失敗）は `ApiErrorPresentable`（`toApiErrorPresentation()`）を実装し、
 *   要約と「詳細」（HTTP ステータス・エラーコード・request ID）を返す。
 */

export type ApiTransportFailureKind = "timeout" | "network";

/** 失敗した要求（「詳細」に出す。path は query を除く）。 */
export interface ApiTransportRequest {
  method: string;
  path: string;
  /** 画面側の待ち時間の上限（ミリ秒）。分かるときだけ。 */
  timeoutMs?: number;
}

/** 応答が届かなかった失敗の、利用者向けの文（既定は日本語。製品の i18n で差し替えられる）。 */
export interface ApiTransportMessages {
  timeout: (seconds: number) => string;
  timeoutUnknownLimit: string;
  timeoutAction: string;
  network: string;
  networkAction: string;
}

export const DEFAULT_API_TRANSPORT_MESSAGES: ApiTransportMessages = {
  timeout: (seconds) => `サーバーの応答が ${seconds} 秒以内に返りませんでした。`,
  timeoutUnknownLimit: "サーバーの応答が規定の時間内に返りませんでした。",
  timeoutAction:
    "サーバーでは処理が続いている可能性があります。少し待ってから画面を更新して結果を確かめ、反映されていなければもう一度実行してください。",
  network: "サーバーに接続できませんでした。",
  networkAction: "ネットワークの接続とサーバーの起動状態を確かめてから、もう一度実行してください。",
};

/** 「詳細」の見出しと項目名（既定は日本語）。 */
export interface ApiErrorDetailLabels {
  details: string;
  request: string;
  limit: string;
  limitSeconds: (seconds: number) => string;
  status: string;
  errorCode: string;
  errorType: string;
  rawMessage: string;
  requestId: string;
  /** 入力の検証エラー（422）の技術的な原文（位置・メッセージ・種別。#1065）。 */
  validationErrors: string;
}

export const DEFAULT_API_ERROR_DETAIL_LABELS: ApiErrorDetailLabels = {
  details: "詳細",
  request: "要求",
  limit: "待ち時間の上限",
  limitSeconds: (seconds) => `${seconds} 秒`,
  status: "HTTP ステータス",
  errorCode: "エラーコード",
  errorType: "エラー種別",
  rawMessage: "元のメッセージ",
  requestId: "リクエストID",
  validationErrors: "入力の検証の原文",
};

export function isAbortError(cause: unknown): boolean {
  return cause instanceof Error && cause.name === "AbortError";
}

/** 待ち時間の上限を超えた（`AbortSignal.timeout` の `TimeoutError`。`ApiTransportError` の timeout も同じ name）。 */
export function isTimeoutError(cause: unknown): boolean {
  return cause instanceof Error && cause.name === "TimeoutError";
}

/**
 * fetch が投げる通信の失敗（ブラウザごとに文が違う `TypeError`。`Failed to fetch` など）。
 * XHR の `error` イベントも製品のラッパーが `TypeError` にして渡す。
 */
export function isNetworkFailure(cause: unknown): boolean {
  return cause instanceof TypeError;
}

/** 要求の path から query を除く（「詳細」に検索語などを出さない）。 */
export function apiRequestPath(path: string): string {
  return path.split("?")[0] ?? path;
}

/**
 * 応答が届かなかった API 呼び出し（待ち時間の上限を超えた・サーバーに接続できない）。
 *
 * `message` は利用者向けの日本語（何が起きたか + 次にできること）にし、ブラウザの英語の文は
 * `causeMessage` に分けて「詳細」に出す。timeout の `name` は `TimeoutError` のままにし、
 * `isTimeoutError` の判定を変えない。
 */
export class ApiTransportError extends Error {
  readonly kind: ApiTransportFailureKind;
  /** 何が起きたか（1 文目）。 */
  readonly summary: string;
  /** 次にできること。 */
  readonly nextAction: string;
  readonly method: string;
  readonly path: string;
  readonly timeoutMs?: number;
  readonly causeName: string;
  readonly causeMessage: string;

  constructor(
    kind: ApiTransportFailureKind,
    request: ApiTransportRequest,
    cause: unknown,
    messages: ApiTransportMessages = DEFAULT_API_TRANSPORT_MESSAGES,
  ) {
    const summary =
      kind === "network"
        ? messages.network
        : request.timeoutMs
          ? messages.timeout(Math.ceil(request.timeoutMs / 1000))
          : messages.timeoutUnknownLimit;
    const nextAction = kind === "network" ? messages.networkAction : messages.timeoutAction;
    super(`${summary}${nextAction}`, { cause });
    this.name = kind === "timeout" ? "TimeoutError" : "NetworkError";
    this.kind = kind;
    this.summary = summary;
    this.nextAction = nextAction;
    this.method = request.method;
    this.path = apiRequestPath(request.path);
    this.timeoutMs = request.timeoutMs;
    this.causeName = cause instanceof Error ? cause.name : typeof cause;
    this.causeMessage = cause instanceof Error ? cause.message : String(cause);
  }
}

export function isTransportError(cause: unknown): cause is ApiTransportError {
  return cause instanceof ApiTransportError;
}

/**
 * 失敗が応答の届かなかったもの（timeout・通信断）なら、その `ApiTransportError` を返す。
 * 製品の `ApiError` が `cause` に包んだもの（RAG の timeout / 通信断。既存の `instanceof ApiError` の分岐で
 * 日本語の文を出すため）も取り出す。
 */
export function transportErrorOf(error: unknown): ApiTransportError | null {
  if (error instanceof ApiTransportError) return error;
  if (error instanceof Error && error.cause instanceof ApiTransportError) return error.cause;
  return null;
}

/**
 * fetch・本文の読み取りが投げた例外を、利用者向けの `ApiTransportError` に変える。
 * timeout・通信断でなければ（利用者の中止の `AbortError` などは）`null` を返し、呼び出し側はそのまま投げる。
 */
export function toApiTransportError(
  cause: unknown,
  request: ApiTransportRequest,
  messages?: ApiTransportMessages,
): ApiTransportError | null {
  if (cause instanceof ApiTransportError) return cause;
  if (isAbortError(cause)) return null;
  if (isTimeoutError(cause)) return new ApiTransportError("timeout", request, cause, messages);
  if (isNetworkFailure(cause)) return new ApiTransportError("network", request, cause, messages);
  return null;
}

export interface ApiErrorDetail {
  label: string;
  value: string;
}

/** 失敗の面に出す情報（UX 契約 messaging.md §10.3）。 */
export interface ApiErrorPresentation {
  /** 何が起きたか（1 文目）。 */
  summary: string;
  /** 次にできること。 */
  nextAction?: string;
  /** 「詳細」に畳む技術的な情報（要求・上限・HTTP ステータス・エラーコード・request ID・元の文）。 */
  details: ApiErrorDetail[];
}

/** 製品の `ApiError` が実装する口。要約（backend の利用者向けの文）と「詳細」を返す。 */
export interface ApiErrorPresentable {
  toApiErrorPresentation(labels?: ApiErrorDetailLabels): ApiErrorPresentation;
}

function isPresentable(error: unknown): error is ApiErrorPresentable {
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as Partial<ApiErrorPresentable>).toApiErrorPresentation === "function"
  );
}

/** 値があるときだけ「詳細」の 1 項目にする。 */
export function apiErrorDetail(label: string, value: string | number | undefined | null): ApiErrorDetail[] {
  return value === undefined || value === null || value === "" ? [] : [{ label, value: String(value) }];
}

/**
 * 入力の欄に結び付く API の問題（problem 契約の `field_errors`）。入力の検証エラー（422）は
 * 技術的な原文（`raw_location` / `raw_message`）と Pydantic の種別（`code`）も持つ（#1065）。
 */
export interface ApiFieldErrorLike {
  pointer?: string;
  code?: string;
  message: string;
  raw_location?: string;
  raw_message?: string;
}

/** 検証エラーの技術的な原文を「詳細」の 1 項目にする（1 件 1 行。原文が無ければ出さない）。 */
function validationErrorDetail(
  fieldErrors: readonly ApiFieldErrorLike[] | undefined,
  label: string,
): ApiErrorDetail[] {
  const lines = (fieldErrors ?? []).flatMap((item) => {
    if (!item.raw_message) return [];
    const location = item.raw_location ? `${item.raw_location}: ` : "";
    const code = item.code ? ` (${item.code})` : "";
    return [`${location}${item.raw_message}${code}`];
  });
  return apiErrorDetail(label, lines.join("\n"));
}

/**
 * 製品の `ApiError`（HTTP の失敗）の要約と「詳細」を作る。`toApiErrorPresentation()` の実装に使う。
 * request ID は本文に重ねず「詳細」に出す。入力の検証エラー（422）の技術的な原文（`fieldErrors` の
 * `raw_location` / `raw_message` / `code`）も「詳細」に出す（#1065）。
 */
export function httpApiErrorPresentation(
  error: {
    status: number;
    messages: readonly string[];
    errorCode?: string | null;
    requestId?: string | null;
    fieldErrors?: readonly ApiFieldErrorLike[];
  },
  labels: ApiErrorDetailLabels = DEFAULT_API_ERROR_DETAIL_LABELS,
): ApiErrorPresentation {
  return {
    summary: error.messages.join("\n"),
    details: [
      ...apiErrorDetail(labels.status, error.status),
      ...apiErrorDetail(labels.errorCode, error.errorCode),
      ...validationErrorDetail(error.fieldErrors, labels.validationErrors),
      ...apiErrorDetail(labels.requestId, error.requestId),
    ],
  };
}

/** JavaScript の組み込みの例外（文が英語で、利用者向けではない）。 */
const BUILTIN_ERROR_NAMES = new Set([
  "TypeError",
  "SyntaxError",
  "RangeError",
  "ReferenceError",
  "EvalError",
  "URIError",
  "AbortError",
  "TimeoutError",
  "NetworkError",
  "NotAllowedError",
  "InvalidStateError",
  "DataError",
]);

/**
 * API の失敗を、利用者の言葉の要約・次の操作・技術的な詳細に分ける。
 *
 * 1. 応答が届かなかった失敗（`ApiTransportError`。製品の `ApiError` が `cause` に包んだものも）は、
 *    日本語の要約と次の操作にし、ブラウザの英語の文は「詳細」の「元のメッセージ」にだけ出す。
 * 2. 製品の `ApiError`（`ApiErrorPresentable`）は、backend の利用者向けの文を要約にし、
 *    HTTP ステータス・エラーコード・request ID を「詳細」に出す。
 * 3. 組み込みの例外（`TypeError` / `SyntaxError` など。文が英語）は画面の既定の文にし、元の文は「詳細」に出す。
 * 4. それ以外の `Error`（製品が日本語の文で投げたもの）は文をそのまま、Error 以外は既定の文にする。
 */
export function presentApiError(
  error: unknown,
  fallback: string,
  labels: ApiErrorDetailLabels = DEFAULT_API_ERROR_DETAIL_LABELS,
): ApiErrorPresentation {
  const transport = transportErrorOf(error);
  if (transport) {
    return {
      summary: transport.summary,
      nextAction: transport.nextAction,
      details: [
        ...apiErrorDetail(labels.request, [transport.method, transport.path].filter(Boolean).join(" ")),
        ...apiErrorDetail(
          labels.limit,
          transport.kind === "timeout" && transport.timeoutMs
            ? labels.limitSeconds(Math.ceil(transport.timeoutMs / 1000))
            : undefined,
        ),
        ...apiErrorDetail(labels.errorType, transport.causeName),
        ...apiErrorDetail(labels.rawMessage, transport.causeMessage),
      ],
    };
  }
  if (isPresentable(error)) {
    const presentation = error.toApiErrorPresentation(labels);
    return { ...presentation, summary: presentation.summary.trim() || fallback };
  }
  if (error instanceof Error && BUILTIN_ERROR_NAMES.has(error.name)) {
    return {
      summary: fallback,
      details: [
        ...apiErrorDetail(labels.errorType, error.name),
        ...apiErrorDetail(labels.rawMessage, error.message),
      ],
    };
  }
  if (error instanceof Error && error.message.trim()) {
    return { summary: error.message, details: [] };
  }
  return { summary: fallback, details: [] };
}

/**
 * 1 つの文で出す所（Toast・`FormStatus`・`ErrorState`・`SaveErrorBanner`）向けの文。
 *
 * - timeout・通信断は要約と次の操作をつなげる（ブラウザの英語の文は含めない）。
 * - 組み込みの例外（`TypeError` / `SyntaxError` など。文が英語）は既定の文にする。
 * - 製品の `ApiError` などは `message` をそのまま使う（「詳細」を出せない所なので、製品が `message` に
 *   付けた request ID などを残す）。「詳細」を出せる所は `ApiErrorBanner` を使う。
 */
export function apiErrorMessage(error: unknown, fallback: string): string {
  const transport = transportErrorOf(error);
  if (transport) return `${transport.summary}${transport.nextAction}`;
  if (error instanceof Error && BUILTIN_ERROR_NAMES.has(error.name)) return fallback;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
