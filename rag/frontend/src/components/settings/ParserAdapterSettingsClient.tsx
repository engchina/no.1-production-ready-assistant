"use client";

import {
  PageBody,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Button,
  FormActionBar,
  FormStatus,
  ProcessingIndicator,
  TimedLoadingState,
  FormSkeleton,
  RoadmapBadge,
  StatusBadge,
  TextField,
  type FieldWidth,
} from "@production-ready/ui";
import {
  SettingsTestResultPanel,
  toSettingsTestResultDetails,
  type SettingsTestResultTone,
} from "@production-ready/system-settings";
import { useMemo, useState } from "react";
import {
  Plug,
  RotateCcw,
  Save,
} from "lucide-react";

import { ApiErrorState } from "@/components/StateViews";
import { useAuth } from "@/components/security/AuthProvider";
import {
  ServiceProfileBadge,
  ServiceStatusBadge,
  type DisplayRuntimeStatus,
} from "@/components/settings/ServicesManagementClient";
import {
  ApiError,
  type ExternalParserBackendName,
  type ExternalParserConnectionData,
  type ExternalParserConnectionStatus,
  type ExternalParserConnectionStatusData,
  type ParserAdapterBackend,
  type ParserAdapterBackendName,
  type ParserAdapterSettingsData,
  type ParserAdapterSettingsUpdate,
  type ParserServiceBackendData,
  type ParserServiceBackendName,
  type ServiceProfile,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { sortParserBackends } from "@/lib/parser-backend-order";
import { useLeaveGuard } from "@/lib/leave-guard";
import { t, type I18nKey } from "@/lib/i18n";
import {
  findParserCapability,
  formatSupportedExtensions,
  formatSupportedFormats,
} from "@/lib/parser-capabilities";
import {
  useParserAdapterSettings,
  useExternalParserStatus,
  useServiceStatusQueries,
  useUpdateParserAdapterSettings,
} from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { FeaturePromptDisclosure } from "./AnswerPromptEditor";
import { MineruLicenseNotice } from "./MineruLicenseNotice";
import { PostParseSettingsCard } from "./PostParseSettingsCard";

type ParserAdapterForm = {
  adapter_backend: ParserAdapterBackend;
  docling_enabled: boolean;
  unstructured_enabled: boolean;
  mineru_enabled: boolean;
  /** MinerU のライセンスの条件の確認（#3359）。確認するまで MinerU を選べない。 */
  mineru_license_acknowledged: boolean;
  dots_ocr_enabled: boolean;
  structured_records_jsonl_enabled: boolean;
  xml_tree_json_enabled: boolean;
  connections: Record<ExternalParserBackendName, ExternalParserConnectionForm>;
};
type ExternalParserConnectionForm = {
  endpoint: string;
  model: string;
  api_key: string;
  clear_api_key: boolean;
};
type ParserAdapterFlagField = Exclude<
  keyof ParserAdapterForm,
  "adapter_backend" | "connections" | "mineru_license_acknowledged"
>;
type ConnectionFieldErrors = Record<string, string>;

// 並びは使用エンジンのカードと同じ（Dots.OCR → MinerU。PARSER_BACKEND_ORDER。#3359）。
const EXTERNAL_BACKENDS: ExternalParserBackendName[] = ["dots_ocr", "mineru"];

/** 外部 GPU 接続の欄の上限（backend の ExternalParserConnectionUpdate の max_length と同じ。#976）。 */
const EXTERNAL_CONNECTION_MAX_LENGTH = { endpoint: 2048, model: 512, apiKey: 4096 } as const;

const ADAPTER_FLAG_FIELDS: Record<ParserAdapterBackendName, ParserAdapterFlagField> = {
  docling: "docling_enabled",
  unstructured: "unstructured_enabled",
  structured_records_jsonl: "structured_records_jsonl_enabled",
  xml_tree_json: "xml_tree_json_enabled",
  mineru: "mineru_enabled",
  dots_ocr: "dots_ocr_enabled",
};

const SERVICE_BACKENDS: ParserServiceBackendName[] = ["oci_document_understanding"];

const PARSER_BACKEND_SERVICE_IDS: Partial<
  Record<ParserAdapterBackendName | ParserServiceBackendName, string>
> = {
  docling: "parser-docling",
  unstructured: "parser-unstructured",
  structured_records_jsonl: "parser-structured-records-jsonl",
  xml_tree_json: "parser-xml-tree-json",
  oci_document_understanding: "parser-oci-document-understanding",
};

function isAdapterBackend(backend: ParserAdapterBackend): backend is ParserAdapterBackendName {
  return backend in ADAPTER_FLAG_FIELDS;
}

function isServiceBackend(backend: ParserAdapterBackend): backend is ParserServiceBackendName {
  return (SERVICE_BACKENDS as ParserAdapterBackend[]).includes(backend);
}

/**
 * 準備中（ロードマップ）のエンジン（OCI Document Understanding は #3374、MinerU は #3359）。
 * カードを灰色にして選ばせず、状態も問い合わせない。外部の接続の欄も出さず、保存にも送らない。
 */
function roadmapBackendsFromSettings(settings: ParserAdapterSettingsData): Set<ParserAdapterBackend> {
  return new Set<ParserAdapterBackend>([
    ...(settings.service_backends ?? [])
      .filter((service) => service.availability === "roadmap")
      .map((service) => service.backend),
    ...settings.adapters
      .filter((adapter) => adapter.availability === "roadmap")
      .map((adapter) => adapter.backend),
  ]);
}

function isExternalBackend(
  backend: ParserAdapterBackend
): backend is ExternalParserBackendName {
  return (EXTERNAL_BACKENDS as ParserAdapterBackend[]).includes(backend);
}

/** Optional parser adapter の runtime 設定と readiness を管理する設定画面。 */
export function ParserAdapterSettingsClient() {
  const query = useParserAdapterSettings();
  const save = useUpdateParserAdapterSettings();
  const [form, setForm] = useState<ParserAdapterForm | null>(null);
  const [connectionErrors, setConnectionErrors] = useState<ConnectionFieldErrors>({});
  // 直前に取り込んだ解析エンジンの server 値。server 値が変わったレンダーだけ、未編集ならフォームをそろえる。
  // 「解析後の処理」の保存でも query.data は変わるため、解析エンジンの部分だけを比べる（#528）。
  // 保存の開始・終了（isPending）ではフォームを戻さない（失敗しても入力を残す。#976）。
  const [base, setBase] = useState<string | null>(null);
  const serverForm = query.data ? formFromSettings(query.data) : null;
  const serverKey = serverForm ? serializeForm(serverForm) : null;
  if (serverForm && serverKey !== base) {
    setBase(serverKey);
    if (form === null || serializeForm(form) === base) setForm(serverForm);
  }

  // 未保存の変更があるときは離脱を確認し、保存中は離脱を止める。
  useLeaveGuard(Boolean(serverKey && form && serializeForm(form) !== serverKey), save.isPending);

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.loading")}
          operationKey="settings-parser-adapters-load"
          placement="page"
          testId="settings-parser-adapters-loading"
        >
          <FormSkeleton fields={2} />
          <FormSkeleton fields={5} />
        </TimedLoadingState>
      </PageBody>
    );
  }

  if (query.isError) {
    return (
      <PageBody wide>
        <ApiErrorState
          error={query.error}
          fallback={t("settings.parserAdapters.loadError")}
          onRetry={() => void query.refetch()}
        />
      </PageBody>
    );
  }

  const settings = query.data;
  if (!settings || !form) return null;

  const dirty = serializeForm(form) !== serializeForm(formFromSettings(settings));
  const saveError =
    save.error instanceof ApiError ? save.error.message : t("settings.parserAdapters.saveError");

  function updateForm(update: Partial<ParserAdapterForm>) {
    save.reset();
    setForm((current) => (current ? { ...current, ...update } : current));
  }

  function selectBackend(adapterBackend: ParserAdapterBackend) {
    // MinerU はライセンスの条件を確認するまで選べない（#3359。backend も 422 で拒否する）。
    if (adapterBackend === "mineru" && !form?.mineru_license_acknowledged) return;
    const enabledUpdate = externalBackendFlagUpdate(adapterBackend);
    updateForm({ adapter_backend: adapterBackend, ...enabledUpdate });
  }

  function acknowledgeMineruLicense(checked: boolean) {
    // 確認を外すときは MinerU の有効化も外す（使用エンジンが MinerU の間は外せない）。
    updateForm(
      checked
        ? { mineru_license_acknowledged: true }
        : { mineru_license_acknowledged: false, mineru_enabled: false }
    );
  }

  function resetForm() {
    save.reset();
    setConnectionErrors({});
    setForm(formFromSettings(settings));
  }

  function updateConnection(
    backend: ExternalParserBackendName,
    update: Partial<ExternalParserConnectionForm>
  ) {
    save.reset();
    setConnectionErrors((current) => {
      const next = { ...current };
      for (const field of Object.keys(update)) delete next[`${backend}.${field}`];
      return next;
    });
    setForm((current) =>
      current
        ? {
            ...current,
            connections: {
              ...current.connections,
              [backend]: { ...current.connections[backend], ...update },
            },
          }
        : current
    );
  }

  function submit() {
    if (!form || save.isPending) return;
    const roadmap = roadmapBackendsFromSettings(settings);
    const errors = validateConnections(form, roadmap);
    setConnectionErrors(errors);
    const firstError = Object.keys(errors)[0];
    if (firstError) {
      requestAnimationFrame(() => focusConnectionError(firstError));
      return;
    }
    save.mutate(parserSettingsUpdate(form, roadmap), {
      onSuccess: (data) => {
        const next = formFromSettings(data);
        setBase(serializeForm(next));
        setForm(next);
        // 保存の成功は Toast、失敗は操作の行の FormStatus（messaging.md §10.2）。
        toast.success(t("settings.parserAdapters.actions.saved"));
      },
    });
  }

  return (
    <PageBody wide>
      <OverviewCard
        dirty={dirty}
        form={form}
        settings={settings}
        saving={save.isPending}
        errorMessage={save.isError ? saveError : null}
        connectionErrors={connectionErrors}
        onBackendChange={selectBackend}
        onMineruLicenseChange={acknowledgeMineruLicense}
        onConnectionChange={updateConnection}
        onReset={resetForm}
        onSubmit={submit}
      />
      {/* 解析の後の Vision・項目抽出・章節木の全体の既定（#528）。Vision の読み取りの指示もこの中で編集する。 */}
      <PostParseSettingsCard settings={settings} />
    </PageBody>
  );
}

