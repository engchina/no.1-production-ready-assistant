import {
  AlertTriangle,
  Cloud,
  KeyRound,
  RefreshCw,
  Save,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  toast,
  Banner,
  ErrorState,
  TextField,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormGrid,
  FormSkeleton,
  FormStatus,
  PageBody,
  ProcessingIndicator,
  SelectField,
  StatusBadge,
  TimedLoadingState,
  type SelectFieldOption,
} from "@production-ready/ui";

import { useSettingsDraftGuard } from "../guards/useSettingsDraftGuard";
import type { UploadStorageSettingsData } from "../upload-storage/types";
import { FileDropzone } from "./FileDropzone";
import { InputActionField } from "./InputActionField";
import { t, type OciMessageKey } from "./messages";
import { SettingsTestResultPanel, toSettingsTestResultDetails } from "./SettingsTestResultPanel";
import type {
  OciConfigReadData,
  OciConfigTestResult,
  OciConfigTestStage,
  OciConfigTestStageStatus,
  OciSettingsApi,
  OciSettingsData,
} from "./types";
import {
  DEFAULT_OCI_SETTINGS,
  FIXED_OCI_CONFIG_FILE,
  FIXED_OCI_CONFIG_PROFILE,
  FIXED_OCI_KEY_FILE,
  normalizeOciSettingsDraft,
  validateOciSettingsDraft,
  type OciSettingsDraft,
  type OciSettingsField,
  type OciValidationCode,
  type OciValidationResult,
} from "./ociSettings";
import { useRequestScope } from "./useRequestScope";

type FeedbackState = "idle" | "loading" | "success" | "error";
type ConfigTestState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "success"; data: OciConfigTestResult }
  | { phase: "error"; message: string };

const OCI_REGION_OPTIONS = [
  { value: "ap-tokyo-1", label: "ap-tokyo-1" },
  { value: "ap-osaka-1", label: "ap-osaka-1" },
  { value: "us-chicago-1", label: "us-chicago-1" },
] as const satisfies readonly SelectFieldOption<string>[];

const AUTH_PROFILE_FIELDS = [
  "configFile",
  "configProfile",
  "userOcid",
  "fingerprint",
  "tenancyOcid",
  "keyFile",
  "region",
] as const satisfies readonly OciSettingsField[];

/** config から反映で値を読み取る欄（configProfile / keyFile は固定値のため含めない）。 */
const IMPORTED_CONFIG_FIELDS = [
  "userOcid",
  "fingerprint",
  "tenancyOcid",
  "region",
] as const satisfies readonly OciSettingsField[];

const OBJECT_STORAGE_FIELDS = [
  "objectStorageRegion",
  "objectStorageNamespace",
] as const satisfies readonly OciSettingsField[];

/** 欄の id を画面の上から順に並べる。保存時は最初のエラーの欄へフォーカスする（#531）。 */
const OCI_FIELD_IDS_IN_ORDER = [
  ["configFile", "oci-config-file"],
  ["configProfile", "oci-config-profile"],
  ["userOcid", "oci-user-ocid"],
  ["tenancyOcid", "oci-tenancy-ocid"],
  ["fingerprint", "oci-fingerprint"],
  ["region", "oci-region"],
  // 秘密鍵は FileDropzone を囲む要素の id。中のファイル入力へフォーカスする。
  ["keyFile", "oci-key-file"],
  ["objectStorageNamespace", "oci-object-storage-namespace"],
  ["objectStorageRegion", "oci-object-storage-region"],
] as const satisfies readonly (readonly [OciSettingsField, string])[];

function focusFirstOciError(errors: OciValidationResult) {
  const first = OCI_FIELD_IDS_IN_ORDER.find(([field]) => errors[field]);
  if (!first) return;
  // エラーの表示（aria-describedby）が描画された後に移す。
  window.setTimeout(() => {
    const element = document.getElementById(first[1]);
    const target =
      element && !element.matches("input, button, select, textarea")
        ? element.querySelector<HTMLElement>("input, button")
        : element;
    target?.focus();
  }, 0);
}

export interface OciSettingsPageProps {
  /** 製品の API 関数（GET / PATCH /api/settings/oci など）。 */
  api: OciSettingsApi;
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
}

function isAbortError(cause: unknown): boolean {
  return cause instanceof Error && cause.name === "AbortError";
}

/**
 * OCI 認証設定（3製品共通。NL2SQL の画面を移設。#100）。
 *
 * - 初期取得に失敗したら再試行を出し、編集させない（NL2SQL #375）
 * - 処理中は編集と重複操作を止め、未保存の変更は離脱前に確認する
 */
