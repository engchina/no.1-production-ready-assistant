/** モデル設定 API の型（backend の pr_system_settings.model と同じ形。#103）。 */
export type EnterpriseAiVlmInputMode = "auto" | "files_api" | "inline_image";
export type ModelSettingsSecretSource =
  "environment" | "legacy_json" | "missing";
export type ModelSettingsTestStatus = "success" | "failed";
export type ModelSettingsTestTargetType =
  "enterprise_text" | "enterprise_vision" | "embedding" | "rerank";

/**
 * OCI Enterprise AI の接続の ID（#533）。接続ごとに backend の Settings の属性・env 名が決まっている。
 * 画面の名前は「プライマリ接続」「セカンダリ接続」「ターシャリ接続」（#542 / #786）。
 * ターシャリ接続は OpenAI / OpenAI 互換 API 向けで、Project OCID は任意。
 */
export type EnterpriseAiConnectionId = "primary" | "secondary" | "tertiary";
/**
 * 接続の並び（プライマリ接続が既定）。タブの並びと、保存する接続の並びもこの順。
 * backend の ENTERPRISE_AI_CONNECTION_IDS と同じ。
 */
export const ENTERPRISE_AI_CONNECTION_IDS: readonly EnterpriseAiConnectionId[] = [
  "primary",
  "secondary",
  "tertiary",
];
/** 接続の上限（backend の MAX_ENTERPRISE_AI_CONNECTIONS と同じ）。 */
export const MAX_ENTERPRISE_AI_CONNECTIONS = ENTERPRISE_AI_CONNECTION_IDS.length;

/**
 * 推論（思考）の量（#1425。Responses API の `reasoning.effort`）。選択肢は 3 モデルとも使える 4 つで、
 * 既定は low。none は「送らない」（`reasoning` を送らない）。backend の ENTERPRISE_AI_REASONING_EFFORTS と同じ。
 */
export const ENTERPRISE_AI_REASONING_EFFORTS = [
  "none",
  "low",
  "medium",
  "high",
] as const;
export type EnterpriseAiReasoningEffort =
  (typeof ENTERPRISE_AI_REASONING_EFFORTS)[number];
/** 推論の量の既定（新しく追加するモデル・値の無い保存済みのモデル。backend と同じ）。 */
export const DEFAULT_ENTERPRISE_AI_REASONING_EFFORT: EnterpriseAiReasoningEffort =
  "low";

/**
 * モデルの入力の上限（tokens。#1437）。登録モデルごとに持ち、値の無いモデルは既定の 128000。範囲は backend の
 * ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN / ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX と同じ（範囲外は保存で 422）。
 */
export const DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS = 128_000;
export const ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN = 4_096;
export const ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX = 2_000_000;

/**
 * モデルの出力の上限（tokens。#1719）。登録モデルごとに持ち、値の無いモデルは既定の 32768（入力の上限がそれより
 * 小さければ入力の上限）。範囲は backend の ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN / _MAX と同じで、入力の上限以下
 * （範囲外・入力の上限を超える値は保存で 422）。推論のあるモデルは推論の token も出力の上限に数える。
 */
export const DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS = 32_768;
export const ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN = 1;
export const ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX = 2_000_000;

/**
 * 登録モデルの種類（#45。backend の EnterpriseAiModelKind と同じ）。text は LLM（画像対応は
 * `vision_enabled`）、embedding は `POST {endpoint}/embeddings`、rerank は `POST {endpoint}/reranks`。
 * 埋め込み / 再ランクも LLM と同じ一覧に、接続を選んで登録する。
 */
export const ENTERPRISE_AI_MODEL_KINDS = ["text", "embedding", "rerank"] as const;
export type EnterpriseAiModelKind = (typeof ENTERPRISE_AI_MODEL_KINDS)[number];

export interface EnterpriseAiConfiguredModel {
  model_id: string;
  display_name: string;
  /** 画像入力に対応（種類 text だけの属性。ほかの種類は backend が false にする）。 */
  vision_enabled: boolean;
  /** モデルの種類（#45）。未指定は text。 */
  kind?: EnterpriseAiModelKind;
  /** 呼び出しに使う接続（#533）。未指定はプライマリ接続。存在しない接続を指すと保存時に欄のエラー。 */
  connection_id?: string;
  /** 推論（思考）の量（#1425）。未指定は既定（medium）、none は送らない。 */
  reasoning_effort?: EnterpriseAiReasoningEffort;
  /** 入力の上限（tokens。#1437）。未指定は既定（128000）。空欄は NaN で持ち、保存の前に欄のエラーで止める。 */
  max_input_tokens?: number;
  /**
   * 出力の上限（tokens。#1719）。入力の上限（prompt と応答の合計）の中の応答の分。未指定は既定（32768）。
   * 空欄は NaN で持ち、保存の前に欄のエラーで止める。
   */
  max_output_tokens?: number;
}

