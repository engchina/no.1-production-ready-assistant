import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ErrorState,
  FormActionBar,
  FormGrid,
  FormSkeleton,
  FormStatus,
  PageBody,
  ProcessingIndicator,
  SelectField,
  StatusBadge,
  TextField,
  TimedLoadingState,
  cn,
  toast,
  type SelectFieldOption,
} from "@production-ready/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cloud, HardDrive, RefreshCw, Save, Settings2, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { useSettingsDraftGuard, type DraftGuardMessages } from "../guards/useSettingsDraftGuard";
import {
  SettingsTestResultPanel,
  toSettingsTestResultDetails,
} from "../oci/SettingsTestResultPanel";
import { UPLOAD_STORAGE_MESSAGES, type UploadStorageMessages } from "./messages";
import {
  UPLOAD_STORAGE_QUERY_KEY,
  type UploadStorageApi,
  type UploadStorageBackend,
  type UploadStorageCheckResult,
  type UploadStorageCheckStage,
  type UploadStorageCheckStageStatus,
  type UploadStorageSettingsData,
  type UploadStorageSettingsUpdate,
} from "./types";

interface UploadStorageForm {
  backend: UploadStorageBackend;
  localStorageDir: string;
  objectStorageRegion: string;
  objectStorageNamespace: string;
  objectStorageBucket: string;
}

type FieldErrors = Partial<Record<keyof UploadStorageForm, string>>;

const EMPTY_FORM: UploadStorageForm = {
  backend: "local",
  localStorageDir: "",
  objectStorageRegion: "",
  objectStorageNamespace: "",
  objectStorageBucket: "",
};

const OBJECT_STORAGE_NAME_PATTERN = /^[A-Za-z0-9._-]+$/;

/** 既定のリージョン候補（NL2SQL と同じ）。 */
export const DEFAULT_OCI_REGION_OPTIONS: readonly SelectFieldOption<string>[] = [
  { value: "ap-tokyo-1", label: "ap-tokyo-1" },
  { value: "ap-osaka-1", label: "ap-osaka-1" },
  { value: "us-chicago-1", label: "us-chicago-1" },
];

export interface UploadStorageSettingsPageProps {
  /** 製品の API 関数（GET / PATCH /api/settings/upload-storage）。 */
  api: UploadStorageApi;
  /** 「OCI 認証設定を開く」で呼ぶ。未保存の変更の確認は画面側で済ませてから呼ぶ。 */
  onOpenOciSettings: () => void;
  /** 入力欄の placeholder（製品ごとの既定の保存先）。 */
  placeholders?: { localStorageDir?: string; objectStorageBucket?: string };
  /** リージョンの選択肢。 */
  regionOptions?: readonly SelectFieldOption<string>[];
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
  /** 既定の文言の上書き。 */
  messages?: Partial<UploadStorageMessages>;
  /** 離脱確認ダイアログの文言の上書き。 */
  draftGuardMessages?: Partial<DraftGuardMessages>;
}

/**
 * アップロード原本の保存先設定（3製品共通。NL2SQL の画面を基準に移設。#97）。
 *
 * - 背景の再取得では、利用者が編集していない項目だけを更新する（NL2SQL #378）
 * - 保存中は入力と再送信を止める
 * - OCI を選んだときは region / namespace / bucket を保存前に検証する（backend も 422 で拒否する）
 * - OCI を選んだときは「接続を確認」で、入力中の値の bucket の存在と object の書き込み・読み取り・
 *   削除の権限を確かめる（保存しない。#2101）。保存のときも backend が同じ確認を行い、bucket が無い・
 *   権限が無いなら保存しない（理由は保存の行の FormStatus）。確かめられずに保存したときは、結果を
 *   warning で出す。
 */
