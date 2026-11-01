"use client";

import {
  ApiErrorBanner,
  Disclosure,
  PageBody,
  Button,
  CopyButton,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormStatus,
  ProcessingIndicator,
  RowActionMenu,
  Skeleton,
  StatusBadge,
  type EntityAction,
  type StatusVariant,
  TimedLoadingState,
  ListSkeleton,
  presentApiError,
  useActionPending,
  surfaceTintFor,
  DISPLAY_TIME_SECONDS,
  formatDisplayDateTime,
} from "@production-ready/ui";
import { Fragment, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  CircleSlash,
  CircleX,
  HardDriveDownload,
  Hourglass,
  MinusCircle,
  Play,
  RefreshCw,
  RotateCw,
  Server,
  SlidersHorizontal,
  Square,
  TerminalSquare,
  type LucideIcon,
} from "lucide-react";

import { ApiErrorState } from "@/components/StateViews";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  ApiError,
  type DeploymentMode,
  type ServiceCatalogItemData,
  type ServiceExecutionPolicy,
  type ServiceLogsData,
  type ServiceModelCacheData,
  type ServiceProfile,
  type ServiceRuntimeStatus,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import {
  useControlService,
  useServiceCatalog,
  useServiceLogs,
  useServiceStatusQueries,
} from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

import { serviceStages } from "./service-stages";

export type DisplayRuntimeStatus = ServiceRuntimeStatus | "loading" | "error";
type DisplayServiceData = ServiceCatalogItemData & {
  status: DisplayRuntimeStatus;
  statusReady: boolean;
};

const PROFILE_META: Record<ServiceProfile, { className: string; labelKey: I18nKey }> = {
  cpu: { className: "bg-surface-hover text-fg-muted", labelKey: "settings.services.profile.cpu" },
  gpu: { className: "bg-accent-muted text-accent-fg-strong", labelKey: "settings.services.profile.gpu" },
};
// 並びは CPU → GPU。サービス管理の工程の中の節の順（OCI のプロキシの節は #22 で削除した）。
export const SERVICE_PROFILE_ORDER: ServiceProfile[] = ["cpu", "gpu"];
const PROFILE_GROUP_META: Record<
  ServiceProfile,
  { suffixKey: I18nKey; noteKey: I18nKey | null }
> = {
  cpu: { suffixKey: "settings.services.cpuSuffix", noteKey: "settings.services.cpuNote" },
  // GPU のサービスは今は無い（ASR は削除した。#2042）。行が無ければ節ごと出さない。
  gpu: { suffixKey: "settings.services.gpuSuffix", noteKey: null },
};

export function serviceExecutionPolicyLabelKey(policy: ServiceExecutionPolicy): I18nKey {
  switch (policy) {
    case "default_adapter":
      return "settings.services.executionPolicy.defaultAdapter";
    case "selected_adapter":
      return "settings.services.executionPolicy.selectedAdapter";
  }
}

export function serviceStoppedHintKey(policy: ServiceExecutionPolicy): I18nKey {
  switch (policy) {
    case "default_adapter":
      return "settings.services.defaultStoppedHint";
    case "selected_adapter":
      return "settings.services.optionalStoppedHint.selectedAdapter";
  }
}

/**
 * 行に常に表示する起動 / 停止のどちらか 1 つを状態から決める（#158）。
 * 稼働中・一部異常・起動中（unit は動いている）は「停止」、それ以外（停止中・起動失敗・未登録・
 * 未設定・状態の取得中 / 失敗）は「起動」。
 */
export function servicePrimaryAction(status: DisplayRuntimeStatus): "start" | "stop" {
  return status === "running" || status === "degraded" || status === "starting" ? "stop" : "start";
}

/** 再起動を出す状態（unit が動いているときだけ。停止中は「起動」を使う）。 */
export function serviceCanRestart(status: DisplayRuntimeStatus): boolean {
  return status === "running" || status === "degraded" || status === "starting";
}

type ServiceControlAction = "start" | "stop" | "restart";
/** 実行中の操作（サービス ID → 操作）。 */
type ServicePendingActions = Partial<Record<string, ServiceControlAction>>;

/** 失敗した操作（サービス ID → 操作と例外）。次にそのサービスの操作を始めるまで行に残す。 */
type ServiceActionFailures = Partial<
  Record<string, { action: ServiceControlAction; error: unknown }>
>;

