import { KeyRound, RefreshCw, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import {
  Button,
  StatusBadge,
  Banner,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  FormStatus,
  ProcessingIndicator,
  Skeleton,
  SelectField,
  TimedLoadingState,
  type SelectFieldOption,
  ExecutionConfirmationField,
  useActionPending,
} from "@production-ready/ui";
import { ErrorState } from "@/components/StateViews";
import { ApiError, type SelectAiCredentialRegion } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import {
  useCreateSelectAiCredential,
  useSelectAiCredential,
} from "@/lib/queries";
import { cn } from "@/lib/utils";

const SELECT_AI_CREDENTIAL_CONFIRMATION = "ADMIN_EXECUTE";
const SELECT_AI_CREDENTIAL_REGION_OPTIONS = [
  { value: "us-chicago-1", label: "us-chicago-1" },
  { value: "ap-osaka-1", label: "ap-osaka-1" },
] satisfies SelectFieldOption<SelectAiCredentialRegion>[];

const SELECT_AI_MISSING_FIELD_KEYS: Record<string, I18nKey> = {
  config_file: "settings.database.selectAiCredential.missing.configFile",
  user: "settings.database.selectAiCredential.missing.user",
  tenancy: "settings.database.selectAiCredential.missing.tenancy",
  fingerprint: "settings.database.selectAiCredential.missing.fingerprint",
  key_file: "settings.database.selectAiCredential.missing.keyFile",
  key_file_permissions:
    "settings.database.selectAiCredential.missing.keyPermissions",
  private_key_encrypted:
    "settings.database.selectAiCredential.missing.keyEncrypted",
  private_key: "settings.database.selectAiCredential.missing.keyInvalid",
};

/** DBMS_CLOUD.CREATE_CREDENTIAL を管理者の明示操作だけで実行する運用カード。 */
export function SelectAiCredentialCard() {
  const status = useSelectAiCredential();
  const changeCredential = useCreateSelectAiCredential();
  const [region, setRegion] =
    useState<SelectAiCredentialRegion>("us-chicago-1");
  const [confirmation, setConfirmation] = useState("");
  const data = status.data;
  const busy =
    status.isFetching || status.isError || changeCredential.isPending;
  const confirmed = confirmation.trim() === SELECT_AI_CREDENTIAL_CONFIRMATION;

  // server 値・取得状態が変わったレンダーで、region と確認語を直す（effect で setState しない）。
  const regionChanged = useValuesChanged([data?.region]);
  if (regionChanged && data?.region) setRegion(data.region);
  const confirmationStale = useValuesChanged([
    status.isFetching,
    data?.schema_name,
    data?.exists,
    data?.oci_auth_ready,
  ]);
  if (confirmationStale) setConfirmation("");

  // 「再取得」を押した取り直しの間だけ回す。作成の後の invalidate・画面へ戻ったときの取り直しでは回さず、
  // 無効にするだけ（押したボタンだけがスピナーを出す。#819）。
  const manualRefresh = useActionPending();
  const refresh = () => {
    setConfirmation("");
    return manualRefresh.track(() => status.refetch());
  };

  const resetFeedback = () => {
    changeCredential.reset();
  };

  const execute = () => {
    if (busy || !data || !confirmed || !data.oci_auth_ready) return;
    setConfirmation("");
    changeCredential.mutate(
      {
        region,
        confirmation: SELECT_AI_CREDENTIAL_CONFIRMATION,
        recreate: data.exists,
      },
      { onSuccess: () => setConfirmation("") },
    );
  };

  const errorMessage =
    changeCredential.error instanceof ApiError
      ? changeCredential.error.message
      : t("settings.database.selectAiCredential.error.change");
  const statusError =
    status.error instanceof ApiError
      ? status.error.message
      : t("settings.database.selectAiCredential.error.load");
  const hasStatusError = Boolean(status.error);
  const missingLabels = (data?.missing_fields ?? []).map((field) =>
    t(
      SELECT_AI_MISSING_FIELD_KEYS[field] ??
        "settings.database.selectAiCredential.missing.unknown",
    ),
  );

  return (
    <Card
      id="select-ai-credential"
      className="min-w-0 max-w-full scroll-mt-24 rounded-md"
      aria-busy={status.isFetching || changeCredential.isPending}
      data-testid="select-ai-credential-card"
    >
      <CardHeader className="p-6 pb-0">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <KeyRound size={20} aria-hidden />
              <CardTitle className="text-base">
                {t("settings.database.selectAiCredential.title")}
              </CardTitle>
            </div>
            <p className="mt-2 text-sm leading-6 text-fg-muted">
              {t("settings.database.selectAiCredential.description")}
            </p>
          </div>
          {data ? (
            <StatusBadge
              variant={data.exists ? "success" : "neutral"}
              label={t(
                data.exists
                  ? "settings.database.selectAiCredential.status.created"
                  : "settings.database.selectAiCredential.status.notCreated",
              )}
            />
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-5 p-6">
        {status.isPending ? (
          // 読み込み中は経過時間と、要約・リージョン・確認語の行の形の Skeleton を出す（UX 契約 messaging.md §3.6）。
          <TimedLoadingState
            label={t("settings.database.selectAiCredential.loading")}
            operationKey="select-ai-credential-status"
            placement="panel"
            testId="select-ai-credential-loading"
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Skeleton className="h-20 w-full rounded-md" />
              <Skeleton className="h-20 w-full rounded-md" />
            </div>
            <Skeleton className="h-24 w-full rounded-md" />
          </TimedLoadingState>
        ) : !data ? (
          <ErrorState
            message={statusError}
            onRetry={() => void refresh()}
            retryLabel={t("common.retry")}
          />
        ) : (
          <>
            {hasStatusError ? (
              <Banner
                severity="danger"
                action={
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={manualRefresh.pending}
                    disabled={status.isFetching && !manualRefresh.pending}
                    onClick={() => void refresh()}
                    icon={RefreshCw}
                  >
                    {t("common.retry")}
                  </Button>
                }
              >
                {statusError}
              </Banner>
            ) : null}

            {/* 広い画面では Credential の要約とリージョン選択を同じ行に置き、カード幅を使う。 */}
            <div className="grid min-w-0 gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] lg:items-start lg:gap-6">
              <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
                <CredentialSummary
                  label={t("settings.database.selectAiCredential.field.name")}
                  value={data.credential_name}
                  mono
                />
                <CredentialSummary
                  label={t("settings.database.selectAiCredential.field.schema")}
                  value={data.schema_name || "-"}
                  mono
                />
              </dl>

              <fieldset
                disabled={status.isFetching || changeCredential.isPending}
                className="min-w-0"
              >
                <SelectField<SelectAiCredentialRegion>
                  id="select-ai-credential-region"
                  label={t("settings.database.selectAiCredential.field.region")}
                  value={region}
                  options={SELECT_AI_CREDENTIAL_REGION_OPTIONS}
                  onValueChange={(value) => {
                    setConfirmation("");
                    setRegion(value);
                    resetFeedback();
                  }}
                  helper={t(
                    "settings.database.selectAiCredential.field.regionHelper",
                  )}
                  // リージョンは短い選択（sm。#1891）。
                  width="sm"
                />
              </fieldset>
            </div>

            {data.oci_auth_ready ? (
              <FormStatus
                tone="success"
                message={t("settings.database.selectAiCredential.ociReady")}
              />
            ) : (
              <div className="space-y-3">
                <FormStatus
                  tone="warning"
                  message={t(
                    "settings.database.selectAiCredential.ociMissing",
                    {
                      fields:
                        missingLabels.join("、") ||
                        t(
                          "settings.database.selectAiCredential.missing.unknown",
                        ),
                    },
                  )}
                />
                {!hasStatusError ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={manualRefresh.pending}
                    disabled={status.isFetching && !manualRefresh.pending}
                    onClick={() => void refresh()}
                    icon={RefreshCw}
                  >
                    {t("settings.database.selectAiCredential.action.refresh")}
                  </Button>
                ) : null}
              </div>
            )}

            <ExecutionConfirmationField
              value={confirmation}
              onChange={(value) => {
                setConfirmation(value);
                resetFeedback();
              }}
              confirmed={confirmed}
              placeholder={SELECT_AI_CREDENTIAL_CONFIRMATION}
              expectedLabel={SELECT_AI_CREDENTIAL_CONFIRMATION}
              helper={t(
                data.exists
                  ? "settings.database.selectAiCredential.confirmation.recreateHelper"
                  : "settings.database.selectAiCredential.confirmation.createHelper",
                { phrase: SELECT_AI_CREDENTIAL_CONFIRMATION },
              )}
              disabled={busy || !data.oci_auth_ready}
              actions={
                <Button
                  type="button"
                  size="lg"
                  variant={data.exists ? "danger" : "primary"}
                  className="w-full sm:w-auto"
                  icon={data.exists ? RotateCcw : KeyRound}
                  loading={changeCredential.isPending}
                  disabled={busy || !confirmed || !data.oci_auth_ready}
                  onClick={() => void execute()}
                >
                  {t(
                    data.exists
                      ? "settings.database.selectAiCredential.action.recreate"
                      : "settings.database.selectAiCredential.action.create",
                  )}
                </Button>
              }
            />

            {changeCredential.isPending ? (
              <ProcessingIndicator
                active
                label={t(
                  data.exists
                    ? "settings.database.selectAiCredential.progress.recreating"
                    : "settings.database.selectAiCredential.progress.creating",
                )}
                operationKey="select-ai-credential"
                placement="action"
                activityIcon="none"
                testId="select-ai-credential-processing"
              />
            ) : null}
            {changeCredential.isError ? (
              <FormStatus tone="danger" message={errorMessage} />
            ) : null}
            {changeCredential.isSuccess ? (
              <div data-testid="select-ai-credential-success">
                <FormStatus
                  tone="success"
                  message={t("settings.database.selectAiCredential.success")}
                />
              </div>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function CredentialSummary({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-surface p-3">
      <dt className="text-xs font-medium text-fg-muted">{label}</dt>
      <dd
        className={cn(
          "mt-1 break-all text-sm font-semibold text-fg",
          mono && "font-mono",
        )}
      >
        {value}
      </dd>
    </div>
  );
}
