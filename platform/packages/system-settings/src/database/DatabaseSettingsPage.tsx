import {
  displayDateTimeFormat,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ErrorState,
  FormActionBar,
  FormGrid,
  FormStatus,
  PageBody,
  ProcessingIndicator,
  SecretField,
  SelectField,
  Skeleton,
  StatusBadge,
  TextField,
  TimedLoadingState,
  toast,
  type SelectFieldOption,
} from "@production-ready/ui";
import {
  CloudDownload,
  Database,
  PlugZap,
  Power,
  PowerOff,
  RefreshCw,
  Save,
  Server,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";

import {
  useSettingsDraftGuard,
  type DraftGuardMessages,
} from "../guards/useSettingsDraftGuard";
import { FileDropzone } from "../oci/FileDropzone";
import {
  SettingsTestResultPanel,
  toSettingsTestResultDetails,
} from "../oci/SettingsTestResultPanel";
import {
  useAdbInfo,
  useDatabaseSettings,
  useDownloadDatabaseWallet,
  useRevealDatabasePassword,
  useStartAdb,
  useStopAdb,
  useTestDatabaseSettings,
  useUpdateAdbSettings,
  useUpdateDatabaseSettings,
  useUploadDatabaseWallet,
  type DatabaseChangedHandler,
} from "./hooks";
import { t, type DatabaseMessageKey } from "./messages";
import type {
  AdbInfoData,
  DatabaseConnectionSecurity,
  DatabaseConnectionTestResult,
  DatabaseSettingsApi,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
} from "./types";

interface DatabaseSettingsForm {
  user: string;
  dsn: string;
  connectionSecurity: DatabaseConnectionSecurity;
  password: string;
  clearPassword: boolean;
  walletPassword: string;
  clearWalletPassword: boolean;
}

interface DatabaseSettingsFormErrors {
  user?: string;
  dsn?: string;
  password?: string;
  walletPassword?: string;
  wallet?: string;
}

type WalletDownloadSource = "auto" | "wallet-field" | "adb-refresh";
type AdbManagementOperation = "save" | "refresh" | "start" | "stop";

const EMPTY_FORM: DatabaseSettingsForm = {
  user: "",
  dsn: "",
  connectionSecurity: "wallet_mtls",
  password: "",
  clearPassword: false,
  walletPassword: "",
  clearWalletPassword: false,
};

function databaseConnectionSecurityOptions() {
  return [
    {
      value: "wallet_mtls",
      label: t("settings.database.connectionSecurity.walletMtlS"),
      description: t(
        "settings.database.connectionSecurity.walletMtlS.description",
      ),
    },
    {
      value: "walletless_tls",
      label: t("settings.database.connectionSecurity.walletlessTls"),
      description: t(
        "settings.database.connectionSecurity.walletlessTls.description",
      ),
    },
  ] satisfies SelectFieldOption<DatabaseConnectionSecurity>[];
}

export interface DatabaseSettingsPageProps {
  /** 製品の API 関数（GET / PATCH /api/settings/database ほか）。 */
  api: DatabaseSettingsApi;
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
  /** Walletless TLS を選べるようにする（backend の接続処理が対応している製品だけ）。 */
  connectionSecurity?: boolean;
  /** 接続設定・Wallet・ADB の起動を変えたあとに呼ぶ（製品の DB 接続状態の query を更新する）。 */
  onDatabaseChanged?: DatabaseChangedHandler;
  /** 読み込み中の表示（NL2SQL は経過時間付きの表示を渡す）。 */
  loadingFallback?: ReactNode;
  /** 離脱確認ダイアログの文言の上書き。 */
  draftGuardMessages?: Partial<DraftGuardMessages>;
  /** 接続設定の下に置く製品固有のカード（RAG のシステムテーブルなど）。 */
  children?: ReactNode;
}

/**
 * データベース設定（3製品共通。NL2SQL の画面を基準に移設。#108）。PageHeader は製品の route が描く。
 *
 * - ADB の情報取得・起動・停止、Oracle AI Database の接続設定、Wallet（アップロード / OCI から自動取得）
 * - 保存中・テスト中は入力を止め、未保存のまま離れようとすると確認する
 * - 背景の再取得では、利用者が編集していない項目だけを更新する
 */
export function DatabaseSettingsPage({
  api,
  errorMessage,
  connectionSecurity: connectionSecurityEnabled = false,
  onDatabaseChanged,
  loadingFallback,
  draftGuardMessages,
  children,
}: DatabaseSettingsPageProps) {
  const query = useDatabaseSettings(api);
  const save = useUpdateDatabaseSettings(api, onDatabaseChanged);
  const walletUpload = useUploadDatabaseWallet(api, onDatabaseChanged);
  const walletDownload = useDownloadDatabaseWallet(api, onDatabaseChanged);
  const passwordReveal = useRevealDatabasePassword(api);
  const test = useTestDatabaseSettings(api);
  const canRevealPassword = Boolean(api.revealDatabasePassword);
  const resetTest = test.reset;

  const [form, setForm] = useState<DatabaseSettingsForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<DatabaseSettingsFormErrors>({});
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [walletPasswordVisible, setWalletPasswordVisible] = useState(false);
  const [optimisticSettings, setOptimisticSettings] =
    useState<DatabaseSettingsData | null>(null);
  const [walletDownloadSource, setWalletDownloadSource] =
    useState<WalletDownloadSource | null>(null);

  const userRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const walletPasswordRef = useRef<HTMLInputElement>(null);
  const autoWalletAttemptedRef = useRef<Set<string>>(new Set());
  const resetPasswordReveal = passwordReveal.reset;

  const baseline = useRef(EMPTY_FORM);
  const operationBusy =
    save.isPending ||
    test.isPending ||
    walletUpload.isPending ||
    walletDownload.isPending ||
    passwordReveal.isPending;
  useSettingsDraftGuard(
    JSON.stringify(form) !== JSON.stringify(baseline.current),
    operationBusy,
    draftGuardMessages,
  );
  const acceptSettings = useCallback(
    (data: DatabaseSettingsData, saved = false) => {
      const next = formFromSettings(data);
      const previous = baseline.current;
      baseline.current = next;
      setForm((current) =>
        saved
          ? next
          : (Object.fromEntries(
              Object.keys(next).map((key) => {
                const field = key as keyof DatabaseSettingsForm;
                return [
                  field,
                  current[field] === previous[field]
                    ? next[field]
                    : current[field],
                ];
              }),
            ) as unknown as DatabaseSettingsForm),
      );
    },
    [],
  );

  useEffect(() => {
    if (query.data) {
      acceptSettings(query.data);
      setOptimisticSettings(null);
    }
  }, [acceptSettings, query.data]);

  const downloadWallet = walletDownload.mutate;
  const downloadWalletAsync = walletDownload.mutateAsync;
  const resetWalletDownload = walletDownload.reset;

  useEffect(() => {
    const settings = query.data;
    const adbOcid = settings?.adb_ocid.trim() ?? "";
    const usesWalletMtlS = settings?.connection_security !== "walletless_tls";
    if (!settings || settings.wallet_uploaded || !adbOcid) return;
    if (!usesWalletMtlS) return;
    if (autoWalletAttemptedRef.current.has(adbOcid)) return;

    autoWalletAttemptedRef.current.add(adbOcid);
    setWalletDownloadSource("auto");
    resetWalletDownload();
    downloadWallet();
  }, [downloadWallet, query.data, resetWalletDownload]);

  useEffect(() => {
    const result = walletDownload.data;
    if (!result) return;
    acceptSettings(result.settings);
    setOptimisticSettings(result.settings);
    resetTest();
    if (result.status === "downloaded") {
      toast.success(t("settings.database.wallet.autoDownload.success"));
    }
    setWalletDownloadSource(null);
  }, [acceptSettings, resetTest, walletDownload.data]);

  function updateForm(update: Partial<DatabaseSettingsForm>) {
    if ("password" in update || "clearPassword" in update)
      resetPasswordReveal();
    setForm((current) => ({ ...current, ...update }));
    setErrors((current) => clearChangedErrors(current, update));
    resetTest();
  }

  function updatePasswordClear(clear: boolean) {
    if (clear) setPasswordVisible(false);
    updateForm({ clearPassword: clear, password: clear ? "" : form.password });
  }

  function updateWalletPasswordClear(clear: boolean) {
    if (clear) setWalletPasswordVisible(false);
    updateForm({
      clearWalletPassword: clear,
      walletPassword: clear ? "" : form.walletPassword,
    });
  }

  async function togglePasswordVisible(settings: DatabaseSettingsData) {
    if (passwordVisible) {
      setPasswordVisible(false);
      return;
    }
    if (canRevealPassword && settings.has_password && !form.password) {
      try {
        const data = await passwordReveal.mutateAsync();
        updateForm({ password: data.password });
        setPasswordVisible(true);
      } catch {
        setPasswordVisible(false);
      }
      return;
    }
    setPasswordVisible(true);
  }

  function submit(settings: DatabaseSettingsData) {
    if (operationBusy) return;
    if (!validateForm(settings, true)) return;
    save.mutate(payloadFromForm(form, settings), {
      onSuccess: (data) => {
        acceptSettings(data, true);
        setPasswordVisible(false);
        setWalletPasswordVisible(false);
        resetPasswordReveal();
        setOptimisticSettings(data);
        setErrors({});
        toast.success(t("settings.database.actions.saved"));
      },
    });
  }

  function runTest(settings: DatabaseSettingsData) {
    if (operationBusy) return;
    if (!validateForm(settings, false)) return;
    test.reset();
    test.mutate(payloadFromForm(form, settings));
  }

  function validateForm(
    settings: DatabaseSettingsData,
    requirePassword: boolean,
  ) {
    const nextErrors: DatabaseSettingsFormErrors = {};
    if (!form.user.trim())
      nextErrors.user = t("settings.database.validation.userRequired");
    if (!form.dsn.trim())
      nextErrors.dsn = t(dsnRequiredMessageKey(form.connectionSecurity, settings.available_services));
    if (
      requirePassword &&
      !settings.has_password &&
      !form.password.trim() &&
      !form.clearPassword
    ) {
      nextErrors.password = t("settings.database.validation.passwordRequired");
    }
    setErrors(nextErrors);

    if (Object.keys(nextErrors).length > 0) {
      focusFirstInvalid(nextErrors, {
        user: userRef,
        password: passwordRef,
        walletPassword: walletPasswordRef,
      });
      return false;
    }
    return true;
  }

  function uploadWallet(file: File) {
    if (operationBusy) return;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setErrors((current) => ({
        ...current,
        wallet: t("settings.database.validation.invalidWalletZip"),
      }));
      return;
    }

    setErrors((current) => ({ ...current, wallet: undefined }));
    setWalletDownloadSource(null);
    resetWalletDownload();
    resetTest();
    walletUpload.mutate(file, {
      onSuccess: (data) => {
        acceptSettings(data);
        setOptimisticSettings(data);
        setPasswordVisible(false);
        setWalletPasswordVisible(false);
        toast.success(
          t("settings.database.actions.walletUploaded", {
            fileName: file.name,
          }),
        );
      },
    });
  }

  async function ensureWalletFromOci() {
    setWalletDownloadSource("adb-refresh");
    resetWalletDownload();
    resetTest();
    return await downloadWalletAsync();
  }

  const saveError =
    errorMessage?.(save.error) ?? t("settings.database.saveError");
  const walletUploadError =
    errorMessage?.(walletUpload.error) ??
    t("settings.database.walletUploadError");
  const walletDownloadError =
    errorMessage?.(walletDownload.error) ??
    t("settings.database.wallet.autoDownload.error");
  const passwordRevealError =
    errorMessage?.(passwordReveal.error) ??
    t("settings.database.secrets.revealError");
  const testResult = test.data;
  const adbWalletDownloadActive = walletDownloadSource === "adb-refresh";
  const walletFieldDownloadActive =
    walletDownloadSource === "auto" || walletDownloadSource === "wallet-field";

  if (query.isPending) {
    return (
      <PageBody wide>
        {loadingFallback ?? (
          // 読み込み中は経過時間付きの表示と、カードの形の Skeleton で覆う（AGENTS.md「読み込み中」。
          // NL2SQL が渡していた表示を既定にした）。
          <TimedLoadingState
            label={t("settings.database.loading")}
            operationKey="settings-database-load"
            placement="page"
            testId="settings-database-loading"
          >
            <Skeleton className="h-20 w-full rounded-lg" />
            <Skeleton className="h-[33rem] w-full rounded-lg" />
          </TimedLoadingState>
        )}
      </PageBody>
    );
  }

  if (query.isError) {
    return (
      <PageBody wide>
        <ErrorState
          message={
            errorMessage?.(query.error) ?? t("settings.database.loadError")
          }
          onRetry={() => void query.refetch()}
        />
      </PageBody>
    );
  }

  const settings = optimisticSettings ?? query.data;
  if (!settings) return null;

  return (
    <PageBody wide>
      {/* ページ全体を <fieldset disabled> で包まない。押したボタン（保存・接続テスト・Wallet・パスワードの表示）まで
          ネイティブの disabled になり、フォーカスが body へ外れるため（#355）。他の操作は部品ごとに disabled にし、
          押したボタンは loading（aria-disabled）でフォーカスを保つ（#835）。 */}
      <div aria-busy={operationBusy} className="min-w-0 space-y-6">
        <AdbManagementCard
          api={api}
          errorMessage={errorMessage}
          onDatabaseChanged={onDatabaseChanged}
          settings={settings}
          ensureWalletFromOci={ensureWalletFromOci}
          walletEnsureError={
            walletDownload.isError && adbWalletDownloadActive
              ? walletDownloadError
              : null
          }
          walletEnsurePending={
            walletDownload.isPending && adbWalletDownloadActive
          }
          externalBusy={operationBusy && !(walletDownload.isPending && adbWalletDownloadActive)}
        />

        <form
          onSubmit={(event) => {
            event.preventDefault();
            submit(settings);
          }}
        >
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Database size={16} className="text-accent-fg" aria-hidden />
                {t("settings.database.cardTitle")}
              </CardTitle>
              <CardDescription>
                {t("settings.database.cardDescription")}
              </CardDescription>
            </CardHeader>

            <CardContent className="space-y-5">
              {/* 接続情報は FormGrid（最大 60rem・置き場所 42rem 以上で 2 列）。意味のペア（ユーザー / パスワード、
                  接続方式 / Wallet ZIP、Wallet パスワード / サービス）を入力順のまま同じ行に置き、案内は全幅にする。
                  欄の幅は値の種類の段（#1891）。保存済みの値の削除は SecretField が入力欄の直下に出す。 */}
              <FormGrid>
                <TextField
                  id="oracle-user"
                  label={t("settings.database.field.dbUser")}
                  required
                  value={form.user}
                  ref={userRef}
                  disabled={operationBusy}
                  onValueChange={(value) => updateForm({ user: value })}
                  placeholder={t("settings.database.placeholder.dbUser")}
                  error={errors.user}
                  width="md"
                />
                <SecretField
                  id="oracle-password"
                  ref={passwordRef}
                  label={t("settings.database.field.dbPassword")}
                  required={!settings.has_password}
                  value={form.password}
                  disabled={operationBusy}
                  onValueChange={(value) => updateForm({ password: value })}
                  visible={passwordVisible}
                  onVisibleChange={() => void togglePasswordVisible(settings)}
                  revealPending={passwordReveal.isPending}
                  revealPendingLabel={t(
                    "settings.database.secrets.revealingPassword",
                  )}
                  revealError={
                    passwordReveal.isError ? passwordRevealError : null
                  }
                  hasSavedSecret={settings.has_password}
                  savedLabel={t("settings.database.secrets.saved")}
                  notSetLabel={t("settings.database.secrets.notSet")}
                  showLabel={t("settings.database.secrets.show")}
                  hideLabel={t("settings.database.secrets.hide")}
                  placeholder={
                    settings.has_password
                      ? t("settings.database.placeholder.passwordSaved")
                      : t("settings.database.placeholder.password")
                  }
                  helper={
                    settings.has_password
                      ? t("settings.database.helper.passwordSavedCompact")
                      : t("settings.database.helper.passwordRequired")
                  }
                  error={errors.password}
                  width="md"
                  clearOption={{
                    label: t("settings.database.secrets.clearPassword"),
                    checked: form.clearPassword,
                    onCheckedChange: updatePasswordClear,
                  }}
                />

                {connectionSecurityEnabled ? (
                  <SelectField<DatabaseConnectionSecurity>
                    id="oracle-connection-security"
                    label={t("settings.database.field.connectionSecurity")}
                    value={form.connectionSecurity}
                    options={databaseConnectionSecurityOptions()}
                    disabled={operationBusy}
                    onValueChange={(value) =>
                      updateForm({
                        connectionSecurity: value,
                        ...(value === "walletless_tls"
                          ? {
                              walletPassword: "",
                              clearWalletPassword: false,
                            }
                          : {}),
                      })
                    }
                    helper={t(
                      `settings.database.connectionSecurity.${form.connectionSecurity}.helper`,
                    )}
                    width="md"
                  />
                ) : null}

                {form.connectionSecurity === "wallet_mtls" ? (
                  <>
                    <div className="min-w-0">
                      <WalletUploadField
                        // アップロード中はアップロードの欄が loading でフォーカスを保つ（#835）。
                        disabled={operationBusy && !walletUpload.isPending}
                        settings={settings}
                        uploadPending={walletUpload.isPending}
                        autoDownloadPending={
                          walletDownload.isPending && walletFieldDownloadActive
                        }
                        autoDownloadError={
                          walletDownload.isError && walletFieldDownloadActive
                            ? walletDownloadError
                            : null
                        }
                        canAutoDownload={Boolean(settings.adb_ocid.trim())}
                        uploadError={
                          walletUpload.isError ? walletUploadError : null
                        }
                        validationError={errors.wallet}
                        onUpload={uploadWallet}
                        onRetryDownload={() => {
                          setWalletDownloadSource("wallet-field");
                          resetWalletDownload();
                          downloadWallet();
                        }}
                      />
                    </div>

                    <SecretField
                      id="oracle-wallet-password"
                      ref={walletPasswordRef}
                      label={t("settings.database.field.walletPassword")}
                      value={form.walletPassword}
                      disabled={operationBusy}
                      onValueChange={(value) =>
                        updateForm({ walletPassword: value })
                      }
                      visible={walletPasswordVisible}
                      onVisibleChange={setWalletPasswordVisible}
                      hasSavedSecret={settings.has_wallet_password}
                      savedLabel={t("settings.database.secrets.saved")}
                      notSetLabel={t("settings.database.secrets.notSet")}
                      showLabel={t("settings.database.secrets.showWalletPassword")}
                      hideLabel={t("settings.database.secrets.hideWalletPassword")}
                      placeholder={
                        settings.has_wallet_password
                          ? t("settings.database.placeholder.passwordSaved")
                          : t("settings.database.placeholder.secret")
                      }
                      helper={
                        settings.has_wallet_password
                          ? t("settings.database.helper.walletPasswordSaved")
                          : t("settings.database.helper.walletPasswordEmpty")
                      }
                      error={errors.walletPassword}
                      width="md"
                      clearOption={{
                        label: t(
                          "settings.database.secrets.clearWalletPassword",
                        ),
                        checked: form.clearWalletPassword,
                        onCheckedChange: updateWalletPasswordClear,
                      }}
                    />

                    <WalletServiceField
                      disabled={operationBusy}
                      value={form.dsn}
                      onChange={(value) => updateForm({ dsn: value })}
                      services={settings.available_services}
                      connectionSecurity={form.connectionSecurity}
                      error={errors.dsn}
                    />

                  </>
                ) : (
                  <>
                    <WalletServiceField
                      disabled={operationBusy}
                      value={form.dsn}
                      onChange={(value) => updateForm({ dsn: value })}
                      services={settings.available_services}
                      connectionSecurity={form.connectionSecurity}
                      error={errors.dsn}
                    />
                    <FormStatus
                      tone="info"
                      className="text-xs lg:col-span-full"
                      message={t(
                        "settings.database.walletlessTls.walletSkipped",
                      )}
                    />
                  </>
                )}
              </FormGrid>

              <FormActionBar
                ariaLabel={t("settings.database.actions.label")}
                primaryActions={[
                  {
                    id: "save",
                    type: "submit",
                    label: t("settings.database.actions.saveDb"),
                    icon: Save,
                    loading: save.isPending,
                    // 押したボタンはネイティブの disabled にせず loading（aria-disabled）でフォーカスを保つ（#355 / #835）。
                    disabled: operationBusy && !save.isPending,
                  },
                ]}
                secondaryActions={[
                  {
                    id: "test",
                    label: t("settings.database.actions.testDb"),
                    icon: PlugZap,
                    loading: test.isPending,
                    disabled: operationBusy && !test.isPending,
                    onClick: () => runTest(settings),
                  },
                ]}
                status={
                  save.isError ? (
                    <FormStatus tone="danger" message={saveError} />
                  ) : null
                }
              />

              {test.isPending ? (
                // DB への接続確認は wallet の読み込みと接続を伴い数秒かかる。
                // スピナーは接続テストのボタンが担う（messaging.md §3.7）。
                <ProcessingIndicator
                  active
                  label={t("settings.database.test.running")}
                  operationKey="database-test"
                  placement="action"
                  activityIcon="none"
                  testId="settings-database-test-processing"
                />
              ) : null}

              <DatabaseTestResultPanel
                result={testResult}
                error={test.error}
                errorMessage={errorMessage}
              />

              <p className="text-xs leading-relaxed text-fg-muted">
                {t("settings.database.hint")}
              </p>
            </CardContent>
          </Card>
        </form>

        {children ? (
          // 製品が足すカードには押したボタンが無いので、処理中は fieldset でまとめて無効にする。
          <fieldset disabled={operationBusy} className="min-w-0 space-y-6">
            {children}
          </fieldset>
        ) : null}
      </div>
    </PageBody>
  );
}