const SERVICE_FAILURE_TITLE_KEYS = {
  start: "settings.services.failure.start",
  stop: "settings.services.failure.stop",
  restart: "settings.services.failure.restart",
} as const satisfies Record<ServiceControlAction, I18nKey>;

const SERVICE_PROCESSING_LABEL_KEYS = {
  start: "settings.services.processing.start",
  stop: "settings.services.processing.stop",
  restart: "settings.services.processing.restart",
} as const satisfies Record<ServiceControlAction, I18nKey>;

/** ファイル準備・文書解析などの工程のサービスの稼働可視化・起動/停止を行う設定画面。 */
export function ServicesManagementClient() {
  const query = useServiceCatalog();
  const serviceIds = query.data?.services.map((service) => service.service_id) ?? [];
  const statusQueries = useServiceStatusQueries(serviceIds);
  const control = useControlService();
  const confirm = useConfirm();
  // 実行中の操作（サービス ID → 操作）。クリックした行・操作だけにスピナーを出す。
  // 別のサービスの操作は並行して実行できるので、サービスごとに持つ（1 つの値にすると、後から始めた
  // 操作が先の操作の実行中の表示を消し、先のサービスのボタンが押せるようになる）。
  const [pending, setPending] = useState<ServicePendingActions>({});
  // 操作の失敗は Toast ではなく、操作した行の直下に出す（messaging.md §10）。サービスごとに持つ。
  const [failures, setFailures] = useState<ServiceActionFailures>({});
  const [logsServiceId, setLogsServiceId] = useState<string | null>(null);
  // 「最新情報を取得」を押した再取得の間だけボタンを回す。5 秒ごとの状態の polling では回さない（静かな polling は
  // 処理中を出さない。同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
  const [manualRefreshing, setManualRefreshing] = useState(false);
  const logsQuery = useServiceLogs(logsServiceId);

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.loading")}
          operationKey="settings-services-load"
          placement="page"
          testId="settings-services-loading"
        >
          <Skeleton className="h-40 w-full rounded-lg" />
          <Skeleton className="h-40 w-full rounded-lg" />
        </TimedLoadingState>
      </PageBody>
    );
  }

  if (query.isError) {
    return (
      <PageBody wide>
        <ApiErrorState
          error={query.error}
          fallback={t("settings.services.loadError")}
          onRetry={() => void query.refetch()}
        />
      </PageBody>
    );
  }

  const data = query.data;
  if (!data) return null;

  const displayServices = data.services.map<DisplayServiceData>((service) => {
    const statusQuery = statusQueries[serviceIds.indexOf(service.service_id)];
    const statusData = statusQuery?.data;
    if (statusData) {
      return {
        ...service,
        ...statusData,
        statusReady: true,
      };
    }
    return {
      ...service,
      status: statusQuery?.isError ? "error" : "loading",
      statusReady: false,
    };
  });
  const controlEnabled = data.control_enabled;
  const deploymentMode = data.deployment_mode;
  // 工程の並び・名前・説明はサイドナビ（nav-config.ts の「取込・検索・回答設定」）と対応する設定画面の説明を正本にする
  // （#638）。各工程は CPU/GPU のうち存在するプロファイルごとにグループを分けて表示する。
  // プロファイル表示順と suffix/note。GPU は単独でも opt-in/要件を note で明示する。
  const PROFILE_ORDER = SERVICE_PROFILE_ORDER.map((profile) => ({
    profile,
    ...PROFILE_GROUP_META[profile],
  }));
  const stageGroups = serviceStages().map(({ category, labelKey, descriptionKey }) => {
    const label = t(labelKey);
    const description = descriptionKey ? t(descriptionKey) : undefined;
    const groups = PROFILE_ORDER.map((p) => ({
      ...p,
      services: displayServices.filter((s) => s.category === category && s.profile === p.profile),
    })).filter((g) => g.services.length > 0);
    return { category, label, description, groups };
  });

  async function act(service: DisplayServiceData, action: ServiceControlAction) {
    if (action === "stop") {
      const ok = await confirm({
        title: t("settings.services.confirm.stop.title"),
        description: t(service.execution_policy === "default_adapter"
          ? "settings.services.confirm.stop.defaultDescription"
          : "settings.services.confirm.stop.description", {
          service: serviceLabel(service),
        }),
        confirmLabel: t("settings.services.confirm.stop.confirm"),
        cancelLabel: t("settings.services.confirm.cancel"),
        tone: "danger",
      });
      if (!ok) return;
    }
    const serviceId = service.service_id;
    setPending((current) => ({ ...current, [serviceId]: action }));
    // 次の操作を始めたら、そのサービスの前の失敗を消す（messaging.md §10.4）。
    setFailures(({ [serviceId]: _previous, ...rest }) => rest);
    // `mutate` に渡すコールバックは最後に呼んだ操作の分しか呼ばれないため、操作ごとの promise で
    // 結果を受け取る（別のサービスの操作を続けて始めても、先の操作の結果を出し、実行中の表示を外す）。
    try {
      await control.mutateAsync({ serviceId, action });
      const toastKey: I18nKey =
        action === "start"
          ? "settings.services.toast.started"
          : action === "restart"
            ? "settings.services.toast.restarted"
            : "settings.services.toast.stopped";
      toast.success(t(toastKey, { service: serviceLabel(service) }));
    } catch (error) {
      setFailures((current) => ({ ...current, [serviceId]: { action, error } }));
    } finally {
      setPending(({ [serviceId]: _done, ...rest }) => rest);
    }
  }

  const latestStatusUpdatedAt = Math.max(
    0,
    ...statusQueries.map((statusQuery) => statusQuery.dataUpdatedAt)
  );
  const lastUpdated = Math.max(query.dataUpdatedAt, latestStatusUpdatedAt);
  const lastUpdatedText = lastUpdated
    ? formatDisplayDateTime(lastUpdated, DISPLAY_TIME_SECONDS)
    : null;
  function refreshServices() {
    setManualRefreshing(true);
    void Promise.allSettled([
      query.refetch(),
      ...statusQueries.map((statusQuery) => statusQuery.refetch()),
    ]).finally(() => setManualRefreshing(false));
  }

  function toggleLogs(service: DisplayServiceData) {
    setLogsServiceId((current) => (current === service.service_id ? null : service.service_id));
  }

  return (
    <PageBody wide>
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
            <div className="flex items-start gap-3">
              <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
                <Server size={20} aria-hidden />
              </div>
              <div>
                <CardTitle>{t("settings.services.overview.title")}</CardTitle>
                <CardDescription>
                  {t("settings.services.overview.description")}
                </CardDescription>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <ModeBadge mode={deploymentMode} />
              <ControlBadge enabled={controlEnabled} />
              <Button
                type="button"
                variant="secondary"
                size="sm"
                loading={manualRefreshing}
                onClick={refreshServices}
                aria-label={t("settings.services.refresh")} icon={RefreshCw}>
                {t("settings.services.refresh")}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-2">
          {controlEnabled ? (
            <FormStatus
              tone="info"
              message={t(
                deploymentMode === "local"
                  ? "settings.services.mode.local.hint"
                  : "settings.services.mode.deployed.hint"
              )}
            />
          ) : (
            <FormStatus tone="info" message={t("settings.services.controlDisabled.hint")} />
          )}
          <ServiceCommandsDisclosure mode={deploymentMode} />
          {lastUpdatedText ? (
            <p className="text-xs tabular-nums text-fg-muted">
              {t("settings.services.lastUpdated", { time: lastUpdatedText })}
            </p>
          ) : null}
        </CardContent>
      </Card>

      {stageGroups.map((stage) => {
        // 単一プロファイルかつ CPU のときだけ suffix 無しのステージ名にする。
        // 複数プロファイル、または GPU は suffix(+note)を付けて区別・要件を明示する。
        const multi = stage.groups.length > 1;
        return (
          <Fragment key={stage.category}>
            {stage.groups.map((g, index) => (
              <ServiceGroup
                key={`${stage.category}-${g.profile}`}
                title={
                  multi || g.profile !== "cpu"
                    ? t(g.suffixKey, { stage: stage.label })
                    : stage.label
                }
                // 工程の説明（設定画面の説明と同じ文）は、工程の最初のグループにだけ出す（CPU/GPU で繰り返さない）。
                description={index === 0 ? stage.description : undefined}
                note={
                  // CPU note(Docling 既定)は文書解析(parser)の工程のみ。ファイル準備の CPU には出さない。
                  g.noteKey && (g.profile !== "cpu" || stage.category === "parser")
                    ? t(g.noteKey)
                    : undefined
                }
                services={g.services}
                controlEnabled={controlEnabled}
                pending={pending}
                failures={failures}
                logsServiceId={logsServiceId}
                logsQuery={logsQuery}
                onAct={act}
                onToggleLogs={toggleLogs}
              />
            ))}
          </Fragment>
        );
      })}
    </PageBody>
  );
}

