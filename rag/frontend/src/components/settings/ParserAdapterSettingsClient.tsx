"use client";

import {
  PageBody,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormStatus,
  TimedLoadingState,
  FormSkeleton,
} from "@production-ready/ui";
import { useMemo, useState } from "react";
import {
  Plug,
  RotateCcw,
  Save,
} from "lucide-react";

import { ApiErrorState } from "@/components/StateViews";
import {
  ServiceProfileBadge,
  ServiceStatusBadge,
  type DisplayRuntimeStatus,
} from "@/components/settings/ServicesManagementClient";
import {
  ApiError,
  type ParserAdapterBackend,
  type ParserAdapterBackendName,
  type ParserAdapterSettingsData,
  type ParserAdapterSettingsUpdate,
  type ServiceProfile,
} from "@/lib/api";
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
  useServiceStatusQueries,
  useUpdateParserAdapterSettings,
} from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { PostParseSettingsCard } from "./PostParseSettingsCard";

type ParserAdapterForm = {
  adapter_backend: ParserAdapterBackend;
  docling_enabled: boolean;
  unstructured_enabled: boolean;
  structured_records_jsonl_enabled: boolean;
  xml_tree_json_enabled: boolean;
};
type ParserAdapterFlagField = Exclude<keyof ParserAdapterForm, "adapter_backend">;

const ADAPTER_FLAG_FIELDS: Record<ParserAdapterBackendName, ParserAdapterFlagField> = {
  docling: "docling_enabled",
  unstructured: "unstructured_enabled",
  structured_records_jsonl: "structured_records_jsonl_enabled",
  xml_tree_json: "xml_tree_json_enabled",
};

const PARSER_BACKEND_SERVICE_IDS: Partial<Record<ParserAdapterBackendName, string>> = {
  docling: "parser-docling",
  unstructured: "parser-unstructured",
  structured_records_jsonl: "parser-structured-records-jsonl",
  xml_tree_json: "parser-xml-tree-json",
};

function isAdapterBackend(backend: ParserAdapterBackend): backend is ParserAdapterBackendName {
  return backend in ADAPTER_FLAG_FIELDS;
}

