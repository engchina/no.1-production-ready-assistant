import { useResetExecutionConsent, useWorkspaceActivation, useWorkspaceState } from "@/components/WorkspaceState";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import { Database, FileSpreadsheet, RefreshCw, Trash2 } from "lucide-react";

import {
  Banner,
  Button,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  ExecutionConfirmationField,
  ProcessingIndicator,
  SelectField,
} from "@production-ready/ui";

import { PageNotice } from "@/components/page-notice";
import { apiGet, apiPost, isAbortError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { useRequestScope } from "@/lib/useRequestScope";
import { DbAdminExecutionResult } from "../components/DbAdminShared";
import { DbObjectName } from "../components/DbObjectName";
import {
  DbManagementLoadingSkeleton,
  DbObjectManagementPanelShell,
  DbObjectManagementTabs,
  DbObjectPanelHeader,
  type DbObjectTab,
} from "../components/DbObjectManagementShared";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
  SchemaSyncNotice,
} from "../components/SchemaRefreshFeedback";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import { useMutationSchemaSync } from "../useMutationSchemaSync";
import type { SampleDataInfo, SampleDataMutationData, SampleDataset } from "../types";

type SampleStep = "tables" | "views" | "data" | "all";
type SampleAction = "import" | "delete";

const SAMPLE_DATA_ID = "sample-data";
/** 実行の結果の直下の案内の「DB 構造を再取得」の起点（SchemaRefreshCoordinator の start(origin)。#821 / #2031）。 */
const SAMPLE_DATA_SCHEMA_REFRESH_NOTICE = "sample-data-notice";
const SAMPLE_STEPS: SampleStep[] = ["all", "tables", "views", "data"];
const SAMPLE_DATASETS: SampleDataset[] = ["hr", "sales", "inquiries"];

function sampleStepLabel(step: SampleStep) {
  return t(`dataTools.sample.step.${step}`);
}

function joinSql(statements: string[]) {
  return statements.join(";\n\n");
}