/**
 * サーバー上で状態・ログを確かめるコマンドを、RAG 検索の詳細条件と同じ折りたたみで提示する(既定で閉じる)。
 * 開発環境では unit と sudoers を登録するコマンドも出す(rag/scripts/rag-services.sh)。
 */
function ServiceCommandsDisclosure({ mode }: { mode: DeploymentMode }) {
  const [open, setOpen] = useState(false);
  const commands = [
    {
      label: t("settings.services.commands.status.label"),
      command: "systemctl list-units --all 'production-ready-rag-*'",
    },
    {
      label: t("settings.services.commands.logs.label"),
      command: "sudo journalctl -u production-ready-rag-parser-docling.service -f",
    },
    ...(mode === "local"
      ? [
          {
            label: t("settings.services.commands.install.label"),
            command: "scripts/rag-services.sh install",
          },
        ]
      : []),
  ];
  return (
    // 共有の Disclosure（card・sunken）。見出しの高さは 40px・タッチ端末 44px（#1135）。
    // コマンドは開いたときだけ描く（閉じている間はページ内検索・読み上げに出さない）。
    <Disclosure
      summary={t("settings.services.commands.title")}
      icon={SlidersHorizontal}
      surface="sunken"
      open={open}
      onOpenChange={setOpen}
    >
      {open ? (
        <div id="service-commands" className="space-y-2">
          <p className="text-xs leading-relaxed text-fg-muted">
            {t("settings.services.commands.description")}
          </p>
          {commands.map((entry) => (
            <CommandRow key={entry.label} label={entry.label} command={entry.command} />
          ))}
        </div>
      ) : null}
    </Disclosure>
  );
}