export function UploadStorageSettingsPage({
  api,
  onOpenOciSettings,
  placeholders,
  regionOptions = DEFAULT_OCI_REGION_OPTIONS,
  errorMessage,
  messages,
  draftGuardMessages,
}: UploadStorageSettingsPageProps) {
  const m = { ...UPLOAD_STORAGE_MESSAGES, ...messages };
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: UPLOAD_STORAGE_QUERY_KEY,
    queryFn: ({ signal }) => api.get({ signal }),
  });
  const save = useMutation({
    mutationFn: (payload: UploadStorageSettingsUpdate) => api.update(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: UPLOAD_STORAGE_QUERY_KEY });
    },
  });
  const check = useMutation({
    mutationFn: (payload: UploadStorageSettingsUpdate) => api.test(payload),
  });
  // 保存したが OCI Object Storage を確かめられなかった（一時的な失敗）ときの結果。
  const [unverifiedCheck, setUnverifiedCheck] = useState<UploadStorageCheckResult | null>(null);
  const [form, setForm] = useState<UploadStorageForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<FieldErrors>({});

  const baseline = useRef(EMPTY_FORM);
  const confirmLeave = useSettingsDraftGuard(
    JSON.stringify(form) !== JSON.stringify(baseline.current),
    save.isPending,
    draftGuardMessages,
  );

  useEffect(() => {
    if (!query.data) return;
    const next = formFromSettings(query.data);
    const previous = baseline.current;
    baseline.current = next;
    setForm(
      (current) =>
        Object.fromEntries(
          Object.keys(next).map((key) => {
            const field = key as keyof UploadStorageForm;
            return [field, current[field] === previous[field] ? next[field] : current[field]];
          }),
        ) as unknown as UploadStorageForm,
    );
  }, [query.data]);

  function updateForm(update: Partial<UploadStorageForm>) {
    if (save.isPending) return;
    setForm((current) => ({ ...current, ...update }));
    setErrors((current) => {
      const next = { ...current };
      for (const key of Object.keys(update) as Array<keyof UploadStorageForm>) {
        delete next[key];
      }
      if ("backend" in update) delete next.objectStorageNamespace;
      return next;
    });
    save.reset();
    // 入力を変えたら、前の値で確かめた結果は出さない。
    check.reset();
    setUnverifiedCheck(null);
  }

  function runCheck() {
    if (save.isPending || check.isPending || !query.data) return;
    const validationErrors = validateUploadStorageForm(form, m);
    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      check.reset();
      focusFirstUploadStorageError(validationErrors);
      return;
    }
    setErrors({});
    setUnverifiedCheck(null);
    check.mutate(payloadFromForm(form));
  }

  function submit() {
    if (save.isPending || !query.data) return;
    const validationErrors = validateUploadStorageForm(form, m);
    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      save.reset();
      focusFirstUploadStorageError(validationErrors);
      return;
    }

    setErrors({});
    check.reset();
    setUnverifiedCheck(null);
    save.mutate(payloadFromForm(form), {
      onSuccess: (data) => {
        baseline.current = formFromSettings(data);
        setForm(baseline.current);
        setErrors({});
        if (data.storage_check?.status === "failed") {
          // 保存はしたが確かめられなかった。自動で消さず、操作の直下に出す（messaging.md §10）。
          setUnverifiedCheck(data.storage_check);
        } else {
          toast.success(m.saved);
        }
      },
    });
  }

  if (query.isPending) {
    // 読み込み中は経過時間と、保存先のカードの形の Skeleton を出す（AGENTS.md「読み込み中」。#1047）。
    return (
      <PageBody wide>
        <TimedLoadingState
          label={m.loading}
          operationKey="upload-storage-settings"
          testId="settings-upload-storage-loading"
        >
          <FormSkeleton fields={3} />
        </TimedLoadingState>
      </PageBody>
    );
  }

  // 初回の取得に失敗したときだけ再試行を出して編集させない。取得済みの値がある再取得の失敗
  // （保存後・画面に戻ったときの再取得）では、入力中のフォームを隠さず、上に警告を出す（#1047）。
  if (query.isError && !query.data) {
    return (
      <PageBody wide>
        <ErrorState
          message={errorMessage?.(query.error) ?? m.loadError}
          onRetry={() => void query.refetch()}
          retryLabel={m.retry}
        />
      </PageBody>
    );
  }

  const saveError = (save.error && errorMessage?.(save.error)) || m.saveError;
  const ociSettingsMissing =
    form.backend === "oci" &&
    (!form.objectStorageRegion.trim() || !form.objectStorageNamespace.trim());

  return (
    <PageBody wide>
      {query.isError ? (
        <Banner
          severity="warning"
          className="mb-5"
          action={
            <Button
              type="button"
              variant="secondary"
              size="sm"
              icon={RefreshCw}
              loading={query.isFetching}
              onClick={() => void query.refetch()}
            >
              {m.retry}
            </Button>
          }
        >
          {m.refetchError}
        </Banner>
      ) : null}
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <fieldset
          disabled={save.isPending}
          aria-busy={save.isPending}
          className="min-w-0 space-y-5"
        >
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                {form.backend === "oci" ? (
                  <Cloud size={16} className="text-accent-fg" aria-hidden />
                ) : (
                  <HardDrive size={16} className="text-accent-fg" aria-hidden />
                )}
                {m.destinationTitle}
              </CardTitle>
              <CardDescription>{m.destinationDescription}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <fieldset className="space-y-3">
                <legend className="text-sm font-medium text-fg">{m.fieldBackend}</legend>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <BackendOption
                    id="upload-storage-local"
                    value="local"
                    checked={form.backend === "local"}
                    icon={<HardDrive size={20} aria-hidden />}
                    title={m.backendLocal}
                    description={m.backendLocalDescription}
                    onChange={(backend) => updateForm({ backend })}
                  />
                  <BackendOption
                    id="upload-storage-oci"
                    value="oci"
                    checked={form.backend === "oci"}
                    icon={<Cloud size={20} aria-hidden />}
                    title={m.backendOci}
                    description={m.backendOciDescription}
                    onChange={(backend) => updateForm({ backend })}
                  />
                </div>
              </fieldset>

              {form.backend === "local" ? (
                // パスは全幅（FormGrid の最大 60rem まで。#1891）。
                <FormGrid columns={1}>
                  <TextField
                    id="upload-storage-local-dir"
                    label={m.fieldLocalStorageDir}
                    value={form.localStorageDir}
                    onValueChange={(value) => updateForm({ localStorageDir: value })}
                    helper={m.helperLocalStorageDir}
                    placeholder={placeholders?.localStorageDir}
                    error={errors.localStorageDir}
                    width="full"
                    required
                    requiredLabel={m.required}
                  />
                </FormGrid>
              ) : (
                <div className="space-y-4">
                  {/* リージョン・ネームスペース・バケットは短い値なので、値の長さの段（sm / md）にする（#1891）。 */}
                  <FormGrid>
                    <SelectField
                      id="upload-storage-object-storage-region"
                      label={m.fieldObjectStorageRegion}
                      value={form.objectStorageRegion}
                      options={regionOptions}
                      onValueChange={(value) => updateForm({ objectStorageRegion: value })}
                      helper={m.helperObjectStorageRegion}
                      placeholder={m.regionPlaceholder}
                      error={errors.objectStorageRegion}
                      width="sm"
                      required
                      requiredLabel={m.required}
                    />
                    <TextField
                      id="upload-storage-object-storage-namespace"
                      label={m.fieldObjectStorageNamespace}
                      value={form.objectStorageNamespace}
                      onValueChange={() => undefined}
                      helper={m.helperObjectStorageNamespace}
                      placeholder="mytenancynamespace"
                      error={errors.objectStorageNamespace}
                      width="md"
                      readOnly
                      required
                      requiredLabel={m.required}
                    />
                    <TextField
                      id="upload-storage-bucket"
                      label={m.fieldObjectStorageBucket}
                      value={form.objectStorageBucket}
                      onValueChange={(value) => updateForm({ objectStorageBucket: value })}
                      helper={m.helperObjectStorageBucket}
                      placeholder={placeholders?.objectStorageBucket}
                      error={errors.objectStorageBucket}
                      width="md"
                      required
                      requiredLabel={m.required}
                    />
                  </FormGrid>
                  {ociSettingsMissing ? (
                    // 状況の警告は Banner 1 枚にし、対処の操作は Banner の action に置く（面を二重にしない。messaging.md §10。#722）。
                    <Banner
                      severity="warning"
                      action={
                        <Button
                          type="button"
                          variant="secondary"
                          size="sm"
                          onClick={async () => {
                            if (await confirmLeave()) onOpenOciSettings();
                          }}
                          icon={Settings2}
                        >
                          {m.openOciSettings}
                        </Button>
                      }
                    >
                      {m.ociSettingsIncomplete}
                    </Banner>
                  ) : null}
                </div>
              )}

              <FormActionBar
                ariaLabel={m.actionsLabel}
                primaryActions={[
                  {
                    id: "save",
                    type: "submit",
                    label: m.save,
                    icon: Save,
                    loading: save.isPending,
                    // 押したボタンはネイティブの disabled にせず loading でフォーカスを保つ（#355）。
                    disabled: check.isPending,
                  },
                ]}
                secondaryActions={
                  form.backend === "oci"
                    ? [
                        {
                          id: "test",
                          label: m.test,
                          icon: ShieldCheck,
                          loading: check.isPending,
                          disabled: save.isPending,
                          onClick: runCheck,
                        },
                      ]
                    : []
                }
                status={save.isError ? <FormStatus tone="danger" message={saveError} /> : null}
              />
              {form.backend === "oci" ? (
                <UploadStorageCheckContent
                  m={m}
                  pending={check.isPending}
                  result={check.data ?? unverifiedCheck}
                  unverifiedSave={!check.data && unverifiedCheck !== null}
                  error={
                    check.isError ? (errorMessage?.(check.error) ?? m.testError) : null
                  }
                />
              ) : null}
              {form.backend === "oci" ? (
                <p className="text-xs leading-relaxed text-fg-muted">{m.testHint}</p>
              ) : null}
            </CardContent>
          </Card>
        </fieldset>
      </form>
    </PageBody>
  );
}

