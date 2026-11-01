import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Disclosure,
  EmptyState,
  ErrorState,
  FormActionBar,
  FormGrid,
  type FieldWidth,
  FormSkeleton,
  FormStatus,
  PageBody,
  ProcessingIndicator,
  RequiredBadge,
  SecretField,
  SelectField,
  type SelectFieldOption,
  Switch,
  TabPanel,
  Tabs,
  TextField,
  TimedLoadingState,
  cn,
  toast,
  useConfirm,
} from "@production-ready/ui";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import {
  Cpu,
  ListChecks,
  Plus,
  RefreshCw,
  Save,
  TestTube2,
  Trash2,
} from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import {
  useSettingsDraftGuard,
  type DraftGuardMessages,
} from "../guards/useSettingsDraftGuard";
import { useFocusAfterCommit } from "../users-roles/shared";
import {
  SettingsTestResultPanel,
  toSettingsTestResultDetails,
} from "../shared/SettingsTestResultPanel";
import {
  PRIMARY_CONNECTION_ID,
  TERTIARY_CONNECTION_ID,
  addConnection,
  connectionFieldId,
  connectionLabel,
  connectionOptions,
  firstConnectionError,
  hasConnection,
  modelConnectionFieldId,
  modelConnectionId,
  modelsUsingConnection,
  moveModelsToAvailableConnections,
  normalizeModelSettings,
  removeConnection,
  unsavedConnectionIds,
  validateConnections,
  validateModelConnections,
  type ConnectionErrors,
  type ConnectionField,
  type ModelConnectionErrors,
} from "./connections";
import {
  DEFAULT_MODEL_FIELD_IDS,
  DEFAULT_MODEL_FIELD_ORDER,
  followModelChange,
  judgeModelOptions,
  kindModelOptions,
  modelKind,
  textModelOptions,
  validateDefaultModels,
  validateModelIds,
  visionModelOptions,
  type DefaultModelErrors,
  type DefaultModelField,
  type ModelIdErrors,
} from "./defaultModels";
import { t } from "./messages";
import {
  DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
  DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
  DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
  ENTERPRISE_AI_CONNECTION_IDS,
  ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX,
  ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN,
  ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX,
  ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN,
  ENTERPRISE_AI_MODEL_KINDS,
  ENTERPRISE_AI_REASONING_EFFORTS,
  MODEL_SETTINGS_QUERY_KEY,
  type EnterpriseAiConfiguredModel,
  type EnterpriseAiConnectionId,
  type EnterpriseAiConnectionSettings,
  type EnterpriseAiModelKind,
  type EnterpriseAiModelSettings,
  type EnterpriseAiReasoningEffort,
  type ModelSettingsApi,
  type ModelSettingsData,
  type ModelSettingsPayload,
  type ModelSettingsTestRequest,
  type ModelSettingsTestResult,
  type ModelSettingsTestTargetType,
  type ModelSettingsUpdatePayload,
} from "./types";

type ModelTestKey = `enterprise:${number}`;
type ModelSaveSection = "enterprise_connection" | "enterprise_models";

export interface ModelSettingsPageProps {
  /** 製品の API 関数（GET / PATCH /api/settings/model、POST /api/settings/model/test）。 */
  api: ModelSettingsApi;
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
  /** 入力欄の placeholder（登録モデルの表示名の例など、製品ごとの値）。 */
  placeholders?: { displayName?: string };
  /** 離脱確認ダイアログの文言の上書き。 */
  draftGuardMessages?: Partial<DraftGuardMessages>;
  /**
   * 保存が成功したときに捨てる製品の cache（既定のモデル・登録モデルから作る候補など。#1416）。
   * 例: RAG のチャット・RAG 検索の「回答するモデル」。`staleTime` を付けた query は、捨てないと
   * 画面を戻ったときに保存前のモデルを出す（`staleTime` の無い query は開き直すと取り直すので要らない）。
   */
  invalidateQueryKeys?: readonly QueryKey[];
}

/**
 * モデル設定（3製品共通。NL2SQL の画面を基準に移設。#103）。PageHeader は製品の route が描く。
 *
 * - 2節（Enterprise AI 接続 / 登録モデル）をそれぞれ保存する。埋め込み / 再ランクのモデルも登録モデルに
 *   種類（テキスト / 埋め込み / 再ランク）と接続を選んで登録し、既定のモデルで選ぶ（#45。独立したカードは無い）。
 *   画面にない項目（API path・VLM 入力方式・timeout など）は保存済みの値をそのまま送る
 * - Enterprise AI の接続はプライマリ接続とセカンダリ接続（#533）。カードの中の Tabs で切り替えて入力し、
 *   登録モデルの行で使う接続を選ぶ（#542）。セカンダリ接続は「設定」したときだけあり、削除は、
 *   使っているモデルがあればプライマリ接続に移すか確認する。エラー・未保存の入力があるタブは Tabs が示す
 * - 登録モデルの節の下で、既定のテキストモデルと既定の Vision モデル（どちらも必須）を選ぶ（#499 / #566）。
 *   選んだモデルの削除・Vision 対応のオフ・保存の操作で、保存前にフィールドのエラーを出す
 * - 保存中・テスト中は入力を止め、未保存のまま離れようとすると確認する
 */