function OverviewCard({
  dirty,
  form,
  settings,
  saving,
  errorMessage,
  connectionErrors,
  onBackendChange,
  onMineruLicenseChange,
  onConnectionChange,
  onReset,
  onSubmit,
}: {
  dirty: boolean;
  form: ParserAdapterForm;
  settings: ParserAdapterSettingsData;
  saving: boolean;
  errorMessage: string | null;
  connectionErrors: ConnectionFieldErrors;
  onBackendChange: (backend: ParserAdapterBackend) => void;
  onMineruLicenseChange: (checked: boolean) => void;
  onConnectionChange: (
    backend: ExternalParserBackendName,
    update: Partial<ExternalParserConnectionForm>
  ) => void;
  onReset: () => void;
  onSubmit: () => void;
}) {
  const backendOptions = useMemo(
    () => backendOptionsFromSettings(settings),
    [settings]
  );
  // MinerU のライセンスの条件の確認・取り消しは system_admin だけ（#3359）。
  const canAcknowledgeMineruLicense = useAuth().user?.is_system_admin === true;
  const mineruLicenseLocked =
    form.mineru_license_acknowledged && form.adapter_backend === "mineru";
  // 正本（PARSER_BACKEND_ORDER。#3359）の順。1 つの 2 列の grid に並べ、375px の 1 列でも同じ順にする
  // （Docling・Unstructured / 構造化レコード JSONL・XML ツリー JSON / Dots.OCR・MinerU /
  // OCI Document Understanding）。処理レシピの選択肢も同じ順。
  const orderedBackends = useMemo(() => sortParserBackends(backendOptions), [backendOptions]);
  // 準備中（ロードマップ）のエンジン（#3374・#3359）。カードを灰色にして選ばせず、状態も問い合わせない。
  const roadmapBackends = useMemo(() => roadmapBackendsFromSettings(settings), [settings]);
  const mineruRoadmap = roadmapBackends.has("mineru");
  const serviceIds = useMemo(
    () => [
      ...new Set(
        backendOptions
          .filter((backend) => !roadmapBackends.has(backend))
          .map(serviceIdForBackend)
          .filter((serviceId): serviceId is string => Boolean(serviceId))
      ),
    ],
    [backendOptions, roadmapBackends]
  );
  const serviceStatusQueries = useServiceStatusQueries(serviceIds);
  const runtimeByServiceId = useMemo(() => {
    const services = new Map<
      string,
      { status: DisplayRuntimeStatus; profile: ServiceProfile | null }
    >();
    serviceIds.forEach((serviceId, index) => {
      const statusQuery = serviceStatusQueries[index];
      services.set(serviceId, {
        status: statusQuery?.data?.status ?? (statusQuery?.isError ? "error" : "loading"),
        profile: statusQuery?.data?.profile ?? null,
      });
    });
    return services;
  }, [serviceIds, serviceStatusQueries]);
  const serviceByName = useMemo(
    () =>
      new Map<ParserServiceBackendName, ParserServiceBackendData>(
        (settings.service_backends ?? []).map((item) => [item.backend, item])
      ),
    [settings.service_backends]
  );
  const connectionByBackend = useMemo(
    () =>
      new Map<ExternalParserBackendName, ExternalParserConnectionData>(
        (settings.connections ?? []).map((connection) => [connection.backend, connection])
      ),
    [settings.connections]
  );
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start gap-3">
          <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
            <Plug size={20} aria-hidden />
          </div>
          <div>
            <CardTitle>{t("settings.parserAdapters.overview.title")}</CardTitle>
            <CardDescription>
              {t("settings.parserAdapters.overview.description")}
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="space-y-2">
          <div className="text-sm font-medium text-fg">
            {t("settings.parserAdapters.backend")}
          </div>
          <div
            role="radiogroup"
            aria-label={t("settings.parserAdapters.backend")}
            className="space-y-2"
          >
            {/* 1 行 2 枚で横幅いっぱいに並べる（下の外部 GPU 接続カードと同じ 2 列）。 */}
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                {orderedBackends.map((backend) => {
                  const roadmap = roadmapBackends.has(backend);
                  const selected = !roadmap && form.adapter_backend === backend;
                  const roadmapReasonId = `settings-parser-backend-${backend}-roadmap-reason`;
                  const service = isServiceBackend(backend)
                    ? serviceByName.get(backend)
                    : undefined;
                  const externalConnection = isExternalBackend(backend)
                    ? connectionByBackend.get(backend)
                    : undefined;
                  const serviceId = serviceIdForBackend(backend);
                  const runtime = serviceId ? runtimeByServiceId.get(serviceId) : null;
                  const runtimeStatus = runtime?.status ?? null;
                  const runtimeProfile = runtime?.profile ?? serviceProfileForBackend(backend);
                  const capability = findParserCapability(settings.capabilities, backend);
                  const supportedFormats = formatSupportedFormats(capability);
                  const supportedExtensions = formatSupportedExtensions(capability);
                  // MinerU はライセンスの条件を確認するまで選べない（#3359）。
                  const licenseBlocked =
                    backend === "mineru" &&
                    !roadmap &&
                    !selected &&
                    !form.mineru_license_acknowledged;
                  return (
                    // 準備中の MinerU は、カードの下（選ぶ操作の外）にライセンスの短い案内と公式のリンクを
                    // 出す（#3359）。行の高さはそろえ、カードは残りの高さを埋める。
                    <div key={backend} className="flex min-w-0 flex-col gap-1">
                    <div className="relative min-w-0 flex-1">
                      <input
                        id={`settings-parser-backend-${backend}`}
                        className="peer absolute inset-0 z-10 cursor-pointer opacity-0 disabled:cursor-not-allowed"
                        type="radio"
                        name="settings-parser-backend"
                        value={backend}
                        checked={selected}
                        // 準備中（ロードマップ）のエンジンは選べない（#3374）。理由は下の文で読み上げる。
                        // MinerU はライセンスの条件を確認するまで選べない（#3359）。
                        disabled={saving || roadmap || licenseBlocked}
                        aria-describedby={
                          roadmap
                            ? roadmapReasonId
                            : licenseBlocked
                              ? "settings-parser-backend-mineru-blocked"
                              : undefined
                        }
                        data-availability={roadmap ? "roadmap" : undefined}
                        onChange={() => onBackendChange(backend)}
                      />
                      <label
                        data-surface-tint={selected ? "accent" : undefined}
                        htmlFor={`settings-parser-backend-${backend}`}
                        className={cn(
                          "block h-full cursor-pointer min-h-[5.43rem] rounded-md border px-3 py-2 text-left transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
                          roadmap
                            ? // 準備中は無効の地と灰色の名前にし、理由は読める濃さのまま残す（全体を薄くしない。#3374）。
                              "border-border bg-surface-disabled text-fg-disabled peer-disabled:opacity-100"
                            : selected
                              ? "border-accent-emphasis bg-accent-subtle text-fg"
                              : "border-border bg-surface text-fg peer-hover:bg-surface-hover"
                        )}
                      >
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="text-sm font-semibold">{backendLabel(backend)}</span>
                          {runtimeProfile ? <ServiceProfileBadge profile={runtimeProfile} /> : null}
                          {roadmap ? <RoadmapBadge label={t("settings.parserAdapters.roadmap.badge")} /> : null}
                        </span>
                        {roadmap ? (
                          <span
                            id={roadmapReasonId}
                            className="mt-1 block text-xs font-medium leading-relaxed text-fg-muted"
                          >
                            {t("settings.parserAdapters.roadmap.reason")}
                          </span>
                        ) : null}
                        <span className="mt-1 block text-xs leading-relaxed text-fg-muted">
                          {t(backendDescriptionKey(backend))}
                        </span>
                        {supportedFormats ? (
                          <span className="mt-1 block text-xs text-fg-muted">
                            {t("settings.parserAdapters.capabilities")}: {supportedFormats}
                          </span>
                        ) : null}
                        {supportedExtensions ? (
                          <span className="mt-0.5 block break-words text-xs leading-4 text-fg-muted">
                            {supportedExtensions}
                          </span>
                        ) : null}
                        {licenseBlocked ? (
                          <span
                            id="settings-parser-backend-mineru-blocked"
                            className="mt-1 block text-xs leading-relaxed text-warning-fg"
                          >
                            {t("mineruLicense.selectBlocked")}
                          </span>
                        ) : null}
                        <span className="mt-2 flex flex-wrap items-center gap-1.5">
                          {runtimeStatus ? <ServiceStatusBadge status={runtimeStatus} /> : null}
                          {service && !roadmap && !service.configured ? (
                            <ConnectionConfiguredBadge configured={false} />
                          ) : null}
                          {externalConnection && !roadmap ? (
                            <ConnectionConfiguredBadge configured={externalConnection.configured} />
                          ) : null}
                        </span>
                      </label>
                    </div>
                    {backend === "mineru" && roadmap ? <MineruLicenseNotice compact /> : null}
                    </div>
                  );
                })}
            </div>
          </div>
          {/* MinerU の選択肢のすぐ下に、ライセンスの主な条件と確認を出す（#3359）。準備中（ロードマップ）の
              間は確認の checkbox を出さない（カードの下の短い案内だけ）。 */}
          {mineruRoadmap ? null : (
          <MineruLicenseNotice
            acknowledgement={settings.mineru_license}
            acknowledge={{
              id: "settings-parser-mineru-license-acknowledged",
              checked: form.mineru_license_acknowledged,
              // 確認・取り消しは system_admin だけ（組織としての判断。backend は 403）。
              // 使用エンジンが MinerU の間は確認を外せない（backend も 422 で拒否する）。
              disabled: saving || !canAcknowledgeMineruLicense || mineruLicenseLocked,
              note: !canAcknowledgeMineruLicense
                ? t("mineruLicense.adminOnly")
                : mineruLicenseLocked
                  ? t("mineruLicense.withdrawLocked")
                  : null,
              onChange: onMineruLicenseChange,
            }}
          />
          )}
          {form.adapter_backend === "local" ? (
            <p className="text-xs leading-relaxed text-warning-fg">
              {t("settings.parserAdapters.legacyBackendNotice")}
            </p>
          ) : null}
        </div>
        <section className="space-y-3 border-t border-border pt-5">
          <div>
            <h3 className="text-sm font-semibold text-fg">
              {t("settings.parserAdapters.connection.title")}
            </h3>
            <p className="mt-1 text-xs leading-relaxed text-fg-muted">
              {t("settings.parserAdapters.connection.description")}
            </p>
          </div>
          <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">
            {EXTERNAL_BACKENDS.filter((backend) => !roadmapBackends.has(backend)).map((backend) => (
              <ExternalConnectionCard
                key={backend}
                backend={backend}
                connection={connectionByBackend.get(backend)}
                value={form.connections[backend]}
                errors={connectionErrors}
                saving={saving}
                onChange={(update) => onConnectionChange(backend, update)}
              />
            ))}
          </div>
        </section>
        <dl className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <RuntimeFact
            label={t("settings.parserAdapters.backend")}
            value={backendLabel(settings.adapter_backend)}
          />
          <RuntimeFact
            label={t("settings.parserAdapters.effectiveOrder")}
            value={formatEffectiveOrder(settings.effective_order)}
          />
          <RuntimeFact
            label={t("settings.parserAdapters.source")}
            value={configSourceLabel(settings.config_source)}
          />
        </dl>
        <FormActionBar
          ariaLabel={t("settings.parserAdapters.actions.label")}
          primaryActions={[
            {
              id: "save",
              label: t("settings.parserAdapters.actions.save"),
              icon: Save,
              loading: saving,
              disabled: !dirty,
              onClick: onSubmit,
            },
          ]}
          secondaryActions={[
            {
              id: "reset",
              label: t("settings.parserAdapters.actions.reset"),
              icon: RotateCcw,
              disabled: !dirty || saving,
              onClick: onReset,
            },
          ]}
          status={
            errorMessage ? (
              <FormStatus tone="danger" message={errorMessage} />
            ) : dirty ? (
              <FormStatus tone="warning" message={t("settings.parserAdapters.actions.unsaved")} />
            ) : null
          }
        />
      </CardContent>
    </Card>
  );
}