/** Optional parser adapter の runtime 設定と readiness を管理する設定画面。 */
export function ParserAdapterSettingsClient() {
  const query = useParserAdapterSettings();
  const save = useUpdateParserAdapterSettings();
  const [form, setForm] = useState<ParserAdapterForm | null>(null);
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
    const enabledUpdate = adapterBackendFlagUpdate(adapterBackend);
    updateForm({ adapter_backend: adapterBackend, ...enabledUpdate });
  }

  function resetForm() {
    save.reset();
    setForm(formFromSettings(settings));
  }

  function submit() {
    if (!form || save.isPending) return;
    save.mutate(parserSettingsUpdate(form), {
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
        onBackendChange={selectBackend}
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
  onBackendChange,
  onReset,
  onSubmit,
}: {
  dirty: boolean;
  form: ParserAdapterForm;
  settings: ParserAdapterSettingsData;
  saving: boolean;
  errorMessage: string | null;
  onBackendChange: (backend: ParserAdapterBackend) => void;
  onReset: () => void;
  onSubmit: () => void;
}) {
  const backendOptions = useMemo(
    () => backendOptionsFromSettings(settings),
    [settings]
  );
  // 正本（PARSER_BACKEND_ORDER）の順。1 つの 2 列の grid に並べ、375px の 1 列でも同じ順にする
  // （Docling・Unstructured / 構造化レコード JSONL・XML ツリー JSON）。処理レシピの選択肢も同じ順。
  const orderedBackends = useMemo(() => sortParserBackends(backendOptions), [backendOptions]);
  const serviceIds = useMemo(
    () => [
      ...new Set(
        backendOptions
          .map(serviceIdForBackend)
          .filter((serviceId): serviceId is string => Boolean(serviceId))
      ),
    ],
    [backendOptions]
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
                  const selected = form.adapter_backend === backend;
                  const serviceId = serviceIdForBackend(backend);
                  const runtime = serviceId ? runtimeByServiceId.get(serviceId) : null;
                  const runtimeStatus = runtime?.status ?? null;
                  const runtimeProfile = runtime?.profile ?? serviceProfileForBackend(backend);
                  const capability = findParserCapability(settings.capabilities, backend);
                  const supportedFormats = formatSupportedFormats(capability);
                  const supportedExtensions = formatSupportedExtensions(capability);
                  return (
                    <div key={backend} className="flex min-w-0 flex-col gap-1">
                    <div className="relative min-w-0 flex-1">
                      <input
                        id={`settings-parser-backend-${backend}`}
                        className="peer absolute inset-0 z-10 cursor-pointer opacity-0 disabled:cursor-not-allowed"
                        type="radio"
                        name="settings-parser-backend"
                        value={backend}
                        checked={selected}
                        disabled={saving}
                        onChange={() => onBackendChange(backend)}
                      />
                      <label
                        data-surface-tint={selected ? "accent" : undefined}
                        htmlFor={`settings-parser-backend-${backend}`}
                        className={cn(
                          "block h-full cursor-pointer min-h-[5.43rem] rounded-md border px-3 py-2 text-left transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
                          selected
                            ? "border-accent-emphasis bg-accent-subtle text-fg"
                            : "border-border bg-surface text-fg peer-hover:bg-surface-hover"
                        )}
                      >
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="text-sm font-semibold">{backendLabel(backend)}</span>
                          {runtimeProfile ? <ServiceProfileBadge profile={runtimeProfile} /> : null}
                        </span>
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
                        <span className="mt-2 flex flex-wrap items-center gap-1.5">
                          {runtimeStatus ? <ServiceStatusBadge status={runtimeStatus} /> : null}
                        </span>
                      </label>
                    </div>
                    </div>
                  );
                })}
            </div>
          </div>
          {form.adapter_backend === "local" ? (
            <p className="text-xs leading-relaxed text-warning-fg">
              {t("settings.parserAdapters.legacyBackendNotice")}
            </p>
          ) : null}
        </div>
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
  return t("settings.parserAdapters.backend.xml_tree_json");
}

function backendLabel(backend: ParserAdapterBackend) {
  if (backend === "local") return t("settings.parserAdapters.backend.local");
  return adapterLabel(backend);
}

function backendOptionsFromSettings(settings: ParserAdapterSettingsData): ParserAdapterBackend[] {
  const ordered: ParserAdapterBackend[] = settings.adapters.map((adapter) => adapter.backend);
  const selected = settings.adapter_backend;
  if (selected !== "local" && !ordered.includes(selected)) ordered.push(selected);
  return [...new Set(ordered)];
}

function serviceIdForBackend(backend: ParserAdapterBackend): string | null {
  if (!isAdapterBackend(backend)) return null;
  return PARSER_BACKEND_SERVICE_IDS[backend] ?? null;
}

function serviceProfileForBackend(backend: ParserAdapterBackend): ServiceProfile | null {
  return isAdapterBackend(backend) ? "cpu" : null;
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
    structured_records_jsonl_enabled: enabledByBackend.get("structured_records_jsonl") ?? false,
    xml_tree_json_enabled: enabledByBackend.get("xml_tree_json") ?? false,
  };
}

function adapterFlagField(
  adapter: ParserAdapterBackendName
): ParserAdapterFlagField {
  return ADAPTER_FLAG_FIELDS[adapter];
}

function adapterBackendFlagUpdate(
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
    structured_records_jsonl_enabled: form.structured_records_jsonl_enabled,
    xml_tree_json_enabled: form.xml_tree_json_enabled,
  });
}

function parserSettingsUpdate(form: ParserAdapterForm): ParserAdapterSettingsUpdate {
  return {
    adapter_backend: form.adapter_backend,
    docling_enabled: form.docling_enabled,
    unstructured_enabled: form.unstructured_enabled,
    structured_records_jsonl_enabled: form.structured_records_jsonl_enabled,
    xml_tree_json_enabled: form.xml_tree_json_enabled,
  };
}