export function ModelSettingsPage({
  api,
  errorMessage,
  placeholders,
  draftGuardMessages,
  invalidateQueryKeys = [],
}: ModelSettingsPageProps) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: MODEL_SETTINGS_QUERY_KEY,
    queryFn: ({ signal }) => api.getModelSettings({ signal }),
  });
  const updateMutation = useMutation({
    mutationFn: (payload: ModelSettingsUpdatePayload) =>
      api.updateModelSettings(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: MODEL_SETTINGS_QUERY_KEY,
      });
      for (const queryKey of invalidateQueryKeys) {
        void queryClient.invalidateQueries({ queryKey });
      }
    },
  });
  const testMutation = useMutation({
    mutationFn: (request: ModelSettingsTestRequest) =>
      api.testModelSettings(request),
  });
  const confirm = useConfirm();
  const displayNamePlaceholder =
    placeholders?.displayName ?? t("settings.model.placeholder.displayName");

  const [draft, setDraft] = useState<ModelSettingsPayload | null>(null);
  const [baselineData, setBaselineData] = useState<ModelSettingsData | null>(
    null,
  );
  const [checkData, setCheckData] = useState<ModelSettingsData | null>(null);
  const [saveErrors, setSaveErrors] = useState<
    Partial<Record<ModelSaveSection, string>>
  >({});
  const [activeSaveSection, setActiveSaveSection] =
    useState<ModelSaveSection | null>(null);
  const [apiKeyVisible, setApiKeyVisible] = useState<
    Partial<Record<EnterpriseAiConnectionId, boolean>>
  >({});
  // 表示中の接続のタブ（#542）。保存で止めたときは、最初のエラーがあるタブへ切り替える。
  const [connectionTab, setConnectionTab] =
    useState<EnterpriseAiConnectionId>(PRIMARY_CONNECTION_ID);
  // 接続の入力のエラー（セカンダリ接続・ターシャリ接続の必須の欄の未入力）は、保存の操作の後から出す。
  const [showConnectionErrors, setShowConnectionErrors] = useState(false);
  // 登録モデルの「接続」のエラーは、接続を選んだ・削除した・保存の操作の後から出す（#533）。
  const [showModelConnectionErrors, setShowModelConnectionErrors] =
    useState(false);
  // 登録モデルのモデル ID の重複のエラー（#1035）も、入力中ではなく保存の操作の後から出す。
  const [showModelIdErrors, setShowModelIdErrors] = useState(false);
  // 既定のモデルのエラーは、関係する操作（選択・Vision 対応の切替・削除・保存）の後から出す。
  // モデル ID の入力中（キー入力ごと）には出さない（messaging.md §3.2）。
  const [showDefaultErrors, setShowDefaultErrors] = useState(false);
  // 登録モデルの入力の上限（#1437）の不正な値のエラーは、保存の操作の後から出す（キー入力ごと・
  // 欄を離れたときには出さない。欄を離れたときに出すと、保存のボタンを押した瞬間にエラーの行が入って
  // ボタンが動き、押せなくなる）。
  const [showMaxInputTokensErrors, setShowMaxInputTokensErrors] =
    useState(false);
  const [testingKey, setTestingKey] = useState<ModelTestKey | null>(null);
  // 保存が競合（409。画面を開いた後にほかの画面で保存された）した節（#1037）。その節の操作の行に
  // 「最新の設定を読み込む」を出す。読み込むまで残す（入力を変えても競合は解けない）。
  const [conflictSection, setConflictSection] =
    useState<ModelSaveSection | null>(null);
  const [reloading, setReloading] = useState(false);
  const [testResults, setTestResults] = useState<
    Partial<Record<ModelTestKey, ModelSettingsTestResult>>
  >({});

  useEffect(() => {
    if (!query.data || draft) return;
    const loaded = cloneSettings(query.data.settings);
    setDraft(loaded);
    // 保存済みの状態が不正（旧設定で Vision 対応のモデルがない等）なら、開いた時点で案内する。
    setShowDefaultErrors(
      Object.keys(validateDefaultModels(loaded.enterprise_ai)).length > 0,
    );
    setShowModelConnectionErrors(
      Object.keys(
        validateModelConnections(
          loaded.enterprise_ai.models,
          loaded.enterprise_ai.connections,
          savedConnectionIds(loaded),
        ),
      ).length > 0,
    );
    setShowModelIdErrors(
      Object.keys(validateModelIds(loaded.enterprise_ai.models)).length > 0,
    );
    setBaselineData(query.data);
    setCheckData(query.data);
  }, [draft, query.data]);

  const canSubmit = Boolean(draft);
  const saveInProgress = activeSaveSection !== null;
  const operationBusy = saveInProgress || testingKey !== null || reloading;
  // タブの切り替え・欄の追加の後は、描画（commit）の後にフォーカスを移す（#542）。
  const scheduleFocus = useFocusAfterCommit(!operationBusy);
  const dirty = Boolean(
    draft &&
    baselineData &&
    JSON.stringify(draft) !==
      JSON.stringify(cloneSettings(baselineData.settings)),
  );
  useSettingsDraftGuard(dirty, operationBusy, draftGuardMessages);
  const legacySecretDetected =
    checkData?.legacy_secret_detected ??
    baselineData?.legacy_secret_detected ??
    query.data?.legacy_secret_detected ??
    false;

  const updateEnterprise = <K extends keyof EnterpriseAiModelSettings>(
    key: K,
    value: EnterpriseAiModelSettings[K],
  ) => {
    setDraft((current) =>
      current
        ? {
            ...current,
            enterprise_ai: { ...current.enterprise_ai, [key]: value },
          }
        : current,
    );
    setCheckData(baselineData);
    setTestResults({});
    const isDefaultModel =
      key === "default_text_model_id" ||
      key === "default_vision_model_id" ||
      key === "default_judge_model_id" ||
      key === "default_embedding_model_id" ||
      key === "default_rerank_model_id";
    if (isDefaultModel) setShowDefaultErrors(true);
    clearSaveError(isDefaultModel ? "enterprise_models" : "enterprise_connection");
  };

  const updateConnection = (
    connectionId: EnterpriseAiConnectionId,
    patch: Partial<Omit<EnterpriseAiConnectionSettings, "connection_id">>,
  ) => {
    setDraft((current) =>
      current
        ? {
            ...current,
            enterprise_ai: {
              ...current.enterprise_ai,
              connections: current.enterprise_ai.connections.map((connection) =>
                connection.connection_id === connectionId
                  ? {
                      ...connection,
                      ...patch,
                      // 削除の指定と新しい key の入力は両立しない。
                      ...(patch.clear_api_key ? { api_key: "" } : {}),
                    }
                  : connection,
              ),
            },
          }
        : current,
    );
    setCheckData(baselineData);
    setTestResults({});
    clearSaveError("enterprise_connection");
  };

  // セカンダリ接続・ターシャリ接続を「設定」する（#542 / #786）。接続はタブの順に並べる。
  const addOptionalConnection = (connectionId: EnterpriseAiConnectionId) => {
    if (!draft || hasConnection(draft.enterprise_ai.connections, connectionId)) {
      return;
    }
    setDraft((current) =>
      current
        ? {
            ...current,
            enterprise_ai: {
              ...current.enterprise_ai,
              connections: addConnection(
                current.enterprise_ai.connections,
                connectionId,
              ),
            },
          }
        : current,
    );
    setCheckData(baselineData);
    setTestResults({});
    clearSaveError("enterprise_connection");
    // 設定した接続の最初の欄へフォーカスする。
    scheduleFocus(() =>
      document
        .getElementById(connectionFieldId(connectionId, "endpoint"))
        ?.focus(),
    );
  };

  const removeOptionalConnectionWithConfirm = async (
    connectionId: EnterpriseAiConnectionId,
  ) => {
    if (!draft) return;
    const connection = connectionLabel(connectionId);
    const inUse = modelsUsingConnection(draft.enterprise_ai.models, connectionId);
    const ok = await confirm({
      title: t("settings.model.connection.removeConfirm.title", { connection }),
      description: inUse.length
        ? t("settings.model.connection.removeConfirm.descriptionInUse", {
            connection,
            models: inUse
              .map((model) => model.display_name.trim() || model.model_id.trim())
              .join("、"),
          })
        : t("settings.model.connection.removeConfirm.description", { connection }),
      confirmLabel: inUse.length
        ? t("settings.model.connection.removeConfirm.moveAndRemove")
        : t("common.delete"),
      tone: "danger",
    });
    if (!ok) return;
    setDraft((current) =>
      current
        ? {
            ...current,
            enterprise_ai: removeConnection(current.enterprise_ai, connectionId),
          }
        : current,
    );
    setApiKeyVisible((current) => ({
      ...current,
      [connectionId]: false,
    }));
    setShowModelConnectionErrors(true);
    setCheckData(baselineData);
    setTestResults({});
    clearSaveError("enterprise_connection");
    clearSaveError("enterprise_models");
    // 削除したボタンは消えるので、同じタブに出る「◯◯接続を設定」へフォーカスを移す。
    scheduleFocus(() =>
      document.getElementById(connectionAddId(connectionId))?.focus(),
    );
  };

  const updateEnterpriseModel = (
    index: number,
    patch: Partial<EnterpriseAiConfiguredModel>,
  ) => {
    setDraft((current) => {
      if (!current) return current;
      const previous = current.enterprise_ai.models[index];
      const models = current.enterprise_ai.models.map((model, modelIndex) =>
        modelIndex === index ? { ...model, ...patch } : model,
      );
      const next = models[index];
      return {
        ...current,
        enterprise_ai: {
          ...current.enterprise_ai,
          models,
          ...(next
            ? followModelChange(current.enterprise_ai, previous, next, models)
            : {}),
        },
      };
    });
    if (patch.vision_enabled !== undefined || patch.kind !== undefined) {
      setShowDefaultErrors(true);
    }
    if (patch.connection_id !== undefined) setShowModelConnectionErrors(true);
    setCheckData(baselineData);
    setTestResults((current) => ({
      ...current,
      [`enterprise:${index}`]: undefined,
    }));
    clearSaveError("enterprise_models");
  };

  const addEnterpriseModel = () => {
    setDraft((current) =>
      current
        ? {
            ...current,
            enterprise_ai: {
              ...current.enterprise_ai,
              models: [
                ...current.enterprise_ai.models,
                {
                  model_id: "",
                  display_name: "",
                  vision_enabled: false,
                  kind: "text",
                  connection_id: PRIMARY_CONNECTION_ID,
                  reasoning_effort: DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
                  max_input_tokens: DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
                  max_output_tokens: DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
                },
              ],
            },
          }
        : current,
    );
    setCheckData(baselineData);
    setTestResults({});
    clearSaveError("enterprise_models");
  };

  const removeEnterpriseModel = async (index: number) => {
    const target = draft?.enterprise_ai.models[index]?.model_id?.trim();
    const ok = await confirm({
      title: t("settings.model.enterprise.removeConfirm.title"),
      description: target
        ? t("settings.model.enterprise.removeConfirm.description", {
            model: target,
          })
        : t("settings.model.enterprise.removeConfirm.descriptionUnnamed"),
      confirmLabel: t("common.delete"),
      tone: "danger",
    });
    if (!ok) return;
    // 既定に選んでいたモデルを消しても既定は変えず、フィールドのエラーで選び直しを案内する。
    setDraft((current) =>
      current
        ? {
            ...current,
            enterprise_ai: {
              ...current.enterprise_ai,
              models: current.enterprise_ai.models.filter(
                (_, modelIndex) => modelIndex !== index,
              ),
            },
          }
        : current,
    );
    setShowDefaultErrors(true);
    setCheckData(baselineData);
    setTestResults({});
    clearSaveError("enterprise_models");
  };

  function clearSaveError(section: ModelSaveSection) {
    setSaveErrors((current) => ({ ...current, [section]: undefined }));
  }

  const handleTestModel = async (
    key: ModelTestKey,
    target: Omit<ModelSettingsTestRequest, "settings">,
  ) => {
    if (!draft || operationBusy) return;
    setTestResults((current) => ({ ...current, [key]: undefined }));
    setTestingKey(key);
    try {
      const result = await testMutation.mutateAsync({
        ...target,
        settings: draft,
      });
      setTestResults((current) => ({ ...current, [key]: result }));
    } catch (error) {
      const message =
        errorMessage?.(error) ?? t("settings.model.test.apiFailed");
      setTestResults((current) => ({
        ...current,
        [key]: buildClientSideTestFailure(target, message),
      }));
    } finally {
      setTestingKey(null);
    }
  };

  const handleSubmit = async (
    event: FormEvent<HTMLFormElement>,
    section: ModelSaveSection,
  ) => {
    event.preventDefault();
    if (!draft || !baselineData || operationBusy) return;
    clearSaveError(section);
    // 送信を止めるときは、最初の不正な欄へフォーカスする（messaging.md §3.2）。
    if (section === "enterprise_connection") {
      const first = firstConnectionError(
        validateConnections(draft.enterprise_ai.connections),
      );
      if (first) {
        setShowConnectionErrors(true);
        const focusFirst = () =>
          document
            .getElementById(connectionFieldId(first.connectionId, first.field))
            ?.focus();
        if (first.connectionId === connectionTab) {
          focusFirst();
        } else {
          // 別のタブの欄なら、そのタブに切り替え、描画の後にフォーカスする（#542）。
          setConnectionTab(first.connectionId);
          scheduleFocus(focusFirst);
        }
        return;
      }
    }
    if (section === "enterprise_models") {
      const connectionErrors = validateModelConnections(
        draft.enterprise_ai.models,
        draft.enterprise_ai.connections,
        savedConnectionIds(baselineData.settings),
      );
      const idErrors = validateModelIds(draft.enterprise_ai.models);
      const tokenErrors = validateMaxInputTokens(draft.enterprise_ai.models);
      const outputTokenErrors = validateMaxOutputTokens(
        draft.enterprise_ai.models,
      );
      // 行の順に、行の中は欄の並び（モデル ID → 接続 → 入力の上限 → 出力の上限）で最初のエラーの欄を探す。
      const invalidRow = draft.enterprise_ai.models.findIndex(
        (_, index) =>
          idErrors[index] ||
          connectionErrors[index] ||
          tokenErrors[index] ||
          outputTokenErrors[index],
      );
      const errors = validateDefaultModels(draft.enterprise_ai);
      const firstInvalid = DEFAULT_MODEL_FIELD_ORDER.find(
        (field) => errors[field],
      );
      if (invalidRow >= 0 || firstInvalid) {
        setShowModelIdErrors(true);
        setShowModelConnectionErrors(true);
        setShowMaxInputTokensErrors(true);
        setShowDefaultErrors(true);
        document
          .getElementById(
            invalidRow >= 0
              ? idErrors[invalidRow]
                ? modelIdFieldId(invalidRow)
                : connectionErrors[invalidRow]
                  ? modelConnectionFieldId(invalidRow)
                  : tokenErrors[invalidRow]
                    ? modelMaxInputTokensFieldId(invalidRow)
                    : modelMaxOutputTokensFieldId(invalidRow)
              : DEFAULT_MODEL_FIELD_IDS[firstInvalid!],
          )
          ?.focus();
        return;
      }
    }
    setActiveSaveSection(section);
    try {
      const payload = buildSectionSavePayload(
        baselineData.settings,
        draft,
        section,
      );
      // 読み込んだ時点の版を送り、ほかの画面で保存されていれば 409 で止める（#1037）。
      const data = await updateMutation.mutateAsync(
        baselineData.revision
          ? { ...payload, base_revision: baselineData.revision }
          : payload,
      );
      const saved = cloneSettings(data.settings);
      setDraft((current) =>
        current ? mergeSavedSectionIntoDraft(current, saved, section) : saved,
      );
      setBaselineData(data);
      setCheckData(data);
      toast.success(t(MODEL_SAVE_SUCCESS_KEYS[section]));
    } catch (error) {
      const conflict = isConflictError(error);
      if (conflict) setConflictSection(section);
      setSaveErrors((current) => ({
        ...current,
        [section]: conflict
          ? t("settings.model.conflict.message")
          : (errorMessage?.(error) ?? t("settings.model.saveError")),
      }));
    } finally {
      setActiveSaveSection(null);
    }
  };

  // 保存の競合の後に、保存済みの最新の設定を読み直す（#1037）。保存していない入力は確認してから破棄する。
  const reloadLatest = async (section: ModelSaveSection) => {
    if (operationBusy) return;
    if (dirty) {
      const ok = await confirm({
        title: t("settings.model.conflict.confirm.title"),
        description: t("settings.model.conflict.confirm.description"),
        confirmLabel: t("settings.model.conflict.confirm.action"),
        tone: "danger",
      });
      if (!ok) return;
    }
    setReloading(true);
    try {
      const result = await query.refetch();
      if (result.isError || !result.data) {
        setSaveErrors((current) => ({
          ...current,
          [section]:
            errorMessage?.(result.error) ?? t("settings.model.loadError"),
        }));
        return;
      }
      // 下書きを捨てると、読み込みの effect が最新の値から下書き・基準を作り直す。
      setDraft(null);
      setConflictSection(null);
      setSaveErrors({});
      setTestResults({});
      setShowConnectionErrors(false);
      setApiKeyVisible({});
      toast.success(t("settings.model.conflict.reloaded"));
    } finally {
      setReloading(false);
    }
  };

  const defaultModelErrors = draft
    ? validateDefaultModels(draft.enterprise_ai)
    : {};
  const connectionErrors: ConnectionErrors =
    draft && showConnectionErrors
      ? validateConnections(draft.enterprise_ai.connections)
      : {};
  const unsavedConnections =
    draft && baselineData
      ? unsavedConnectionIds(
          draft.enterprise_ai.connections,
          normalizeModelSettings(baselineData.settings).enterprise_ai.connections,
        )
      : new Set<EnterpriseAiConnectionId>();
  const modelConnectionErrors: ModelConnectionErrors =
    draft && baselineData && showModelConnectionErrors
      ? validateModelConnections(
          draft.enterprise_ai.models,
          draft.enterprise_ai.connections,
          savedConnectionIds(baselineData.settings),
        )
      : {};
  const modelIdErrors: ModelIdErrors =
    draft && showModelIdErrors ? validateModelIds(draft.enterprise_ai.models) : {};
  const maxInputTokensErrors: Record<number, string> =
    draft && showMaxInputTokensErrors
      ? validateMaxInputTokens(draft.enterprise_ai.models)
      : {};
  // 出力の上限（#1719）のエラーも、入力の上限と同じく保存の操作の後から出す。
  const maxOutputTokensErrors: Record<number, string> =
    draft && showMaxInputTokensErrors
      ? validateMaxOutputTokens(draft.enterprise_ai.models)
      : {};

  // 取得の失敗を出すのは、下書きがまだない（初回の取得が失敗した）ときだけ（#1036）。
  // 画面は初回の取得の後は下書きを使うので、裏の再取得（画面への復帰・保存の後）が失敗しても
  // 編集中のフォームを取得失敗の表示に置き換えない。
  if (query.isError && !draft) {
    return (
      <PageBody wide>
        <ErrorState
          message={errorMessage?.(query.error) ?? t("settings.model.loadError")}
          onRetry={() => void query.refetch()}
        />
      </PageBody>
    );
  }

  if (query.isPending || !draft) {
    return (
      <PageBody wide>
        {/* 読み込み中は経過時間と、2 枚のカード（接続 / 登録モデル）の形の Skeleton
            （UX 契約 messaging.md §3.6。#1036）。 */}
        <TimedLoadingState
          label={t("settings.model.loading")}
          operationKey="settings-model"
          placement="panel"
          testId="settings-model-loading"
        >
          <FormSkeleton fields={3} />
          <FormSkeleton fields={4} />
        </TimedLoadingState>
      </PageBody>
    );
  }

  return (
    <PageBody wide>
      {legacySecretDetected ? (
        <Banner
          severity="warning"
          title={t("settings.model.legacySecret.title")}
        >
          {t("settings.model.legacySecret.description")}
        </Banner>
      ) : null}
      <fieldset
        disabled={operationBusy}
        aria-busy={operationBusy}
        className="min-w-0 space-y-6"
      >
        <form
          // セカンダリ接続の API key（SecretField の required）でブラウザの既定の検証を出さない。
          // 未入力は保存の操作で欄の直下に出し、最初のエラーの欄へフォーカスする（messaging.md §3.2.1）。
          noValidate
          onSubmit={(event) =>
            void handleSubmit(event, "enterprise_connection")
          }
        >
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Cpu size={16} className="text-accent-fg" aria-hidden />
                {t("settings.model.enterprise.title")}
              </CardTitle>
              <CardDescription>
                {t("settings.model.enterprise.description")}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              {/* プライマリ接続・セカンダリ接続・ターシャリ接続は同じ設定（接続）の別の枠なので、共有の Tabs で
                  この順に切り替える（#542 / #786。README §4「Tabs」）。エラーのあるタブは invalid、保存していない
                  入力は「未保存」で示す。 */}
              <Tabs
                idPrefix={CONNECTION_TABS_ID_PREFIX}
                ariaLabel={t("settings.model.connection.tabs")}
                value={connectionTab}
                onChange={(value) =>
                  setConnectionTab(value as EnterpriseAiConnectionId)
                }
                items={ENTERPRISE_AI_CONNECTION_IDS.map((id) => ({
                  id,
                  label: connectionLabel(id),
                  badge: unsavedConnections.has(id)
                    ? t("settings.model.connection.unsaved")
                    : undefined,
                  badgeTestId: `enterprise-connection-tab-${id}-unsaved`,
                  invalid: Boolean(connectionErrors[id]),
                }))}
              />
              <TabPanel
                id={connectionTab}
                value={connectionTab}
                idPrefix={CONNECTION_TABS_ID_PREFIX}
              >
                <ConnectionTabContent
                  connectionId={connectionTab}
                  connection={draft.enterprise_ai.connections.find(
                    (connection) => connection.connection_id === connectionTab,
                  )}
                  errors={connectionErrors[connectionTab]}
                  apiKeyVisible={Boolean(apiKeyVisible[connectionTab])}
                  onApiKeyVisibleChange={(visible) =>
                    setApiKeyVisible((current) => ({
                      ...current,
                      [connectionTab]: visible,
                    }))
                  }
                  onChange={(patch) => updateConnection(connectionTab, patch)}
                  onAdd={() => addOptionalConnection(connectionTab)}
                  onRemove={() =>
                    void removeOptionalConnectionWithConfirm(connectionTab)
                  }
                />
              </TabPanel>
              <ModelFormActions
                sectionLabel={t("settings.model.enterprise.title")}
                canSubmit={canSubmit}
                saving={activeSaveSection === "enterprise_connection"}
                disabled={saveInProgress}
                errorText={saveErrors.enterprise_connection}
                onReload={
                  conflictSection === "enterprise_connection"
                    ? () => void reloadLatest("enterprise_connection")
                    : undefined
                }
                reloading={reloading}
              />
            </CardContent>
          </Card>
        </form>

        <form
          // 入力の上限の範囲外（number の min / max）でブラウザの既定の検証を出さない（#1437）。
          // 保存の操作で欄の直下にエラーを出し、最初のエラーの欄へフォーカスする（messaging.md §3.2.1）。
          noValidate
          onSubmit={(event) => void handleSubmit(event, "enterprise_models")}
        >
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ListChecks size={16} className="text-accent-fg" aria-hidden />
                {t("settings.model.enterprise.models")}
              </CardTitle>
              <CardDescription>
                {t("settings.model.enterprise.modelsDescription")}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <ModelCatalogEditor
                models={draft.enterprise_ai.models}
                connections={draft.enterprise_ai.connections}
                connectionErrors={modelConnectionErrors}
                modelIdErrors={modelIdErrors}
                maxInputTokensErrors={maxInputTokensErrors}
                maxOutputTokensErrors={maxOutputTokensErrors}
                testingKey={testingKey}
                testResults={testResults}
                onModelChange={updateEnterpriseModel}
                onAdd={addEnterpriseModel}
                onRemove={removeEnterpriseModel}
                onTest={(key, target) => void handleTestModel(key, target)}
                displayNamePlaceholder={displayNamePlaceholder}
              />
              <DefaultModelFields
                enterprise={draft.enterprise_ai}
                errors={showDefaultErrors ? defaultModelErrors : {}}
                onChange={(field, value) => updateEnterprise(field, value)}
              />
              <ModelFormActions
                sectionLabel={t("settings.model.enterprise.models")}
                canSubmit={canSubmit}
                saving={activeSaveSection === "enterprise_models"}
                disabled={saveInProgress}
                errorText={saveErrors.enterprise_models}
                onReload={
                  conflictSection === "enterprise_models"
                    ? () => void reloadLatest("enterprise_models")
                    : undefined
                }
                reloading={reloading}
              />
            </CardContent>
          </Card>
        </form>

      </fieldset>
    </PageBody>
  );
}