function ExternalConnectionCard({
  backend,
  connection,
  value,
  errors,
  saving,
  onChange,
}: {
  backend: ExternalParserBackendName;
  connection: ExternalParserConnectionData | undefined;
  value: ExternalParserConnectionForm;
  errors: ConnectionFieldErrors;
  saving: boolean;
  onChange: (update: Partial<ExternalParserConnectionForm>) => void;
}) {
  const statusQuery = useExternalParserStatus(backend);
  // 接続確認の所要時間（API は返さないため、押してから結果が返るまでを画面で測る。messaging.md §10.3）。
  const [elapsedMs, setElapsedMs] = useState<number | undefined>(undefined);
  const runConnectionTest = async () => {
    const startedAt = Date.now();
    await statusQuery.refetch();
    setElapsedMs(Date.now() - startedAt);
  };
  const modelSupported = backend !== "mineru";
  const dirty =
    value.endpoint.trim() !== (connection?.endpoint ?? "") ||
    (modelSupported && value.model.trim() !== (connection?.model ?? "")) ||
    Boolean(value.api_key) ||
    value.clear_api_key;
  const endpointError = errors[`${backend}.endpoint`];
  const modelError = errors[`${backend}.model`];
  const endpointId = `external-parser-${backend}-endpoint`;
  const modelId = `external-parser-${backend}-model`;
  const apiKeyId = `external-parser-${backend}-api-key`;
  const clearApiKeyId = `external-parser-${backend}-clear-api-key`;

  return (
    <div data-testid={`external-parser-connection-${backend}`} data-surface-tint="neutral" className="min-w-0 rounded-md border border-border bg-surface-hover p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold text-fg">{backendLabel(backend)}</h4>
          <p className="mt-0.5 break-all text-xs text-fg-muted">
            {connectionProtocolLabel(connection?.protocol ?? externalProtocol(backend))}
          </p>
        </div>
        <ConnectionConfiguredBadge configured={Boolean(connection?.configured)} />
      </div>

      <div className="mt-4 space-y-3">
        <ConnectionTextField
          id={endpointId}
          label={t("settings.parserAdapters.connection.endpoint")}
          width="full"
          type="url"
          maxLength={EXTERNAL_CONNECTION_MAX_LENGTH.endpoint}
          value={value.endpoint}
          placeholder={externalEndpointPlaceholder(backend)}
          error={endpointError}
          disabled={saving}
          onChange={(endpoint) => onChange({ endpoint })}
        />
        <ConnectionTextField
          id={modelId}
          label={t("settings.parserAdapters.connection.model")}
          width="md"
          maxLength={EXTERNAL_CONNECTION_MAX_LENGTH.model}
          value={modelSupported ? value.model : t("settings.parserAdapters.connection.nativeModel")}
          placeholder={modelSupported ? "model-id" : undefined}
          // Endpoint を入力したときだけ Model が要る（validateConnections と同じ条件。#531）
          required={modelSupported && Boolean(value.endpoint.trim())}
          error={modelError}
          disabled={saving || !modelSupported}
          onChange={(model) => onChange({ model })}
        />
        <ConnectionTextField
          id={apiKeyId}
          label={t("settings.parserAdapters.connection.apiKey")}
          width="lg"
          type="password"
          maxLength={EXTERNAL_CONNECTION_MAX_LENGTH.apiKey}
          value={value.api_key}
          // 任意の欄は placeholder で「任意」と示さない（#531）。保存済みのときだけ保持の説明を出す。
          placeholder={
            connection?.api_key_configured ? t("settings.parserAdapters.connection.apiKeyRetained") : undefined
          }
          disabled={saving || value.clear_api_key}
          onChange={(api_key) => onChange({ api_key, clear_api_key: false })}
        />
        <label
          htmlFor={clearApiKeyId}
          className="flex min-h-[var(--control-height-touch)] cursor-pointer items-center gap-2 rounded-md text-xs text-fg"
        >
          <input
            id={clearApiKeyId}
            type="checkbox"
            checked={value.clear_api_key}
            disabled={saving || !connection?.api_key_configured}
            onChange={(event) =>
              onChange({ clear_api_key: event.target.checked, api_key: "" })
            }
            className="h-4 w-4 rounded border-border accent-accent-emphasis"
          />
          {t("settings.parserAdapters.connection.clearApiKey")}
        </label>
      </div>

      <div className="mt-4 space-y-2 border-t border-border pt-3">
        <Button
          type="button"
          variant="secondary"
          className="w-full sm:w-auto"
          loading={statusQuery.isFetching}
          disabled={saving || dirty || !connection?.configured}
          onClick={() => void runConnectionTest()} icon={Plug}>
          {t("settings.parserAdapters.connection.test")}
        </Button>
        {statusQuery.isFetching ? (
          // 外部の GPU parser へ実際に接続するため、起動直後や高負荷時は数秒以上かかる。
          <ProcessingIndicator
            active
            label={t("settings.parserAdapters.connection.testing", { backend: backendLabel(backend) })}
            operationKey={`external-parser-status-${backend}`}
            placement="action"
            activityIcon="none"
            testId={`external-parser-status-processing-${backend}`}
          />
        ) : null}
        {dirty ? (
          <p className="text-xs leading-relaxed text-fg-muted">
            {t("settings.parserAdapters.connection.saveBeforeTest")}
          </p>
        ) : null}
        {/* 結果は「接続を確認」の直下にカードの全幅で出し、入力を変えたら消す（messaging.md §10）。 */}
        {!dirty && !statusQuery.isFetching && (statusQuery.data || statusQuery.isError) ? (
          <ConnectionTestResult
            backend={backend}
            data={statusQuery.isError ? undefined : statusQuery.data}
            error={statusQuery.isError ? statusQuery.error : null}
            elapsedMs={elapsedMs}
            checkedAt={
              statusQuery.isError ? statusQuery.errorUpdatedAt : statusQuery.dataUpdatedAt
            }
          />
        ) : null}
      </div>
      {backend === "dots_ocr" ? <div className="mt-4"><FeaturePromptDisclosure promptKey="external_ocr" /></div> : null}
    </div>
  );
}