interface AdbOperationLogEntry {
  status: AdbInfoData["status"];
  message: string;
  timestamp: string;
}

/**
 * Autonomous Database をサポートするリージョン（Terraform の stack の `region` の validation と同じ。#660）。
 * us-chicago-1 はサポートしない。OCI 認証設定のリージョンの候補とは別の一覧。
 */
const ADB_REGION_OPTIONS = [
  { value: "ap-tokyo-1", label: "ap-tokyo-1" },
  { value: "ap-osaka-1", label: "ap-osaka-1" },
] satisfies SelectFieldOption<string>[];

const ADB_DEFAULT_REGION = "ap-osaka-1";

const ADB_LIFECYCLE_LABEL_KEYS: Record<string, DatabaseMessageKey> = {
  AVAILABLE: "settings.adb.lifecycle.AVAILABLE",
  STARTING: "settings.adb.lifecycle.STARTING",
  STOPPING: "settings.adb.lifecycle.STOPPING",
  STOPPED: "settings.adb.lifecycle.STOPPED",
  UNAVAILABLE: "settings.adb.lifecycle.UNAVAILABLE",
  PROVISIONING: "settings.adb.lifecycle.PROVISIONING",
  TERMINATING: "settings.adb.lifecycle.TERMINATING",
  TERMINATED: "settings.adb.lifecycle.TERMINATED",
  FAILED: "settings.adb.lifecycle.FAILED",
  UPDATING: "settings.adb.lifecycle.UPDATING",
  RESTORING: "settings.adb.lifecycle.RESTORING",
  BACKUP_IN_PROGRESS: "settings.adb.lifecycle.BACKUP_IN_PROGRESS",
  MAINTENANCE_IN_PROGRESS: "settings.adb.lifecycle.MAINTENANCE_IN_PROGRESS",
  ROLE_CHANGE_IN_PROGRESS: "settings.adb.lifecycle.ROLE_CHANGE_IN_PROGRESS",
  UPGRADING: "settings.adb.lifecycle.UPGRADING",
  INACCESSIBLE: "settings.adb.lifecycle.INACCESSIBLE",
  STANDBY: "settings.adb.lifecycle.STANDBY",
};

