import type { TraceRecordingSettingsData } from "@production-ready/system-settings";
import {
  confirmDatabaseUnavailable,
  isDatabaseReadinessRequest,
  PERSISTENCE_RECOVERY_PATH,
  reportDatabaseOperationalFailure,
  shouldConfirmDatabaseUnavailable,
  type DatabaseOperationalFailure,
} from "./database-load-error.ts";
import { appPath } from "./base-path.ts";
import { t } from "./i18n";
import {
  ApiTransportError,
  httpApiErrorPresentation,
  isAbortError,
  isNetworkFailure,
  isTimeoutError,
  type ApiErrorDetailLabels,
  type ApiErrorPresentable,
  type ApiErrorPresentation,
} from "@production-ready/ui";
// Cookie セッションの CSRF と 401 / 403 の通知は3製品共通（platform の共有パッケージ。#220）。
import {
  csrfHeader,
  notifyAuthResponse,
  type DatabaseStatusData as SharedDatabaseStatusData,
} from "@production-ready/system-settings";

// OCI 認証 API の型は platform の共有パッケージが正本（#100）。
// モデル設定の API 型は3製品共通（platform の共有パッケージ。#103）。
// データベース設定の API 型は3製品共通（platform の共有パッケージ。#108）。
export type {
  AdbInfoData,
  AdbOperationStatus,
  AdbSettingsUpdate,
  DatabaseConnectionSecurity,
  DatabaseConnectionTestResult,
  DatabaseConnectionTestStatus,
  DatabasePasswordRevealData,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
  DatabaseWalletDownloadData,
} from "@production-ready/system-settings";
import type {
  AdbInfoData,
  AdbSettingsUpdate,
  DatabaseConnectionTestResult,
  DatabasePasswordRevealData,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
  DatabaseWalletDownloadData,
  SystemTableColumnRef,
} from "@production-ready/system-settings";
export type {
  EnterpriseAiConfiguredModel,
  EnterpriseAiModelSettings,
  EnterpriseAiVlmInputMode,
  EnterpriseAiModelKind,
  ModelSettingsData,
  ModelSettingsPayload,
  ModelSettingsSecretSource,
  ModelSettingsTestRequest,
  ModelSettingsTestResult,
  ModelSettingsTestStatus,
  ModelSettingsTestTargetType,
} from "@production-ready/system-settings";
import type {
  ModelSettingsData,
  ModelSettingsPayload,
  ModelSettingsTestRequest,
  ModelSettingsTestResult,
} from "@production-ready/system-settings";
export type {
  OciConfigField,
  OciConfigReadData,
  OciConfigReadRequest,
  OciConfigTestResult,
  OciConfigTestStage,
  OciConfigTestStageKey,
  OciConfigTestStageStatus,
  OciConfigTestStatus,
  OciObjectStorageNamespaceData,
  OciObjectStorageNamespaceRequest,
  OciObjectStorageSettingsUpdate,
  OciPrivateKeyUploadData,
  OciSettingsData,
  OciSettingsUpdate,
} from "@production-ready/system-settings";
import type {
  OciConfigReadData,
  OciConfigReadRequest,
  OciConfigTestResult,
  OciObjectStorageNamespaceData,
  OciObjectStorageNamespaceRequest,
  OciObjectStorageSettingsUpdate,
  OciPrivateKeyUploadData,
  OciSettingsData,
  OciSettingsUpdate,
} from "@production-ready/system-settings";

// 回答の注意書き API の型は platform の共有パッケージが正本（#2405）。
import type {
  AnswerNoticeSettingsData,
  AnswerNoticeSettingsUpdate,
  ChatAttachmentSettingsData,
  ChatAttachmentSettingsUpdate,
} from "@production-ready/system-settings";
import type { ChatAttachmentApiItem, ChatAttachmentApiLimits } from "@production-ready/ui";