/**
 * OCI Enterprise AI の接続 1 件（#533）。`api_key` は書き込み専用で、応答では空。
 * 表示名は持たない（#542。画面は ID から「プライマリ接続」などと表示する）。
 */
export interface EnterpriseAiConnectionSettings {
  connection_id: EnterpriseAiConnectionId;
  endpoint: string;
  project_ocid: string;
  api_key: string;
  has_api_key: boolean;
  clear_api_key: boolean;
}

export interface EnterpriseAiModelSettings {
  /**
   * 1 件目（プライマリ接続）が既定。セカンダリ接続・ターシャリ接続は任意（#533 / #786）。
   * 並びは ENTERPRISE_AI_CONNECTION_IDS の順（セカンダリ接続なしでターシャリ接続だけも可）。
   */
  connections: EnterpriseAiConnectionSettings[];
  models: EnterpriseAiConfiguredModel[];
  /** 画像を扱わない処理の既定。モデルを 1 つ以上登録したら必須（#499 / #566）。 */
  default_text_model_id: string;
  /** 画像を読む処理の既定。モデルを登録したら必須で、Vision 対応のモデルに限る（#499）。 */
  default_vision_model_id: string;
  /**
   * 品質評価で回答を判定するモデル（#3574）。空（既定）は既定のテキストモデル、
   * JUDGE_SAME_AS_ANSWER（"same_as_answer"）は回答を作ったモデル、それ以外は登録モデルの ID。
   */
  default_judge_model_id?: string;
  /** 埋め込みに使うモデル（#45。種類 embedding の登録モデル。その種類を登録したら必須）。 */
  default_embedding_model_id?: string;
  /** 再ランクに使うモデル（#45。種類 rerank の登録モデル。その種類を登録したら必須）。 */
  default_rerank_model_id?: string;
  api_path: string;
  vlm_input_mode: EnterpriseAiVlmInputMode;
  text_payload_template: string;
  vision_payload_template: string;
  text_response_path: string;
  vision_response_path: string;
  timeout_seconds: number;
  max_retries: number;
}

/**
 * モデル設定の読み書きの payload。#45 で埋め込み / 再ランクの独立した節（`generative_ai`）を外し、
 * 登録モデル（種類 embedding / rerank）と既定のモデルで表す。
 */
export interface ModelSettingsPayload {
  enterprise_ai: EnterpriseAiModelSettings;
}

export interface ModelSettingsData {
  settings: ModelSettingsPayload;
  model_settings_file: string;
  source: "runtime";
  secret_source: ModelSettingsSecretSource;
  legacy_secret_detected: boolean;
  /**
   * 保存済みのモデル設定の版（#1037）。保存の `base_revision` に返し、画面を開いた後にほかの画面
   * （別の製品を含む）で保存されていれば、API は 409 にして古い値で上書きしない。
   * 古い backend は返さない（そのときは送らない）。
   */
  revision?: string;
  /**
   * 埋め込みの次元（#45。読み取り専用の表示用）。backend の設定 `oci_genai_embedding_dim`（Oracle の
   * VECTOR 列に合わせた値）で、埋め込みの呼び出しに `dimensions` として送る。古い backend は返さない。
   */
  embedding_dim?: number | null;
}

/** 保存（PATCH）の body。`base_revision` は読み込んだ時点の `ModelSettingsData.revision`（#1037）。 */
export type ModelSettingsUpdatePayload = ModelSettingsPayload & {
  base_revision?: string;
};

export interface ModelSettingsTestRequest {
  settings: ModelSettingsPayload;
  target_type: ModelSettingsTestTargetType;
  model_id: string;
  vision_enabled: boolean;
}

export interface ModelSettingsTestResult {
  status: ModelSettingsTestStatus;
  target_type: ModelSettingsTestTargetType;
  model_id: string;
  message: string;
  troubleshooting: string[];
  raw_error: string | null;
  error_type: string | null;
  elapsed_ms: number;
  checked_at: string;
  details: Record<string, string | number | boolean | null>;
}

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface ModelSettingsApi {
  getModelSettings: (options?: {
    signal?: AbortSignal;
  }) => Promise<ModelSettingsData>;
  updateModelSettings: (
    payload: ModelSettingsUpdatePayload,
  ) => Promise<ModelSettingsData>;
  testModelSettings: (
    request: ModelSettingsTestRequest,
  ) => Promise<ModelSettingsTestResult>;
}

/** 3製品で共通の React Query key（RAG の文書画面も同じ key でモデル設定を読む）。 */
export const MODEL_SETTINGS_QUERY_KEY = ["settings", "model"] as const;