/** 登録モデルの行のモデル ID の欄の id（保存前の検証で最初の不正な欄へフォーカスする）。 */
function modelIdFieldId(index: number): string {
  return `enterprise-model-${index}-model-id`;
}

/** 接続のタブの id の接頭辞（同じ画面のほかの Tabs と分ける）。 */
const CONNECTION_TABS_ID_PREFIX = "enterprise-connection";
/** 「◯◯接続を設定」の id（削除の後にフォーカスを戻す。セカンダリ接続は enterprise-secondary-add）。 */
function connectionAddId(connectionId: EnterpriseAiConnectionId): string {
  return `enterprise-${connectionId}-add`;
}

/** 接続ごとの文言（説明・空の状態）。プライマリ接続は空の状態を持たない。 */
const OPTIONAL_CONNECTION_MESSAGES = {
  secondary: {
    description: "settings.model.connection.secondaryDescription",
    emptyTitle: "settings.model.connection.secondaryEmpty.title",
    emptyHint: "settings.model.connection.secondaryEmpty.hint",
  },
  tertiary: {
    description: "settings.model.connection.tertiaryDescription",
    emptyTitle: "settings.model.connection.tertiaryEmpty.title",
    emptyHint: "settings.model.connection.tertiaryEmpty.hint",
  },
} as const;