// アップロード保存先 API の型は platform の共有パッケージが正本（#97）。
export type {
  UploadStorageBackend,
  UploadStorageCheckResult,
  UploadStorageSettingsData,
  UploadStorageSettingsUpdate,
} from "@production-ready/system-settings";
import type {
  UploadStorageCheckResult,
  UploadStorageSettingsData,
  UploadStorageSettingsUpdate,
} from "@production-ready/system-settings";

export interface ApiEnvelope<T> {
  data: T;
  error?: string;
  error_code?: string;
  request_id?: string;
  problem?: ApiProblem;
}

export interface ApiFieldProblem {
  pointer: string;
  code: string;
  message: string;
  /** 入力の検証エラー（422）の、利用者に見せる位置（`cases[0].query`）と技術的な原文（#1065）。 */
  location?: string;
  raw_location?: string;
  raw_message?: string;
}

export interface ApiProblem {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
  request_id: string;
  retryable: boolean;
  field_errors: ApiFieldProblem[];
}

export interface ApiErrorDetails {
  summary?: string;
  cause?: string;
  actions?: string[];
  target_name?: string;
  target_type?: string;
  operation?: string;
  raw_message?: string;
}

export interface ApiRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  headers?: HeadersInit;
}

// 応答が届かなかった API 呼び出し（timeout・通信断）の利用者向けの失敗は3製品共通（platform の共有パッケージ。#900 / #906）。
export {
  ApiTransportError,
  isAbortError,
  isTimeoutError,
  isTransportError,
  type ApiTransportFailureKind,
} from "@production-ready/ui";

/** requestSignal が付けた待ち時間の上限（timeout の文に秒数を出すため）。 */
const signalTimeouts = new WeakMap<AbortSignal, number>();

function requestSignal(options: ApiRequestOptions): AbortSignal | undefined {
  if (!options.timeoutMs || options.timeoutMs <= 0) return options.signal;
  const timeoutSignal = AbortSignal.timeout(options.timeoutMs);
  const signal = options.signal
    ? AbortSignal.any([options.signal, timeoutSignal])
    : timeoutSignal;
  signalTimeouts.set(signal, options.timeoutMs);
  return signal;
}

type TransportRequest = { method: string; path: string; timeoutMs?: number };

/** 応答の本文を読むときの失敗に、要求（method・path・上限）を添えるため。 */
const responseRequests = new WeakMap<Response, TransportRequest>();

function transportRequest(
  method: string,
  path: string,
  signal?: AbortSignal | null,
): TransportRequest {
  return {
    method,
    path: path.split("?")[0] ?? path,
    timeoutMs: signal ? signalTimeouts.get(signal) : undefined,
  };
}

let inFlightPersistenceRecovery: Promise<boolean> | null = null;

function recoverPersistenceForSafeRead(): Promise<boolean> {
  if (inFlightPersistenceRecovery) return inFlightPersistenceRecovery;
  let recovery: Promise<boolean>;
  recovery = (async () => {
    const headers = new Headers({
      Accept: "application/json",
      ...csrfHeader("POST"),
    });
    try {
      const response = await fetch(appPath(PERSISTENCE_RECOVERY_PATH), {
        method: "POST",
        headers,
        credentials: "include",
        signal: AbortSignal.timeout(5_000),
      });
      if (!response.ok) return false;
      const payload = (await response.json()) as {
        data?: { ready?: unknown; writable?: unknown };
      };
      return payload.data?.ready === true && payload.data?.writable === true;
    } catch {
      return false;
    }
  })().finally(() => {
    if (inFlightPersistenceRecovery === recovery)
      inFlightPersistenceRecovery = null;
  });
  inFlightPersistenceRecovery = recovery;
  return recovery;
}

/**
 * 応答の 401 / 403 を共通の認証イベントとして通知する。403 は error_code が経路の権限拒否の
 * ときだけ権限なしの画面へ移し、権限の付与の制限などは画面がその場で表示する（#224）。
 * 本文を読む前に呼ぶ（本文は消費しない）。
 */
