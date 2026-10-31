import { useWorkspaceState, useWorkspaceRevalidation, useWorkspaceActivation, useWorkspaceActive, WorkspaceResultNotice } from "@/components/WorkspaceState";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowRightLeft, BookOpen, Database, FileText, RefreshCw } from "lucide-react";

import {
  Button,
  EmptyState,
  StatusBadge,
  PageHeader,
  FormStatus,
  Skeleton,
  PageBody,
  ProcessingIndicator,
  TimedLoadingState,
  FixedSplitPane,
  TextareaField,
  SelectField,
  FormGrid,
  DISPLAY_DATE_TIME_SECONDS,
  formatDisplayDateTime,
} from "@production-ready/ui";

import { PageNotice } from "@/components/page-notice";
import { apiGet, apiPost, isAbortError } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";
import { useRequestScope } from "@/lib/useRequestScope";
import {
  DbObjectManagementPanelShell,
  DbObjectManagementTabs,
  DbObjectPanelHeader,
  DbObjectStepIndicator,
  type DbObjectTab,
} from "../components/DbObjectManagementShared";
import { QuestionText } from "../components/QuestionText";
import { FIXED_SPLIT_STORAGE_PREFIX } from "@/lib/ui-store";
import { profileDisplayLabel } from "../profileDisplay";
import { DbObjectName } from "../components/DbObjectName";
import { formatDbObjectName } from "../dbObjectIdentity";
import type {
  Nl2SqlLogicalStructureItem,
  ProfileSummary,
  ProfileSummaryPage,
  ProfileUsageContext,
  ReverseSqlData,
  SchemaObjectDetail,
  SchemaObjectPage,
  SchemaTable,
} from "../types";

type SqlToQuestionPanel = "input" | "structure";
type GeneratedSql = { sql: string; explanation: string; warnings: string[]; at: string };
// 質問の再利用と旧結果の識別に必要な項目だけを同一タブに保存する。
const EMPTY_QUESTION_SNAPSHOT = {
  question: "", logicalStructure: "", sourceSql: "", warnings: [] as string[], generatedAt: "",
};