/** ラベル付きコマンドを等幅表示し、ワンクリックでクリップボードへコピーする行。 */
function CommandRow({ label, command }: { label: string; command: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-fg">{label}</p>
      <div className="flex items-stretch gap-2">
        <code className="flex-1 overflow-x-auto whitespace-nowrap rounded border border-border bg-surface px-3 py-2 font-mono text-xs text-fg">
          {command}
        </code>
        <CopyButton
          size="sm"
          className="shrink-0 whitespace-nowrap"
          text={command}
          label={t("settings.services.commands.copy")}
          copiedMessage={t("settings.services.commands.copied")}
          failedMessage={t("settings.services.commands.copyFailed")}
        />
      </div>
    </div>
  );
}

function ServiceGroup({
  title,
  description,
  note,
  services,
  controlEnabled,
  pending,
  failures,
  logsServiceId,
  logsQuery,
  onAct,
  onToggleLogs,
}: {
  title: string;
  description?: string;
  note?: string;
  services: DisplayServiceData[];
  controlEnabled: boolean;
  pending: ServicePendingActions;
  failures: ServiceActionFailures;
  logsServiceId: string | null;
  logsQuery: UseQueryResult<ServiceLogsData>;
  onAct: (service: DisplayServiceData, action: ServiceControlAction) => void;
  onToggleLogs: (service: DisplayServiceData) => void;
}) {
  if (services.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
        {note ? <CardDescription>{note}</CardDescription> : null}
      </CardHeader>
      <CardContent className="space-y-2">
        <ul className="divide-y divide-border">
          {services.map((service) => (
            <ServiceRow
              key={service.service_id}
              service={service}
              controlEnabled={controlEnabled}
              pending={pending}
              failure={failures[service.service_id]}
              logsOpen={logsServiceId === service.service_id}
              logsQuery={logsQuery}
              onAct={onAct}
              onToggleLogs={onToggleLogs}
            />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function ServiceRow({
  service,
  controlEnabled,
  pending,
  failure,
  logsOpen,
  logsQuery,
  onAct,
  onToggleLogs,
}: {
  service: DisplayServiceData;
  controlEnabled: boolean;
  pending: ServicePendingActions;
  failure?: ServiceActionFailures[string];
  logsOpen: boolean;
  logsQuery: UseQueryResult<ServiceLogsData>;
  onAct: (service: DisplayServiceData, action: ServiceControlAction) => void;
  onToggleLogs: (service: DisplayServiceData) => void;
}) {
  const stopped = service.status === "stopped";
  const statusLoading = service.status === "loading";
  const statusError = service.status === "error";
  const stoppedHintKey = stopped ? serviceStoppedHintKey(service.execution_policy) : null;
  const notInstalled = service.status === "not_installed";
  const failed = service.status === "failed";
  const pendingAction = pending[service.service_id];
  const startPending = pendingAction === "start";
  const stopPending = pendingAction === "stop";
  const restartPending = pendingAction === "restart";
  // ponytail: このサービス自身の操作中だけ自分の起動/停止/再起動を排他する(他サービスは無関係)
  const thisPending = startPending || stopPending || restartPending;
  let controlHint: string | undefined;
  if (!controlEnabled) {
    controlHint = t("settings.services.controlDisabled.hint");
  } else if (notInstalled) {
    controlHint = t("settings.services.notInstalledHint");
  } else if (statusLoading) {
    controlHint = t("settings.services.statusLoadingHint");
  } else if (statusError) {
    controlHint = t("settings.services.statusLoadErrorHint");
  } else if (stoppedHintKey) {
    controlHint = t(stoppedHintKey);
  }

  const logsPanelId = `service-logs-${service.service_id}`;
  const primaryAction = servicePrimaryAction(service.status);
  const primaryPending = primaryAction === "start" ? startPending : stopPending;
  // ログ・再起動などの副操作は行に 1 個の RowActionMenu にまとめる（buttons.md §5.1）。
  const rowActions: EntityAction[] = [
    {
      id: "logs",
      label: logsOpen ? t("settings.services.action.hideLogs") : t("settings.services.action.logs"),
      icon: TerminalSquare,
      testId: `service-action-logs-${service.service_id}`,
      onSelect: () => onToggleLogs(service),
    },
    {
      id: "restart",
      label: t("settings.services.action.restart"),
      icon: RotateCw,
      visible: serviceCanRestart(service.status),
      loading: restartPending,
      disabled: !controlEnabled || !service.statusReady || (thisPending && !restartPending),
      testId: `service-action-restart-${service.service_id}`,
      onSelect: () => onAct(service, "restart"),
    },
  ];

  return (
    <li className="py-3" data-testid={`service-row-${service.service_id}`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold text-fg">{serviceLabel(service)}</p>
            <ServiceExecutionPolicyBadge policy={service.execution_policy} />
          </div>
          <p className="font-mono text-xs text-fg-muted">{service.service_id}</p>
          {service.model_cache ? <ServiceModelCacheRow cache={service.model_cache} /> : null}
          {notInstalled || failed ? (
            <p className="mt-1 flex items-center gap-1 text-xs text-fg-muted">
              <AlertTriangle size={14} aria-hidden />
              {t(notInstalled ? "settings.services.notInstalledHint" : "settings.services.failedHint")}
            </p>
          ) : null}
          {stoppedHintKey ? (
            <p className="mt-1 text-xs text-fg-muted">{t(stoppedHintKey)}</p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ServiceStatusBadge status={service.status} />
          {/* 行に常に出すのは状態に応じた起動 / 停止の 1 つだけ（#158）。 */}
          <span className="inline-flex" title={controlHint}>
            <Button
              type="button"
              variant="secondary"
              tone={primaryAction === "stop" ? "danger" : "default"}
              size="sm"
              loading={primaryPending}
              disabled={
                !controlEnabled ||
                !service.statusReady ||
                notInstalled ||
                (thisPending && !primaryPending)
              }
              onClick={() => onAct(service, primaryAction)}
              aria-label={`${serviceLabel(service)} ${t(`settings.services.action.${primaryAction}` as I18nKey)}`}
              icon={primaryAction === "stop" ? Square : Play}
              data-testid={`service-primary-action-${service.service_id}`}
            >
              {t(`settings.services.action.${primaryAction}` as I18nKey)}
            </Button>
          </span>
          <RowActionMenu
            actions={rowActions}
            ariaLabel={t("common.objectActions.aria", { name: serviceLabel(service) })}
            loading={restartPending}
            testId={`service-row-actions-${service.service_id}`}
          />
        </div>
      </div>
      {thisPending ? (
        // 起動・停止・再起動はコンテナの起動待ちやモデルの読み込みで数十秒かかる。
        // スピナーは操作したボタン（または行メニュー）の loading が担う（messaging.md §3.7）。
        <ProcessingIndicator
          active
          label={t(SERVICE_PROCESSING_LABEL_KEYS[pendingAction ?? "restart"], {
            service: serviceLabel(service),
          })}
          operationKey={`${service.service_id}:${pendingAction}`}
          placement="action"
          activityIcon="none"
          className="mt-3 rounded-md border border-border bg-surface-sunken px-3 py-2"
          testId={`service-processing-${service.service_id}`}
        />
      ) : null}
      {failure && !thisPending ? <ServiceActionFailure service={service} failure={failure} /> : null}
      {logsOpen ? (
        <ServiceLogPanel id={logsPanelId} service={service} logsQuery={logsQuery} />
      ) : null}
    </li>
  );
}

/**
 * 起動・停止・再起動の失敗（処理の失敗）。操作した行の直下に danger の Banner で出す（messaging.md §10）。
 * 見出しに何が起きたか、本文に backend の原因と対処、HTTP ステータス・エラーコードなどは開いた「詳細」に出す。
 */
function ServiceActionFailure({
  service,
  failure,
}: {
  service: DisplayServiceData;
  failure: NonNullable<ServiceActionFailures[string]>;
}) {
  const presentation = presentApiError(failure.error, t("settings.services.failure.fallback"));
  return (
    <ApiErrorBanner
      error={failure.error}
      fallback={t("settings.services.failure.fallback")}
      summary={t(SERVICE_FAILURE_TITLE_KEYS[failure.action], { service: serviceLabel(service) })}
      nextAction={[presentation.summary, presentation.nextAction].filter(Boolean).join("")}
      className="mt-3"
      testId={`service-failure-${service.service_id}`}
    />
  );
}

function ServiceExecutionPolicyBadge({ policy }: { policy: ServiceExecutionPolicy }) {
  return (
    <span
      data-surface-tint="neutral"
      className="inline-flex min-h-5 items-center rounded-full bg-surface-hover px-2 py-0.5 text-xs font-medium text-fg-muted"
    >
      {t(serviceExecutionPolicyLabelKey(policy))}
    </span>
  );
}

function ServiceLogPanel({
  id,
  service,
  logsQuery,
}: {
  id: string;
  service: DisplayServiceData;
  logsQuery: UseQueryResult<ServiceLogsData>;
}) {
  const content = logsQuery.data?.content ?? "";
  // 「最新情報を取得」を押した取り直しの間だけ回す。サービスの起動・停止の後の invalidate（["services"] の下）や
  // ウィンドウのフォーカスでの取り直しでは回さない（#819）。
  const manualRefetch = useActionPending();

  return (
    <div
      id={id}
      data-surface="code"
      className="mt-3 overflow-hidden rounded-md border border-border bg-surface text-fg"
    >
      <div className="flex flex-col gap-2 border-b border-border bg-surface-raised px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold text-fg">
            {t("settings.services.logs.title", { service: serviceLabel(service) })}
          </p>
          {logsQuery.data ? (
            <p className="mt-0.5 text-xs text-fg-muted">
              {t("settings.services.logs.source.journald", {
                unit: service.systemd_unit,
                lines: String(logsQuery.data.lines),
              })}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            // 初回の取得は下の読込表示がスピナーを出す。ボタンは押した再取得だけ回す
            // （同じ処理のスピナーは 1 つ。messaging §3.7、#416 / #819）。
            loading={manualRefetch.pending}
            disabled={logsQuery.isPending}
            onClick={() => void manualRefetch.track(() => logsQuery.refetch())}
            aria-label={t("settings.services.logs.refresh")} icon={RefreshCw}>
            {t("settings.services.logs.refresh")}
          </Button>
          <CopyButton
            size="sm"
            disabled={!content}
            text={content}
            label={t("settings.services.logs.copy")}
            copiedMessage={t("settings.services.logs.copied")}
            failedMessage={t("settings.services.logs.copyFailed")}
          />
        </div>
      </div>
      {logsQuery.isPending ? (
        <TimedLoadingState
          label={t("settings.services.logs.loading")}
          operationKey="services-logs-load"
          framed={false}
          className="px-3 py-3"
          testId="services-logs-loading"
        >
          <ListSkeleton rows={4} rowClassName="h-4" className="gap-2" />
        </TimedLoadingState>
      ) : logsQuery.isError ? (
        <div className="px-3 py-4 text-xs text-danger-fg" role="alert">
          {logsQuery.error instanceof ApiError
            ? logsQuery.error.message
            : t("settings.services.logs.loadError")}
        </div>
      ) : content ? (
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words px-3 py-3 font-mono text-xs leading-relaxed text-fg">
          {content}
        </pre>
      ) : (
        <div className="px-3 py-4 text-xs text-fg-muted">
          {t("settings.services.logs.empty")}
        </div>
      )}
    </div>
  );
}

/** 配備モード(全環境で systemd の unit を操作する)を示すバッジ(色だけに頼らずアイコン+ラベル併記)。 */
function ModeBadge({ mode }: { mode: DeploymentMode }) {
  const Icon = mode === "local" ? TerminalSquare : Server;
  return (
    <span
      data-surface-tint={mode === "local" ? "info" : "accent"}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium",
        mode === "local" ? "bg-info-subtle text-info-fg" : "bg-accent-muted text-accent-fg-strong"
      )}
    >
      <Icon size={14} aria-hidden />
      {t(`settings.services.mode.${mode}`)}
    </span>
  );
}

/** 起動/停止が全体で有効か無効かを示すバッジ。 */
function ControlBadge({ enabled }: { enabled: boolean }) {
  return (
    <span
      data-surface-tint={enabled ? "success" : "neutral"}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium",
        enabled ? "bg-success-subtle text-success-fg" : "bg-surface-hover text-fg-muted"
      )}
    >
      {t("settings.services.controlEnabled")}:{" "}
      {enabled
        ? t("settings.services.controlEnabled.on")
        : t("settings.services.controlEnabled.off")}
    </span>
  );
}

export function ServiceProfileBadge({ profile }: { profile: ServiceProfile }) {
  const meta = PROFILE_META[profile];
  return (
    <span
      data-surface-tint={surfaceTintFor(meta.className)}
      className={cn(
        "inline-flex rounded-sm px-1.5 py-0.5 text-xs font-medium whitespace-nowrap",
        meta.className
      )}
    >
      {t(meta.labelKey)}
    </span>
  );
}

/**
 * 稼働状態 → 共有の StatusBadge の variant / アイコンの対応表（messaging.md §10.2。#723）。
 * 状態の取得中は回るアイコンを使わない（状態の polling は静かに行い、スピナーは押したボタンだけ。§3.7）。
 */
export const SERVICE_STATUS_BADGE: Record<
  DisplayRuntimeStatus,
  { variant: StatusVariant; icon: LucideIcon }
> = {
  running: { variant: "success", icon: CheckCircle2 },
  degraded: { variant: "warning", icon: AlertTriangle },
  starting: { variant: "info", icon: Hourglass },
  failed: { variant: "danger", icon: CircleX },
  stopped: { variant: "neutral", icon: CircleSlash },
  not_installed: { variant: "neutral", icon: CircleDashed },
  unconfigured: { variant: "neutral", icon: MinusCircle },
  loading: { variant: "neutral", icon: Hourglass },
  error: { variant: "danger", icon: AlertTriangle },
};

/** 稼働状態バッジ（共有の StatusBadge。色だけに頼らずアイコン + 日本語ラベル）。 */
export function ServiceStatusBadge({ status }: { status: DisplayRuntimeStatus }) {
  const { variant, icon } = SERVICE_STATUS_BADGE[status];
  return (
    <StatusBadge
      variant={variant}
      icon={icon}
      label={t(`settings.services.status.${status}` as I18nKey)}
    />
  );
}

function serviceLabel(service: ServiceCatalogItemData): string {
  return t(service.label_key as I18nKey);
}

/** モデルキャッシュの場所(サービスの実行ユーザーの ~/.cache)を表示する行。 */
function ServiceModelCacheRow({ cache }: { cache: ServiceModelCacheData }) {
  return (
    <p
      className="mt-1 flex flex-wrap items-center gap-1 text-xs text-fg-muted"
      title={t("settings.services.modelCache.hint")}
    >
      <HardDriveDownload size={14} aria-hidden />
      <span className="text-fg-muted">{t("settings.services.modelCache.label")}:</span>
      <span className="font-mono break-all text-fg">{cache.path}</span>
      <span data-surface-tint="neutral" className="rounded-sm bg-surface-hover px-1.5 py-0.5 text-xs font-medium text-fg-muted">
        {t("settings.services.modelCache.readonly")}
      </span>
    </p>
  );
}

export default ServicesManagementClient;