function notifyResponseAuthStatus(response: Response) {
  if (response.ok) return;
  void notifyAuthResponse(response);
}

async function recoverAndRetrySafeRequest(
  path: string,
  method: string,
  init: RequestInit,
  failure: DatabaseOperationalFailure | null,
): Promise<Response | null> {
  if (
    failure?.kind !== "persistence" ||
    (method !== "GET" && method !== "HEAD") ||
    !(await recoverPersistenceForSafeRead())
  ) {
    return null;
  }
  try {
    return await fetch(appPath(path), init);
  } catch {
    return null;
  }
}

/** 要求の signal（待ち時間の上限・中止）が先に来たら、promise を待たずにその理由で終える。 */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal | null | undefined): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (cause: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(cause);
      },
    );
  });
}

/**
 * 失敗が DB の未到達によるものかを確かめる。確認は要求の signal（待ち時間の上限・中止）でも打ち切る（#1160）。
 * 確認が返らない間に要求の上限を過ぎたら、要求の timeout として失敗にする（取り直しを止めたままにしない）。
 */
async function confirmDatabaseUnavailableFor(
  method: string,
  path: string,
  signal: AbortSignal | null | undefined,
): Promise<DatabaseOperationalFailure | null> {
  let failure: DatabaseOperationalFailure | null = null;
  try {
    await untilAborted(
      confirmDatabaseUnavailable(fetch, (next) => {
        failure = next;
      }),
      signal,
    );
  } catch (cause) {
    if (isTimeoutError(cause)) {
      throw new ApiTransportError("timeout", transportRequest(method, path, signal), cause);
    }
    throw cause;
  }
  return failure;
}

/**
 * アプリ全体の API 境界。Cookie セッション、CSRF、認証状態イベントを一箇所で扱う。
 * `path` は画面の書く `/api/...` のまま受け取り、送る直前に配信の前置き（`/nl2sql/` など。#1316）を付ける。
 * 判定・エラーの文（readiness の判定・transport の失敗の path）は前置きの無い `path` で行う。
 */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", headers.get("Accept") ?? "application/json");
  for (const [name, value] of Object.entries(csrfHeader(method))) {
    headers.set(name, value);
  }
  const requestInit = {
    ...init,
    headers,
    credentials: "include",
  } satisfies RequestInit;
  let response: Response;
  try {
    response = await fetch(appPath(path), requestInit);
  } catch (cause) {
    if (isAbortError(cause)) throw cause;
    if (isTimeoutError(cause)) {
      throw new ApiTransportError(
        "timeout",
        transportRequest(method, path, init.signal),
        cause,
      );
    }
    if (!isDatabaseReadinessRequest(path)) {
      const failure = await confirmDatabaseUnavailableFor(method, path, init.signal);
      const retried = await recoverAndRetrySafeRequest(
        path,
        method,
        requestInit,
        failure,
      );
      if (retried) {
        notifyResponseAuthStatus(retried);
        return retried;
      }
      if (failure) reportDatabaseOperationalFailure(failure);
    }
    if (isNetworkFailure(cause)) {
      throw new ApiTransportError(
        "network",
        transportRequest(method, path, init.signal),
        cause,
      );
    }
    throw cause;
  }
  responseRequests.set(response, transportRequest(method, path, init.signal));
  notifyResponseAuthStatus(response);
  if (shouldConfirmDatabaseUnavailable(path, response.status)) {
    const failure = await confirmDatabaseUnavailableFor(method, path, init.signal);
    const retried = await recoverAndRetrySafeRequest(
      path,
      method,
      requestInit,
      failure,
    );
    if (retried) {
      notifyResponseAuthStatus(retried);
      return retried;
    }
    if (failure) reportDatabaseOperationalFailure(failure);
  }
  return response;
}