function ConnectionTextField({
  id,
  label,
  value,
  width,
  type = "text",
  maxLength,
  placeholder,
  required = false,
  error,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  /** 値の種類の幅（URL は full、モデル名は md、API キーは lg。#1891）。 */
  width: FieldWidth;
  type?: "text" | "url" | "password";
  /** backend（ExternalParserConnectionUpdate）と同じ上限（#976）。 */
  maxLength: number;
  placeholder?: string;
  required?: boolean;
  error?: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <TextField
      id={id}
      className="min-w-0"
      label={label}
      width={width}
      type={type}
      maxLength={maxLength}
      value={value}
      placeholder={placeholder}
      required={required}
      disabled={disabled}
      autoComplete="off"
      error={error}
      onValueChange={onChange}
    />
  );
}

/** 接続設定の有無（リソースの状態）。共有の StatusBadge で出す（messaging.md §10.2）。 */
function ConnectionConfiguredBadge({ configured }: { configured: boolean }) {
  return (
    <StatusBadge
      variant={configured ? "success" : "warning"}
      label={
        configured
          ? t("settings.parserAdapters.connection.configured")
          : t("settings.parserAdapters.serviceBackend.unconfigured")
      }
    />
  );
}

const CONNECTION_TEST_TONE: Record<ExternalParserConnectionStatus, SettingsTestResultTone> = {
  available: "success",
  unconfigured: "warning",
  unreachable: "danger",
  model_missing: "danger",
  invalid_response: "danger",
};