const CHECK_STAGE_STATUS_VARIANT = {
  success: "success",
  failed: "danger",
  skipped: "neutral",
} as const satisfies Record<UploadStorageCheckStageStatus, "success" | "danger" | "neutral">;

function checkStageLabel(stage: UploadStorageCheckStage, m: UploadStorageMessages): string {
  return {
    config: m.testStageConfig,
    bucket: m.testStageBucket,
    write: m.testStageWrite,
    read: m.testStageRead,
    delete: m.testStageDelete,
  }[stage.key];
}

function checkStatusLabel(
  status: UploadStorageCheckStageStatus,
  m: UploadStorageMessages,
): string {
  return { success: m.testStageSuccess, failed: m.testStageFailed, skipped: m.testStageSkipped }[
    status
  ];
}

/** 「接続を確認」の結果（OCI 認証設定の接続テストと同じ結果パネル。messaging.md §10。#2101）。 */
function UploadStorageCheckContent({
  m,
  pending,
  result,
  unverifiedSave,
  error,
}: {
  m: UploadStorageMessages;
  pending: boolean;
  result: UploadStorageCheckResult | null;
  /** 保存はしたが確かめられなかった結果（保存の応答の `storage_check`）。 */
  unverifiedSave: boolean;
  error: string | null;
}) {
  if (pending) {
    // OCI への確認は通信を伴い数秒かかる。スピナーは「接続を確認」のボタンが担う（messaging.md §3.7）。
    return (
      <ProcessingIndicator
        active
        label={m.testRunning}
        operationKey="upload-storage-check"
        placement="action"
        activityIcon="none"
        testId="settings-upload-storage-check-processing"
      />
    );
  }
  if (error) {
    return (
      <SettingsTestResultPanel
        tone="danger"
        message={error}
        testId="settings-upload-storage-check-result"
      />
    );
  }
  if (!result) return null;
  const failed = result.status === "failed";
  const troubleshooting = result.stages.flatMap((stage) =>
    stage.status === "failed" && stage.action ? [stage.action] : [],
  );
  return (
    <SettingsTestResultPanel
      tone={!failed ? "success" : unverifiedSave || result.retryable ? "warning" : "danger"}
      message={unverifiedSave ? `${m.savedUnverified} ${result.message}` : result.message}
      elapsedMs={result.elapsed_ms}
      details={toSettingsTestResultDetails({
        region: result.region,
        http_status: result.http_status,
        service_code: result.service_code,
        opc_request_id: result.request_id,
        check_object: result.check_object,
      })}
      troubleshooting={troubleshooting}
      errorType={result.error_type}
      testId="settings-upload-storage-check-result"
    >
      <ol
        aria-label={m.testStagesLabel}
        data-testid="settings-upload-storage-check-stages"
        className="min-w-0 space-y-2"
      >
        {result.stages.map((stage) => (
          <li
            key={stage.key}
            data-stage={stage.key}
            data-stage-status={stage.status}
            className="flex min-w-0 flex-col gap-1 sm:flex-row sm:items-start sm:gap-3"
          >
            <span className="flex shrink-0 items-center gap-2 sm:w-52">
              <StatusBadge
                variant={CHECK_STAGE_STATUS_VARIANT[stage.status]}
                label={checkStatusLabel(stage.status, m)}
              />
              <span className="whitespace-nowrap text-xs font-medium text-fg">
                {checkStageLabel(stage, m)}
              </span>
            </span>
            <span className="min-w-0 break-words text-xs leading-relaxed text-fg-muted">
              {stage.message}
            </span>
          </li>
        ))}
      </ol>
    </SettingsTestResultPanel>
  );
}