/**
 * 接続のタブの中身（#542 / #786）。
 * - プライマリ接続: 入力欄。OCI で運用するときだけ必須なので、Endpoint URL と API key は「OCI 運用時必須」（保存は止めない）
 * - セカンダリ接続・ターシャリ接続（未設定）: 空の状態と「◯◯接続を設定」
 * - セカンダリ接続・ターシャリ接続（設定済み）: 入力欄（Endpoint URL と API key は「必須」）と「◯◯接続を削除」。
 *   削除は説明の行の右端に置く（主操作の保存と隣に並べない。README「カード内の操作行」）。
 *   API key だけを消す指定は出さない（必須の欄を空にするため。消すときは接続ごと削除する）
 * - Project OCID はどの接続でも任意（OCI Enterprise AI を使うときだけ入れる。#9）
 */
function ConnectionTabContent({
  connectionId,
  connection,
  errors,
  apiKeyVisible,
  onApiKeyVisibleChange,
  onChange,
  onAdd,
  onRemove,
}: {
  connectionId: EnterpriseAiConnectionId;
  connection?: EnterpriseAiConnectionSettings;
  errors?: Partial<Record<ConnectionField, string>>;
  apiKeyVisible: boolean;
  onApiKeyVisibleChange: (visible: boolean) => void;
  onChange: (
    patch: Partial<Omit<EnterpriseAiConnectionSettings, "connection_id">>,
  ) => void;
  onAdd: () => void;
  onRemove: () => void;
}) {
  const primary = connectionId === PRIMARY_CONNECTION_ID;
  const tertiary = connectionId === TERTIARY_CONNECTION_ID;
  const label = connectionLabel(connectionId);
  const messages =
    connectionId === "primary" ? null : OPTIONAL_CONNECTION_MESSAGES[connectionId];
  if (!connection) {
    return (
      <div data-testid={`enterprise-connection-${connectionId}-empty`}>
        <EmptyState
          title={messages ? t(messages.emptyTitle) : label}
          hint={messages ? t(messages.emptyHint) : undefined}
          action={
            <Button
              id={connectionAddId(connectionId)}
              type="button"
              variant="secondary"
              icon={Plus}
              onClick={onAdd}
            >
              {t("settings.model.connection.add", { connection: label })}
            </Button>
          }
        />
      </div>
    );
  }
  // プライマリ接続は条件付きの必須（文言で区別。README §4「必須の表示」）。セカンダリ接続・ターシャリ接続は
  // 既定の「必須」。Project OCID はどの接続でも任意なので印を出さない（#786 / #9）。
  const requiredLabel = primary ? t("settings.model.requiredInOci") : undefined;
  return (
    <div
      data-testid={`enterprise-connection-${connectionId}`}
      className="space-y-4"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <p className="min-w-0 text-xs leading-relaxed text-fg-muted">
          {messages
            ? t(messages.description)
            : t("settings.model.connection.primaryDescription")}
        </p>
        {primary ? null : (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            tone="danger"
            icon={Trash2}
            onClick={onRemove}
            className="w-full shrink-0 sm:ml-auto sm:w-auto"
          >
            {t("settings.model.connection.remove", { connection: label })}
          </Button>
        )}
      </div>
      {/* Endpoint URL・Project OCID は長いので全幅（FormGrid の最大 60rem まで）、API key は lg の段（#1891）。 */}
      <FormGrid>
        <TextField
          id={connectionFieldId(connectionId, "endpoint")}
          label={t("settings.model.enterprise.endpoint")}
          required
          requiredLabel={requiredLabel}
          value={connection.endpoint}
          placeholder={
            tertiary
              ? t("settings.model.placeholder.endpointTertiary")
              : t("settings.model.placeholder.endpoint")
          }
          error={errors?.endpoint}
          helper={
            tertiary ? (
              t("settings.model.enterprise.endpointHelpTertiary")
            ) : (
              <>
                <span className="block">
                  {t("settings.model.enterprise.endpointHelp")}
                </span>
                <a
                  href="https://docs.oracle.com/en-us/iaas/Content/generative-ai/openai-compatible-api.htm"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex min-h-11 items-center rounded-sm text-accent-fg underline underline-offset-4 hover:text-accent-fg"
                >
                  {t("settings.model.enterprise.endpointDocs")}
                </a>
              </>
            )
          }
          onValueChange={(value) => onChange({ endpoint: value })}
          width="full"
          className="col-span-full"
        />
        <TextField
          id={connectionFieldId(connectionId, "project_ocid")}
          label={t("settings.model.enterprise.project")}
          value={connection.project_ocid}
          placeholder={t("settings.model.placeholder.project")}
          helper={t("settings.model.enterprise.projectHelp")}
          error={errors?.project_ocid}
          onValueChange={(value) => onChange({ project_ocid: value })}
          width="full"
          className="col-span-full"
        />
        <SecretField
          id={connectionFieldId(connectionId, "api_key")}
          label={t("settings.model.enterprise.apiKey")}
          required
          requiredLabel={requiredLabel}
          value={connection.api_key}
          onValueChange={(value) => onChange({ api_key: value })}
          visible={apiKeyVisible}
          onVisibleChange={onApiKeyVisibleChange}
          hasSavedSecret={connection.has_api_key}
          savedLabel={t("settings.model.enterprise.apiKeySaved")}
          notSetLabel={t("settings.model.enterprise.apiKeyNotSet")}
          showLabel={t("settings.model.enterprise.apiKeyShow")}
          hideLabel={t("settings.model.enterprise.apiKeyHide")}
          placeholder={t("settings.model.placeholder.apiKey")}
          helper={t("settings.model.enterprise.apiKeyHelp")}
          error={errors?.api_key}
          clearOption={
            primary
              ? {
                  label: t("settings.model.enterprise.clearApiKey"),
                  checked: connection.clear_api_key,
                  onCheckedChange: (clear) => onChange({ clear_api_key: clear }),
                }
              : undefined
          }
          width="lg"
        />
      </FormGrid>
    </div>
  );
}