/**
 * 接続確認の結果パネル（システム設定の接続テストと同じ部品。messaging.md §10）。
 * 1 文目に何が起きたか、次に所要時間。backend・状態・version・警告コードは「詳細」に畳む。
 */
function ConnectionTestResult({
  backend,
  data,
  error,
  elapsedMs,
  checkedAt,
}: {
  backend: ExternalParserBackendName;
  data: ExternalParserConnectionStatusData | undefined;
  error: unknown;
  elapsedMs: number | undefined;
  checkedAt: number;
}) {
  const checkedAtText = checkedAt ? formatDateTime(new Date(checkedAt).toISOString()) : undefined;
  const testId = `external-parser-test-result-${backend}`;
  if (error || !data) {
    const apiError = error instanceof ApiError ? error : null;
    return (
      <SettingsTestResultPanel
        tone="danger"
        message={apiError?.message ?? t("settings.parserAdapters.connection.testFailed")}
        elapsedMs={elapsedMs}
        checkedAt={checkedAtText}
        details={toSettingsTestResultDetails({
          backend,
          status_code: apiError?.status,
          error_code: apiError?.errorCode,
          request_id: apiError?.requestId,
        })}
        testId={testId}
      />
    );
  }
  return (
    <SettingsTestResultPanel
      tone={CONNECTION_TEST_TONE[data.status]}
      message={t(`settings.parserAdapters.connection.status.${data.status}` as I18nKey)}
      elapsedMs={elapsedMs}
      checkedAt={checkedAtText}
      details={toSettingsTestResultDetails({
        backend: data.backend,
        status: data.status,
        version: data.version,
        warning_code: data.warning_code,
      })}
      testId={testId}
    />
  );
}