function BackendOption({
  id,
  value,
  checked,
  icon,
  title,
  description,
  onChange,
}: {
  id: string;
  value: UploadStorageBackend;
  checked: boolean;
  icon: ReactNode;
  title: string;
  description: string;
  onChange: (value: UploadStorageBackend) => void;
}) {
  return (
    <label
      htmlFor={id}
      data-surface-tint={checked ? "info" : undefined}
      className={cn(
        "flex min-h-32 cursor-pointer items-start gap-3 rounded-md border bg-surface p-4 text-left transition-colors",
        checked
          ? "border-accent-emphasis bg-info-subtle"
          : "border-border hover:border-accent-emphasis hover:bg-surface-hover",
      )}
    >
      <input
        id={id}
        type="radio"
        name="upload-storage-backend"
        value={value}
        checked={checked}
        onChange={() => onChange(value)}
        className="mt-1 h-4 w-4 cursor-pointer accent-[var(--color-accent-emphasis)]"
      />
      {/* 名前と説明の 2 行のまとまりの横の印の箱（文字の 1 行目にそろえる規則の例外。#2371）。 */}
      <span
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-surface-sunken text-accent-fg"
        data-icon-align-skip=""
      >
        {icon}
      </span>
      <span>
        <span className="block text-sm font-semibold text-fg">{title}</span>
        <span className="mt-1 block text-xs leading-relaxed text-fg-muted">{description}</span>
      </span>
    </label>
  );
}