/** 節ごとの保存の操作行（UX 契約 buttons §5.2.1 の FormActionBar。保存は form の submit）。 */
function ModelFormActions({
  sectionLabel,
  canSubmit,
  saving,
  disabled,
  errorText,
  onReload,
  reloading = false,
}: {
  sectionLabel: string;
  canSubmit: boolean;
  saving: boolean;
  disabled: boolean;
  errorText?: string;
  /** 保存が競合したときだけ渡す。「最新の設定を読み込む」を保存の左に出す（#1037）。 */
  onReload?: () => void;
  reloading?: boolean;
}) {
  const saveLabel = t("settings.model.save");

  return (
    <FormActionBar
      ariaLabel={t("settings.model.actions.label", { section: sectionLabel })}
      primaryActions={[
        {
          id: "save",
          type: "submit",
          label: saveLabel,
          ariaLabel: `${sectionLabel}: ${saveLabel}`,
          icon: Save,
          loading: saving,
          disabled: !canSubmit || disabled,
        },
      ]}
      secondaryActions={
        onReload
          ? [
              {
                id: "reload-latest",
                label: t("settings.model.conflict.reload"),
                icon: RefreshCw,
                loading: reloading,
                disabled: disabled || reloading,
                onClick: onReload,
              },
            ]
          : undefined
      }
      status={
        errorText ? <FormStatus tone="danger" message={errorText} /> : null
      }
    />
  );
}