function SampleObjectSummary({ sampleInfo }: { sampleInfo: SampleDataInfo | null }) {
  return (
    <section className="grid gap-2 rounded-md border border-border bg-surface-sunken p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-fg">{t("dataTools.sample.objects")}</p>
        <div className="flex flex-wrap items-center gap-2">
          <span className="sr-only" data-testid="sample-data-object-count">
            {formatNumber(sampleInfo?.objects.length ?? 0)}
          </span>
          <StatusBadge
            icon={false}
            variant="neutral"
            label={`${t("dataTools.sample.metric.objects")} ${formatNumber(sampleInfo?.objects.length ?? 0)}`}
          />
          <span className="sr-only" data-testid="sample-data-imported-count">
            {formatNumber(sampleInfo?.imported_objects.length ?? 0)}
          </span>
          <StatusBadge
            variant="success"
            label={`${t("dataTools.sample.metric.imported")} ${formatNumber(sampleInfo?.imported_objects.length ?? 0)}`}
          />
        </div>
      </div>
      <ul className="grid gap-1 sm:grid-cols-2" data-testid="sample-data-object-list">
        {(sampleInfo?.objects ?? []).map((objectName) => {
          const ref = sampleInfo?.object_refs?.find((item) => item.name === objectName);
          const conflict = Boolean(sampleInfo?.conflicting_objects?.includes(objectName));
          const imported = !conflict && Boolean(sampleInfo?.imported_objects.includes(objectName));
          // 状態は色だけに頼らず、ラベルとアイコン付きの StatusBadge で示す。
          return (
            <li
              key={objectName}
              className="flex min-w-0 flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface px-2 py-1"
              data-testid="sample-data-object"
            >
              <DbObjectName
                object={{ name: objectName, owner: ref?.owner ?? sampleInfo?.owner, qualified_name: ref?.qualified_name }}
                size="xs"
                className="min-w-0"
              />
              <StatusBadge
                variant={conflict ? "warning" : imported ? "success" : "neutral"}
                label={t(
                  conflict
                    ? "dataTools.sample.objectStatus.conflict"
                    : imported
                      ? "dataTools.sample.objectStatus.imported"
                      : "dataTools.sample.objectStatus.notImported"
                )}
              />
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function SampleSqlPreview({ sql }: { sql: string }) {
  return (
    <section className="grid gap-2">
      <div>
        <p className="font-semibold text-fg">{t("dataTools.sample.sqlPreview")}</p>
        <p className="mt-1 text-sm text-fg-muted">{t("dataTools.sample.sqlPreviewHint")}</p>
      </div>
      <pre data-surface="code" className="max-h-80 overflow-auto rounded-md border border-border bg-surface p-3 text-sm leading-6 text-fg">
        <code>{sql || "-"}</code>
      </pre>
    </section>
  );
}

export function SampleDataPage() {
  const [dataset, setDataset] = useWorkspaceState<SampleDataset>("sampleData.dataset", "hr");
  const [sampleInfo, setSampleInfo] = useState<SampleDataInfo | null>(null);
  const [sampleLoadFailed, setSampleLoadFailed] = useState(false);
  const [sampleStep, setSampleStep] = useWorkspaceState<SampleStep>("sampleData.step", "all");
  const [activeAction, setActiveAction] = useWorkspaceState<SampleAction>("sampleData.action", "import");
  const [sampleConfirmation, setSampleConfirmation] = useState("");
  const [sampleResult, setSampleResult] = useState<SampleDataMutationData | null>(null);
  const [loading, setLoading] = useState("");
  // どのボタンが始めた読込か。スピナーは押したボタンだけが出す（#819）。初回の読込は §3.7 のとおりヘッダーの
  // 「最新情報を取得」が出す。種類の切り替え・画面へ戻ったときの読込は null（作業領域の読込表示がスピナーを出す）。
  const [loadOrigin, setLoadOrigin] = useState<"header" | "notice" | null>("header");
  const [datasetSeen, setDatasetSeen] = useState(false);
  const [message, setMessage] = useState("");
  // 取込・削除の実行の失敗は実行のボタンの直下に出す（ページ先頭へ送らない。messaging.md §10.1、#724）。
  const [executeError, setExecuteError] = useState("");
  const loadSequence = useRef(0);
  // dataset の切り替えで始めた読み込みの回数。取得は effect で行う。
  const [datasetLoadRequest, setDatasetLoadRequest] = useState(0);
  // 報告済みの schema refresh の終端（`<job_id>:<status>`）。
  const { abortAll, run: runScopedRequest } = useRequestScope();
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  const schemaRefreshing = sharedSchemaRefresh.isRefreshing;

  const sampleInfoUrl = dataset === "hr" ? "/api/nl2sql/sample-data" : `/api/nl2sql/sample-data?dataset=${encodeURIComponent(dataset)}`;
  const sampleReady = Boolean(sampleInfo) && (sampleInfo?.dataset ?? "hr") === dataset && !sampleLoadFailed;
  const expectedConfirmation = sampleInfo?.confirmation ?? "";
  const confirmationMatched = sampleReady && Boolean(expectedConfirmation) && sampleConfirmation.trim() === expectedConfirmation;
  const isDeleteAction = activeAction === "delete";

  const sampleSqlPreview = useMemo(() => {
    if (!sampleInfo) return "";
    if (activeAction === "delete") return joinSql(sampleInfo.sql.delete ?? []);
    const steps = sampleStep === "all" ? ["tables", "views", "data"] : [sampleStep];
    return joinSql(steps.flatMap((step) => sampleInfo.sql[step] ?? []));
  }, [activeAction, sampleInfo, sampleStep]);

  useResetExecutionConsent(() => setSampleConfirmation(""), JSON.stringify([dataset, activeAction, sampleStep, sampleSqlPreview, expectedConfirmation]));

  // sample 情報を取り直す。loading / message は呼び出し側で先に設定しておく。
  // state の更新は応答の callback の中だけで行う（effect からも呼ぶため）。
  const fetchSampleInfo = (announce: boolean) => {
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    return runScopedRequest(async (signal) => {
      const data = await apiGet<SampleDataInfo>(sampleInfoUrl, { signal });
      if (!signal.aborted && sequence === loadSequence.current) {
        setSampleInfo(data);
        setSampleLoadFailed(false);
      }
    })
      .then(() => {
        if (announce && sequence === loadSequence.current) {
          toast.success(t("common.action.refreshed"));
        }
      })
      .catch((err: unknown) => {
        if (isAbortError(err)) {
          return;
        }
        setSampleLoadFailed(true);
        setMessage(err instanceof Error ? err.message : t("dataTools.error.sample"));
      })
      .finally(() => {
        if (sequence === loadSequence.current) setLoading("");
      });
  };

  const load = async (announce = false, origin: "header" | "notice" | null = null) => {
    if (loading) return;
    setLoading("load");
    setLoadOrigin(origin);
    setMessage("");
    await fetchSampleInfo(announce);
  };

  // dataset が変わったレンダーで読み込み中の表示にし（effect で setState しない）、取得は effect で行う。
  // 別の処理の実行中は読み込まない（load() と同じ）。
  const datasetChanged = useValuesChanged([dataset]);
  if (datasetChanged && !loading) {
    // 最初のレンダーは初回の読込（ヘッダーが回る）。以降の種類の切り替えはボタンを回さない（#819）。
    if (datasetSeen) setLoadOrigin(null);
    else setDatasetSeen(true);
    setLoading("load");
    setMessage("");
    setDatasetLoadRequest((request) => request + 1);
  }
  // 取得は読み込みの要求が増えたときだけ行う。取得の関数は毎レンダー作り直すので、最新のものを ref から呼ぶ。
  const fetchSampleInfoRef = useRef(fetchSampleInfo);
  useLayoutEffect(() => { fetchSampleInfoRef.current = fetchSampleInfo; });
  useEffect(() => {
    if (datasetLoadRequest > 0) void fetchSampleInfoRef.current(false);
  }, [datasetLoadRequest]);
  // abortAll は固定の関数なので、dataset が変わったときだけ前の読み込みを捨てる条件は変わらない。
  useEffect(() => {
    return () => {
      loadSequence.current += 1;
      abortAll();
    };
  }, [dataset, abortAll]);

  // 初回取得は上の effect が担い、keep-alive からの復帰時は状態だけ再検証する。
  const visited = useRef(false);
  useWorkspaceActivation(() => {
    if (visited.current) void load();
    visited.current = true;
  });

  const reloadSampleState = async () => {
    const sequence = loadSequence.current;
    await runScopedRequest(async (signal) => {
      const info = await apiGet<SampleDataInfo>(sampleInfoUrl, { signal });
      if (!signal.aborted && sequence === loadSequence.current) setSampleInfo(info);
    });
  };

  // 再取得は同期・再取得の job が完了したときだけ行う。sampleInfoUrl（dataset）が変わっても再取得しないよう、
  // 最新の関数を ref から呼ぶ。
  const reloadSampleStateRef = useRef(reloadSampleState);
  useLayoutEffect(() => { reloadSampleStateRef.current = reloadSampleState; });
  const reloadAfterSchemaSync = useCallback(() => {
    void reloadSampleStateRef.current().catch((err: unknown) => {
      setMessage(err instanceof Error ? err.message : t("dataTools.error.sample"));
    });
  }, []);
  // 取込・削除の後の DB 構造の同期の案内と進行は、実行の結果の直下の 1 か所に出す。案内の中の
  // 「DB 構造を再取得」の進行は、そのボタンの直下に出す（#2031）。
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: SAMPLE_DATA_SCHEMA_REFRESH_NOTICE,
    onDone: reloadAfterSchemaSync,
  });

  // 取込・削除を実行し、結果を出してから状態を取り直す。実行の API の失敗だけを実行のボタンの直下に出し、
  // 実行が成功した後の取り直しの失敗は読込の失敗としてページ上部の案内（「再読み込み」）に出す。実行が
  // 失敗したように見せず、backend が投入したスキーマの更新の job は取り直しの成否にかかわらず追う（#948）。
  const runSampleMutation = async (action: SampleAction) => {
    if (loading || !confirmationMatched || !sampleInfo) return;
    setSampleResult(null);
    schemaSync.reset();
    setLoading(action === "delete" ? "sample-delete" : "sample-import");
    setExecuteError("");
    try {
      let result: SampleDataMutationData;
      try {
        result = await apiPost<SampleDataMutationData>(`/api/nl2sql/sample-data/${action}`, {
          dataset,
          step: action === "delete" ? "all" : sampleStep,
          confirmation: sampleConfirmation.trim(),
          reason: `ui-sample-${action}`,
        });
      } catch (err) {
        setExecuteError(err instanceof Error ? err.message : t("dataTools.error.sample"));
        return;
      }
      setSampleResult(result);
      schemaSync.track(result);
      if (!result.executed) return;
      try {
        await reloadSampleState();
      } catch (err) {
        if (!isAbortError(err)) {
          setMessage(err instanceof Error ? err.message : t("dataTools.error.sample"));
        }
      }
    } finally {
      setLoading("");
    }
  };

  const actionTitle = isDeleteAction ? t("dataTools.sample.delete") : t("dataTools.sample.import");
  const datasetLabel = t(`dataTools.sample.dataset.${dataset}`);
  const actionDescription = isDeleteAction
    ? t("dataTools.sample.deleteHint", { name: datasetLabel })
    : t("dataTools.sample.importHint", { name: datasetLabel });
  // 読込（load）のスピナーは押したボタン（PageHeader の「最新情報を取得」か案内の「再読み込み」）の 1 つだけにし、
  // もう一方は無効にするだけ（同じ処理のスピナーは 1 つ。messaging §3.7、#416。押したボタンだけが回す。#819）。
  // 種類の切り替えなどボタンを押していない読込は、どちらも回さず作業領域の読込表示がスピナーを出す。
  const noticeLoadPressed = loading === "load" && loadOrigin === "notice";
  const pageNoticeActionLoading = noticeLoadPressed;
  const pageNoticeActionDisabled = (Boolean(loading) && !noticeLoadPressed) || schemaRefreshing;
  // 案内の取り直しは、失敗（danger）なら「再試行」、警告ならヘッダーの「最新情報を取得」と名前で区別できる
  // 「サンプルデータの最新情報を取得」（buttons.md §7「表示を取り直す操作」、#1868）。
  const pageNoticeShown = Boolean(message || sampleInfo?.warnings.length);
  // 押したボタンが見えていて回っているときだけ、作業領域の読込表示はスピナーを出さない（案内は読込で消えることがある）。
  const loadButtonSpinning =
    loading === "load" && (loadOrigin === "header" || (loadOrigin === "notice" && pageNoticeShown));

  return (
    <>
      <PageHeader wide
        title={t("sampleData.title")}
        subtitle={t("sampleData.subtitle")}
        status={<SchemaRefreshHeaderStatus testId="sample-data-schema-refresh-status" />}
        actionsTestId="sample-data-actions"
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => load(true, "header"),
            loading: loading === "load" && loadOrigin === "header",
            disabled: Boolean(loading) && !(loading === "load" && loadOrigin === "header"),
          },
        ]}
      />
      <PageBody wide className="grid gap-4">
        <PageNotice
          notice={
            message
              ? { tone: "danger", message }
              : sampleInfo?.warnings.length
                  ? { tone: "warning", message: sampleInfo.warnings.join("\n") }
                  : null
          }
          action={
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={pageNoticeActionLoading}
              disabled={pageNoticeActionDisabled}
              onClick={() => void load(false, "notice")} icon={RefreshCw}>
              <span>
                {message ? t("common.retry") : t("dataTools.sample.refresh")}
              </span>
            </Button>
          }
        />

        {/* 種類の選択（1）と、その説明・クエリ例（2）を同じ行に置き、選択欄だけを左に残さない。 */}
        <section className="grid min-w-0 gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] lg:items-end lg:gap-x-6" aria-label={t("dataTools.sample.dataset.label")}>
          <SelectField<SampleDataset>
            id="sample-data-dataset"
            label={t("dataTools.sample.dataset.label")}
            value={dataset}
            options={SAMPLE_DATASETS.map((item) => ({ value: item, label: t(`dataTools.sample.dataset.${item}`) }))}
            disabled={Boolean(loading) || schemaRefreshing}
            // 右に出す種類の説明を読み上げる（#647）。
            describedBy="sample-data-dataset-description"
            onValueChange={(nextDataset) => {
              setSampleInfo(null);
              setSampleResult(null);
              setSampleConfirmation("");
              setMessage("");
              setExecuteError("");
              schemaSync.reset();
              setDataset(nextDataset);
            }}
            // 種類の名前（「問い合わせサンプルデータ」など）は名前の幅（md。#1891）。
            width="md"
            className="min-w-0"
          />
          <div className="grid min-w-0 gap-1 lg:min-h-11 lg:content-center">
            <p id="sample-data-dataset-description" className="text-sm text-fg-muted">{t(`dataTools.sample.dataset.${dataset}.description`)}</p>
            <p className="text-sm text-fg-muted">{t(`dataTools.sample.dataset.${dataset}.example`)}</p>
          </div>
        </section>

        <DbObjectManagementTabs
          activeView={activeAction}
          disabled={Boolean(loading)}
          tabs={[
            { id: "import", label: t("dataTools.sample.import"), icon: FileSpreadsheet },
            { id: "delete", label: t("dataTools.sample.delete"), icon: Trash2 },
          ] satisfies Array<DbObjectTab<SampleAction>>}
          idPrefix={SAMPLE_DATA_ID}
          ariaLabel={t("dataTools.sample.tabs.label")}
          onViewChange={(view) => {
            setActiveAction(view);
            setSampleResult(null);
            setExecuteError("");
            schemaSync.reset();
          }}
        />

        <DbObjectManagementPanelShell
          id={`sample-data-panel-${activeAction}`}
          labelledBy={`sample-data-tab-${activeAction}`}
          idPrefix={SAMPLE_DATA_ID}
          ariaLabel={t("dataTools.sample.workspace.label")}
          splitId={`sample-data-${activeAction}`}
          processing={
            schemaRefreshing && !schemaSync.ownsProgress ? (
              <SchemaRefreshProcessing testId="sample-data-workspace-processing" />
            ) : undefined
          }
        >
          {loading === "load" ? (
            <DbManagementLoadingSkeleton
              idPrefix="sample-data-workspace-refresh"
              ariaLabel={t("common.processing.refreshing")}
              variant="detail"
              operationKey="sample-data-refresh"
              placement="workspace"
              testId="sample-data-workspace-refresh-skeleton"
              activityIcon={loadButtonSpinning ? "none" : "spinner"}
            />
          ) : (
            <section className="grid min-w-0 content-start gap-4" aria-labelledby="sample-data-action-heading">
              <DbObjectPanelHeader
                headingId="sample-data-action-heading"
                icon={isDeleteAction ? Trash2 : FileSpreadsheet}
                title={actionTitle}
                description={actionDescription}
              />

              {!isDeleteAction && (
                <SelectField<SampleStep>
                  id="sample-data-step"
                  label={t("dataTools.sample.step")}
                  value={sampleStep}
                  options={SAMPLE_STEPS.map((step) => ({ value: step, label: sampleStepLabel(step) }))}
                  disabled={Boolean(loading)}
                  onValueChange={setSampleStep}
                  // 取り込み対象（すべて・テーブル・ビュー・データ）は短い選択（sm。#1891）。
                  width="sm"
                />
              )}

              <ExecutionConfirmationField
                value={sampleConfirmation}
                disabled={Boolean(loading)}
                onChange={setSampleConfirmation}
                confirmed={confirmationMatched}
                placeholder={expectedConfirmation}
                expectedLabel={expectedConfirmation}
                helper={t("dataTools.sample.confirmationHelper", { phrase: expectedConfirmation })}
                actions={
                  <Button
                    type="button"
                    variant={isDeleteAction ? "danger" : "primary"}
                    size="lg"
                    className="w-full sm:w-auto"
                    icon={isDeleteAction ? Trash2 : FileSpreadsheet}
                    loading={loading === (isDeleteAction ? "sample-delete" : "sample-import")}
                    disabled={Boolean(loading) || !confirmationMatched || !sampleInfo}
                    onClick={() => void runSampleMutation(activeAction)}
                  >
                    <span>{actionTitle}</span>
                  </Button>
                }
              />
              {loading === "sample-import" || loading === "sample-delete" ? (
                // 表の作成とデータの投入（または削除）を順に実行し、数十秒かかる（messaging.md §3.7）。
                <ProcessingIndicator
                  active
                  label={t(loading === "sample-delete" ? "dataTools.sample.progress.deleting" : "dataTools.sample.progress.importing")}
                  operationKey={loading}
                  placement="action"
                  activityIcon="none"
                  testId="sample-data-processing"
                />
              ) : null}
              {executeError ? <Banner severity="danger">{executeError}</Banner> : null}
            </section>
          )}

          {loading !== "load" && (
            <section className="grid min-w-0 content-start gap-4">
              <DbObjectPanelHeader
                icon={Database}
                title={t("dataTools.sample.previewTitle")}
                description={t("dataTools.sample.previewHint")}
              />
              <SampleObjectSummary sampleInfo={sampleInfo} />
              <SampleSqlPreview sql={sampleSqlPreview} />
              {sampleResult && (
                <DbAdminExecutionResult
                  result={{
                    executed: sampleResult.executed,
                    runtime: sampleResult.runtime,
                    select_result: null,
                    statements: sampleResult.statements,
                    committed: false,
                    rolled_back: false,
                    warnings: sampleResult.warnings,
                    timing: sampleResult.timing,
                  }}
                />
              )}
              <SchemaSyncNotice
                sync={schemaSync}
                testId="sample-data-schema-sync-notice"
                processingTestId="sample-data-schema-refresh-processing"
              />
            </section>
          )}
        </DbObjectManagementPanelShell>
      </PageBody>
    </>
  );
}