/** 本文の読み取り中に待ち時間の上限を超えた・接続が切れたときも、利用者向けの失敗にする（#900）。 */
async function readJsonBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch (cause) {
    const request = responseRequests.get(response) ?? { method: "", path: "" };
    if (isTimeoutError(cause)) throw new ApiTransportError("timeout", request, cause);
    if (isNetworkFailure(cause)) throw new ApiTransportError("network", request, cause);
    throw cause;
  }
}

async function parseJson<T>(response: Response): Promise<T> {
  const payload = (await readJsonBody(response)) as ApiEnvelope<T> & {
    error_messages?: unknown;
    detail?: unknown;
    error_details?: ApiErrorDetails;
  };
  if (!response.ok) {
    // 共通例外ハンドラは ApiResponse { error_messages: [...] } 形式で返す
    const errorMessages = payload.error_messages;
    const detailMessages =
      typeof payload.detail === "object" &&
      payload.detail !== null &&
      "errors" in payload.detail
        ? (payload.detail as { errors?: unknown }).errors
        : undefined;
    const messages = Array.isArray(detailMessages)
      ? detailMessages.map(String)
      : Array.isArray(errorMessages) && errorMessages.length > 0
        ? errorMessages.map(String)
        : [
            payload.error ||
              (payload.detail
                ? String(payload.detail)
                : "API リクエストに失敗しました"),
          ];
    const problem = decodeApiProblem(payload.problem);
    const requestId =
      problem?.request_id ||
      response.headers.get("X-Request-ID") ||
      payload.request_id;
    throw new ApiError(
      response.status,
      messages,
      payload.error_code,
      payload.error_details,
      problem,
      requestId,
    );
  }
  return payload.data;
}

export interface ApiResponseMetadata<T> {
  data: T;
  etag: string;
}

export async function apiGet<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await apiFetch(path, { signal: requestSignal(options) });
  return parseJson<T>(response);
}

export async function apiGetWithMetadata<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<ApiResponseMetadata<T>> {
  const response = await apiFetch(path, { signal: requestSignal(options) });
  const data = await parseJson<T>(response);
  return {
    data,
    etag: response.headers.get("ETag")?.replaceAll('"', "") ?? "",
  };
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
  options: ApiRequestOptions = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  headers.set("Content-Type", "application/json");
  const response = await apiFetch(path, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: requestSignal(options),
  });
  return parseJson<T>(response);
}

export async function apiPostForm<T>(
  path: string,
  body: FormData,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await apiFetch(path, {
    method: "POST",
    body,
    signal: requestSignal(options),
  });
  return parseJson<T>(response);
}

export async function apiPatch<T>(
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await apiFetch(path, {
    method: "PATCH",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: requestSignal(options),
  });
  return parseJson<T>(response);
}

export async function apiPut<T>(
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await apiFetch(path, {
    method: "PUT",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: requestSignal(options),
  });
  return parseJson<T>(response);
}

export async function apiDelete<T>(
  path: string,
  headers: Record<string, string> = {},
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await apiFetch(path, {
    method: "DELETE",
    headers: { Accept: "application/json", ...headers },
    signal: requestSignal(options),
  });
  return parseJson<T>(response);
}

export type JsonValue =
  string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

/** DB の状態 API（`GET /api/ready/database`）の応答。契約は3製品共通（#325）。 */
export type DatabaseStatusData = SharedDatabaseStatusData;

export interface PersistenceStatusData {
  mode: "memory" | "oracle";
  ready: boolean;
  durable: boolean;
  writable: boolean;
  snapshot_loaded: boolean;
  reason_code: string | null;
  checked_at: string;
  state_backend?: "incremental" | "snapshot";
  circuit_state?: "closed" | "open" | "half_open";
  retry_after_seconds?: number;
}

export interface SettingsApiResponse<T> {
  data: T | null;
  error_messages: string[];
  warning_messages: string[];
  error_code?: string;
  problem?: ApiProblem;
  request_id?: string;
}