function RuntimeFact({ label, value }: { label: string; value: string }) {
  return (
    <div data-surface-tint="neutral" className="rounded-md border border-border bg-surface-hover p-3">
      <dt className="text-xs font-medium text-fg-muted">{label}</dt>
      <dd className="mt-1 break-words text-sm font-semibold text-fg">{value}</dd>
    </div>
  );
}

function adapterLabel(adapter: ParserAdapterBackendName) {
  if (adapter === "docling") return "Docling";
  if (adapter === "unstructured") return "Unstructured";
  if (adapter === "structured_records_jsonl")
    return t("settings.parserAdapters.backend.structured_records_jsonl");
  if (adapter === "xml_tree_json") return t("settings.parserAdapters.backend.xml_tree_json");
  if (adapter === "mineru") return "MinerU";
  return "Dots.OCR";
}

function backendLabel(backend: ParserAdapterBackend) {
  if (backend === "local") return t("settings.parserAdapters.backend.local");
  if (backend === "mineru") return "MinerU";
  if (backend === "dots_ocr") return "Dots.OCR";
  if (backend === "oci_document_understanding")
    return t("settings.parserAdapters.backend.oci_document_understanding");
  return adapterLabel(backend);
}

function backendOptionsFromSettings(settings: ParserAdapterSettingsData): ParserAdapterBackend[] {
  const ordered: ParserAdapterBackend[] = [
    ...settings.adapters.map((adapter) => adapter.backend),
    ...(settings.service_backends ?? []).map((service) => service.backend),
  ];
  const selected = settings.adapter_backend;
  if (selected !== "local" && !ordered.includes(selected)) ordered.push(selected);
  return [...new Set(ordered)];
}