function ModelCatalogEditor({
  models,
  connections,
  connectionErrors,
  modelIdErrors,
  maxInputTokensErrors,
  maxOutputTokensErrors,
  testingKey,
  testResults,
  onModelChange,
  onAdd,
  onRemove,
  onTest,
  displayNamePlaceholder,
}: {
  models: EnterpriseAiConfiguredModel[];
  connections: EnterpriseAiConnectionSettings[];
  connectionErrors: ModelConnectionErrors;
  modelIdErrors: ModelIdErrors;
  maxInputTokensErrors: Record<number, string>;
  maxOutputTokensErrors: Record<number, string>;
  testingKey: ModelTestKey | null;
  testResults: Partial<Record<ModelTestKey, ModelSettingsTestResult>>;
  onModelChange: (
    index: number,
    patch: Partial<EnterpriseAiConfiguredModel>,
  ) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
  onTest: (
    key: ModelTestKey,
    target: Omit<ModelSettingsTestRequest, "settings">,
  ) => void;
  displayNamePlaceholder: string;
}) {
  return (
    <div
      role="group"
      aria-labelledby="enterprise-model-catalog-label"
      className="space-y-3"
    >
      <div className="flex min-h-8 flex-wrap items-center justify-between gap-2">
        {/* 登録モデルは複数の入力をまとめた一覧で、label で結べる 1 つの入力が無い。見出しの行に「追加」を並べるため
            fieldset の legend にもできないので、共有の FieldLabel / FieldLegend で表せず、条件付きの必須のタグを
            RequiredBadge で直接置く（#531）。group は aria-required を持てないので、タグは読み上げ対象に残す。 */}
        <span
          id="enterprise-model-catalog-label"
          className="text-sm font-medium text-fg"
        >
          {t("settings.model.enterprise.models")}
          {/* oxlint-disable-next-line design-system/restricted-syntax -- 上のコメントの理由で RequiredBadge を直接置く（#531） */}
          <RequiredBadge
            // OCI に依存しない構成（DeepSeek・百煉など）でも必須なので「必須」（#45）。
            label={t("settings.model.required")}
            className="ml-2 align-middle"
          />
        </span>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={onAdd}
          disabled={models.length >= 20}
          icon={Plus}
        >
          {t("settings.model.enterprise.addModel")}
        </Button>
      </div>
      {/* 2xl 以上は表頭のある表の形の編集グリッド（列の幅は表頭の列が決める）。欄は値の種類の幅を上限に列へ収めるので、
          e2e の幅の段の検査（findFieldsOffWidthScale）からは外す（表の中の欄と同じ扱い。#1891）。 */}
      <div
        id="enterprise-model-catalog"
        className="overflow-hidden rounded-md border border-border bg-surface-sunken"
        data-field-width-exempt=""
      >
        <div
          className={cn(
            "hidden border-b border-border bg-surface px-3 py-2 text-xs font-medium text-fg-muted 2xl:grid 2xl:gap-3",
            CATALOG_COLUMNS,
          )}
        >
          <span>{t("settings.model.enterprise.modelId")}</span>
          <span>{t("settings.model.enterprise.displayName")}</span>
          <span>{t("settings.model.enterprise.kind")}</span>
          <span>{t("settings.model.enterprise.connection")}</span>
          <span>{t("settings.model.enterprise.reasoningEffort")}</span>
          <span>{t("settings.model.enterprise.maxInputTokens")}</span>
          <span>{t("settings.model.enterprise.maxOutputTokens")}</span>
          <span>{t("settings.model.enterprise.vision")}</span>
          <span>{t("settings.model.test.action")}</span>
          <span aria-hidden />
        </div>
        {models.map((model, index) => {
          const modelNumber = index + 1;
          const trimmedModelId = model.model_id.trim();
          const testKey: ModelTestKey = `enterprise:${index}`;
          const kind = modelKind(model);
          const isText = kind === "text";
          const targetType = modelTestTargetType(model);
          return (
            <div
              key={index}
              className={cn(
                "grid gap-3 border-b border-border p-3 last:border-b-0 md:grid-cols-2 md:items-start",
                CATALOG_COLUMNS,
              )}
            >
              <CompactTextInput
                id={modelIdFieldId(index)}
                label={`${t("settings.model.enterprise.modelId")} ${modelNumber}`}
                value={model.model_id}
                placeholder={t("settings.model.placeholder.modelId")}
                width="md"
                error={modelIdErrors[index]}
                onChange={(value) => onModelChange(index, { model_id: value })}
              />
              <CompactTextInput
                id={`enterprise-model-${index}-display-name`}
                label={`${t("settings.model.enterprise.displayName")} ${modelNumber}`}
                value={model.display_name}
                placeholder={displayNamePlaceholder}
                width="md"
                onChange={(value) =>
                  onModelChange(index, { display_name: value })
                }
              />
              {/* 種類（#45）。埋め込み / 再ランクのモデルも同じ一覧に、接続を選んで登録する。 */}
              <SelectField
                id={modelKindFieldId(index)}
                label={t("settings.model.enterprise.kindOfModel", {
                  number: modelNumber,
                })}
                value={kind}
                options={MODEL_KIND_OPTIONS}
                width="sm"
                onValueChange={(value) =>
                  onModelChange(index, {
                    kind: value,
                    // 画像対応はテキストのモデルだけの属性（backend も false にする）。
                    ...(value === "text" ? {} : { vision_enabled: false }),
                  })
                }
                className="min-w-0 max-2xl:[&>label]:text-xs max-2xl:[&>label]:text-fg-muted 2xl:[&>label]:sr-only"
              />
              <SelectField
                id={modelConnectionFieldId(index)}
                // 行の番号は接続の名前と紛れないよう「モデル N の接続」と読ませる。
                label={t("settings.model.enterprise.connectionOfModel", {
                  number: modelNumber,
                })}
                value={modelConnectionId(model)}
                options={connectionOptions(connections)}
                width="md"
                error={connectionErrors[index]}
                onValueChange={(value) =>
                  onModelChange(index, { connection_id: value })
                }
                // 広い画面では表頭が見出しになるので、欄のラベルは読み上げだけにする。狭い画面の
                // ラベルは同じ行の他の欄（CompactTextInput）と同じ小さい文字にそろえる。
                className="min-w-0 max-2xl:[&>label]:text-xs max-2xl:[&>label]:text-fg-muted 2xl:[&>label]:sr-only"
              />
              {/* 推論の量・入力と出力の上限・画像入力はテキストのモデル（LLM）だけの設定（#45）。
                  埋め込み / 再ランクの行は無効表示にする（列はそろえたまま。値は既定のまま送る）。 */}
              <SelectField
                id={modelReasoningEffortFieldId(index)}
                label={t("settings.model.enterprise.reasoningEffortOfModel", {
                  number: modelNumber,
                })}
                value={
                  model.reasoning_effort ?? DEFAULT_ENTERPRISE_AI_REASONING_EFFORT
                }
                options={REASONING_EFFORT_OPTIONS}
                width="sm"
                disabled={!isText}
                describedBy={REASONING_EFFORT_HELP_ID}
                onValueChange={(value) =>
                  onModelChange(index, { reasoning_effort: value })
                }
                // 接続の欄と同じく、広い画面のラベルは読み上げだけにする（表頭が見出し）。
                className="min-w-0 max-2xl:[&>label]:text-xs max-2xl:[&>label]:text-fg-muted 2xl:[&>label]:sr-only"
              />
              <TextField
                id={modelMaxInputTokensFieldId(index)}
                label={t("settings.model.enterprise.maxInputTokensOfModel", {
                  number: modelNumber,
                })}
                type="number"
                inputMode="numeric"
                min={ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN}
                max={ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX}
                step={1}
                value={maxInputTokensInputValue(model)}
                width="xs"
                disabled={!isText}
                aria-describedby={MAX_INPUT_TOKENS_HELP_ID}
                error={maxInputTokensErrors[index]}
                onValueChange={(next) =>
                  onModelChange(index, {
                    max_input_tokens:
                      next.trim() === "" ? Number.NaN : Number(next),
                  })
                }
                inputClassName="tnum"
                // 推論の量の欄と同じく、広い画面のラベルは読み上げだけにする（表頭が見出し）。
                className="min-w-0 max-2xl:[&>label]:text-xs max-2xl:[&>label]:text-fg-muted 2xl:[&>label]:sr-only"
              />
              {/* 出力の上限（#1719）。入力の上限の隣に置き、説明で入力の上限との違いを示す。 */}
              <TextField
                id={modelMaxOutputTokensFieldId(index)}
                label={t("settings.model.enterprise.maxOutputTokensOfModel", {
                  number: modelNumber,
                })}
                type="number"
                inputMode="numeric"
                min={ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN}
                max={ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX}
                step={1}
                value={maxOutputTokensInputValue(model)}
                width="xs"
                disabled={!isText}
                aria-describedby={MAX_OUTPUT_TOKENS_HELP_ID}
                error={maxOutputTokensErrors[index]}
                onValueChange={(next) =>
                  onModelChange(index, {
                    max_output_tokens:
                      next.trim() === "" ? Number.NaN : Number(next),
                  })
                }
                inputClassName="tnum"
                className="min-w-0 max-2xl:[&>label]:text-xs max-2xl:[&>label]:text-fg-muted 2xl:[&>label]:sr-only"
              />
              {/* 同じ行の入力欄・選択欄（md）と同じ高さの最小（#613）。 */}
              <div className="flex min-h-[var(--control-height-md)] items-center justify-between gap-3 text-sm text-fg md:self-end 2xl:self-start 2xl:justify-start">
                <span className="2xl:sr-only">
                  {t("settings.model.enterprise.vision")}
                </span>
                <Switch
                  checked={model.vision_enabled}
                  disabled={!isText}
                  aria-label={`${t("settings.model.enterprise.vision")} ${modelNumber}`}
                  onCheckedChange={(checked) =>
                    onModelChange(index, { vision_enabled: checked })
                  }
                />
              </div>
              <div className="flex min-h-[var(--control-height-md)] items-center">
                {/* 375px でラベルが「テス / ト」と折り返さないよう、ボタンに幅を譲らせる。 */}
                <span className="mr-2 shrink-0 whitespace-nowrap text-xs font-medium text-fg-muted 2xl:sr-only">
                  {t("settings.model.test.action")}
                </span>
                <TestButton
                  modelId={trimmedModelId}
                  fallbackLabel={`${t("settings.model.enterprise.modelId")} ${modelNumber}`}
                  testing={testingKey === testKey}
                  disabled={!trimmedModelId}
                  onClick={() =>
                    onTest(testKey, {
                      target_type: targetType,
                      model_id: model.model_id,
                      vision_enabled: model.vision_enabled,
                    })
                  }
                />
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                iconOnly
                tone="danger"
                aria-label={`${t("settings.model.enterprise.removeModel")} ${modelNumber}`}
                onClick={() => onRemove(index)}
                icon={Trash2}
              ></Button>
              <ModelTestResultPanel
                result={testResults[testKey]}
                testing={testingKey === testKey}
                model={trimmedModelId || `${t("settings.model.enterprise.modelId")} ${modelNumber}`}
                className="md:col-span-2 2xl:col-span-10"
              />
            </div>
          );
        })}
      </div>
      <Disclosure
        summary={t("settings.model.enterprise.parameterHelp")}
        defaultOpen={false}
        data-testid="model-parameter-help"
        contentClassName="space-y-3 border-t border-border p-3"
      >
        <p id={REASONING_EFFORT_HELP_ID} className="text-xs leading-relaxed text-fg-muted">
          {t("settings.model.enterprise.reasoningEffortHelp")}
        </p>
        <p id={MAX_INPUT_TOKENS_HELP_ID} className="text-xs leading-relaxed text-fg-muted">
          {t("settings.model.enterprise.maxInputTokensHelp")}
        </p>
        <p id={MAX_OUTPUT_TOKENS_HELP_ID} className="text-xs leading-relaxed text-fg-muted">
          {t("settings.model.enterprise.maxOutputTokensHelp")}
        </p>
        <div className="flex flex-wrap gap-3 text-xs">
          <a href="https://docs.oracle.com/en-us/iaas/Content/generative-ai/models.htm" target="_blank" rel="noopener noreferrer" className="text-accent-fg underline underline-offset-2">
            {t("settings.model.enterprise.modelDocs")}
          </a>
          <a href="https://docs.oracle.com/en-us/iaas/Content/generative-ai/responses-api.htm" target="_blank" rel="noopener noreferrer" className="text-accent-fg underline underline-offset-2">
            {t("settings.model.enterprise.apiDocs")}
          </a>
        </div>
      </Disclosure>
    </div>
  );
}

/** 入力の上限の説明の要素の id（#1437）。 */
const MAX_INPUT_TOKENS_HELP_ID = "enterprise-model-max-input-tokens-help";