export type SelectAiCredentialRegion = "ap-osaka-1" | "us-chicago-1";
export type SelectAiCredentialOperation =
  "created" | "recreated" | "already_exists";

export interface SelectAiCredentialData {
  credential_name: "NL2SQL_OCI_CRED";
  schema_name: string;
  exists: boolean;
  region: SelectAiCredentialRegion;
  oci_auth_ready: boolean;
  missing_fields: string[];
  operation: SelectAiCredentialOperation | null;
}

export interface SelectAiCredentialCreateRequest {
  region: SelectAiCredentialRegion;
  confirmation: string;
  recreate: boolean;
}

export type SystemTableSchemaStatus =
  "missing" | "partial" | "outdated" | "ready";
export type SystemTableOperationStatus = "idle" | "running" | "failed";
export type SystemTableOperationResult =
  "no_op" | "initialized" | "migrated" | "recreated";

export interface SystemTableMissingObject {
  name: string;
  object_type: "TABLE" | "INDEX" | "SEQUENCE" | "PACKAGE" | "PACKAGE BODY";
}

export interface SystemTableMetadata {
  name: string;
  exists: boolean;
  estimated_rows: number | null;
  created_at: string | null;
  last_analyzed_at: string | null;
}

export type SystemObjectType =
  "TABLE" | "INDEX" | "SEQUENCE" | "PACKAGE" | "PACKAGE BODY";

export interface SystemObjectMetadata {
  name: string;
  /** 接続ユーザーの schema と所有者付き名前。旧 backend では未返却。 */
  owner?: string;
  qualified_name?: string;
  object_type: SystemObjectType;
  exists: boolean;
  estimated_rows: number | null;
  created_at: string | null;
  last_analyzed_at: string | null;
}

export interface SystemTableOperationState {
  status: SystemTableOperationStatus;
  operation_kind: "initialize" | "recreate" | null;
  lease_expires_at: string | null;
  last_error_code: string | null;
  schema_epoch: number;
  updated_at: string | null;
}

export interface SystemTablesStatusData {
  status: SystemTableSchemaStatus;
  schema_head: number;
  applied_versions: number[];
  pending_versions: number[];
  expected_object_count: number;
  existing_object_count: number;
  expected_table_count: number;
  existing_table_count: number;
  missing_objects: SystemTableMissingObject[];
  /** 既存の表に無い正本の列（台帳が適用済みでも欠けた列。#2458）。 */
  missing_columns?: SystemTableColumnRef[];
  tables: SystemTableMetadata[];
  objects?: SystemObjectMetadata[];
  operation_state: SystemTableOperationState;
}

export interface SystemTablesInitializeRequest {
  recreate: boolean;
  confirmation?: string;
}

export interface SystemTablesOperationData extends SystemTablesStatusData {
  operation: SystemTableOperationResult;
  dropped_object_count: number;
  created_object_count: number;
}

export type DatabaseWalletDownloadStatus = "downloaded" | "already_configured";

export interface SchemaOwnersData {
  current_owner: string;
  owners: Array<{
    owner: string;
    is_current: boolean;
    table_count: number;
    view_count: number;
  }>;
  excluded_oracle_maintained_count: number;
}

/** API 由来のエラー。`messages` は日本語のユーザー向け文言。 */
export class ApiError extends Error implements ApiErrorPresentable {
  readonly status: number;
  /** 表示用メッセージ。認証・rate limit・5xx・migration 未適用では request ID を付与する */
  readonly messages: string[];
  /** リクエスト ID を付与していない元メッセージ(利用者向けに ID を出したくない画面で使う) */
  readonly baseMessages: string[];
  readonly errorCode?: string;
  readonly details?: ApiErrorDetails;
  readonly problem?: ApiProblem;
  readonly fieldErrors: ApiFieldProblem[];
  readonly requestId?: string;
  readonly retryable: boolean;