export function SqlToQuestionPage() {
  useWorkspaceRevalidation();
  const [savedPanel, setActivePanel] = useWorkspaceState<SqlToQuestionPanel | "result">("activePanel", "input");
  // 旧3タブ版で保存した質問候補の選択を、草稿を維持して統合先へ移行する。
  const activePanel: SqlToQuestionPanel = savedPanel === "input" ? "input" : "structure";
  useEffect(() => {
    if (savedPanel === "result") setActivePanel("structure");
  }, [savedPanel, setActivePanel]);
  const [profiles, setProfiles] = useState<ProfileSummary[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<ProfileUsageContext | null>(null);
  const [schemaTables, setSchemaTables] = useState<SchemaTable[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useWorkspaceState("selectedProfileId", "");
  const [sql, setSql] = useWorkspaceState(`sql:${selectedProfileId}`, "");
  const [structureText, setStructureText] = useWorkspaceState(`structureText:${selectedProfileId}`, "");
  const [questionSnapshot, setQuestionSnapshot] = useWorkspaceState(`questionSnapshot:${selectedProfileId}`, EMPTY_QUESTION_SNAPSHOT);
  const reverse = questionSnapshot.generatedAt && questionSnapshot.sourceSql === sql ? questionSnapshot : null;
  // この訪問で生成した結果（render で比べるため state で持つ）。
  const [generatedThisVisit, setGeneratedThisVisit] = useState<typeof EMPTY_QUESTION_SNAPSHOT | null>(null);
  const [editingStructure, setEditingStructure] = useState(false);
  const [sqlGenerationLoading, setSqlGenerationLoading] = useState(false);
  const [sqlGenerationError, setSqlGenerationError] = useState("");
  const [regenerated, setRegenerated] = useState<{ sql: string; explanation: string; warnings: string[]; signature: string; at: string } | null>(null);
  const [questionSql, setQuestionSql] = useState<GeneratedSql | null>(null);
  const [questionSqlLoading, setQuestionSqlLoading] = useState(false);
  const [questionSqlError, setQuestionSqlError] = useState("");
  const questionSqlInFlight = useRef(false);
  const structureSignature = JSON.stringify([selectedProfileId, structureText, false]);
  const workspaceActive = useWorkspaceActive();
  const activeRef = useRef(workspaceActive);
  // 最新の表示状態を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { activeRef.current = workspaceActive; });
  const focusStructure = useRef(false);
  useEffect(() => {
    if (activePanel === "structure" && focusStructure.current && workspaceActive) {
      focusStructure.current = false;
      document.getElementById("sql-to-question-tab-structure")?.focus();
    }
  }, [activePanel, workspaceActive]);
  const [structureItems, setStructureItems] = useState<Nl2SqlLogicalStructureItem[]>([]);
  // Profile・SQL・質問が変わったら、前の SQL 生成結果を render 中に消す。
  if (useValuesChanged([selectedProfileId, sql, reverse])) { setQuestionSql(null); setQuestionSqlError(""); }
  // Profile が変わったら、構造の編集状態を render 中に初期化する。
  if (useValuesChanged([selectedProfileId])) { setStructureItems([]); setEditingStructure(false); setRegenerated(null); setSqlGenerationError(""); }
  const [loading, setLoading] = useState(false);
  // どのボタンが始めた読込か。スピナーは押したボタンだけが出す（#819）。初回の読込は §3.7 のとおり
  // ヘッダーの「最新情報を取得」が出す。業務プロファイルの切り替え・画面へ戻ったときの取り直しは null
  // （業務プロファイルの構造の読込中の表示がスピナーを出す）。
  const [refreshOrigin, setRefreshOrigin] = useState<"header" | "notice" | null>("header");
  const [reverseLoading, setReverseLoading] = useState(false);
  const reverseInFlight = useRef(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [sqlError, setSqlError] = useState("");
  const [structureError, setStructureError] = useState("");
  const [referenceRefreshVersion, setReferenceRefreshVersion] = useState(0);
  const loadSequence = useRef(0);
  const detailSequence = useRef(0);
  const { abortAll, run: runScopedRequest } = useRequestScope();

  // 読込中・エラー表示の初期化は呼び出し側で行う（effect から呼ぶときは render 中に行う）。
  const requestReferenceData = useCallback(async () => {
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    // 業務プロファイルを選んだときは、続く詳細の読込（下の effect）が読込中の表示を終える。
    // ここで先に終えると、詳細の読込の前に「参照できる表がありません」が一瞬出る。
    let detailPending = false;
    try {
      // 失敗の表示は catch の callback で行う（effect から呼んでも同期の setState にしない）。
      await runScopedRequest(async (signal) => {
        const profilePage = await apiGet<ProfileSummaryPage>(
          "/api/nl2sql/profiles/search?limit=100",
          { signal, timeoutMs: API_TIMEOUT_MS.interactiveList }
        );
        if (signal.aborted || sequence !== loadSequence.current) return;
        setProfiles(profilePage.items);
        // 保存した業務プロファイルが一覧（この画面の選択肢）に無い（削除・アーカイブ・利用権限の解除）ときは、
        // 先頭のプロファイルへ切り替える。残すと usage-context が 404 / 403 になり、再読込でも抜けられない（#913）。
        setSelectedProfileId((current) =>
          current && profilePage.items.some((profile) => profile.id === current)
            ? current
            : profilePage.items[0]?.id || ""
        );
        // 一覧が空でなければ、必ずどれかを選ぶ（詳細の読込が続く）。
        detailPending = profilePage.items.length > 0;
        setReferenceRefreshVersion((current) => current + 1);
      }).catch((err: unknown) => {
        if (isAbortError(err)) {
          return;
        }
        setLoadError(actionableError(err, t("sqlToQuestion.error.load")));
      });
    } finally {
      if (sequence === loadSequence.current && !detailPending) setLoading(false);
    }
  }, [runScopedRequest, setSelectedProfileId]);
  const loadReferenceData = useCallback((origin: "header" | "notice" | null) => {
    setLoading(true);
    setRefreshOrigin(origin);
    setLoadError("");
    return requestReferenceData();
  }, [requestReferenceData]);

  // Profile が未選択になったら、詳細を render 中に消す（読込は下の effect で行う）。
  if (useValuesChanged([referenceRefreshVersion, runScopedRequest, selectedProfileId]) && !selectedProfileId) {
    setSelectedProfile(null);
    setSchemaTables([]);
  }

  useEffect(() => {
    // 業務プロファイルの一覧で選択を確かめてから詳細を読む（一覧の読込前は、保存した ID が今も使えるか分からない。#913）。
    if (!selectedProfileId || referenceRefreshVersion === 0) return;
    const sequence = detailSequence.current + 1;
    detailSequence.current = sequence;
    void runScopedRequest(async (signal) => {
      setLoading(true);
      setLoadError("");
      try {
        const params = new URLSearchParams({ limit: "100", profile_id: selectedProfileId });
        const [profile, page] = await Promise.all([
          apiGet<ProfileUsageContext>(
            `/api/nl2sql/profiles/${encodeURIComponent(selectedProfileId)}/usage-context`,
            {
              signal,
              timeoutMs: API_TIMEOUT_MS.interactiveList,
            }
          ),
          apiGet<SchemaObjectPage>(`/api/schema/objects?${params}`, {
            signal,
            timeoutMs: API_TIMEOUT_MS.interactiveList,
          }),
        ]);
        const visibleDetails = await Promise.all(
          page.items.slice(0, 8).map(async (item) => {
            const detail = await apiGet<SchemaObjectDetail>(
              `/api/schema/objects/${encodeURIComponent(item.owner)}/${encodeURIComponent(item.object_name)}`,
              { signal, timeoutMs: API_TIMEOUT_MS.interactiveDetail }
            );
            return [JSON.stringify([item.owner, item.object_name]), detail.table] as const;
          })
        );
        if (signal.aborted || sequence !== detailSequence.current) return;
        const detailsByName = new Map(visibleDetails);
        setSelectedProfile(profile);
        setSchemaTables(
          page.items.map(
            (item) =>
              detailsByName.get(JSON.stringify([item.owner, item.object_name])) ?? schemaSummaryTable(item)
          )
        );
      } catch (error) {
        if (!isAbortError(error) && sequence === detailSequence.current) {
          setLoadError(actionableError(error, t("sqlToQuestion.error.load")));
        }
      } finally {
        if (sequence === detailSequence.current) setLoading(false);
      }
    });
  }, [referenceRefreshVersion, runScopedRequest, selectedProfileId]);

  const referenceVisited = useRef(false);
  useWorkspaceActivation(() => {
    if (referenceVisited.current) void loadReferenceData(null);
    referenceVisited.current = true;
  });
  // 初回表示（と依存の変化）で参照データを読み込む。読込中の表示は render 中に立てる。
  if (useValuesChanged([abortAll, requestReferenceData])) {
    setLoading(true);
    setLoadError("");
  }
  useEffect(() => {
    void requestReferenceData();
    return () => {
      loadSequence.current += 1;
      abortAll();
    };
  }, [abortAll, requestReferenceData]);

  const generateQuestion = async () => {
    const trimmedSql = sql.trim();
    if (actionBusy || reverseInFlight.current) return;
    // 未入力は押せないボタンだけにせず、押したときに欄の直下へ理由を出す（#541）。backend も sql を必須にする。
    if (!trimmedSql) {
      setSqlError(t("sqlToQuestion.sql.error.required"));
      document.getElementById("sql-to-question-sql-input")?.focus();
      return;
    }
    setSqlError("");
    reverseInFlight.current = true;
    setReverseLoading(true);
    focusStructure.current = activeRef.current;
    setActivePanel("structure");
    setActionError("");
    try {
      await runScopedRequest(async (signal) => {
        const data = await apiPost<ReverseSqlData>("/api/nl2sql/reverse/deep", {
          sql: trimmedSql,
          profile_id: selectedProfileId || undefined,
          use_glossary: false,
        }, { signal, timeoutMs: API_TIMEOUT_MS.longRunningJob });
        if (signal.aborted) return;
        const snapshot = {
          question: data.question,
          logicalStructure: data.logical_structure || "",
          sourceSql: sql,
          warnings: data.warnings ?? [],
          generatedAt: new Date().toISOString(),
        };
        setGeneratedThisVisit(snapshot);
        setQuestionSnapshot(snapshot);
        setStructureText(data.logical_structure || "");
        setRegenerated(null);
        setSqlGenerationError("");
        setStructureItems(data.logical_structure_items ?? []);
        focusStructure.current = activeRef.current;
        setActivePanel("structure");
      });
    } catch (err) {
      if (isAbortError(err)) return;
      setActionError(actionableError(err, t("sqlToQuestion.error.reverse")));
      if (activeRef.current) setActivePanel("input");
    } finally {
      reverseInFlight.current = false;
      setReverseLoading(false);
    }
  };

  const generateSql = async () => {
    if (actionBusy) return;
    if (!structureText.trim()) {
      setStructureError(t("sqlToQuestion.structure.error.required"));
      document.getElementById("sql-to-question-structure-input")?.focus();
      return;
    }
    setStructureError("");
    setSqlGenerationLoading(true);
    setSqlGenerationError("");
    try {
      await runScopedRequest(async (signal) => {
        const data = await apiPost<{ sql: string; explanation: string; warnings: string[] }>(
          "/api/nl2sql/reverse/sql",
          { logical_structure: structureText, profile_id: selectedProfileId || undefined, use_glossary: false },
          { signal, timeoutMs: API_TIMEOUT_MS.longRunningJob }
        );
        if (signal.aborted) return;
        setRegenerated({ ...data, signature: structureSignature, at: formatDisplayDateTime(new Date(), DISPLAY_DATE_TIME_SECONDS) });
      });
    } catch (err) {
      if (!isAbortError(err)) setSqlGenerationError(actionableError(err, t("sqlToQuestion.error.regenerate")));
    } finally {
      setSqlGenerationLoading(false);
    }
  };

  const generateQuestionSql = async () => {
    const question = reverse?.question.trim();
    if (!question || actionBusy || questionSqlInFlight.current) return;
    questionSqlInFlight.current = true;
    setQuestionSqlLoading(true);
    setQuestionSqlError("");
    try {
      await runScopedRequest(async (signal) => {
        const data = await apiPost<Omit<GeneratedSql, "at">>(
          "/api/nl2sql/reverse/question-sql",
          { question, profile_id: selectedProfileId || undefined },
          { signal, timeoutMs: API_TIMEOUT_MS.longRunningJob }
        );
        if (signal.aborted) return;
        setQuestionSql({ ...data, at: formatDisplayDateTime(new Date(), DISPLAY_DATE_TIME_SECONDS) });
      });
    } catch (err) {
      if (!isAbortError(err)) setQuestionSqlError(actionableError(err, t("sqlToQuestion.error.questionSql")));
    } finally {
      questionSqlInFlight.current = false;
      setQuestionSqlLoading(false);
    }
  };

  const actionBusy = reverseLoading || sqlGenerationLoading || questionSqlLoading;
  const panels = useMemo(
    () =>
      [
        { id: "input", label: t("sqlToQuestion.tabs.input"), icon: ArrowRightLeft },
        { id: "structure", label: t("sqlToQuestion.tabs.structure"), icon: FileText },
      ] satisfies Array<DbObjectTab<SqlToQuestionPanel>>,
    []
  );
  const activePanelIndex = Math.max(
    0,
    panels.findIndex((panel) => panel.id === activePanel)
  );
  const renderStepIndicator = (panel: SqlToQuestionPanel) =>
    activePanel === panel ? (
      <DbObjectStepIndicator
        steps={panels.map((item) => item.label)}
        activeIndex={activePanelIndex}
        ariaLabel={t("sqlToQuestion.tabs.label")}
        dataTestId="sql-to-question-steps"
      />
    ) : null;

  return (
    <>
      <PageHeader wide
        title={t("nav.sqlToQuestion")}
        subtitle={t("sqlToQuestion.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => void loadReferenceData("header"),
            loading: loading && refreshOrigin === "header",
            disabled: loading && refreshOrigin !== "header",
          },
        ]}
      />
      <PageBody wide className="grid gap-4">
        <PageNotice
          notice={loadError ? { tone: "danger", message: loadError } : null}
          action={
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={loading && refreshOrigin === "notice"}
              disabled={loading && refreshOrigin !== "notice"}
              onClick={() => void loadReferenceData("notice")} icon={RefreshCw}>
              <span>{t("sqlToQuestion.action.reload")}</span>
            </Button>
          }
        />

        <DbObjectManagementTabs
          activeView={activePanel}
          tabs={panels}
          idPrefix="sql-to-question"
          ariaLabel={t("sqlToQuestion.tabs.label")}
          onViewChange={setActivePanel}
        />

        <DbObjectManagementPanelShell
          id="sql-to-question-panel-input"
          labelledBy="sql-to-question-tab-input"
          idPrefix="sql-to-question"
          ariaLabel={t("sqlToQuestion.input.title")}
          className={activePanel === "input" ? "" : "hidden"}
          topContent={renderStepIndicator("input")}
        >
          <FixedSplitPane
            storagePrefix={FIXED_SPLIT_STORAGE_PREFIX}
            splitId="sql-to-question-input"
            left={
              <section className="grid min-w-0 content-start gap-4" aria-labelledby="sql-to-question-input-heading">
              <DbObjectPanelHeader
                headingId="sql-to-question-input-heading"
                icon={ArrowRightLeft}
                title={t("sqlToQuestion.input.title")}
                description={t("sqlToQuestion.input.hint")}
              />

              {/* 業務プロファイル（対象の選択は lg）と対象 SQL（full）の並びは FormGrid の読みやすい幅まで（#1891）。 */}
              <FormGrid columns={1}>
                {/* 業務プロファイルは空にできない選択欄（読み込み後に先頭を選ぶ）で、API で省略したときも backend が
                    既定の業務プロファイルを使うので、「必須」は付けない（UX 契約 messaging.md §3.2.1。#540）。 */}
                <SelectField
                  id="sql-to-question-profile"
                  label={t("sqlToQuestion.profile.label")}
                  value={selectedProfileId}
                  options={profiles.map((profile) => ({ value: profile.id, label: profileDisplayLabel(profile) }))}
                  onValueChange={(nextProfileId) => {
                    // 業務プロファイルの切り替えによる読込はボタンを回さない（#819）。
                    setRefreshOrigin(null);
                    setSelectedProfileId(nextProfileId);
                    setActionError("");
                    setActivePanel("input");
                  }}
                  disabled={loading || actionBusy || profiles.length === 0}
                  width="lg"
                  className="min-w-0"
                />

                <TextareaField
                  id="sql-to-question-sql-input"
                  label={t("sqlToQuestion.sql.label")}
                  required
                  error={sqlError || undefined}
                  value={sql}
                  onChange={(event) => {
                    setSql(event.currentTarget.value);
                    setStructureText("");
                    setEditingStructure(false);
                    setStructureItems([]);
                    setRegenerated(null);
                    setQuestionSnapshot(EMPTY_QUESTION_SNAPSHOT);
                    setActionError("");
                    setSqlError("");
                    setActivePanel("input");
                  }}
                  rows={9}
                  monospace
                  width="full"
                  textareaClassName="min-w-0"
                  // 下書きは業務プロファイルごとのキー（sql:<id>）に保存する。プロファイルが決まる前に入力させると、
                  // 自動で選ばれた時点でキーが変わって入力が消えるため、決まるまでは入力させない（#455）。
                  disabled={actionBusy || !selectedProfileId}
                />
              </FormGrid>

              <div className="flex flex-col gap-2 border-t border-border pt-4 sm:flex-row sm:flex-wrap sm:items-center">
                <Button
                  type="button"
                  variant="primary"
                  size="lg"
                  className="w-full whitespace-nowrap sm:w-auto"
                  loading={reverseLoading}
                  disabled={actionBusy || loading || !!loadError || !selectedProfile}
                  onClick={() => void generateQuestion()} icon={ArrowRightLeft}>
                  <span>{t("sqlToQuestion.action.generate")}</span>
                </Button>
                <FormStatus tone="danger" message={actionError} className="sm:ml-auto" />
              </div>
              {reverseLoading ? (
                <ProcessingIndicator
                  active
                  label={t("sqlToQuestion.action.generate")}
                  operationKey="reverse-deep"
                  placement="action"
                  testId="sql-to-question-processing"
                  activityIcon="none"
                />
              ) : null}
              </section>
            }
            right={
              <SchemaPreview
                loading={loading}
                buttonSpinning={refreshOrigin !== null}
                profile={selectedProfile}
                tables={schemaTables}
              />
            }
          />
        </DbObjectManagementPanelShell>

        <DbObjectManagementPanelShell
          id="sql-to-question-panel-structure"
          labelledBy="sql-to-question-tab-structure"
          idPrefix="sql-to-question"
          ariaLabel={t("sqlToQuestion.tabs.structure")}
          className={activePanel === "structure" ? "" : "hidden"}
          topContent={renderStepIndicator("structure")}
        >
          <div aria-busy={reverseLoading} className="grid min-w-0 gap-4" data-testid="sql-to-question-analysis-content">
          {reverseLoading && (
            <TimedLoadingState label={t("sqlToQuestion.analysis.loading")} placement="result" framed={false} testId="sql-to-question-analysis-loading">
              <Skeleton className="h-5 w-40" aria-hidden="true" />
              <Skeleton className="h-8 w-full" aria-hidden="true" />
              <Skeleton className="h-80 w-full" aria-hidden="true" />
              <Skeleton className="h-11 w-56 max-w-full" aria-hidden="true" />
              <Skeleton className="h-5 w-32" aria-hidden="true" />
              <Skeleton className="h-28 w-full" aria-hidden="true" />
            </TimedLoadingState>
          )}
          <div hidden={reverseLoading} className="grid min-w-0 gap-4">
          <DbObjectPanelHeader
            headingId="sql-to-question-structure-heading"
            icon={FileText}
            title={t("sqlToQuestion.structure.title")}
            description={t("sqlToQuestion.structure.hint")}
          />
          {structureText || reverse || structureItems.length > 0 || regenerated || editingStructure ? (
            <section className="grid min-w-0 gap-4">
              <TextList label={t("sqlToQuestion.result.warnings")} items={reverse?.warnings ?? []} />
              <TextareaField
                id="sql-to-question-structure-input"
                label={t("sqlToQuestion.structure.editor")}
                required
                error={structureError || undefined}
                className="min-w-0"
                value={structureText}
                onChange={(event) => { setEditingStructure(true); setStructureText(event.currentTarget.value); setStructureItems([]); setSqlGenerationError(""); setStructureError(""); }}
                rows={16}
                disabled={actionBusy}
                monospace
                width="full"
                textareaClassName="min-w-0"
              />
              {structureItems.length > 0 && <LogicalStructureList items={structureItems} />}
              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                <Button type="button" size="lg" loading={sqlGenerationLoading} disabled={actionBusy || loading || !!loadError || !selectedProfile} onClick={() => void generateSql()} icon={ArrowRightLeft}>
                  {t("sqlToQuestion.actions.regenerateSql")}
                </Button>
              </div>
              <FormStatus tone="danger" message={sqlGenerationError} />
              {sqlGenerationLoading && <ProcessingIndicator active label={t("sqlToQuestion.actions.regenerateSql")} operationKey="structure-to-sql" placement="action" activityIcon="none" />}
              {regenerated && (
                <section className="grid min-w-0 gap-2" aria-label={t("sqlToQuestion.regenerated.title")}>
                  <h3 className="font-semibold">{t("sqlToQuestion.regenerated.title")}</h3>
                  <p className="text-sm text-fg-muted">{t("sqlToQuestion.regenerated.at", { at: regenerated.at })}</p>
                  {regenerated.signature !== structureSignature && <FormStatus tone="warning" message={t("sqlToQuestion.regenerated.stale")} />}
                  <pre data-surface="code" className="max-h-96 overflow-auto rounded-md border border-border bg-surface p-4 text-sm leading-6 text-fg"><code>{regenerated.sql}</code></pre>
                  <p className="text-sm">{regenerated.explanation}</p>
                  <TextList label={t("sqlToQuestion.result.warnings")} items={regenerated.warnings ?? []} />
                </section>
              )}
            </section>
          ) : (
            <EmptyState title={t("sqlToQuestion.structure.emptyTitle")} hint={t("sqlToQuestion.structure.emptyHint")} />
          )}
          <section
            id="sql-to-question-results"
            aria-labelledby="sql-to-question-result-heading"
            className="grid min-w-0 gap-4 border-t border-border pt-4"
          >
            <DbObjectPanelHeader
              headingId="sql-to-question-result-heading"
              icon={BookOpen}
              title={t("sqlToQuestion.result.title")}
              description={t("sqlToQuestion.result.hint")}
            />
            {reverse ? (
              <section className="grid content-start gap-3 text-sm">
                {structureText !== reverse.logicalStructure && <FormStatus tone="warning" message={t("sqlToQuestion.result.staleStructure")} />}
                <WorkspaceResultNotice result={reverse} inputSignature={JSON.stringify([selectedProfileId, sql, false])} finishedAt={reverse.generatedAt} restored={reverse !== generatedThisVisit} />
                <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                  <p className="text-xs font-medium text-fg-muted">{t("sqlToQuestion.result.question")}</p>
                  <QuestionText
                    value={reverse.question}
                    variant="detail"
                    maxLines={0}
                    className="mt-1 font-semibold"
                  />
                </div>
                <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                  <Button type="button" size="lg" loading={questionSqlLoading} disabled={actionBusy || !reverse.question.trim() || loading || !!loadError || !selectedProfile} onClick={() => void generateQuestionSql()} icon={ArrowRightLeft}>
                    {t("sqlToQuestion.actions.questionSql")}
                  </Button>
                </div>
                <FormStatus tone="danger" message={questionSqlError} />
                {questionSqlLoading && <ProcessingIndicator active label={t("sqlToQuestion.actions.questionSql")} operationKey="question-to-sql" placement="action" activityIcon="none" />}
                {questionSql && (
                  <section className="grid min-w-0 gap-2" aria-label={t("sqlToQuestion.questionSql.title")}>
                    <h3 className="font-semibold">{t("sqlToQuestion.questionSql.title")}</h3>
                    <p className="text-sm text-fg-muted">{t("sqlToQuestion.regenerated.at", { at: questionSql.at })}</p>
                    <pre data-surface="code" className="max-h-96 overflow-auto rounded-md border border-border bg-surface p-4 text-sm leading-6 text-fg"><code>{questionSql.sql}</code></pre>
                    <p className="text-sm">{questionSql.explanation}</p>
                    <TextList label={t("sqlToQuestion.result.warnings")} items={questionSql.warnings ?? []} />
                  </section>
                )}
              </section>
            ) : (
              <EmptyState
                title={t("sqlToQuestion.result.emptyTitle")}
                hint={t("sqlToQuestion.result.emptyHint")}
              />
            )}
          </section>
          </div>
          </div>
        </DbObjectManagementPanelShell>
      </PageBody>
    </>
  );
}

function SchemaPreview({
  loading,
  buttonSpinning,
  profile,
  tables,
}: {
  loading: boolean;
  buttonSpinning: boolean;
  profile: ProfileUsageContext | null;
  tables: SchemaTable[];
}) {
  if (loading) {
    return (
      <TimedLoadingState
        label={t("sqlToQuestion.schema.loading")}
        operationKey="sql-to-question-schema"
        placement="panel"
        className="min-h-56 content-start"
        testId="sql-to-question-schema-skeleton"
        // 押したボタン（PageHeader・PageNotice の「最新情報を取得」）が回っているときは、そのボタンがスピナーを出す
        // （同じ処理のスピナーは 1 つ。messaging §3.7、#416）。業務プロファイルの切り替えなど、ボタンを押していない
        // 読込はこの表示がスピナーを出す（#819）。
        activityIcon={buttonSpinning ? "none" : "spinner"}
      >
        <Skeleton className="h-5 w-40" aria-hidden="true" />
        <Skeleton className="h-16 w-full" aria-hidden="true" />
        <Skeleton className="h-16 w-full" aria-hidden="true" />
      </TimedLoadingState>
    );
  }

  return (
    <section className="grid content-start gap-3 rounded-md border border-border bg-surface-sunken p-3 text-sm">
      <div className="flex flex-wrap gap-2">
        <StatusBadge icon={false} variant="neutral" label={profile ? profileDisplayLabel(profile) : "-"} />
        <span data-testid="sql-to-question-table-count">
          <StatusBadge icon={false} variant="info" label={t("sqlToQuestion.schema.tableCount", { count: tables.length })} />
        </span>
      </div>
      <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
        <Database size={16} aria-hidden="true" />
        {t("sqlToQuestion.schema.title")}
      </h3>
      {tables.length === 0 ? (
        <EmptyState
          title={t("sqlToQuestion.schema.emptyTitle")}
          hint={t("sqlToQuestion.schema.empty")}
        />
      ) : (
        <div className="grid max-h-96 gap-2 overflow-auto pr-1">
          {tables.map((table) => (
            <section key={JSON.stringify([table.owner, table.table_name])} className="rounded-md border border-border bg-surface p-3">
              <p className="font-semibold text-fg">
                {table.logical_name || table.table_name}
                <DbObjectName
                  object={{ owner: table.owner, name: table.table_name, qualified_name: table.qualified_name }}
                  size="xs"
                  className="ml-2"
                />
              </p>
              <p className="mt-1 text-xs leading-5 text-fg-muted">{table.comment || "-"}</p>
              <p className="mt-2 break-words font-sans text-xs leading-5 text-fg">
                {table.columns
                  .slice(0, 8)
                  .map((column) => column.logical_name || column.column_name)
                  .join(", ") || "-"}
              </p>
            </section>
          ))}
        </div>
      )}
    </section>
  );
}

function actionableError(error: unknown, fallback: string) {
  const message = error instanceof Error ? error.message : fallback;
  return `${message} ${t("sqlToQuestion.error.retryHint")}`;
}

function schemaSummaryTable(item: SchemaObjectPage["items"][number]): SchemaTable {
  return {
    table_name: item.object_name,
    qualified_name: formatDbObjectName({ owner: item.owner, name: item.object_name }),
    logical_name: item.logical_name,
    owner: item.owner,
    table_type: item.object_type,
    comment: item.comment,
    row_count: item.row_count,
    columns: [],
    constraints: [],
  };
}

// SQL 論理構造の項目 kind -> 見出し文言。未知の kind は技術詳細だけを見出し無しで出す。
const STRUCTURE_LABEL_KEYS: Record<string, string> = {
  summary: "sqlToQuestion.structure.summary",
  statement: "sqlToQuestion.structure.statement",
  operations: "sqlToQuestion.structure.operations",
  filters: "sqlToQuestion.structure.filters",
  joins: "sqlToQuestion.structure.joins",
  group_by: "sqlToQuestion.structure.groupBy",
  order_by: "sqlToQuestion.structure.orderBy",
  aggregations: "sqlToQuestion.structure.aggregations",
};

/** SQL 論理構造を「見出し + 業務者向け説明 + 技術詳細」で併記する。 */
function LogicalStructureList({ items }: { items: Nl2SqlLogicalStructureItem[] }) {
  return (
    <dl
      className="grid gap-2"
      aria-label={t("sqlToQuestion.structure.listAria")}
      data-testid="sql-to-question-structure-list"
    >
      {items.map((item, index) => {
        const labelKey = STRUCTURE_LABEL_KEYS[item.kind ?? ""];
        return (
          <div
            key={`${index}-${item.kind ?? ""}`}
            className="grid gap-1 rounded-md border border-border bg-surface px-3 py-2"
            data-structure-kind={item.kind || undefined}
          >
            <dt className="text-xs font-medium text-fg-muted">
              {labelKey ? t(labelKey) : (item.kind ?? "")}
            </dt>
            <dd className="grid min-w-0 gap-1">
              <span className="min-w-0 text-sm leading-6 text-fg [overflow-wrap:anywhere]">
                {item.business}
              </span>
              {item.technical && (
                <span className="flex min-w-0 items-start gap-1.5 text-xs leading-5 text-fg-muted">
                  <span className="sr-only">{t("nl2sql.logicalSteps.technicalSrLabel")}</span>
                  <span
                    data-surface-tint="neutral"
                    className="mt-0.5 shrink-0 rounded bg-surface-hover px-1.5 font-medium"
                    aria-hidden="true"
                  >
                    {t("nl2sql.logicalSteps.technicalLabel")}
                  </span>
                  <code className="min-w-0 font-mono [overflow-wrap:anywhere]">
                    {item.technical}
                  </code>
                </span>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function TextList({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-fg-muted">{label}</p>
      <ul className="grid gap-1">
        {items.map((item, index) => (
          <li
            // 同一文が並ぶことがあるため index を key に含める。
            key={`${index}-${item}`}
            className="flex min-w-0 items-start gap-2 rounded-md border border-border bg-surface px-3 py-2 text-fg"
          >
            <span className="min-w-0 [overflow-wrap:anywhere]">{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