function modelMaxInputTokensFieldId(index: number): string {
  return `enterprise-model-max-input-tokens-${index}`;
}

/** 入力の上限の欄の表示値（値の無いモデルは既定、空欄（NaN）は空）。 */
function maxInputTokensInputValue(model: EnterpriseAiConfiguredModel): number | "" {
  const value = model.max_input_tokens ?? DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS;
  return Number.isNaN(value) ? "" : value;
}

/** 入力の上限が保存できる値か（4096〜2000000 の整数。backend の検証と同じ範囲。#1437）。 */
function isValidMaxInputTokens(value: number | undefined): boolean {
  const tokens = value ?? DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS;
  return (
    Number.isInteger(tokens) &&
    tokens >= ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN &&
    tokens <= ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX
  );
}

/** 行ごとの入力の上限のエラー（行の番号 → 文言。不正な行だけ）。 */
function validateMaxInputTokens(
  models: readonly EnterpriseAiConfiguredModel[],
): Record<number, string> {
  const errors: Record<number, string> = {};
  models.forEach((model, index) => {
    if (!isValidMaxInputTokens(model.max_input_tokens)) {
      errors[index] = t("settings.model.enterprise.maxInputTokensInvalid");
    }
  });
  return errors;
}

/** 出力の上限の説明の要素の id（#1719）。 */
const MAX_OUTPUT_TOKENS_HELP_ID = "enterprise-model-max-output-tokens-help";

function modelMaxOutputTokensFieldId(index: number): string {
  return `enterprise-model-max-output-tokens-${index}`;
}

/** 出力の上限の欄の表示値（値の無いモデルは既定、空欄（NaN）は空）。 */
function maxOutputTokensInputValue(model: EnterpriseAiConfiguredModel): number | "" {
  const value = model.max_output_tokens ?? DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS;
  return Number.isNaN(value) ? "" : value;
}

/**
 * 行ごとの出力の上限のエラー（#1719。行の番号 → 文言。不正な行だけ）。1〜2000000 の整数で、入力の上限
 * （prompt と応答の合計）以下（backend の検証と同じ）。入力の上限が不正な行は、入力の上限のエラーだけを出す。
 */
function validateMaxOutputTokens(
  models: readonly EnterpriseAiConfiguredModel[],
): Record<number, string> {
  const errors: Record<number, string> = {};
  models.forEach((model, index) => {
    const tokens = model.max_output_tokens ?? DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS;
    if (
      !Number.isInteger(tokens) ||
      tokens < ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN ||
      tokens > ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX
    ) {
      errors[index] = t("settings.model.enterprise.maxOutputTokensInvalid");
      return;
    }
    const input = model.max_input_tokens ?? DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS;
    if (isValidMaxInputTokens(model.max_input_tokens) && tokens > input) {
      errors[index] = t("settings.model.enterprise.maxOutputTokensOverInput", {
        max: input,
      });
    }
  });
  return errors;
}

/** 推論の量の説明の要素の id（#1425）。 */
const REASONING_EFFORT_HELP_ID = "enterprise-model-reasoning-effort-help";

function modelReasoningEffortFieldId(index: number): string {
  return `enterprise-model-reasoning-effort-${index}`;
}

/**
 * 推論の量の選択肢（#1425）。none / low / medium / high の 4 つで、既定は low。
 * none は「送らない」（`reasoning` を送らない）と分かるように書く。ほかは API の語のまま出し、
 * 意味は表の下の説明に書く（表の列が狭いため）。
 */
const REASONING_EFFORT_OPTIONS: readonly SelectFieldOption<EnterpriseAiReasoningEffort>[] =
  ENTERPRISE_AI_REASONING_EFFORTS.map((effort) => ({
    value: effort,
    label:
      effort === "none"
        ? t("settings.model.enterprise.reasoningEffort.none")
        : effort,
  }));

/**
 * 登録モデルの一覧の列（モデル ID / 表示名 / 種類（#45）/ 接続 / 推論の量 / 入力の上限 / 出力の上限 /
 * 画像入力（Vision）に対応 / テスト / 削除）。8 列はサイドバーを引いた xl（1280px）の本文幅ではモデル ID・接続が読めないほど狭いため、2xl（1536px）から
 * 表の形にし、md〜2xl は 1 行のモデルの欄を 2 列に並べる（#533 / #1425 / #1437。7 列の #1425 までは xl から表）。
 * 推論の量は「none（送らない）」が読める固定の幅、入力の上限は 7 桁が読める固定の幅にし、残りをモデル ID・表示名・
 * 接続に回す。
 */
const CATALOG_COLUMNS =
  "2xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.9fr)_9.5rem_minmax(0,1fr)_13.5rem_8.5rem_8.5rem_6.5rem_7rem_3.25rem]";

/** 登録モデルの行の「種類」の選択欄の id（#45）。 */
function modelKindFieldId(index: number): string {
  return `enterprise-model-kind-${index}`;
}

const MODEL_KIND_LABEL_KEYS = {
  text: "settings.model.enterprise.kind.text",
  embedding: "settings.model.enterprise.kind.embedding",
  rerank: "settings.model.enterprise.kind.rerank",
} as const satisfies Record<EnterpriseAiModelKind, Parameters<typeof t>[0]>;

/** 種類の選択肢（#45。テキスト（LLM）/ 埋め込み / 再ランク）。 */
const MODEL_KIND_OPTIONS: readonly SelectFieldOption<EnterpriseAiModelKind>[] =
  ENTERPRISE_AI_MODEL_KINDS.map((kind) => ({
    value: kind,
    label: t(MODEL_KIND_LABEL_KEYS[kind]),
  }));

/**
 * 登録モデルの行の「テスト」の対象（#45）。テキストは画像対応ならチャットの画像の呼び出し、それ以外は
 * テキストの呼び出し。埋め込みは `/embeddings`（次元 1536 の確認）、再ランクは `/reranks`（並び順とスコア）。
 */
function modelTestTargetType(
  model: EnterpriseAiConfiguredModel,
): ModelSettingsTestTargetType {
  const kind = modelKind(model);
  if (kind === "embedding" || kind === "rerank") return kind;
  return model.vision_enabled ? "enterprise_vision" : "enterprise_text";
}


/**
 * 既定のモデル（#499）。登録モデルの一覧の下に置き、登録モデルの節と一緒に保存する。
 * 並びはテキスト → Vision → 埋め込み → 再ランク → 判定（desktop の 2 列では 1 行目がテキスト・Vision、
 * 2 行目が埋め込み・再ランク、3 行目が判定。375px の縦積みでも同じ順。#566・#3574・#45）。
 * - 既定のテキストモデル: 必須。選択肢は登録モデルすべて
 * - 既定の Vision モデル: 必須。選択肢は画像入力（Vision）に対応した登録モデルだけ
 * - 既定の判定モデル: 任意。先頭が既定の「既定のテキストモデル」（空）、次が「回答と同じモデル」、続けて登録モデルすべて
 * - 既定の埋め込み / 再ランクモデル: 種類 embedding / rerank の登録モデルだけ。その種類を登録したら必須（#45）
 * 一覧から消えた・Vision 対応でなくなったモデルが選ばれたままなら、その ID を出したままエラーにする。
 */