export function OciSettingsPage({ api, errorMessage }: OciSettingsPageProps) {
  const apiMessage = (error: unknown): string => errorMessage?.(error) ?? "";
  const hasApiMessage = (error: unknown): boolean => errorMessage?.(error) !== undefined;
  const [draft, setDraft] = useState<OciSettingsDraft>(DEFAULT_OCI_SETTINGS);
  const [errors, setErrors] = useState<OciValidationResult>({});
  const [authSaveState, setAuthSaveState] = useState<FeedbackState>("idle");
  // 保存の失敗の文。検証エラーと API の失敗（理由は API の文）を分けて操作の行に出す（messaging.md §10。#1028）。
  const [authSaveError, setAuthSaveError] = useState("");
  const [storageSaveState, setStorageSaveState] = useState<FeedbackState>("idle");
  const [storageSaveError, setStorageSaveError] = useState("");
  const [configImportState, setConfigImportState] = useState<FeedbackState>("idle");
  const [configImportMessage, setConfigImportMessage] = useState("");
  const [keyFileState, setKeyFileState] = useState<FeedbackState>("idle");
  const [keyFileMessage, setKeyFileMessage] = useState("");
  const [keyFileExists, setKeyFileExists] = useState<boolean | null>(null);
  // config はあるが読めないときの理由。欄が空でも「未設定」と見せずに警告する（#1067）。
  const [configError, setConfigError] = useState<{ path: string; reason: string } | null>(null);
  const [namespaceFetchState, setNamespaceFetchState] = useState<FeedbackState>("idle");
  const [namespaceFetchMessage, setNamespaceFetchMessage] = useState("");
  const [configTestState, setConfigTestState] = useState<ConfigTestState>({ phase: "idle" });
  const { abortAll, run: runScopedRequest } = useRequestScope();

  const [baseline, setBaseline] = useState<OciSettingsDraft | null>(null);
  const [loadState, setLoadState] = useState<FeedbackState>("loading");
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const busy = [authSaveState, storageSaveState, configImportState, keyFileState,
    namespaceFetchState, configTestState.phase].includes("loading");
  useSettingsDraftGuard(Boolean(baseline && !sameDraft(draft, baseline)), busy);

  useEffect(() => {
    setLoadState("loading");
    runScopedRequest(async (signal) => {
      const [oci, storage] = await Promise.all([
        api.getOciSettings({ signal }), api.getUploadStorageSettings({ signal }),
      ]);
      if (signal.aborted) return;
      const loaded = normalizeOciSettingsDraft({
        ...DEFAULT_OCI_SETTINGS, ...runtimeOciSettingsToDraft(oci),
        ...runtimeObjectStorageSettingsToDraft(storage),
      });
      setDraft(loaded);
      setBaseline(loaded);
      setKeyFileExists(oci.key_file_exists);
      setConfigError(configErrorFrom(oci));
      setLoadState("success");
    }).catch((cause: unknown) => {
      if (isAbortError(cause)) return;
      setLoadError(hasApiMessage(cause) ? apiMessage(cause) : t("settings.oci.loadError"));
      setLoadState("error");
    });
    return () => abortAll();
  }, [abortAll, runScopedRequest, loadAttempt]);

  function updateDraft<K extends OciSettingsField>(field: K, value: OciSettingsDraft[K]) {
    if (busy || loadState !== "success" || field === "objectStorageNamespace") return;
    setDraft((current) => ({ ...current, [field]: value }));
    setErrors((current) => {
      if (!current[field]) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
    if (fieldInGroup(AUTH_PROFILE_FIELDS, field)) setAuthSaveState("idle");
    if (fieldInGroup(AUTH_PROFILE_FIELDS, field)) setConfigTestState({ phase: "idle" });
    if (fieldInGroup(OBJECT_STORAGE_FIELDS, field)) setStorageSaveState("idle");
    setConfigImportState("idle");
    setConfigImportMessage("");
    setKeyFileState("idle");
    setKeyFileMessage("");
    if (field === "objectStorageRegion" || field === "objectStorageNamespace") {
      setNamespaceFetchState("idle");
      setNamespaceFetchMessage("");
    }
  }

  async function saveAuthDraft() {
    if (busy || loadState !== "success") return;
    const validationErrors = validationErrorsForFields(draft, AUTH_PROFILE_FIELDS);
    if (hasValidationErrors(validationErrors)) {
      setErrors((current) => ({
        ...clearSectionErrors(current, AUTH_PROFILE_FIELDS),
        ...validationErrors,
      }));
      setAuthSaveState("error");
      setAuthSaveError(t("settings.oci.status.invalid"));
      setConfigTestState({ phase: "idle" });
      focusFirstOciError(validationErrors);
      return;
    }

    setErrors((current) => clearSectionErrors(current, AUTH_PROFILE_FIELDS));
    setAuthSaveState("loading");
    try {
      const saved = await api.updateOciSettings({
        user: draft.userOcid,
        fingerprint: draft.fingerprint,
        tenancy: draft.tenancyOcid,
        region: draft.region,
      });
      setBaseline((current) => current && normalizeOciSettingsDraft({ ...current, ...runtimeOciSettingsToDraft(saved) }));
      setKeyFileExists(saved.key_file_exists);
      setConfigError(configErrorFrom(saved));
      setDraft((current) =>
        normalizeOciSettingsDraft({
          ...current,
          ...runtimeOciSettingsToDraft(saved),
        })
      );
      setAuthSaveState("idle");
      setAuthSaveError("");
      toast.success(t("settings.oci.message.saved"));
    } catch (error) {
      setAuthSaveState("error");
      setAuthSaveError(
        hasApiMessage(error) ? apiMessage(error) : t("settings.oci.status.saveFailed")
      );
      setConfigTestState({ phase: "idle" });
    }
  }

  async function testAuthConfig() {
    if (busy || loadState !== "success") return;
    setErrors((current) => clearSectionErrors(current, AUTH_PROFILE_FIELDS));
    setConfigTestState({ phase: "loading" });
    try {
      setConfigTestState({ phase: "success", data: await api.testOciConfig() });
    } catch (error) {
      setConfigTestState({
        phase: "error",
        message:
          hasApiMessage(error)
            ? t("settings.oci.configTest.apiError", { message: apiMessage(error) })
            : t("settings.oci.configTest.error"),
      });
    }
  }

  async function saveStorageDraft() {
    if (busy || loadState !== "success") return;
    const validationErrors = validationErrorsForFields(draft, OBJECT_STORAGE_FIELDS);
    if (hasValidationErrors(validationErrors)) {
      setErrors((current) => ({
        ...clearSectionErrors(current, OBJECT_STORAGE_FIELDS),
        ...validationErrors,
      }));
      setStorageSaveState("error");
      setStorageSaveError(t("settings.oci.status.invalid"));
      focusFirstOciError(validationErrors);
      return;
    }

    setErrors((current) => clearSectionErrors(current, OBJECT_STORAGE_FIELDS));
    setStorageSaveState("loading");
    try {
      const saved = await api.updateOciObjectStorageSettings({
        object_storage_region: draft.objectStorageRegion,
        object_storage_namespace: draft.objectStorageNamespace,
      });
      setBaseline((current) => current && normalizeOciSettingsDraft({ ...current, ...runtimeObjectStorageSettingsToDraft(saved) }));
      setDraft((current) =>
        normalizeOciSettingsDraft({
          ...current,
          ...runtimeObjectStorageSettingsToDraft(saved),
        })
      );
      setStorageSaveState("idle");
      setStorageSaveError("");
      toast.success(t("settings.oci.message.storageSaved"));
    } catch (error) {
      setStorageSaveState("error");
      setStorageSaveError(
        hasApiMessage(error) ? apiMessage(error) : t("settings.oci.status.storageSaveFailed")
      );
    }
  }

  async function importConfigFromPath() {
    if (busy || loadState !== "success") return;
    const pathAndProfileErrors: OciValidationResult = {};
    if (!draft.configFile.trim()) pathAndProfileErrors.configFile = "required";
    if (Object.keys(pathAndProfileErrors).length > 0) {
      setErrors((current) => ({ ...current, ...pathAndProfileErrors }));
      setConfigImportState("error");
      setConfigImportMessage(t("settings.oci.configContent.applyError"));
      return;
    }

    setConfigImportState("loading");
    setConfigImportMessage("");
    try {
      const imported = await api.readOciConfig({
        config_file: FIXED_OCI_CONFIG_FILE,
        profile: FIXED_OCI_CONFIG_PROFILE,
      });
      const parsed = ociConfigReadDataToDraft(imported);
      // configProfile / keyFile は固定値なので、user / fingerprint / tenancy / region のどれも
      // 読み取れなければ反映できたものは無い（成功と出さない。#1028）。
      if (!parsed.appliedFields.some((field) => fieldInGroup(IMPORTED_CONFIG_FIELDS, field))) {
        setConfigImportState("error");
        setConfigImportMessage(t("settings.oci.configContent.applyError"));
        return;
      }

      setDraft((current) => normalizeOciSettingsDraft({ ...current, ...parsed.values }));
      setErrors((current) => {
        const next = { ...current };
        for (const field of parsed.appliedFields) {
          delete next[field];
        }
        return next;
      });
      setConfigImportState("idle");
      setAuthSaveState("idle");
      setKeyFileState("idle");
      toast.success(t("settings.oci.message.configImported"));
    } catch (error) {
      setConfigImportState("error");
      setConfigImportMessage(
        hasApiMessage(error) ? apiMessage(error) : t("settings.oci.configContent.applyError")
      );
    }
  }

  async function selectKeyFile(file: File | undefined) {
    if (!file || busy || loadState !== "success") return;
    if (!/\.(pem|key)$/i.test(file.name)) {
      setKeyFileState("error");
      setKeyFileMessage(t("settings.oci.validation.invalidKeyFile"));
      return;
    }
    setKeyFileState("loading");
    setKeyFileMessage("");
    try {
      await api.uploadOciPrivateKey(file);
      updateDraft("keyFile", FIXED_OCI_KEY_FILE);
      setKeyFileExists(true);
      setKeyFileState("success");
      setConfigTestState({ phase: "idle" });
      toast.success(t("settings.oci.message.keyUploaded"));
    } catch (error) {
      setKeyFileState("error");
      setKeyFileMessage(
        hasApiMessage(error) ? apiMessage(error) : t("settings.oci.actions.keyFileUploadFailed")
      );
    }
  }

  async function fetchObjectStorageNamespace() {
    if (busy || loadState !== "success") return;
    if (!draft.objectStorageRegion.trim()) {
      setErrors((current) => ({ ...current, objectStorageRegion: "required" }));
      setNamespaceFetchState("error");
      setNamespaceFetchMessage(requiredText("objectStorageRegion"));
      return;
    }

    setNamespaceFetchState("loading");
    setNamespaceFetchMessage("");
    try {
      const data = await api.readOciObjectStorageNamespace({
        config_file: FIXED_OCI_CONFIG_FILE,
        profile: FIXED_OCI_CONFIG_PROFILE,
        region: draft.objectStorageRegion,
      });
      const namespace = data.namespace.trim();
      if (!namespace) {
        setNamespaceFetchState("error");
        setNamespaceFetchMessage(t("settings.oci.actions.namespaceFetchFailed"));
        return;
      }

      setDraft((current) =>
        normalizeOciSettingsDraft({ ...current, objectStorageNamespace: namespace })
      );
      setErrors((current) => {
        const next = { ...current };
        delete next.objectStorageNamespace;
        return next;
      });
      setStorageSaveState("idle");
      setNamespaceFetchState("success");
      toast.success(t("settings.oci.message.namespaceFetched"));
    } catch (error) {
      setNamespaceFetchState("error");
      setNamespaceFetchMessage(
        hasApiMessage(error)
          ? apiMessage(error)
          : t("settings.oci.actions.namespaceFetchFailed")
      );
    }
  }

  // 読み込み中は経過時間と、2 枚のカード（認証・Object Storage）の形の Skeleton を出す（AGENTS.md。#1028）。
  if (loadState === "loading") return <PageBody wide>
    <TimedLoadingState
      label={t("settings.oci.loading")}
      operationKey={`oci-settings-${loadAttempt}`}
      testId="settings-oci-loading"
    >
      <FormSkeleton fields={7} />
      <FormSkeleton fields={2} />
    </TimedLoadingState>
  </PageBody>;
  if (loadState === "error") return <PageBody wide><ErrorState
    message={loadError} onRetry={() => setLoadAttempt((current) => current + 1)}
  /></PageBody>;

  return (
    <PageBody wide>
      {/* ページ全体を <fieldset disabled> で包まない。押したボタン（保存・接続テスト・取込・取得・鍵のファイル）まで
          ネイティブの disabled になり、フォーカスが body へ外れるため（#355）。他の操作は部品ごとに disabled にし、
          押したボタンは loading（aria-disabled）でフォーカスを保つ（#835）。 */}
      <div aria-busy={busy} className="min-w-0 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <KeyRound size={16} className="text-accent-fg" aria-hidden />
              {t("settings.oci.auth.cardTitle")}
            </CardTitle>
            <CardDescription>{t("settings.oci.auth.cardDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            {configError ? (
              <Banner severity="warning">
                {t("settings.oci.configError", configError)}
              </Banner>
            ) : null}
            {/* 欄の並びは FormGrid（最大 60rem・置き場所 42rem 以上で 2 列）。パス・OCID は全幅、プロファイル・
                フィンガープリント・リージョンは値の長さの段（#1891）。 */}
            <FormGrid>
              <ConfigFileField
                id="oci-config-file"
                label={t("settings.oci.field.configFile")}
                value={draft.configFile}
                error={errorText(errors.configFile, "configFile")}
                helper={t("settings.oci.helper.configFile")}
                placeholder="~/.oci/config"
                importState={configImportState}
                importError={configImportMessage}
                busy={busy}
                onApply={() => void importConfigFromPath()}
                readOnly
                required
                className="col-span-full"
              />
              <TextField
                id="oci-config-profile"
                label={t("settings.oci.field.configProfile")}
                value={draft.configProfile}
                error={errorText(errors.configProfile, "configProfile")}
                helper={t("settings.oci.helper.configProfile")}
                placeholder="DEFAULT"
                width="md"
                readOnly
                required
              />
              <TextField
                id="oci-user-ocid"
                label={t("settings.oci.field.userOcid")}
                value={draft.userOcid}
                onValueChange={(value) => updateDraft("userOcid", value)}
                disabled={busy}
                error={errorText(errors.userOcid, "userOcid")}
                helper={t("settings.oci.helper.userOcid")}
                placeholder="ocid1.user.oc1.."
                width="full"
                className="col-span-full"
                required
              />
              <TextField
                id="oci-tenancy-ocid"
                label={t("settings.oci.field.tenancyOcid")}
                value={draft.tenancyOcid}
                onValueChange={(value) => updateDraft("tenancyOcid", value)}
                disabled={busy}
                error={errorText(errors.tenancyOcid, "tenancyOcid")}
                helper={t("settings.oci.helper.tenancyOcid")}
                placeholder="ocid1.tenancy.oc1.."
                width="full"
                className="col-span-full"
                required
              />
              <TextField
                id="oci-fingerprint"
                label={t("settings.oci.field.fingerprint")}
                value={draft.fingerprint}
                onValueChange={(value) => updateDraft("fingerprint", value)}
                disabled={busy}
                error={errorText(errors.fingerprint, "fingerprint")}
                helper={t("settings.oci.helper.fingerprint")}
                placeholder="12:34:56:78:90:ab:cd:ef"
                // フィンガープリントは 47 文字（16 バイトのコロン区切り）が入る lg。
                width="lg"
                required
              />
              <SelectField
                id="oci-region"
                label={t("settings.oci.field.region")}
                value={draft.region}
                options={OCI_REGION_OPTIONS}
                onValueChange={(value) => updateDraft("region", value)}
                disabled={busy}
                error={errorText(errors.region, "region")}
                helper={t("settings.oci.helper.region")}
                placeholder={t("settings.oci.placeholder.region")}
                width="sm"
                required
              />
            </FormGrid>

            <PrivateKeyDropzoneField
              id="oci-key-file"
              label={t("settings.oci.field.keyFile")}
              value={draft.keyFile}
              error={errorText(errors.keyFile, "keyFile")}
              // 鍵のファイルの読み込み中は、その欄が loading でフォーカスを保つ（#835）。
              disabled={busy && keyFileState !== "loading"}
              fileState={keyFileState}
              fileMessage={keyFileMessage}
              keyFileExists={keyFileExists}
              onFileChange={selectKeyFile}
              required
            />

            <SectionActions
              ariaContext={t("nav.settingsOci")}
              busy={busy}
              saveState={authSaveState}
              saveError={authSaveError}
              saveLabel={t("settings.oci.actions.saveAuth")}
              onSave={() => void saveAuthDraft()}
              testState={configTestState.phase}
              testLabel={t("settings.oci.actions.test")}
              onTest={() => void testAuthConfig()}
            />
            <ConfigTestContent state={configTestState} />
            <p className="text-xs leading-relaxed text-fg-muted">{t("settings.oci.hint")}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Cloud size={16} className="text-accent-fg" aria-hidden />
              {t("settings.oci.storage.title")}
            </CardTitle>
            <CardDescription>{t("settings.oci.storage.description")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <FormGrid>
              <NamespaceField
                id="oci-object-storage-namespace"
                label={t("settings.oci.field.objectStorageNamespace")}
                value={draft.objectStorageNamespace}
                error={errorText(errors.objectStorageNamespace, "objectStorageNamespace")}
                helper={t("settings.oci.helper.objectStorageNamespace")}
                placeholder="mytenancynamespace"
                fetchState={namespaceFetchState}
                fetchError={namespaceFetchMessage}
                busy={busy}
                onFetch={() => void fetchObjectStorageNamespace()}
                required
              />
              <SelectField
                id="oci-object-storage-region"
                label={t("settings.oci.field.objectStorageRegion")}
                value={draft.objectStorageRegion}
                options={OCI_REGION_OPTIONS}
                onValueChange={(value) => updateDraft("objectStorageRegion", value)}
                disabled={busy}
                error={errorText(errors.objectStorageRegion, "objectStorageRegion")}
                helper={t("settings.oci.helper.objectStorageRegion")}
                placeholder={t("settings.oci.placeholder.region")}
                width="sm"
                required
              />
            </FormGrid>

            <SectionActions
              ariaContext={t("settings.oci.storage.title")}
              busy={busy}
              saveState={storageSaveState}
              saveError={storageSaveError}
              saveLabel={t("settings.oci.actions.save")}
              onSave={saveStorageDraft}
            />
          </CardContent>
        </Card>
      </div>
    </PageBody>
  );
}

/**
 * カード末尾の操作行（UX 契約 buttons §5.2.1 の FormActionBar）。
 * 保存中・接続テスト中もラベルは変えず、先頭アイコンだけが Button の loading でスピナーになる。
 */
function SectionActions({
  ariaContext,
  busy,
  saveState,
  saveError,
  saveLabel,
  onSave,
  testState,
  testLabel,
  onTest,
}: {
  ariaContext: string;
  /** ページのいずれかの操作の処理中。押したボタン以外を無効にする。 */
  busy: boolean;
  saveState: FeedbackState;
  /** 保存の失敗の文（検証エラー・API の失敗の理由）。 */
  saveError: string;
  saveLabel: string;
  onSave: () => void;
  testState?: ConfigTestState["phase"];
  testLabel?: string;
  onTest?: () => void;
}) {
  const isSaving = saveState === "loading";
  const isTesting = testState === "loading";

  return (
    <FormActionBar
      ariaLabel={t("settings.oci.actions.label", { section: ariaContext })}
      primaryActions={[
        {
          id: "save",
          label: saveLabel,
          ariaLabel: `${ariaContext}: ${saveLabel}`,
          icon: Save,
          loading: isSaving,
          // 押したボタンはネイティブの disabled にせず loading（aria-disabled）でフォーカスを保つ（#355 / #835）。
          disabled: busy && !isSaving,
          onClick: onSave,
        },
      ]}
      secondaryActions={
        onTest && testLabel
          ? [
              {
                id: "test",
                label: testLabel,
                ariaLabel: `${ariaContext}: ${testLabel}`,
                icon: ShieldCheck,
                loading: isTesting,
                disabled: busy && !isTesting,
                onClick: onTest,
              },
            ]
          : []
      }
      status={
        saveState === "error" ? (
          <FormStatus tone="danger" message={saveError || t("settings.oci.status.invalid")} />
        ) : null
      }
    />
  );
}

function ConfigTestContent({ state }: { state: ConfigTestState }) {
  if (state.phase === "idle") return null;
  if (state.phase === "loading") {
    // OCI への認証の確認は通信を伴い数秒かかる。スピナーは接続テストのボタンが担う（messaging.md §3.7）。
    return (
      <ProcessingIndicator
        active
        label={t("settings.oci.configTest.running")}
        operationKey="oci-config-test"
        placement="action"
        activityIcon="none"
        testId="settings-oci-test-processing"
      />
    );
  }

  if (state.phase === "error") {
    return (
      <SettingsTestResultPanel
        tone="danger"
        message={state.message}
        testId="settings-oci-test-result"
      />
    );
  }

  const result = state.data;
  const failed = result.status === "failed";
  const stages = result.stages ?? [];
  const troubleshooting = [
    ...stages.flatMap((stage) =>
      stage.status === "failed" && stage.action ? [stage.action] : []
    ),
    ...result.missing_fields.map((field) =>
      t("settings.oci.configTest.missingField", { field })
    ),
    ...result.permission_issues,
    !result.key_file_exists ? t("settings.oci.configTest.missingKey") : "",
  ].filter(Boolean);

  return (
    <SettingsTestResultPanel
      tone={failed ? "danger" : "success"}
      message={result.message}
      elapsedMs={result.elapsed_ms}
      details={toSettingsTestResultDetails({
        profile: result.profile,
        region: result.region,
        auth_check: result.auth_check_operation,
        http_status: result.http_status,
        service_code: result.service_code,
        opc_request_id: result.request_id,
        oci_directory_mode: result.oci_directory_mode,
        config_file_mode: result.config_file_mode,
        key_file_mode: result.key_file_mode,
      })}
      troubleshooting={troubleshooting}
      errorType={result.error_type}
      testId="settings-oci-test-result"
    >
      {stages.length > 0 ? <ConfigTestStages stages={stages} /> : null}
    </SettingsTestResultPanel>
  );
}

const STAGE_STATUS_VARIANT = {
  success: "success",
  failed: "danger",
  skipped: "neutral",
} as const satisfies Record<OciConfigTestStageStatus, "success" | "danger" | "neutral">;

function ConfigTestStages({ stages }: { stages: readonly OciConfigTestStage[] }) {
  return (
    <ol
      aria-label={t("settings.oci.configTest.stagesLabel")}
      data-testid="settings-oci-test-stages"
      className="min-w-0 space-y-2"
    >
      {stages.map((stage) => (
        <li
          key={stage.key}
          data-stage={stage.key}
          data-stage-status={stage.status}
          className="flex min-w-0 flex-col gap-1 sm:flex-row sm:items-start sm:gap-3"
        >
          <span className="flex shrink-0 items-center gap-2 sm:w-52">
            <StatusBadge
              variant={STAGE_STATUS_VARIANT[stage.status]}
              label={t(`settings.oci.configTest.stageStatus.${stage.status}`)}
            />
            <span className="whitespace-nowrap text-xs font-medium text-fg">
              {t(`settings.oci.configTest.stage.${stage.key}`)}
            </span>
          </span>
          <span className="min-w-0 break-words text-xs leading-relaxed text-fg-muted">
            {stage.message}
          </span>
        </li>
      ))}
    </ol>
  );
}

function configErrorFrom(settings: OciSettingsData): { path: string; reason: string } | null {
  const reason = settings.config_error?.trim();
  if (!reason) return null;
  return { path: settings.config_file || FIXED_OCI_CONFIG_FILE, reason };
}

function runtimeOciSettingsToDraft(
  settings: OciSettingsData
): Pick<
  OciSettingsDraft,
  "configFile" | "configProfile" | "userOcid" | "fingerprint" | "tenancyOcid" | "keyFile" | "region"
> {
  return {
    configFile: (settings.config_file ?? "").trim() || FIXED_OCI_CONFIG_FILE,
    configProfile: (settings.profile ?? "").trim() || FIXED_OCI_CONFIG_PROFILE,
    userOcid: (settings.user ?? "").trim(),
    fingerprint: (settings.fingerprint ?? "").trim(),
    tenancyOcid: (settings.tenancy ?? "").trim(),
    keyFile: FIXED_OCI_KEY_FILE,
    region: (settings.region ?? "").trim(),
  };
}

function runtimeObjectStorageSettingsToDraft(
  settings: UploadStorageSettingsData
): Pick<OciSettingsDraft, "objectStorageRegion" | "objectStorageNamespace"> {
  return {
    objectStorageRegion: (settings.object_storage_region ?? "").trim(),
    objectStorageNamespace: (settings.object_storage_namespace ?? "").trim(),
  };
}

function ociConfigReadDataToDraft(data: OciConfigReadData): {
  values: Partial<OciSettingsDraft>;
  appliedFields: OciSettingsField[];
} {
  const values: Partial<OciSettingsDraft> = {
    configProfile: FIXED_OCI_CONFIG_PROFILE,
  };
  const appliedFields: OciSettingsField[] = ["configProfile"];

  addImportedValue(values, appliedFields, "userOcid", data.user);
  addImportedValue(values, appliedFields, "fingerprint", data.fingerprint);
  addImportedValue(values, appliedFields, "tenancyOcid", data.tenancy);
  addImportedValue(values, appliedFields, "region", data.region);
  values.keyFile = FIXED_OCI_KEY_FILE;
  appliedFields.push("keyFile");

  return { values, appliedFields };
}

function addImportedValue(
  values: Partial<OciSettingsDraft>,
  appliedFields: OciSettingsField[],
  field: OciSettingsField,
  value: string
) {
  const cleaned = value.trim();
  if (!cleaned) return;
  values[field] = cleaned as never;
  appliedFields.push(field);
}

// loading 中もラベルは変えない（先頭アイコンがスピナーになる。デザインシステムの禁止事項）。
function configImportButtonLabel(state: FeedbackState): string {
  if (state === "success") return t("settings.oci.actions.applied");
  return t("settings.oci.actions.applyConfig");
}

function namespaceFetchButtonLabel(state: FeedbackState): string {
  if (state === "success") return t("settings.oci.actions.namespaceFetched");
  return t("settings.oci.actions.fetchNamespace");
}

function ConfigFileField({
  id,
  label,
  value,
  onChange,
  error,
  helper,
  placeholder,
  importState,
  importError,
  busy,
  onApply,
  readOnly = false,
  required,
  className,
}: {
  id: string;
  label: string;
  value: string;
  onChange?: (value: string) => void;
  error?: string;
  helper: string;
  placeholder: string;
  importState: FeedbackState;
  importError: string;
  busy: boolean;
  onApply: () => void;
  readOnly?: boolean;
  required?: boolean;
  className?: string;
}) {
  return (
    <InputActionField
      id={id}
      label={label}
      value={value}
      onChange={onChange}
      className={className}
      placeholder={placeholder}
      helper={helper}
      error={error}
      actionError={
        importState === "error"
          ? importError || t("settings.oci.configContent.applyError")
          : undefined
      }
      readOnly={readOnly}
      required={required}
      action={{
        label: configImportButtonLabel(importState),
        icon: RefreshCw,
        loading: importState === "loading",
        disabled: busy && importState !== "loading",
        onClick: onApply,
      }}
    />
  );
}

function NamespaceField({
  id,
  label,
  value,
  error,
  helper,
  placeholder,
  fetchState,
  fetchError,
  busy,
  onFetch,
  required,
}: {
  id: string;
  label: string;
  value: string;
  error?: string;
  helper: string;
  placeholder: string;
  fetchState: FeedbackState;
  fetchError: string;
  busy: boolean;
  onFetch: () => void;
  required?: boolean;
}) {
  const buttonLabel = namespaceFetchButtonLabel(fetchState);

  return (
    <InputActionField
      id={id}
      label={label}
      value={value}
      placeholder={placeholder}
      helper={helper}
      error={error}
      // ネームスペースは短い値（20 文字前後）なので md。取得の操作は欄のすぐ右（#1891）。
      width="md"
      actionError={
        fetchState === "error"
          ? fetchError || t("settings.oci.actions.namespaceFetchFailed")
          : undefined
      }
      readOnly
      required={required}
      inputClassName="text-fg"
      action={{
        label: buttonLabel,
        ariaLabel: `${label}: ${buttonLabel}`,
        icon: RefreshCw,
        loading: fetchState === "loading",
        disabled: busy && fetchState !== "loading",
        onClick: onFetch,
      }}
    />
  );
}

function PrivateKeyDropzoneField({
  disabled,
  id,
  label,
  value,
  error,
  fileState,
  fileMessage,
  keyFileExists,
  onFileChange,
  required,
}: {
  disabled?: boolean;
  id: string;
  label: string;
  value: string;
  error?: string;
  fileState: FeedbackState;
  fileMessage: string;
  keyFileExists: boolean | null;
  onFileChange: (file: File | undefined) => void | Promise<void>;
  required?: boolean;
}) {
  const statusId = `${id}-status`;
  const warningId = `${id}-warning`;
  const isConfigured = keyFileExists === true || fileState === "success";
  const warning =
    keyFileExists === false && fileState !== "success" ? t("settings.oci.keyFile.missing") : "";
  const statusMessage =
    fileState === "success"
      ? t("settings.oci.privateKey.loaded")
      : keyFileExists === true
        ? t("settings.oci.privateKey.configuredOnServer")
        : "";
  const helper = isConfigured
    ? t("settings.oci.privateKey.helpConfigured")
    : t("settings.oci.privateKey.helpUpload");
  return (
    <div id={id} className="space-y-2">
      <FileDropzone
        disabled={disabled}
        label={label}
        ariaLabel={t("settings.oci.keyFileInput.aria")}
        accept=".pem,.key"
        formatLabel=".PEM / .KEY"
        selectedText={
          isConfigured ? t("settings.oci.privateKey.replaceCta") : ""
        }
        hint={helper}
        errorText={
          fileState === "error"
            ? fileMessage || t("settings.oci.validation.invalidKeyFile")
            : error
        }
        required={required}
        loading={fileState === "loading"}
        loadingText={t("settings.oci.actions.uploadingKeyFile")}
        dataTestId="oci-key-file-upload"
        onFiles={([file]) => void onFileChange(file)}
      />
      {statusMessage ? (
        <div id={statusId}>
          <FormStatus
            tone="success"
            message={statusMessage}
            className="text-xs"
          />
        </div>
      ) : null}
      {value ? (
        <p className="break-all text-xs leading-relaxed text-fg-muted">
          {t("settings.oci.privateKey.path", { path: value })}
        </p>
      ) : null}
      {warning ? (
        <p
          id={warningId}
          className="flex items-start gap-1.5 text-xs leading-relaxed text-warning-fg"
          role="status"
        >
          <span className="pr-icon-line">
            <AlertTriangle size={12} aria-hidden />
          </span>
          <span>{warning}</span>
        </p>
      ) : null}
    </div>
  );
}

/** 選んで入れる欄（選択・ファイル）。未入力の文言を「〇〇を選択してください。」にする（messaging.md §3.2.1。#541）。 */
const OCI_SELECT_FIELDS: ReadonlySet<OciSettingsField> = new Set([
  "region",
  "objectStorageRegion",
  "keyFile",
]);

const OCI_FIELD_LABEL_KEYS = {
  configFile: "settings.oci.field.configFile",
  configProfile: "settings.oci.field.configProfile",
  userOcid: "settings.oci.field.userOcid",
  fingerprint: "settings.oci.field.fingerprint",
  tenancyOcid: "settings.oci.field.tenancyOcid",
  keyFile: "settings.oci.field.keyFile",
  region: "settings.oci.field.region",
  objectStorageRegion: "settings.oci.field.objectStorageRegion",
  objectStorageNamespace: "settings.oci.field.objectStorageNamespace",
} as const satisfies Record<OciSettingsField, OciMessageKey>;

/** 未入力の文言（欄のラベルと同じ語で「〇〇を入力してください。」「〇〇を選択してください。」）。 */
function requiredText(field: OciSettingsField): string {
  const label = t(OCI_FIELD_LABEL_KEYS[field]);
  return t(
    OCI_SELECT_FIELDS.has(field)
      ? "settings.oci.validation.requiredSelect"
      : "settings.oci.validation.required",
    // 英数字で終わるラベル（「ユーザー OCID」）は、ほかの文言と同じく助詞の前に空白を入れる。
    { field: /[A-Za-z0-9]$/.test(label) ? `${label} ` : label }
  );
}

function errorText(code: OciValidationCode | undefined, field: OciSettingsField): string | undefined {
  if (!code) return undefined;
  if (code === "required") return requiredText(field);
  return t(validationMessageKey(code));
}

function validationMessageKey(code: Exclude<OciValidationCode, "required">): OciMessageKey {
  switch (code) {
    case "invalid_user_ocid":
      return "settings.oci.validation.invalidUserOcid";
    case "invalid_tenancy_ocid":
      return "settings.oci.validation.invalidTenancyOcid";
    case "invalid_fingerprint":
      return "settings.oci.validation.invalidFingerprint";
    case "invalid_profile":
      return "settings.oci.validation.invalidProfile";
  }
}

function fieldInGroup(
  fields: readonly OciSettingsField[],
  field: OciSettingsField
): boolean {
  return fields.includes(field);
}

function clearSectionErrors(
  current: OciValidationResult,
  fields: readonly OciSettingsField[]
): OciValidationResult {
  const next = { ...current };
  for (const field of fields) {
    delete next[field];
  }
  return next;
}

function validationErrorsForFields(
  draft: OciSettingsDraft,
  fields: readonly OciSettingsField[]
): OciValidationResult {
  const allErrors = validateOciSettingsDraft(draft);
  const sectionErrors: OciValidationResult = {};
  for (const field of fields) {
    if (allErrors[field]) {
      sectionErrors[field] = allErrors[field];
    }
  }
  return sectionErrors;
}

function hasValidationErrors(errors: OciValidationResult): boolean {
  return Object.keys(errors).length > 0;
}

function sameDraft(left: OciSettingsDraft, right: OciSettingsDraft): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}