  constructor(
    status: number,
    messages: string[],
    errorCode?: string,
    details?: ApiErrorDetails,
    problem?: ApiProblem,
    requestId?: string,
  ) {
    const baseMessages =
      messages.length > 0 ? messages : [`APIエラー (${status})`];
    const displayMessages = errorMessagesWithRequestId(
      status,
      baseMessages,
      problem?.request_id || requestId,
      problem?.code ?? errorCode,
    );
    super(displayMessages[0] ?? `APIエラー (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.messages = displayMessages;
    this.baseMessages = baseMessages;
    this.errorCode = problem?.code ?? errorCode;
    this.details = details;
    this.problem = problem;
    this.fieldErrors = problem?.field_errors ?? [];
    this.requestId = problem?.request_id || requestId;
    this.retryable = problem?.retryable ?? false;
  }

  /** 失敗の面の要約（request ID を重ねない元の文）と「詳細」（共通の `ApiErrorBanner`。#900 / #906）。 */
  toApiErrorPresentation(labels?: ApiErrorDetailLabels): ApiErrorPresentation {
    return httpApiErrorPresentation(
      {
        status: this.status,
        messages: this.baseMessages,
        errorCode: this.errorCode,
        requestId: this.requestId,
        fieldErrors: this.fieldErrors,
      },
      labels,
    );
  }
}

/** 新旧 envelope の共存期間中、妥当な problem だけを採用する。 */
export function decodeApiProblem(value: unknown): ApiProblem | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Partial<ApiProblem>;
  if (
    typeof candidate.type !== "string" ||
    typeof candidate.title !== "string" ||
    typeof candidate.status !== "number" ||
    typeof candidate.detail !== "string" ||
    typeof candidate.code !== "string"
  ) {
    return undefined;
  }
  const fieldErrors = Array.isArray(candidate.field_errors)
    ? candidate.field_errors.filter(isApiFieldProblem)
    : [];
  return {
    type: candidate.type,
    title: candidate.title,
    status: candidate.status,
    detail: candidate.detail,
    code: candidate.code,
    request_id:
      typeof candidate.request_id === "string" ? candidate.request_id : "",
    retryable: candidate.retryable === true,
    field_errors: fieldErrors,
  };
}

function isApiFieldProblem(value: unknown): value is ApiFieldProblem {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ApiFieldProblem>;
  return (
    typeof candidate.pointer === "string" &&
    candidate.pointer.startsWith("/") &&
    typeof candidate.code === "string" &&
    typeof candidate.message === "string"
  );
}

function errorMessagesWithRequestId(
  status: number,
  messages: string[],
  requestId?: string | null,
  errorCode?: string,
): string[] {
  if (
    !requestId ||
    !(
      status === 401 ||
      status === 403 ||
      status === 429 ||
      status >= 500 ||
      errorCode === "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    )
  ) {
    return messages;
  }
  const [first, ...rest] = messages;
  return [
    t("common.errorWithRequestId", {
      message: first || t("security.common.saveError"),
      requestId,
    }),
    ...rest,
  ];
}

async function parseSettingsEnvelope<T>(
  response: Response,
): Promise<SettingsApiResponse<T>> {
  try {
    const payload = (await response.json()) as Partial<SettingsApiResponse<T>> &
      Partial<ApiEnvelope<T>> & { detail?: unknown };
    const errorMessages =
      payload.error_messages ??
      (payload.error
        ? [payload.error]
        : payload.detail
          ? [String(payload.detail)]
          : []);
    return {
      data: payload.data ?? null,
      error_messages: errorMessages,
      warning_messages: payload.warning_messages ?? [],
      error_code: payload.error_code,
      problem: decodeApiProblem(payload.problem),
      request_id: payload.request_id,
    };
  } catch {
    return { data: null, error_messages: [], warning_messages: [] };
  }
}

async function settingsRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await apiFetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });
  const envelope = await parseSettingsEnvelope<T>(response);
  if (!response.ok) {
    const requestId =
      envelope.problem?.request_id ||
      response.headers.get("X-Request-ID") ||
      envelope.request_id;
    const messages =
      envelope.error_messages.length > 0
        ? envelope.error_messages
        : [`APIエラー (${response.status})`];
    throw new ApiError(
      response.status,
      messages,
      envelope.error_code,
      undefined,
      envelope.problem,
      requestId,
    );
  }
  return envelope.data as T;
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const api = {
  getDatabaseStatus: (options: ApiRequestOptions = {}) =>
    settingsRequest<DatabaseStatusData>("/api/ready/database", {
      signal: options.signal,
    }),
  getPersistenceStatus: (options: ApiRequestOptions = {}) =>
    settingsRequest<PersistenceStatusData>("/api/nl2sql/persistence", {
      signal: options.signal,
    }),
  recoverPersistence: () =>
    settingsRequest<PersistenceStatusData>("/api/nl2sql/persistence/recover", {
      method: "POST",
    }),
  getModelSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<ModelSettingsData>("/api/settings/model", {
      signal: options.signal,
    }),
  updateModelSettings: (body: ModelSettingsPayload) =>
    settingsRequest<ModelSettingsData>("/api/settings/model", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  testModelSettings: (body: ModelSettingsTestRequest) =>
    settingsRequest<ModelSettingsTestResult>(
      "/api/settings/model/test",
      jsonBody(body),
    ),

  getDatabaseSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<DatabaseSettingsData>("/api/settings/database", {
      signal: options.signal,
    }),
  getSelectAiCredential: (options: ApiRequestOptions = {}) =>
    settingsRequest<SelectAiCredentialData>(
      "/api/settings/database/select-ai-credential",
      { signal: options.signal },
    ),
  createSelectAiCredential: (body: SelectAiCredentialCreateRequest) =>
    settingsRequest<SelectAiCredentialData>(
      "/api/settings/database/select-ai-credential",
      jsonBody(body),
    ),
  getSystemTablesStatus: (options: ApiRequestOptions = {}) =>
    settingsRequest<SystemTablesStatusData>(
      "/api/settings/database/system-tables",
      {
        signal: options.signal,
      },
    ),
  initializeSystemTables: (body: SystemTablesInitializeRequest) =>
    settingsRequest<SystemTablesOperationData>(
      "/api/settings/database/system-tables/initialize",
      jsonBody(body),
    ),
  getSchemaOwners: (options: ApiRequestOptions = {}) =>
    settingsRequest<SchemaOwnersData>("/api/schema/owners", {
      signal: options.signal,
    }),
  updateDatabaseSettings: (body: DatabaseSettingsUpdate) =>
    settingsRequest<DatabaseSettingsData>("/api/settings/database", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  uploadDatabaseWallet: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return settingsRequest<DatabaseSettingsData>(
      "/api/settings/database/wallet",
      {
        method: "POST",
        body: form,
      },
    );
  },
  downloadDatabaseWallet: () =>
    settingsRequest<DatabaseWalletDownloadData>(
      "/api/settings/database/wallet/download",
      { method: "POST" },
    ),
  revealDatabasePassword: () =>
    settingsRequest<DatabasePasswordRevealData>(
      "/api/settings/database/password/reveal",
      { method: "POST" },
    ),
  testDatabaseSettings: (body: DatabaseSettingsUpdate) =>
    settingsRequest<DatabaseConnectionTestResult>(
      "/api/settings/database/test",
      jsonBody(body),
    ),

  getAdbInfo: (options: ApiRequestOptions = {}) =>
    settingsRequest<AdbInfoData>("/api/settings/database/adb", {
      signal: options.signal,
    }),
  updateAdbSettings: (body: AdbSettingsUpdate) =>
    settingsRequest<AdbInfoData>(
      "/api/settings/database/adb/settings",
      jsonBody(body),
    ),
  startAdb: () =>
    settingsRequest<AdbInfoData>("/api/settings/database/adb/start", {
      method: "POST",
    }),
  stopAdb: () =>
    settingsRequest<AdbInfoData>("/api/settings/database/adb/stop", {
      method: "POST",
    }),

  // 回答の注意書き（設定は製品ごと。#2405・#2449）
  // チャットの添付（#2517）
  uploadChatAttachment: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return settingsRequest<ChatAttachmentApiItem>("/api/chat-attachments", { method: "POST", body });
  },
  listChatAttachments: () =>
    settingsRequest<{ items: ChatAttachmentApiItem[]; limits: ChatAttachmentApiLimits }>("/api/chat-attachments"),
  deleteChatAttachment: (attachmentId: string) =>
    settingsRequest<{ deleted: boolean }>(`/api/chat-attachments/${encodeURIComponent(attachmentId)}`, {
      method: "DELETE",
    }),
  // 運用設定: チャットの添付（設定は製品ごと。#2517）
  getTraceRecordingSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<TraceRecordingSettingsData>("/api/settings/trace-recording", { signal: options.signal }),
  updateTraceRecordingSettings: (body: { enabled: boolean }) =>
    settingsRequest<TraceRecordingSettingsData>("/api/settings/trace-recording", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getChatAttachmentSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<ChatAttachmentSettingsData>("/api/settings/chat-attachments", { signal: options.signal }),
  updateChatAttachmentSettings: (body: ChatAttachmentSettingsUpdate) =>
    settingsRequest<ChatAttachmentSettingsData>("/api/settings/chat-attachments", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getAnswerNoticeSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<AnswerNoticeSettingsData>("/api/settings/answer-notice", { signal: options.signal }),
  updateAnswerNoticeSettings: (body: AnswerNoticeSettingsUpdate) =>
    settingsRequest<AnswerNoticeSettingsData>("/api/settings/answer-notice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  getUploadStorageSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<UploadStorageSettingsData>("/api/settings/upload-storage", {
      signal: options.signal,
    }),
  updateUploadStorageSettings: (body: UploadStorageSettingsUpdate) =>
    settingsRequest<UploadStorageSettingsData>("/api/settings/upload-storage", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // 入力中の値で OCI Object Storage の bucket と権限を確かめる（保存しない。#2101）。
  testUploadStorageSettings: (body: UploadStorageSettingsUpdate) =>
    settingsRequest<UploadStorageCheckResult>("/api/settings/upload-storage/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  getOciSettings: (options: ApiRequestOptions = {}) =>
    settingsRequest<OciSettingsData>("/api/settings/oci", {
      signal: options.signal,
    }),
  updateOciSettings: (body: OciSettingsUpdate) =>
    settingsRequest<OciSettingsData>("/api/settings/oci", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateOciObjectStorageSettings: (body: OciObjectStorageSettingsUpdate) =>
    settingsRequest<UploadStorageSettingsData>(
      "/api/settings/oci/object-storage",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  readOciConfig: (body: OciConfigReadRequest) =>
    settingsRequest<OciConfigReadData>(
      "/api/settings/oci/config/read",
      jsonBody(body),
    ),
  testOciConfig: () =>
    settingsRequest<OciConfigTestResult>("/api/settings/oci/config/test", {
      method: "POST",
    }),
  readOciObjectStorageNamespace: (body: OciObjectStorageNamespaceRequest) =>
    settingsRequest<OciObjectStorageNamespaceData>(
      "/api/settings/oci/object-storage/namespace",
      jsonBody(body),
    ),
  uploadOciPrivateKey: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return settingsRequest<OciPrivateKeyUploadData>(
      "/api/settings/oci/key-file",
      {
        method: "POST",
        body: form,
      },
    );
  },
};