function DefaultModelFields({
  enterprise,
  errors,
  onChange,
}: {
  enterprise: EnterpriseAiModelSettings;
  errors: DefaultModelErrors;
  onChange: (field: DefaultModelField, value: string) => void;
}) {
  const headingId = "enterprise-default-models-title";
  const embeddingOptions = kindModelOptions(enterprise.models, "embedding");
  const rerankOptions = kindModelOptions(enterprise.models, "rerank");
  return (
    <section
      aria-labelledby={headingId}
      className="space-y-4 border-t border-border pt-5"
    >
      <div className="space-y-1">
        <h3 id={headingId} className="text-sm font-semibold text-fg">
          {t("settings.model.defaults.title")}
        </h3>
        <p className="text-xs leading-relaxed text-fg-muted">
          {t("settings.model.defaults.description")}
        </p>
      </div>
      <FormGrid>
        <SelectField
          id={DEFAULT_MODEL_FIELD_IDS.default_text_model_id}
          label={t("settings.model.defaults.text")}
          required
          value={enterprise.default_text_model_id}
          options={textModelOptions(enterprise.models)}
          placeholder={t("settings.model.defaults.placeholder")}
          helper={t("settings.model.defaults.textHelp")}
          width="lg"
          error={errors.default_text_model_id}
          onValueChange={(value) => onChange("default_text_model_id", value)}
        />
        <SelectField
          id={DEFAULT_MODEL_FIELD_IDS.default_vision_model_id}
          label={t("settings.model.defaults.vision")}
          required
          value={enterprise.default_vision_model_id}
          options={visionModelOptions(enterprise.models)}
          placeholder={t("settings.model.defaults.placeholder")}
          helper={t("settings.model.defaults.visionHelp")}
          width="lg"
          error={errors.default_vision_model_id}
          onValueChange={(value) => onChange("default_vision_model_id", value)}
        />
        {/* 埋め込み / 再ランク（#45）。テキスト / 画像対応の次の行に並べる。選択肢は種類が合う登録モデルだけで、
            その種類を登録したら必須。 */}
        <SelectField
          id={DEFAULT_MODEL_FIELD_IDS.default_embedding_model_id}
          label={t("settings.model.defaults.embedding")}
          required={embeddingOptions.length > 0}
          value={enterprise.default_embedding_model_id ?? ""}
          options={embeddingOptions}
          placeholder={t("settings.model.defaults.placeholder")}
          helper={t(
            embeddingOptions.length > 0
              ? "settings.model.defaults.embeddingHelp"
              : "settings.model.defaults.embeddingEmpty",
          )}
          width="lg"
          error={errors.default_embedding_model_id}
          onValueChange={(value) => onChange("default_embedding_model_id", value)}
        />
        <SelectField
          id={DEFAULT_MODEL_FIELD_IDS.default_rerank_model_id}
          label={t("settings.model.defaults.rerank")}
          required={rerankOptions.length > 0}
          value={enterprise.default_rerank_model_id ?? ""}
          options={rerankOptions}
          placeholder={t("settings.model.defaults.placeholder")}
          helper={t(
            rerankOptions.length > 0
              ? "settings.model.defaults.rerankHelp"
              : "settings.model.defaults.rerankEmpty",
          )}
          width="lg"
          error={errors.default_rerank_model_id}
          onValueChange={(value) => onChange("default_rerank_model_id", value)}
        />
        <SelectField
          id={DEFAULT_MODEL_FIELD_IDS.default_judge_model_id}
          label={t("settings.model.defaults.judge")}
          value={enterprise.default_judge_model_id ?? ""}
          options={judgeModelOptions(enterprise.models, enterprise.default_text_model_id)}
          helper={t("settings.model.defaults.judgeHelp")}
          width="lg"
          error={errors.default_judge_model_id}
          onValueChange={(value) => onChange("default_judge_model_id", value)}
        />
      </FormGrid>
    </section>
  );
}

function CompactTextInput({
  id,
  label,
  value,
  placeholder,
  width,
  error,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  placeholder?: string;
  width?: FieldWidth;
  error?: string;
  onChange: (value: string) => void;
}) {
  return (
    <TextField
      id={id}
      label={label}
      value={value}
      placeholder={placeholder}
      width={width}
      error={error}
      onValueChange={onChange}
      // 広い画面では表頭が見出しになるので、欄のラベルは読み上げだけにする。狭い画面のラベルは
      // 同じ行の選択欄（接続）と同じ小さい文字にする（#631 でネイティブの input から置き換えた）。
      className="min-w-0 max-2xl:[&>label]:text-xs max-2xl:[&>label]:text-fg-muted 2xl:[&>label]:sr-only"
    />
  );
}

function TestButton({
  modelId,
  fallbackLabel,
  testing,
  disabled,
  onClick,
}: {
  modelId: string;
  fallbackLabel: string;
  testing: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  const label = t("settings.model.test.action");
  return (
    <Button
      type="button"
      variant="secondary"
      size="md"
      className="w-full whitespace-nowrap lg:w-auto"
      aria-label={t("settings.model.test.aria", {
        model: modelId || fallbackLabel,
      })}
      disabled={disabled}
      loading={testing}
      onClick={onClick}
      icon={TestTube2}
    >
      {label}
    </Button>
  );
}

function ModelTestResultPanel({
  result,
  testing = false,
  model,
  className,
}: {
  result?: ModelSettingsTestResult;
  /** テスト中は結果の位置に経過時間を出す（スピナーはテストのボタンが担う。messaging.md §3.7）。 */
  testing?: boolean;
  model?: string;
  className?: string;
}) {
  if (testing) {
    return (
      <ProcessingIndicator
        active
        label={t("settings.model.test.running", { model: model ?? "" })}
        operationKey={model ?? null}
        placement="action"
        activityIcon="none"
        className={className}
        testId="settings-model-test-processing"
      />
    );
  }
  if (!result) return null;
  return (
    <SettingsTestResultPanel
      tone={result.status === "success" ? "success" : "danger"}
      message={result.message}
      elapsedMs={result.elapsed_ms}
      details={toSettingsTestResultDetails(result.details)}
      troubleshooting={result.troubleshooting}
      errorType={result.error_type}
      rawError={result.raw_error}
      className={className}
    />
  );
}

/** 保存の競合（409）か。製品の ApiError はどれも HTTP の状態を `status` に持つ。 */
function isConflictError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { status?: unknown }).status === 409
  );
}

function buildClientSideTestFailure(
  target: Omit<ModelSettingsTestRequest, "settings">,
  rawError: string,
): ModelSettingsTestResult {
  return {
    status: "failed",
    target_type: target.target_type,
    model_id: target.model_id,
    message: t("settings.model.test.failed"),
    troubleshooting: [t("settings.model.test.apiFailed")],
    raw_error: rawError,
    error_type: "ApiError",
    elapsed_ms: 0,
    checked_at: new Date().toISOString(),
    details: {},
  };
}

function cloneSettings(settings: ModelSettingsPayload): ModelSettingsPayload {
  return normalizeModelSettings(settings);
}

function savedConnectionIds(settings: ModelSettingsPayload): string[] {
  return normalizeModelSettings(settings).enterprise_ai.connections.map(
    (connection) => connection.connection_id,
  );
}

function cloneConnections(
  connections: readonly EnterpriseAiConnectionSettings[],
): EnterpriseAiConnectionSettings[] {
  return connections.map((connection) => ({ ...connection }));
}

const MODEL_SAVE_SUCCESS_KEYS = {
  enterprise_connection: "settings.model.enterprise.saved",
  enterprise_models: "settings.model.enterprise.modelsSaved",
} as const satisfies Record<ModelSaveSection, Parameters<typeof t>[0]>;

export function buildSectionSavePayload(
  baseline: ModelSettingsPayload,
  draft: ModelSettingsPayload,
  section: ModelSaveSection,
): ModelSettingsPayload {
  const payload = cloneSettings(baseline);
  if (section === "enterprise_connection") {
    const connections = cloneConnections(draft.enterprise_ai.connections);
    payload.enterprise_ai.connections = connections;
    // 削除した接続を使っていた保存済みのモデルは、削除の確認のとおりプライマリ接続に移して送る。
    payload.enterprise_ai.models = moveModelsToAvailableConnections(
      payload.enterprise_ai.models,
      connections.map((connection) => connection.connection_id),
    );
  } else if (section === "enterprise_models") {
    payload.enterprise_ai.models = draft.enterprise_ai.models.map((model) => ({
      ...model,
    }));
    payload.enterprise_ai.default_text_model_id =
      draft.enterprise_ai.default_text_model_id;
    payload.enterprise_ai.default_vision_model_id =
      draft.enterprise_ai.default_vision_model_id;
    payload.enterprise_ai.default_judge_model_id =
      draft.enterprise_ai.default_judge_model_id ?? "";
    payload.enterprise_ai.default_embedding_model_id =
      draft.enterprise_ai.default_embedding_model_id ?? "";
    payload.enterprise_ai.default_rerank_model_id =
      draft.enterprise_ai.default_rerank_model_id ?? "";
  }
  return payload;
}

function mergeSavedSectionIntoDraft(
  draft: ModelSettingsPayload,
  saved: ModelSettingsPayload,
  section: ModelSaveSection,
): ModelSettingsPayload {
  if (section === "enterprise_connection") {
    return {
      enterprise_ai: {
        ...saved.enterprise_ai,
        // 入力中の新しい key は保存で消える（応答は空）。削除の指定も戻す。
        connections: cloneConnections(saved.enterprise_ai.connections),
        models: draft.enterprise_ai.models.map((model) => ({ ...model })),
        default_text_model_id: draft.enterprise_ai.default_text_model_id,
        default_vision_model_id: draft.enterprise_ai.default_vision_model_id,
        default_judge_model_id: draft.enterprise_ai.default_judge_model_id,
        default_embedding_model_id: draft.enterprise_ai.default_embedding_model_id,
        default_rerank_model_id: draft.enterprise_ai.default_rerank_model_id,
      },
    };
  }
  return {
    enterprise_ai: {
      ...saved.enterprise_ai,
      connections: cloneConnections(draft.enterprise_ai.connections),
    },
  };
}