/** Autonomous Database の情報取得・起動・停止を行う運用パネル。 */
function AdbManagementCard({
  api,
  errorMessage,
  onDatabaseChanged,
  settings,
  ensureWalletFromOci,
  walletEnsureError,
  walletEnsurePending,
  externalBusy,
}: {
  api: DatabaseSettingsApi;
  errorMessage?: (error: unknown) => string | undefined;
  onDatabaseChanged?: DatabaseChangedHandler;
  settings: DatabaseSettingsData;
  ensureWalletFromOci: () => Promise<unknown>;
  walletEnsureError: string | null;
  walletEnsurePending: boolean;
  /** DB 接続のカードの処理中（保存・接続テスト・Wallet・パスワードの表示）。このカードの操作を無効にする。 */
  externalBusy: boolean;
}) {
  const infoQuery = useAdbInfo(api);
  const saveSettings = useUpdateAdbSettings(api);
  const start = useStartAdb(api, onDatabaseChanged);
  const stop = useStopAdb(api);

  // ADB OCID は platform/.env を正本とする読み取り専用値。
  const ocid = settings.adb_ocid;
  const [region, setRegion] = useState(settings.region || ADB_DEFAULT_REGION);
  const [log, setLog] = useState<AdbOperationLogEntry[]>([]);
  const [refreshAttemptedWallet, setRefreshAttemptedWallet] = useState(false);
  const [activeOperation, setActiveOperation] =
    useState<AdbManagementOperation | null>(null);

  useEffect(() => {
    setRegion(settings.region || ADB_DEFAULT_REGION);
    setRefreshAttemptedWallet(false);
  }, [settings.region]);

  const regionSupported = ADB_REGION_OPTIONS.some(
    (option) => option.value === region,
  );
  const info = infoQuery.data;
  const lifecycle = info?.lifecycle_state ?? null;
  const canStart = lifecycle === "STOPPED" || lifecycle === "UNAVAILABLE";
  const canStop = lifecycle === "AVAILABLE";
  const showInfoPanel = Boolean(
    info && (info.lifecycle_state || info.status !== "success"),
  );
  const refreshWalletPending = refreshAttemptedWallet && walletEnsurePending;
  // 「保存」と「情報を再取得」は同じ保存の mutation と Wallet の取得を使うが、スピナーは
  // 押したボタンだけが出す。もう一方は下の busy で disabled にするだけ（buttons.md §8。#819）。
  const saveOrRefreshPending = saveSettings.isPending || refreshWalletPending;
  const saveButtonLoading = activeOperation === "save" && saveOrRefreshPending;
  const refreshButtonLoading =
    activeOperation === "refresh" && saveOrRefreshPending;
  // 遷移中は useAdbInfo が背景ポーリングするため、その isFetching で操作ボタンを
  // 無効化しない(4 秒ごとのちらつき/無効化を避ける)。明示的な操作の最中だけ busy。
  const busy =
    activeOperation !== null ||
    saveSettings.isPending ||
    start.isPending ||
    stop.isPending ||
    walletEnsurePending ||
    externalBusy;
  // 押したボタンは busy でも disabled にしない（loading の aria-disabled でフォーカスを保つ。#355 / #835）。
  const startPressed =
    activeOperation === "start" && (saveSettings.isPending || start.isPending);
  const stopPressed =
    activeOperation === "stop" && (saveSettings.isPending || stop.isPending);

  function appendLog(result: AdbInfoData) {
    setLog((current) =>
      [
        {
          status: result.status,
          message: result.message,
          timestamp: formatDateTime(new Date().toISOString()),
        },
        ...current,
      ].slice(0, 3),
    );
  }

  async function persist(): Promise<AdbInfoData | null> {
    if (!ocid.trim()) return null;
    try {
      return await saveSettings.mutateAsync({
        adb_ocid: ocid.trim(),
        region: region.trim(),
      });
    } catch {
      return null;
    }
  }

  async function handleRefresh(
    operation: Extract<AdbManagementOperation, "save" | "refresh">,
  ) {
    setActiveOperation(operation);
    try {
      const result = await persist();
      if (!result) return;
      appendLog(result);
      setRefreshAttemptedWallet(true);
      try {
        await ensureWalletFromOci();
      } catch {
        /* Wallet 取得エラーは ADB 操作フィードバックの FormStatus が担う */
      }
    } finally {
      setActiveOperation(null);
    }
  }

  async function handleStart() {
    setActiveOperation("start");
    try {
      if (!(await persist())) return;
      const result = await start.mutateAsync();
      appendLog(result);
    } catch {
      /* mutation error surface は下部の FormStatus が担う */
    } finally {
      setActiveOperation(null);
    }
  }

  async function handleStop() {
    setActiveOperation("stop");
    try {
      if (!(await persist())) return;
      const result = await stop.mutateAsync();
      appendLog(result);
    } catch {
      /* mutation error surface は下部の FormStatus が担う */
    } finally {
      setActiveOperation(null);
    }
  }

  const startButtonLoading = startPressed || lifecycle === "STARTING";
  const stopButtonLoading = stopPressed || lifecycle === "STOPPING";

  // 押した操作の説明を優先する（STARTING / STOPPING の間に「情報を再取得」を押したときなど）。
  const adbProcessingLabel =
    saveButtonLoading || refreshButtonLoading
      ? t("settings.adb.processing.refresh")
      : startButtonLoading
        ? t("settings.adb.processing.start")
        : stopButtonLoading
          ? t("settings.adb.processing.stop")
          : null;

  const apiMessage = (error: unknown) =>
    error ? errorMessage?.(error) : undefined;
  const actionError =
    apiMessage(saveSettings.error) ??
    apiMessage(start.error) ??
    apiMessage(stop.error) ??
    (refreshAttemptedWallet && walletEnsureError
      ? walletEnsureError
      : start.isError || stop.isError || saveSettings.isError
        ? t("settings.adb.notify.actionFailed")
        : null);
  const infoError =
    apiMessage(infoQuery.error) ??
    (infoQuery.isError ? t("settings.adb.notify.infoFailed") : null);

  return (
    <Card id="adb-management" className="scroll-mt-4">
      <CardHeader>
        {/* 見出しの行の右端に、カード全体を対象にする「情報を再取得」を置く。 */}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <CardTitle className="flex flex-wrap items-center gap-2">
              <Server size={16} className="text-accent-fg" aria-hidden />
              {t("settings.adb.title")}
              {/* ADB の稼働状態は見出しの StatusBadge で出す。常設の success の面にしない（messaging.md §10。#705）。 */}
              {showInfoPanel && info?.lifecycle_state ? (
                <StatusBadge
                  variant={ADB_LIFECYCLE_VARIANT[adbLifecycleTone(info.lifecycle_state)]}
                  label={`${t("settings.adb.operational.lifecycle")}: ${adbLifecycleLabel(info.lifecycle_state)}`}
                />
              ) : null}
            </CardTitle>
            <CardDescription>{t("settings.adb.description")}</CardDescription>
          </div>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            loading={refreshButtonLoading}
            disabled={busy && !refreshButtonLoading}
            onClick={() => void handleRefresh("refresh")}
            icon={RefreshCw}
          >
            {t("settings.adb.action.refresh")}
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-5">

        {/* リージョンは短い値（sm）、OCID は長い値なので全幅（FormGrid の最大 60rem まで。#1891）。 */}
        <FormGrid>
          <SelectField
            id="adb-region"
            label={t("settings.adb.field.region")}
            value={region}
            options={ADB_REGION_OPTIONS}
            onValueChange={setRegion}
            disabled={busy}
            width="sm"
            helper={
              // 候補に無い保存値（以前の候補の us-chicago-1 など）はそのまま出し、選び直すよう案内する（#660）。
              regionSupported
                ? undefined
                : t("settings.adb.helper.regionUnsupported", { region })
            }
          />
          <TextField
            id="adb-ocid"
            label={t("settings.adb.field.ocid")}
            value={ocid}
            readOnly
            width="full"
            className="col-span-full"
            placeholder={t("settings.adb.placeholder.ocidEmpty")}
            helper={
              // 環境変数名（PLATFORM_ORACLE_ADB_OCID）は長い 1 語なので、375px でも折り返せるようにする。
              <span className="pr-break-anywhere">
                {t("settings.adb.helper.ocidReadonly")}
              </span>
            }
          />
        </FormGrid>

        <FormActionBar
          ariaLabel={t("settings.adb.actions.label")}
          primaryActions={[
            {
              id: "save",
              label: t("settings.database.actions.save"),
              icon: Save,
              loading: saveButtonLoading,
              disabled: (busy && !saveButtonLoading) || !ocid.trim(),
              onClick: () => void handleRefresh("save"),
            },
          ]}
          secondaryActions={[
            {
              id: "start",
              label: t("settings.adb.action.start"),
              icon: Power,
              loading: startButtonLoading,
              disabled: startPressed ? false : busy || !ocid.trim() || !canStart,
              onClick: () => void handleStart(),
            },
            {
              id: "stop",
              label: t("settings.adb.action.stop"),
              icon: PowerOff,
              loading: stopButtonLoading,
              disabled: stopPressed ? false : busy || !ocid.trim() || !canStop,
              onClick: () => void handleStop(),
            },
          ]}
          status={
            actionError ? (
              <FormStatus
                tone="danger"
                className="text-xs"
                message={actionError}
              />
            ) : null
          }
        />

        {adbProcessingLabel ? (
          // ADB の起動・停止は OCI 側の遷移（STARTING / STOPPING）が終わるまで数分かかる。
          // スピナーは操作したボタンが担う（messaging.md §3.7）。
          <ProcessingIndicator
            active
            label={adbProcessingLabel}
            operationKey={adbProcessingLabel}
            placement="action"
            activityIcon="none"
            testId="settings-adb-processing"
          />
        ) : null}

        {infoQuery.isPending ? (
          // ADB の状態（見出しの StatusBadge）と起動・停止の可否は OCI から取得する。取得の間は
          // 経過時間付きで示す（起動・停止が押せない理由を空白にしない。AGENTS.md「読み込み中」）。
          // 下を情報のパネルの形の Skeleton で覆い、届いたときに下の内容を押し下げない（#2510）。
          <TimedLoadingState
            label={t("settings.adb.loading")}
            operationKey="settings-adb-info"
            framed={false}
            testId="settings-adb-loading"
          >
            <Skeleton className="h-24 w-full" />
          </TimedLoadingState>
        ) : null}

        {infoError ? (
          <FormStatus tone="danger" className="text-xs" message={infoError} />
        ) : null}

        {showInfoPanel && info ? <AdbInfoPanel info={info} /> : null}

        {log.length > 0 ? <AdbOperationLog entries={log} /> : null}
      </CardContent>
    </Card>
  );
}