function serviceIdForBackend(backend: ParserAdapterBackend): string | null {
  if (backend === "local" || isExternalBackend(backend)) return null;
  if (isAdapterBackend(backend) || isServiceBackend(backend)) {
    return PARSER_BACKEND_SERVICE_IDS[backend] ?? null;
  }
  return null;
}

function serviceProfileForBackend(backend: ParserAdapterBackend): ServiceProfile | null {
  if (backend === "mineru" || backend === "dots_ocr") return "gpu";
  if (isServiceBackend(backend)) return "oci";
  if (isAdapterBackend(backend)) return "cpu";
  return null;
}

function configSourceLabel(source: string) {
  return source === "runtime" ? t("settings.common.currentConfig") : source;
}

function backendDescriptionKey(backend: ParserAdapterBackend): I18nKey {
  return `settings.parserAdapters.backend.${backend}.description` as I18nKey;
}

function formatEffectiveOrder(order: ParserAdapterBackendName[]) {
  if (!order.length) return t("settings.parserAdapters.noEffectiveOrder");
  return order.map(adapterLabel).join(" -> ");
}

function formFromSettings(settings: ParserAdapterSettingsData): ParserAdapterForm {
  const enabledByBackend = new Map(
    settings.adapters.map((adapter) => [adapter.backend, adapter.enabled])
  );
  return {
    adapter_backend: settings.adapter_backend,
    docling_enabled: enabledByBackend.get("docling") ?? false,
    unstructured_enabled: enabledByBackend.get("unstructured") ?? false,
    mineru_enabled: enabledByBackend.get("mineru") ?? false,
    mineru_license_acknowledged: settings.mineru_license?.acknowledged ?? false,
    dots_ocr_enabled: enabledByBackend.get("dots_ocr") ?? false,
    structured_records_jsonl_enabled: enabledByBackend.get("structured_records_jsonl") ?? false,
    xml_tree_json_enabled: enabledByBackend.get("xml_tree_json") ?? false,
    connections: Object.fromEntries(
      EXTERNAL_BACKENDS.map((backend) => {
        const connection = (settings.connections ?? []).find(
          (candidate) => candidate.backend === backend
        );
        return [
          backend,
          {
            endpoint: connection?.endpoint ?? "",
            model: connection?.model ?? defaultExternalModel(backend),
            api_key: "",
            clear_api_key: false,
          },
        ];
      })
    ) as Record<ExternalParserBackendName, ExternalParserConnectionForm>,
  };
}

