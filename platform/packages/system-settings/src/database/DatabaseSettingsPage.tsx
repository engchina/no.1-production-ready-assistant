import {
  displayDateTimeFormat,
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
  Database,
  PlugZap,
  Save,
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
import { FileDropzone } from "../shared/FileDropzone";
import {
  SettingsTestResultPanel,
  toSettingsTestResultDetails,
} from "../shared/SettingsTestResultPanel";
import {
  useDatabaseSettings,
  useRevealDatabasePassword,
  useTestDatabaseSettings,
  useUpdateDatabaseSettings,
  useUploadDatabaseWallet,
  type DatabaseChangedHandler,
} from "./hooks";
import { t } from "./messages";
import type {
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
  /** 接続設定・Wallet を変えたあとに呼ぶ（製品の DB 接続状態の query を更新する）。 */
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
 * - Oracle AI Database の接続設定、Wallet（ZIP のアップロード。OCI からの自動取得は持たない。#11）、接続テスト
 * - DB の起動・停止はこの画面では扱わない（ADB Free コンテナは `docker start` / `docker stop`。#10）
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

  const userRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const walletPasswordRef = useRef<HTMLInputElement>(null);
  const resetPasswordReveal = passwordReveal.reset;

  const baseline = useRef(EMPTY_FORM);
  const operationBusy =
    save.isPending ||
    test.isPending ||
    walletUpload.isPending ||
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

  const saveError =
    errorMessage?.(save.error) ?? t("settings.database.saveError");
  const walletUploadError =
    errorMessage?.(walletUpload.error) ??
    t("settings.database.walletUploadError");
  const passwordRevealError =
    errorMessage?.(passwordReveal.error) ??
    t("settings.database.secrets.revealError");
  const testResult = test.data;

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
                        uploadError={
                          walletUpload.isError ? walletUploadError : null
                        }
                        validationError={errors.wallet}
                        onUpload={uploadWallet}
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
  uploadError,
  validationError,
  onUpload,
}: {
  disabled?: boolean;
  settings: DatabaseSettingsData;
  uploadPending: boolean;
  uploadError: string | null;
  validationError?: string;
  onUpload: (file: File) => void;
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
        disabled={disabled}
        dataTestId="oracle-wallet-upload"
        onFiles={([file]) => onUpload(file)}
      />

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