function AdbInfoPanel({ info }: { info: AdbInfoData }) {
  const known = info.status === "success" || info.status === "accepted";
  const messageTone = info.status === "error" ? "danger" : "warning";
  // 自己完結したステータスバーを余分なパネルで囲まない。
  return (
    <div className="space-y-2">
      {!known ? (
        <FormStatus
          tone={messageTone}
          className="text-xs"
          message={info.message}
        />
      ) : null}
    </div>
  );
}

function AdbOperationLog({ entries }: { entries: AdbOperationLogEntry[] }) {
  return (
    <div className="space-y-2">
      <span className="block text-sm font-medium text-fg">
        {t("settings.adb.operationResult.title")}
      </span>
      <ul className="space-y-1.5">
        {entries.map((entry, index) => (
          <li
            key={`${entry.timestamp}-${index}`}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-surface px-3 py-2 text-xs"
          >
            <span className="text-fg-muted">{entry.timestamp}</span>
            {/* 操作の結果の状態は StatusBadge（アイコン + 日本語の状態）で出す。API の enum を画面に出さない（messaging.md §10。#722）。 */}
            <StatusBadge
              variant={ADB_OPERATION_STATUS_VARIANT[entry.status] ?? "warning"}
              label={adbOperationStatusLabel(entry.status)}
            />
            <span className="text-fg">{entry.message}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const ADB_OPERATION_STATUS_VARIANT: Record<
  AdbInfoData["status"],
  "success" | "info" | "warning" | "danger"
> = {
  success: "success",
  accepted: "success",
  already_available: "info",
  already_stopped: "info",
  not_configured: "warning",
  cannot_start: "warning",
  cannot_stop: "warning",
  error: "danger",
};

const ADB_OPERATION_STATUS_LABEL_KEYS: Record<
  AdbInfoData["status"],
  DatabaseMessageKey
> = {
  success: "settings.adb.operationStatus.success",
  accepted: "settings.adb.operationStatus.accepted",
  already_available: "settings.adb.operationStatus.already_available",
  already_stopped: "settings.adb.operationStatus.already_stopped",
  not_configured: "settings.adb.operationStatus.not_configured",
  cannot_start: "settings.adb.operationStatus.cannot_start",
  cannot_stop: "settings.adb.operationStatus.cannot_stop",
  error: "settings.adb.operationStatus.error",
};

/** 想定外の値（API の追加など）は値をそのまま出す。 */
function adbOperationStatusLabel(status: AdbInfoData["status"]): string {
  const key = ADB_OPERATION_STATUS_LABEL_KEYS[status];
  return key ? t(key) : status;
}

function adbLifecycleLabel(state: string | null): string {
  if (!state) return t("settings.adb.statusUnknown");
  const key = ADB_LIFECYCLE_LABEL_KEYS[state];
  return key ? t(key) : state;
}

const ADB_LIFECYCLE_VARIANT = {
  ok: "success",
  danger: "danger",
  warning: "warning",
  muted: "neutral",
} as const;

function adbLifecycleTone(
  state: string | null,
): "ok" | "danger" | "warning" | "muted" {
  if (state === "AVAILABLE") return "ok";
  if (state === "FAILED" || state === "TERMINATED" || state === "INACCESSIBLE")
    return "danger";
  if (state === "STOPPED" || state === "UNAVAILABLE" || state === "STANDBY")
    return "muted";
  if (!state) return "muted";
  return "warning";
}

function WalletServiceField({
  value,
  services,
  connectionSecurity,
  error,
  disabled = false,
  onChange,
}: {
  value: string;
  disabled?: boolean;
  services: string[];
  connectionSecurity: DatabaseConnectionSecurity;
  error?: string;
  onChange: (value: string) => void;
}) {
  const usesWalletMtlS = connectionSecurity === "wallet_mtls";
  const serviceOptions = (
    usesWalletMtlS
      ? services.map((service) => ({
          value: service,
          label: service,
        }))
      : []
  ) satisfies SelectFieldOption<string>[];

  if (serviceOptions.length > 0) {
    return (
      <SelectField
        id={WALLET_SERVICE_FIELD_ID}
        label={t("settings.database.field.serviceDsn")}
        value={value.trim()}
        options={serviceOptions}
        onValueChange={onChange}
        disabled={disabled}
        required
        error={error}
        placeholder={t("settings.database.placeholder.serviceDsn")}
        helper={t("settings.database.helper.dsnService")}
        width="md"
      />
    );
  }

  return (
    <TextField
      id={WALLET_SERVICE_FIELD_ID}
      label={
        usesWalletMtlS
          ? t("settings.database.field.serviceDsn")
          : t("settings.database.field.directDsn")
      }
      required
      value={value}
      disabled={disabled}
      onValueChange={onChange}
      placeholder={
        usesWalletMtlS
          ? t("settings.database.placeholder.serviceDsnManual")
          : t("settings.database.placeholder.directDsn")
      }
      helper={
        usesWalletMtlS
          ? t("settings.database.helper.dsnServiceManual")
          : t("settings.database.helper.directDsn")
      }
      error={error}
      // Wallet のサービス名は短い名前（md）、Walletless TLS の接続文字列（host:port/service）は長いので全幅（#1891）。
      width={usesWalletMtlS ? "md" : "full"}
      className={usesWalletMtlS ? undefined : "col-span-full"}
    />
  );
}

function WalletUploadField({
  disabled,
  settings,
  uploadPending,
  autoDownloadPending,
  autoDownloadError,
  canAutoDownload,
  uploadError,
  validationError,
  onUpload,
  onRetryDownload,
}: {
  disabled?: boolean;
  settings: DatabaseSettingsData;
  uploadPending: boolean;
  autoDownloadPending: boolean;
  autoDownloadError: string | null;
  canAutoDownload: boolean;
  uploadError: string | null;
  validationError?: string;
  onUpload: (file: File) => void;
  onRetryDownload: () => void;
}) {
  return (
    <div className="space-y-2">
      <FileDropzone
        label={t("settings.database.wallet.title")}
        ariaLabel={t("settings.database.walletInput.aria")}
        accept=".zip,application/zip,application/x-zip-compressed,application/octet-stream"
        formatLabel=".ZIP"
        selectedText={
          settings.wallet_uploaded
            ? t("settings.database.wallet.replaceCta")
            : ""
        }
        hint={t("settings.database.wallet.help")}
        errorText={validationError}
        loading={uploadPending}
        loadingText={t("settings.database.actions.uploadingWallet")}
        disabled={disabled || autoDownloadPending}
        dataTestId="oracle-wallet-upload"
        onFiles={([file]) => onUpload(file)}
      />

      {autoDownloadPending ? (
        <FormStatus
          tone="info"
          className="text-xs"
          message={t("settings.database.wallet.autoDownload.pending")}
        />
      ) : null}
      {!settings.wallet_uploaded && !canAutoDownload ? (
        <FormStatus
          tone="info"
          className="text-xs"
          message={t("settings.database.wallet.autoDownload.missingOcid")}
        />
      ) : null}
      {autoDownloadError ? (
        <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center">
          <FormStatus
            tone="danger"
            className="min-w-0 flex-1 text-xs"
            message={autoDownloadError}
          />
          <Button
            type="button"
            size="md"
            variant="secondary"
            className="w-full shrink-0 sm:w-auto"
            aria-label={t("settings.database.wallet.autoDownload.retryAria")}
            onClick={onRetryDownload}
            icon={CloudDownload}
          >
            {t("settings.database.wallet.autoDownload.retry")}
          </Button>
        </div>
      ) : null}

      {uploadError ? (
        <FormStatus tone="danger" className="text-xs" message={uploadError} />
      ) : null}

      <div className="space-y-1 text-xs leading-relaxed text-fg-muted">
        {/* Wallet の状態は色だけで表さず、StatusBadge（アイコン + 文言）で出す（messaging.md §10。#722）。 */}
        <p className="flex flex-wrap items-center gap-2">
          <span>{t("settings.database.wallet.status")}:</span>
          <StatusBadge
            variant={settings.wallet_uploaded ? "success" : "warning"}
            label={
              settings.wallet_uploaded
                ? t("settings.database.wallet.statusConfigured")
                : t("settings.database.wallet.statusNotConfigured")
            }
          />
        </p>
        <p>
          <span>{t("settings.database.wallet.location")}:</span>{" "}
          <span className="break-all text-fg">
            {settings.wallet_dir || "—"}
          </span>
        </p>
      </div>
    </div>
  );
}

function DatabaseTestResultPanel({
  result,
  error,
  errorMessage,
}: {
  result?: DatabaseConnectionTestResult;
  error: Error | null;
  errorMessage?: (error: unknown) => string | undefined;
}) {
  if (!result && !error) return null;

  if (error) {
    const apiMessage = errorMessage?.(error);
    const message =
      apiMessage !== undefined
        ? t("settings.database.test.apiError", { message: apiMessage })
        : t("settings.database.test.apiFailed");
    return (
      <SettingsTestResultPanel
        tone="danger"
        message={message}
        testId="settings-database-test-result"
      />
    );
  }

  if (!result) return null;
  const tone = result.status === "success" ? "success" : "danger";

  return (
    <SettingsTestResultPanel
      tone={tone}
      message={result.message}
      elapsedMs={result.elapsed_ms}
      checkedAt={formatDateTime(result.checked_at)}
      details={toSettingsTestResultDetails(result.details)}
      troubleshooting={result.troubleshooting}
      errorType={result.error_type}
      testId="settings-database-test-result"
    />
  );
}

function formFromSettings(
  settings: DatabaseSettingsData,
): DatabaseSettingsForm {
  return {
    user: settings.user,
    dsn: settings.dsn,
    connectionSecurity: settings.connection_security ?? "wallet_mtls",
    password: "",
    clearPassword: false,
    walletPassword: "",
    clearWalletPassword: false,
  };
}

function payloadFromForm(
  form: DatabaseSettingsForm,
  settings: DatabaseSettingsData,
): DatabaseSettingsUpdate {
  const payload: DatabaseSettingsUpdate = {
    user: form.user,
    dsn: form.dsn,
    connection_security: form.connectionSecurity,
    wallet_dir: settings.wallet_dir,
  };
  if (form.clearPassword) payload.clear_password = true;
  else if (form.password !== "") payload.password = form.password;
  if (form.connectionSecurity === "wallet_mtls") {
    if (form.clearWalletPassword) payload.clear_wallet_password = true;
    else if (form.walletPassword !== "")
      payload.wallet_password = form.walletPassword;
  }
  return payload;
}

function clearChangedErrors(
  errors: DatabaseSettingsFormErrors,
  update: Partial<DatabaseSettingsForm>,
): DatabaseSettingsFormErrors {
  const next = { ...errors };
  if ("user" in update) next.user = undefined;
  if ("dsn" in update) next.dsn = undefined;
  if ("password" in update || "clearPassword" in update)
    next.password = undefined;
  if ("walletPassword" in update || "clearWalletPassword" in update) {
    next.walletPassword = undefined;
  }
  return next;
}

function focusFirstInvalid(
  errors: DatabaseSettingsFormErrors,
  refs: {
    user: RefObject<HTMLInputElement | null>;
    password: RefObject<HTMLInputElement | null>;
    walletPassword: RefObject<HTMLInputElement | null>;
  },
) {
  if (errors.user) refs.user.current?.focus();
  else if (errors.password) refs.password.current?.focus();
  else if (errors.walletPassword) refs.walletPassword.current?.focus();
  // サービス名 / DSN は選択（SelectField）と入力（TextField）が入れ替わるので、共通の id で探す。
  else if (errors.dsn) document.getElementById(WALLET_SERVICE_FIELD_ID)?.focus();
}

/** サービス名 / DSN の欄の id（WalletServiceField の SelectField / TextField で共通）。 */
const WALLET_SERVICE_FIELD_ID = "oracle-wallet-service";

/**
 * サービス名 / DSN の未入力のエラー（#531）。WalletServiceField と同じ条件で、選択肢があれば「選択」、無ければ「入力」にする。
 */
function dsnRequiredMessageKey(
  connectionSecurity: DatabaseConnectionSecurity,
  services: string[],
) {
  if (connectionSecurity !== "wallet_mtls") return "settings.database.validation.directDsnRequired";
  return services.length > 0
    ? "settings.database.validation.serviceDsnSelectRequired"
    : "settings.database.validation.serviceDsnRequired";
}

// システムの設定の時間帯で書く（#3265）。
const dateTimeFormat = displayDateTimeFormat({
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

/** ISO 文字列を「MM/DD HH:mm」へ。未設定・無効値はダッシュ（NL2SQL の formatDateTime）。 */
function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return dateTimeFormat.format(date);
}