function formFromSettings(settings: UploadStorageSettingsData): UploadStorageForm {
  return {
    backend: settings.backend,
    localStorageDir: settings.local_storage_dir,
    objectStorageRegion: settings.object_storage_region,
    objectStorageNamespace: settings.object_storage_namespace,
    objectStorageBucket: settings.object_storage_bucket,
  };
}

function payloadFromForm(form: UploadStorageForm): UploadStorageSettingsUpdate {
  const payload: UploadStorageSettingsUpdate = {
    backend: form.backend,
    local_storage_dir: form.localStorageDir,
    object_storage_bucket: form.objectStorageBucket,
  };
  if (form.backend === "oci") {
    payload.object_storage_region = form.objectStorageRegion;
    payload.object_storage_namespace = form.objectStorageNamespace;
  }
  return payload;
}

/** 保存前の検証（backend の pr_system_settings と同じ規則）。 */
/** 欄の id を画面の上から順に並べる。保存時は最初のエラーの欄へフォーカスする（#531）。 */
const UPLOAD_STORAGE_FIELD_IDS_IN_ORDER = [
  ["localStorageDir", "upload-storage-local-dir"],
  ["objectStorageRegion", "upload-storage-object-storage-region"],
  ["objectStorageNamespace", "upload-storage-object-storage-namespace"],
  ["objectStorageBucket", "upload-storage-bucket"],
] as const satisfies readonly (readonly [keyof FieldErrors, string])[];

function focusFirstUploadStorageError(errors: FieldErrors) {
  const first = UPLOAD_STORAGE_FIELD_IDS_IN_ORDER.find(([field]) => errors[field]);
  if (!first) return;
  // エラーの表示（aria-describedby）が描画された後に移す。
  window.setTimeout(() => document.getElementById(first[1])?.focus(), 0);
}

export function validateUploadStorageForm(
  form: UploadStorageForm,
  m: UploadStorageMessages = UPLOAD_STORAGE_MESSAGES,
): FieldErrors {
  const errors: FieldErrors = {};
  if (form.backend === "local") {
    if (!form.localStorageDir.trim()) {
      errors.localStorageDir = m.validationLocalStorageDir;
    }
    return errors;
  }

  const region = form.objectStorageRegion.trim();
  const namespace = form.objectStorageNamespace.trim();
  const bucket = form.objectStorageBucket.trim();
  if (!region) {
    errors.objectStorageRegion = m.validationObjectStorageRegion;
  }
  if (!namespace) {
    errors.objectStorageNamespace = m.validationObjectStorageNamespace;
  } else if (!OBJECT_STORAGE_NAME_PATTERN.test(namespace)) {
    errors.objectStorageNamespace = m.validationObjectStorageName;
  }
  if (!bucket) {
    errors.objectStorageBucket = m.validationObjectStorageBucket;
  } else if (!OBJECT_STORAGE_NAME_PATTERN.test(bucket)) {
    errors.objectStorageBucket = m.validationObjectStorageName;
  }
  return errors;
}