function adapterFlagField(
  adapter: ParserAdapterBackendName
): ParserAdapterFlagField {
  return ADAPTER_FLAG_FIELDS[adapter];
}

function externalBackendFlagUpdate(
  backend: ParserAdapterBackend
): Partial<ParserAdapterForm> {
  if (isAdapterBackend(backend)) {
    return { [adapterFlagField(backend)]: true } as Partial<ParserAdapterForm>;
  }
  return {};
}

function serializeForm(form: ParserAdapterForm) {
  return JSON.stringify({
    adapter_backend: form.adapter_backend,
    docling_enabled: form.docling_enabled,
    unstructured_enabled: form.unstructured_enabled,
    mineru_enabled: form.mineru_enabled,
    mineru_license_acknowledged: form.mineru_license_acknowledged,
    dots_ocr_enabled: form.dots_ocr_enabled,
    structured_records_jsonl_enabled: form.structured_records_jsonl_enabled,
    xml_tree_json_enabled: form.xml_tree_json_enabled,
    connections: form.connections,
  });
}

function parserSettingsUpdate(
  form: ParserAdapterForm,
  roadmap: ReadonlySet<ParserAdapterBackend>
): ParserAdapterSettingsUpdate {
  return {
    adapter_backend: form.adapter_backend,
    docling_enabled: form.docling_enabled,
    unstructured_enabled: form.unstructured_enabled,
    mineru_enabled: form.mineru_enabled,
    mineru_license_acknowledged: form.mineru_license_acknowledged,
    dots_ocr_enabled: form.dots_ocr_enabled,
    structured_records_jsonl_enabled: form.structured_records_jsonl_enabled,
    xml_tree_json_enabled: form.xml_tree_json_enabled,
    connections: EXTERNAL_BACKENDS.filter((backend) => !roadmap.has(backend)).map((backend) => {
      const connection = form.connections[backend];
      return {
        backend,
        endpoint: connection.endpoint.trim(),
        ...(backend === "mineru" ? {} : { model: connection.model.trim() }),
        ...(connection.api_key ? { api_key: connection.api_key } : {}),
        ...(connection.clear_api_key ? { clear_api_key: true } : {}),
      };
    }),
  };
}

function validateConnections(
  form: ParserAdapterForm,
  roadmap: ReadonlySet<ParserAdapterBackend>
): ConnectionFieldErrors {
  const errors: ConnectionFieldErrors = {};
  for (const backend of EXTERNAL_BACKENDS) {
    if (roadmap.has(backend)) continue;
    const connection = form.connections[backend];
    const endpoint = connection.endpoint.trim();
    if (endpoint) {
      try {
        const parsed = new URL(endpoint);
        if (
          // backend（ExternalParserConnectionUpdate）と同じく `http(s)://host` の形だけを受け付ける。
          // `new URL("http:foo")` は `http://foo/` として通るため、書き方も確かめる（#976）。
          !/^https?:\/\/[^/?#]/i.test(endpoint) ||
          !["http:", "https:"].includes(parsed.protocol) ||
          parsed.username ||
          parsed.password ||
          parsed.search ||
          parsed.hash
        ) {
          errors[`${backend}.endpoint`] = t("settings.parserAdapters.connection.invalidEndpoint");
        }
      } catch {
        errors[`${backend}.endpoint`] = t("settings.parserAdapters.connection.invalidEndpoint");
      }
    }
    if (endpoint && backend !== "mineru" && !connection.model.trim()) {
      errors[`${backend}.model`] = t("settings.parserAdapters.connection.modelRequired");
    }
  }
  return errors;
}

function focusConnectionError(key: string) {
  const [backend, field] = key.split(".");
  document.getElementById(`external-parser-${backend}-${field}`)?.focus();
}

function externalProtocol(backend: ExternalParserBackendName) {
  return backend === "mineru" ? "mineru_v1" : "openai_chat_completions";
}

function connectionProtocolLabel(protocol: string) {
  return protocol === "mineru_v1" ? "MinerU /v1/parse/jobs" : "OpenAI /v1/chat/completions";
}

function externalEndpointPlaceholder(backend: ExternalParserBackendName) {
  return backend === "mineru" ? "https://parser.example.com" : "https://api.example.com/v1";
}

function defaultExternalModel(backend: ExternalParserBackendName) {
  if (backend === "dots_ocr") return "rednote-hilab/dots.mocr";
  return "";
}
