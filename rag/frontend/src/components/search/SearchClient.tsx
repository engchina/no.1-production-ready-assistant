"use client";

import {
  AnswerFooter,
  DisclosureChevron,
  PageBody,
  PageHeader,
  Banner,
  Button,
  FieldActionRow,
  FormGrid,
  TextareaField,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Switch,
  TextField,
  TimedLoadingState,
  Skeleton,
  StatusBadge,
  RunStopButton,
  type ChatProgressStep,
} from "@production-ready/ui";
import {
  Plus,
  Search as SearchIcon,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { CategorySelectFields } from "@/components/documents/CategorySelectFields";
import { SearchAnswerProfileSelect, SearchAnswerProfileSelectSkeleton } from "@/components/search-answer-profiles/SearchAnswerProfileSelect";
import { AnswerCitations, CitationList } from "./CitationList";
import {
  buildFeedbackContentSnapshot,
  FeedbackControls,
} from "@/components/feedback/FeedbackControls";
import { ApiErrorState, EmptyState, ErrorState } from "@/components/StateViews";
import {
  api,
  type ApprovedFaqSuggestionData,
  type RetrievedChunk,
  type SearchDiagnostics,
} from "@/lib/api";
import { streamSearch } from "@/lib/search-stream";
import { useRagTraceProgress } from "@/lib/trace-progress";
import { isSubmitEnter } from "@/lib/keyboard";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";
import {
  useAllSearchAnswerProfiles,
  useSearchExtractionFields,
} from "@/lib/queries";
import { isNullableString, useWorkspaceState } from "@/lib/workspace-state";
import { AnswerNotices } from "./AnswerDetailsPanel";
import { AnswerProcess } from "./AnswerProcess";
import { answerAttachedImages, answerModelHeading, parseAnswerDiagnostics, unreadSectionLinks } from "@/lib/answer-diagnostics";
import { canOpenDocumentDetail } from "@/lib/route-permissions";
import { useAuth } from "@/components/security/AuthProvider";
import { AnswerText } from "./AnswerText";
import { ApprovedFaqAnswer, ApprovedFaqSuggestions } from "./ApprovedFaqSuggestions";
import { ExtractionFieldFilters } from "./ExtractionFieldFilters";
import {
  EXTRACTION_FIELD_FILTER_KEY,
  type ExtractionFieldFilterRow,
  extractionFieldConditionLabel,
  extractionFieldConditions,
  extractionFieldFilterValue,
  isActiveExtractionFieldFilterRow,
  isExtractionFieldFilterRows,
  parseExtractionFieldFilterValue,
} from "./extraction-field-filters";
import {
  CLASSIFICATION_FILTER_KEYS,
  type ClassificationFilterValues,
  EMPTY_CLASSIFICATION_FILTERS,
  hasSearchPrefillParams,
  SEARCH_PREFILL_KEYS,
  searchPrefillFromParams,
} from "./search-prefill";

type Phase = "idle" | "streaming" | "done" | "cancelled" | "error";

interface Meta {
  trace_id: string;
  elapsed_ms: number;
  guardrail_warnings: string[];
  diagnostics: Partial<SearchDiagnostics> | null;
  /** 回答の注意書き（#2405。無いときは null）。 */
  notice?: string | null;
}

interface SearchRun {
  /** この実行で回答を生成したか（実行後にスイッチを変えても表示は実行時のまま）。 */
  generateAnswer: boolean;
  startedAtMs: number;
  startedAtIso: string;
  endedAtMs: number | null;
  traceId: string | null;
  /** 実行の id（配信の `run`。処理の経過を実行の trace の API で読む。#3226）。届くまでは null。 */
  runId: string | null;
}

/**
 * 検索 1 回の条件。「検索」は入力欄・選択から作り、表示中の結果への操作（再試行・類似問を使用しない・
 * 「自動」の条件を外す）はこの条件で送り直す（入力欄を書き換えていても前の質問を送る。#914）。
 */
interface SearchRequestSnapshot {
  query: string;
  searchAnswerProfileId: string;
  generateAnswer: boolean;
  filters: Record<string, string>;
  /** 質問から読み取った条件のうち、利用者が外した項目（#652）。 */
  excludedAutoFields: string[];
}

/**
 * RAG 検索画面。チャットと同じ工程（質問の理解・拡張・検索・rerank）で検索し、既定は検索結果までを表示する。
 * 「LLM で回答を生成する」をオンにしたときだけ、チャットと同じく回答（CRAG を含む）まで行う（#649）。
 */
export function SearchClient() {
  // 「回答に未反映の関連章節」の行から章節の原文を開くリンクは、文書の詳細を開ける利用者だけ（#2040・#303）。
  const canOpenSections = canOpenDocumentDetail(useAuth().hasPermission);
  // span の段の名前（段の名前・技術の span の名前）は root の文言で付ける（#3226）。
  // 入力中の質問・検索・回答プロファイル・詳細条件は、ページを行き来しても再読込しても残す（workspace-state.md）。
  // 回答・引用などの結果は保存せず、戻っただけで検索を送り直さない。
  // 実行の詳細の「RAG 検索で開く」（#3309）は URL で質問と条件を渡す。開いたときに一度だけ入力欄と条件へ入れ
  // （保存値より優先し、URL に無い条件は既定に戻す）、URL から外す。検索はしない（利用者が「検索」を押す）。
  const [searchParams, setSearchParams] = useSearchParams();
  const [prefill] = useState(() => searchPrefillFromParams(searchParams));
  const [query, setQuery] = useWorkspaceState("search.query", "", undefined, prefill?.question);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<RetrievedChunk[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  // 検索の失敗（要約・次の操作・「詳細」の trace_id などを出す。#2023）。
  const [searchError, setSearchError] = useState<unknown>(null);
  const [classification, setClassification] = useWorkspaceState<ClassificationFilterValues>(
    "search.classification",
    EMPTY_CLASSIFICATION_FILTERS,
    isClassificationFilterValues,
    prefill?.classification
  );
  const [classificationOpen, setClassificationOpen] = useState(() =>
    Boolean(prefill && Object.values(prefill.classification).some((value) => value))
  );
  // 抽出項目の値の条件（#549）。行は作業状態に残し、項目の型は選んだ検索・回答プロファイルの KB の定義から引く。
  const [extractionRows, setExtractionRows] = useWorkspaceState<ExtractionFieldFilterRow[]>(
    "search.extractionFields",
    [],
    isExtractionFieldFilterRows,
    prefill?.extractionRows
  );
  const [extractionOpen, setExtractionOpen] = useState(() => Boolean(prefill?.extractionRows.length));
  // URL で条件を入れたときは詳細条件を開き、入った条件を見せる。
  const [advancedOpen, setAdvancedOpen] = useWorkspaceState(
    "search.advancedOpen",
    false,
    undefined,
    prefill
      ? prefill.extractionRows.length > 0 ||
          Object.values(prefill.classification).some((value) => value)
      : undefined
  );
  // 回答の生成は既定でオフ（検索結果までを表示する。#649）。選んだ値は作業状態に残す。
  const [generateAnswer, setGenerateAnswer] = useWorkspaceState(
    "search.generateAnswer",
    false,
    undefined,
    prefill?.generateAnswer
  );
  const [appliedFilters, setAppliedFilters] = useState<Record<string, string>>({});
  // 対象の検索・回答プロファイルは 1 つ（#635）。チャットと同じく ID を 1 つだけ作業状態に残す。
  const [searchAnswerProfileId, setSearchAnswerProfileId] = useWorkspaceState<string | null>(
    "search.searchAnswerProfileId",
    null,
    isNullableString,
    prefill ? prefill.searchAnswerProfileId : undefined
  );
  // URL の初期値は一度だけ入れ、URL から外す（再読込・戻るで作業状態の値を上書きしない）。
  useEffect(() => {
    if (!hasSearchPrefillParams(searchParams)) return;
    const remaining = new URLSearchParams(searchParams);
    SEARCH_PREFILL_KEYS.forEach((key) => remaining.delete(key));
    setSearchParams(remaining, { replace: true });
  }, [searchParams, setSearchParams]);
  const [scopeError, setScopeError] = useState("");
  const [run, setRun] = useState<SearchRun | null>(null);
  // 表示中の回答を生成したときの検索・回答プロファイル。回答・引用の評価はこの検索・回答プロファイルへ送る。
  // 検索後に選択を変えても、表示中の回答の評価先は変えない（#285）。
  const [answerSearchAnswerProfileId, setAnswerSearchAnswerProfileId] = useState<string | null>(null);
  // 直前の送信が類似 FAQ の提示を飛ばしたか。エラーの再試行を同じ操作にする（#285）。
  const [lastSkipFaq, setLastSkipFaq] = useState(false);
  // 直前に送った検索の条件。再試行・類似問を使用しない・「自動」の条件を外すはこれで送り直す（#914）。
  // 外した「自動」の条件（#652）もここに持ち、入力欄から新しく検索するたびに空に戻す。
  const [lastRequest, setLastRequest] = useState<SearchRequestSnapshot | null>(null);
  const [faqSuggestions, setFaqSuggestions] = useState<ApprovedFaqSuggestionData[] | null>(null);
  const [faqAnswer, setFaqAnswer] = useState<ApprovedFaqSuggestionData | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const navigate = useNavigate();
  const searchAnswerProfilesQuery = useAllSearchAnswerProfiles({ status: "ACTIVE" });
  const searchAnswerProfiles = searchAnswerProfilesQuery.data?.items ?? [];
  // 復元した検索・回答プロファイルがアーカイブ・削除されていたら選択を外す（別の対象へ置き換えない）。
  const searchAnswerProfileMissing =
    Boolean(searchAnswerProfileId) &&
    Boolean(searchAnswerProfilesQuery.data) &&
    !searchAnswerProfilesQuery.data?.has_next &&
    !searchAnswerProfiles.some((view) => view.id === searchAnswerProfileId);
  // 一覧で使えると分かった検索・回答プロファイル。分かるまでは項目・質問の候補を取りに行かない（使えない検索・回答
  // プロファイルの 404 を出さない。#3355）。
  const confirmedSearchAnswerProfileId =
    searchAnswerProfilesQuery.data && !searchAnswerProfileMissing ? searchAnswerProfileId : null;
  // 選んだ検索・回答プロファイルが参照 KB を持たないなら検索しない（利用者の全 KB を検索しない。#304）。
  // backend も 409 で理由を返すが、送信する前にこの場で理由を示す。
  const selectedSearchAnswerProfile = searchAnswerProfiles.find((view) => view.id === searchAnswerProfileId);
  const selectedWithoutKnowledgeBases = selectedSearchAnswerProfile?.knowledge_base_count === 0;
  // 画面を離れたら生成中の検索を止める（backend の pipeline と LLM を無駄に動かし続けない。#285）。
  useEffect(() => () => abortRef.current?.abort(), []);
  // 実行履歴の「RAG 検索で開く」で受け取った検索・回答プロファイルが使えない（削除・アーカイブ・権限なし）ときは、
  // 選択を空にして選び直しを案内する（黙って外さない。#3355）。
  const [prefillProfileUnavailable, setPrefillProfileUnavailable] = useState(false);
  if (
    searchAnswerProfileMissing &&
    !prefillProfileUnavailable &&
    Boolean(prefill?.searchAnswerProfileId) &&
    prefill?.searchAnswerProfileId === searchAnswerProfileId
  ) {
    setPrefillProfileUnavailable(true);
  }
  useEffect(() => {
    if (searchAnswerProfileMissing) setSearchAnswerProfileId(null);
  }, [searchAnswerProfileMissing, setSearchAnswerProfileId]);
  const hasClassificationFilters = Object.values(classification).some((value) => value.trim());
  const hasExtractionFieldFilters = extractionRows.some(isActiveExtractionFieldFilterRow);
  const extractionFieldsQuery = useSearchExtractionFields(
    confirmedSearchAnswerProfileId,
    extractionOpen || hasExtractionFieldFilters
  );
  const fieldFilter = extractionFieldConditions(
    extractionRows,
    extractionFieldsQuery.data?.fields
  );
  const hasFilters = hasClassificationFilters || hasExtractionFieldFilters;
  // 開閉は利用者の操作だけで決める。閉じていても条件は効くので、見出しに「設定中」を出す（#461）。
  const classificationVisible = classificationOpen;
  const hasAdvancedSettings = hasFilters;

  /** 入力欄・選択から検索の条件を作って送る（「検索」ボタンと Enter）。 */
  const runSubmit = async () => {
    const trimmed = query.trim();
    if (!trimmed || phase === "streaming") return;
    if (!searchAnswerProfileId) {
      setScopeError(t("searchAnswerProfiles.scope.required"));
      return;
    }
    if (selectedWithoutKnowledgeBases) return;
    // 誤りのある項目の条件を黙って外して検索しない。条件を開いて最初の誤りへフォーカスする。
    const firstFieldError = extractionRows.find((row) => fieldFilter.errors[row.key]);
    if (firstFieldError) {
      setAdvancedOpen(true);
      setExtractionOpen(true);
      requestAnimationFrame(() => document.getElementById(`${firstFieldError.key}-name`)?.focus());
      return;
    }
    setScopeError("");
    await runRequest(
      {
        query: trimmed,
        searchAnswerProfileId,
        generateAnswer,
        filters: buildSearchFilters({
          classification,
          extractionFields: extractionFieldFilterValue(fieldFilter.conditions),
        }),
        excludedAutoFields: [],
      },
      false
    );
  };

  /**
   * 検索の条件で、類似 FAQ の照会と検索の stream を行う。押した時点で実行中にし、照会の間も「停止」と
   * 進行を出す（照会は embedding を呼ぶことがあり時間がかかる。#915）。
   */
  const runRequest = async (request: SearchRequestSnapshot, skipFaq: boolean) => {
    if (phase === "streaming") return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const startedAtMs = runStartedAtMs();

    setLastRequest(request);
    setSubmittedQuery(request.query);
    setLastSkipFaq(skipFaq);
    setFaqSuggestions(null);
    setFaqAnswer(null);
    setPhase("streaming");
    setAnswer("");
    setCitations([]);
    setMeta(null);
    setSearchError(null);
    setAnswerSearchAnswerProfileId(request.searchAnswerProfileId);
    setAppliedFilters(request.filters);
    setRun({
      generateAnswer: request.generateAnswer,
      startedAtMs,
      startedAtIso: new Date(startedAtMs).toISOString(),
      endedAtMs: null,
      traceId: null,
      runId: null,
    });

    try {
      if (!skipFaq) {
        // 検索・回答プロファイルの承認済み FAQ に類似問があれば、回答生成の前に提示する(rag_poc の類似問)。
        let suggestions: ApprovedFaqSuggestionData[] = [];
        try {
          suggestions = (
            await api.suggestApprovedFaq(
              request.searchAnswerProfileId,
              request.query,
              "search",
              controller.signal
            )
          ).suggestions ?? [];
        } catch {
          // 類似問の照会に失敗しても通常の回答生成は続ける（停止したときは下で抜ける）。
        }
        if (controller.signal.aborted) return;
        if (suggestions.length > 0) {
          // 選ぶまで検索しない。実行の表示を消して FAQ の提示を出す。
          setFaqSuggestions(suggestions);
          setRun(null);
          setPhase("idle");
          return;
        }
      }

      await streamSearch(
        {
          query: request.query,
          search_answer_profile_id: request.searchAnswerProfileId,
          generate_answer: request.generateAnswer,
          ...(request.excludedAutoFields.length
            ? { auto_field_filter_excluded: request.excludedAutoFields }
            : {}),
          ...(Object.keys(request.filters).length ? { filters: request.filters } : {}),
        },
        {
          onRun: ({ trace_id }) =>
            setRun((current) => (current && current.runId === null ? { ...current, runId: trace_id } : current)),
          onMetadata: (m) => {
            setMeta({
              trace_id: m.trace_id,
              elapsed_ms: m.elapsed_ms,
              guardrail_warnings: m.guardrail_warnings,
              diagnostics: m.diagnostics ?? null,
              notice: m.notice ?? null,
            });
            setRun((current) =>
              current
                ? {
                    ...current,
                    traceId: current.traceId ?? m.trace_id,
                  }
                : current
            );
          },
          onDelta: (text) => setAnswer((prev) => prev + text),
          onReplace: (text) => setAnswer(text),
          onCitations: (list) => setCitations(list),
          onDone: () => {
            finishRun();
            setPhase("done");
            abortRef.current = null;
          },
        },
        controller.signal
      );
      setPhase((current) => (current === "streaming" ? "done" : current));
    } catch (error) {
      if (controller.signal.aborted) return;
      setSearchError(error);
      finishRun();
      setPhase("error");
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  };

  // pointerdown と click の両方から呼ばれる。類似問の照会を await する間も二重送信しないよう ref で守る。
  const submittingRef = useRef(false);
  const guardSubmit = async (run: () => Promise<void>) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    try {
      await run();
    } finally {
      submittingRef.current = false;
    }
  };
  const submit = () => guardSubmit(runSubmit);
  /**
   * 表示中の結果（エラー・類似 FAQ の提示）を出した検索を、同じ条件で送り直す。入力欄・選択を書き換えて
   * いても使わない（#914）。`excludedAutoFields` を渡すと、外した「自動」の条件を差し替える。
   */
  const resubmit = (skipFaq: boolean, excludedAutoFields?: string[]) =>
    guardSubmit(async () => {
      if (!lastRequest) return;
      await runRequest(
        excludedAutoFields ? { ...lastRequest, excludedAutoFields } : lastRequest,
        skipFaq
      );
    });

  const cancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    finishRun();
    setPhase("cancelled");
    setSearchError(null);
  };
  const finishRun = () => {
    setRun((current) =>
      current && current.endedAtMs == null ? { ...current, endedAtMs: Date.now() } : current
    );
  };
  const clearFilters = () => {
    setClassification(EMPTY_CLASSIFICATION_FILTERS);
    setClassificationOpen(false);
    setExtractionRows([]);
    setExtractionOpen(false);
    setAdvancedOpen(false);
  };

  const answerOutcome = parseAnswerDiagnostics(meta?.diagnostics?.answer)?.outcome;
  // 回答を作ったモデル（チャットの列の見出しと同じ。根拠の画像を添付した回答は「（画像も参照）」。#3488）。
  const answerModel =
    phase === "done"
      ? answerModelHeading(
          parseAnswerDiagnostics(meta?.diagnostics?.answer)?.models.llm?.label,
          answerAttachedImages(meta?.diagnostics?.answer, null)
        )
      : null;
  const awaitingClarification = answerOutcome === "needs_clarification";
  const noResults = phase === "done" && citations.length === 0 && !awaitingClarification && answerOutcome !== "needs_human";
  const answerMode = run?.generateAnswer ?? false;
  const isStreaming = phase === "streaming";
  const feedbackSnapshot = buildFeedbackContentSnapshot(submittedQuery, answer, citations);
  // 処理の経過の行（共通の span の表の snapshot を実行の trace の API で読む。チャットと同じ共通の組み立て。#3226）。
  // 実行中は読み直し、終わったら 1 回だけ読む。画面で止めた・失敗した実行は記録の終端が無いことがあるので、実行中の
  // 行を止めた・失敗した状態にする。
  const trace = useRagTraceProgress({
    key: run?.runId ?? null,
    active: phase === "streaming",
    fetchSnapshot: (signal) => api.getSearchRunTrace(run?.runId ?? "", signal),
  });
  const processSteps = run ? searchProcessSteps(trace.steps, phase) : [];
  const processTraceId = run ? trace.traceId : undefined;
  // 処理の経過（#2096。チャットと同じ AnswerProcess）。実行中も終わった後も回答・検索結果の上の同じ場所に 1 つ出す
  // （作成中の「N ステップ完了」が終わると「処理の経過」になり、完了したら一覧を閉じる。末尾に出し直さない。
  // #2115 / #2118）。「処理の詳細」（処理の経過の中の末尾。#2325）は回答の記録の詳細でチャットと同じ。
  const running = phase === "streaming";
  const processView = run ? (
    <AnswerProcess
      // 最初の工程が届くまでは「検索の準備」を今の工程にする（止まって見えない。#1176）。
      steps={
        !running || processSteps.length > 0
          ? processSteps
          : [{ id: "prepare", label: t("answer.progress.preparing"), status: "running", startedAt: run.startedAtIso }]
      }
      progressKey={!running || processSteps.length > 0 ? "steps" : "prepare"}
      active={running}
      startedAt={running ? run.startedAtMs : undefined}
      elapsedMs={
        running ? undefined : (meta?.elapsed_ms ?? (run.endedAtMs != null ? run.endedAtMs - run.startedAtMs : null))
      }
      traceId={running ? undefined : processTraceId}
      partial={trace.partial}
      diagnostics={running ? undefined : meta?.diagnostics?.answer}
      testId="search-run-progress"
    />
  ) : null;
  const conditionChips = (
    <SearchConditionChips
      appliedFilters={appliedFilters}
      meta={meta}
      phase={phase}
      disabled={isStreaming}
      onRemoveAuto={(name) => void resubmit(true, [...(lastRequest?.excludedAutoFields ?? []), name])}
    />
  );

  return (
    <div>
      <PageHeader wide title={t("nav.search")} subtitle={t("search.initial")} />
      <PageBody wide>
        <section className="space-y-6">
          {searchAnswerProfilesQuery.isLoading ? (
            <Card>
              <CardContent className="pt-5">
                <TimedLoadingState
                  label={t("search.searchAnswerProfileLoading")}
                  operationKey="search-search-answer-profiles-load"
                  framed={false}
                  testId="search-search-answer-profiles-loading"
                >
                  <SearchAnswerProfileSelectSkeleton />
                  <Skeleton className="h-[var(--button-height-md)] w-full" />
                </TimedLoadingState>
              </CardContent>
            </Card>
          ) : searchAnswerProfilesQuery.isError ? (
            <ErrorState
              message={t("search.searchAnswerProfileError")}
              onRetry={() => void searchAnswerProfilesQuery.refetch()}
            />
          ) : searchAnswerProfiles.length === 0 ? (
            <Card>
              <CardContent className="pt-5">
                <EmptyState
                  title={t("search.searchAnswerProfileRequired.title")}
                  hint={t("search.searchAnswerProfileRequired.hint")}
                  action={
                    <Button onClick={() => navigate(`${APP_ROUTES.searchAnswerProfiles}?id=new`)} icon={Plus}>
                      {t("search.searchAnswerProfileRequired.cta")}
                    </Button>
                  }
                />
              </CardContent>
            </Card>
          ) : (
            <>
          {/* 検索条件。カードの余白は CardContent の既定（見出しの無いカードは上も同じ pt-5）で、
              チャットの検索・回答プロファイルのカードと 375px でも内側の幅をそろえる（#1778）。 */}
          <Card>
            <CardContent className="space-y-4 pt-5">
              <SearchAnswerProfileSelect
                id={SEARCH_ANSWER_PROFILE_SCOPE_INPUT_ID}
                items={searchAnswerProfiles}
                value={searchAnswerProfileId}
                onChange={(next) => {
                  setSearchAnswerProfileId(next);
                  setScopeError("");
                  setPrefillProfileUnavailable(false);
                }}
                disabled={isStreaming}
                error={
                  scopeError ||
                  (selectedWithoutKnowledgeBases ? t("searchAnswerProfiles.scope.noKnowledgeBases") : "")
                }
              />
              {prefillProfileUnavailable && !searchAnswerProfileId ? (
                // 選択欄の直下に出す（NL2SQL の「SQL 生成で開く」と同じ。#3355）。
                <div data-testid="search-prefill-profile-unavailable">
                  <Banner severity="warning">{t("search.prefill.profileUnavailable")}</Banner>
                </div>
              ) : null}

              <div className="rounded-md border border-border bg-surface-sunken">
                <button
                  type="button"
                  aria-expanded={advancedOpen}
                  aria-controls="search-advanced-conditions"
                  onClick={() => setAdvancedOpen((open) => !open)}
                  className="flex w-full cursor-pointer items-center gap-1.5 px-3 py-2 text-left text-sm font-medium text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
                >
                  <SlidersHorizontal size={14} className="text-accent-fg" aria-hidden />
                  {t("search.filters.advanced")}
                  {!advancedOpen && hasAdvancedSettings ? (
                    <StatusBadge variant="info" label={t("search.filters.active")} />
                  ) : null}
                  <DisclosureChevron
                    expanded={advancedOpen}
                    size={14}
                    className="ml-auto text-fg-muted"
                  />
                </button>
                {advancedOpen ? (
                <fieldset id="search-advanced-conditions" className="space-y-4 border-t border-border p-3">
                  <legend className="sr-only">{t("search.filters.title")}</legend>
                  <div className="rounded-md border border-border bg-surface">
                    <button
                      type="button"
                      aria-expanded={classificationVisible}
                      aria-controls="search-classification-filters"
                      onClick={() => setClassificationOpen((open) => !open)}
                      className="flex w-full cursor-pointer items-center justify-between gap-2 px-3 py-2 text-left text-xs font-medium text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
                    >
                      <span>{t("search.filters.classificationGroup")}</span>
                      <span className="flex items-center gap-2">
                        {!classificationVisible && hasClassificationFilters ? (
                          <StatusBadge variant="info" label={t("search.filters.active")} />
                        ) : null}
                        <DisclosureChevron expanded={classificationVisible} size={14} className="text-fg-muted" />
                      </span>
                    </button>
                    {classificationVisible ? (
                      <div id="search-classification-filters" className="space-y-3 border-t border-border p-3">
                        <p className="text-xs leading-relaxed text-fg-muted">
                          {t("search.filters.classificationHelper")}
                        </p>
                        {/* 分類は「文書の分類」の一覧から選ぶ（どれも任意。#2131）。名前の段（md）、基準日は日付の段
                            （sm。#1891）。 */}
                        <FormGrid>
                          <CategorySelectFields
                            idPrefix="search"
                            labels={{
                              large_category: t("search.filters.large_category"),
                              middle_category: t("search.filters.middle_category"),
                              small_category: t("search.filters.small_category"),
                            }}
                            values={classification}
                            onChange={(values) => setClassification((current) => ({ ...current, ...values }))}
                          />
                          <TextField
                            id="search-as_of"
                            width="sm"
                            type="date"
                            label={t("search.filters.as_of")}
                            value={classification.as_of}
                            onValueChange={(value) =>
                              setClassification((current) => ({ ...current, as_of: value }))
                            }
                          />
                        </FormGrid>
                      </div>
                    ) : null}
                  </div>

                  <div className="rounded-md border border-border bg-surface">
                    <button
                      type="button"
                      aria-expanded={extractionOpen}
                      aria-controls="search-extraction-field-filters"
                      onClick={() => setExtractionOpen((open) => !open)}
                      className="flex w-full cursor-pointer items-center justify-between gap-2 px-3 py-2 text-left text-xs font-medium text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
                    >
                      <span>{t("search.filters.fields.group")}</span>
                      <span className="flex items-center gap-2">
                        {!extractionOpen && hasExtractionFieldFilters ? (
                          <StatusBadge variant="info" label={t("search.filters.active")} />
                        ) : null}
                        <DisclosureChevron expanded={extractionOpen} size={14} className="text-fg-muted" />
                      </span>
                    </button>
                    {extractionOpen ? (
                      <div id="search-extraction-field-filters" className="space-y-3 border-t border-border p-3">
                        <p className="text-xs leading-relaxed text-fg-muted">
                          {t("search.filters.fields.helper")}
                        </p>
                        {!searchAnswerProfileId ? (
                          <p className="text-sm text-fg-muted">{t("search.filters.fields.chooseScope")}</p>
                        ) : (
                          <ExtractionFieldFilters
                            fieldsState={
                              extractionFieldsQuery.isPending
                                ? { status: "pending" }
                                : extractionFieldsQuery.isError
                                  ? {
                                      status: "error",
                                      error: extractionFieldsQuery.error,
                                      retry: () => void extractionFieldsQuery.refetch(),
                                    }
                                  : { status: "ready", fields: extractionFieldsQuery.data.fields }
                            }
                            rows={extractionRows}
                            onRowsChange={setExtractionRows}
                            errors={fieldFilter.errors}
                            disabled={isStreaming}
                          />
                        )}
                      </div>
                    ) : null}
                  </div>

                  <div className="flex justify-end">
                    <Button
                      type="button"
                      variant="secondary"
                      size="md"
                      onClick={clearFilters}
                      disabled={!hasAdvancedSettings || isStreaming}
                      className="w-full sm:w-auto" icon={X}>
                      {t("search.filters.clear")}
                    </Button>
                  </div>
                </fieldset>
                ) : null}
              </div>

              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p id="search-generate-answer-label" className="text-sm font-medium text-fg">
                    {t("search.generateAnswer.label")}
                  </p>
                  <p id="search-generate-answer-help" className="mt-0.5 text-xs leading-relaxed text-fg-muted">
                    {t("search.generateAnswer.help")}
                  </p>
                </div>
                <Switch
                  checked={generateAnswer}
                  onCheckedChange={setGenerateAnswer}
                  disabled={isStreaming}
                  aria-labelledby="search-generate-answer-label"
                  aria-describedby="search-generate-answer-help"
                  data-testid="search-generate-answer"
                />
              </div>


              {/* 質問と検索の行はフォームの最後（詳細条件・スイッチの下。#413）。チャットの入力欄と同じく
                  複数行の入力欄（2 行）と lg のボタンを FieldActionRow に置く（ボタンは入力欄の下端にそろい、
                  375px では下に全幅。#613）。検索と停止は同じボタンで、実行中は同じ位置で「停止」になる。
                  Enter で検索、Shift+Enter で改行（IME の変換を確定する Enter では検索しない。#459）。
                  実行中の Enter は submit が無視し、停止しない。 */}
              <div className="space-y-2 border-t border-border pt-4">
                <FieldActionRow
                  actions={
                    <RunStopButton
                      running={isStreaming}
                      onRun={() => void submit()}
                      onStop={cancel}
                      runLabel={t("search.button")}
                      stopLabel={t("search.cancel")}
                      runIcon={SearchIcon}
                      size="lg"
                      testId="search-run-stop"
                    />
                  }
                >
                  <TextareaField
                    id="search-query"
                    width="full"
                    label={t("nav.search")}
                    labelHidden
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    onKeyDown={(event) => {
                      if (isSubmitEnter(event) && !event.shiftKey) {
                        event.preventDefault();
                        void submit();
                      }
                    }}
                    // 質問の入力欄は 3 製品で 3.5 行（チャット・NL2SQL の質問・Agent の実行の目標と同じ。#2008）。
                    rows={3.5}
                    placeholder={t("search.placeholder")}
                    // ラベルは読み上げだけ（sr-only）なので、欄の上に余白を空けない。
                    className="space-y-0"
                  />
                </FieldActionRow>
              </div>
            </CardContent>
          </Card>

          {/* 状態別表示 */}
          {faqSuggestions ? (
            <ApprovedFaqSuggestions
              suggestions={faqSuggestions}
              onUse={(suggestion) => {
                setFaqSuggestions(null);
                setFaqAnswer(suggestion);
              }}
              onSkip={() => void resubmit(true)}
            />
          ) : faqAnswer ? (
            <ApprovedFaqAnswer suggestion={faqAnswer} />
          ) : phase === "idle" ? (
            <Card>
              <CardContent className="pt-5">
                <EmptyState title={t("search.initial")} hint={t("search.initialHint")} />
              </CardContent>
            </Card>
          ) : phase === "error" ? (
            <ApiErrorState
              error={searchError}
              fallback={t("search.error.failed")}
              onRetry={() => void resubmit(lastSkipFaq)}
            />
          ) : (
            <>
              {/* 安全チェック警告 */}
              {meta?.guardrail_warnings.length ? (
                <Banner severity="warning">
                  <span className="font-medium">{t("search.guardrail")}: </span>
                  {meta.guardrail_warnings.join(" / ")}
                </Banner>
              ) : null}

              {phase === "cancelled" ? (
                <div className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-fg-muted" role="status">
                  {t("search.cancelled")}
                </div>
              ) : null}

              {/* 回答（スイッチがオンのときだけ。#649）。処理の経過 → 回答の本文 → 根拠の一覧（AI に渡した箇所）→ 評価 の
                  順で、チャットと同じ部品・並び（#2096 / #2097 / #2115）。処理の経過は実行中（工程の一覧が増え、今の工程に
                  Spinner と経過時間）も終わった後も同じ場所に出す。 */}
              {answerMode ? (
                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      <Sparkles size={16} className="text-accent-fg" aria-hidden />
                      {t("search.answer")}
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {answerModel ? (
                      <h3
                        className="min-w-0 break-words border-b border-border pb-2 text-sm font-semibold text-fg [overflow-wrap:anywhere]"
                        title={answerModel}
                        data-testid="search-answer-model-heading"
                      >
                        {answerModel}
                      </h3>
                    ) : null}
                    {processView}
                    {conditionChips}
                    <AnswerText
                      text={answer || (phase === "cancelled" ? t("search.cancelledHint") : "")}
                      streaming={isStreaming}
                      citations={citations}
                      unreadSections={canOpenSections ? unreadSectionLinks(meta?.diagnostics?.answer) : []}
                      diagnostics={phase === "done" ? meta?.diagnostics?.answer : null}
                      cursor={
                        isStreaming ? (
                          <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-accent-emphasis align-middle" />
                        ) : null
                      }
                    />
                    {/* 回答を読むときに要る案内（資料では決まらない値など）は回答の本文の直下（「詳細」に畳まない。#2096）。 */}
                    {meta && phase === "done" ? <AnswerNotices diagnostics={meta.diagnostics?.answer} /> : null}
                    {/*
                      回答の下は 3 製品のチャットと同じ共通の並び（#2707）: 回答の注意書き（#2405）→ 根拠の一覧 → 評価
                      （評価は回答の枠の最後。根拠を確かめてから評価できるよう、その後に置く。#1202 / #1420。
                      page-archetypes.md §6.1）。
                    */}
                    <AnswerFooter
                      notice={meta && phase === "done" ? meta.notice : null}
                      noticeLabel={t("answer.notice.label")}
                      evidence={
                        <AnswerCitations
                          citations={citations}
                          idPrefix="search"
                          cardProps={{
                            traceId: meta?.trace_id,
                            searchAnswerProfileId: answerSearchAnswerProfileId,
                            sourceSurface: "search",
                            contentSnapshot: feedbackSnapshot,
                          }}
                          className="border-t border-border pt-1"
                        />
                      }
                      feedback={
                        meta && phase === "done" ? (
                          <FeedbackControls
                            traceId={meta.trace_id}
                            searchAnswerProfileId={answerSearchAnswerProfileId}
                            targetType="answer"
                            sourceSurface="search"
                            contentSnapshot={feedbackSnapshot}
                          />
                        ) : null
                      }
                    />

                  </CardContent>
                </Card>
              ) : run ? (
                // 回答を作らない検索: 処理の経過（実行中は今の工程と経過時間、終わった後は工程の一覧とその後ろの「処理の詳細」。
                // 同じ場所のまま切り替える。#2115）・検索の条件・検索できなかった理由（検索の準備が無いなど）。
                <Card>
                  <CardContent className="space-y-3 pt-5">
                    {processView}
                    {conditionChips}
                    {answer ? <Banner severity="warning">{answer}</Banner> : null}
                    {phase === "cancelled" ? <p className="text-sm text-fg-muted">{t("search.cancelledHint")}</p> : null}
                  </CardContent>
                </Card>
              ) : (
                // 実行の前（類似問の提示の間を含む）: 検索の条件だけ。
                <div className="space-y-3 empty:hidden">
                  {conditionChips}
                  {answer ? <Banner severity="warning">{answer}</Banner> : null}
                  {phase === "cancelled" ? <p className="text-sm text-fg-muted">{t("search.cancelledHint")}</p> : null}
                </div>
              )}

              {/* 引用 / no-results */}
              {phase === "done" && awaitingClarification ? (
                <Banner severity="info">{t("search.clarificationHint")}</Banner>
              ) : noResults ? (
                <Card>
                  <CardContent className="pt-5">
                    <EmptyState title={t("search.noResults")} hint={t("search.noResultsHint")} />
                  </CardContent>
                </Card>
              ) : citations.length > 0 && !answerMode ? (
                // 回答を作らない検索は、検索結果が主な結果なので回答の枠の外に一覧で出す。
                <section>
                  <h2 className="mb-1 text-sm font-semibold text-fg">
                    {`${t("search.results")}（${citations.length}）`}
                  </h2>
                  <p className="mb-3 text-xs leading-relaxed text-fg-muted">{t("search.results.hint")}</p>
                  <CitationList
                    citations={citations}
                    idPrefix="search-results"
                    cardProps={{
                      traceId: meta?.trace_id,
                      searchAnswerProfileId: answerSearchAnswerProfileId,
                      sourceSurface: "search",
                      contentSnapshot: feedbackSnapshot,
                    }}
                  />
                </section>
              ) : null}

            </>
          )}
            </>
          )}
        </section>
      </PageBody>
    </div>
  );
}

/**
 * 実行を始めた時刻。`runRequest` はイベントの処理（検索・再試行など）からだけ呼ばれるが、await の前で
 * 時刻を取るため、React Compiler の lint が描画中の呼び出しと見なさないよう関数に分ける。
 */
/** 画面で止めた・失敗した実行の、実行中のままの行を止めた（スキップ）・失敗の状態にする。 */
function searchProcessSteps(steps: ChatProgressStep[], phase: string): ChatProgressStep[] {
  const ended = phase === "cancelled" ? "skipped" : phase === "error" ? "failed" : null;
  if (ended === null) return steps;
  return steps.map((step) =>
    step.status === "running" || step.status === "pending" ? { ...step, status: ended } : step
  );
}

function runStartedAtMs(): number {
  return Date.now();
}

/**
 * 質問から読み取って検索に足した条件（#652）。「自動」のチップで出し、外して検索し直せる。
 * 読み取った条件で見つからず外して検索したときは、その旨を出す。
 */
function AutoFieldFilterChips({
  diagnostics,
  disabled,
  onRemove,
}: {
  diagnostics: unknown;
  disabled: boolean;
  onRemove: (name: string) => void;
}) {
  const auto = parseAnswerDiagnostics(diagnostics)?.autoFieldFilter;
  if (!auto) return null;
  const names = [...new Set(auto.conditions.map((condition) => condition.name))];
  return (
    <div aria-label={t("search.answerDetails.autoFieldFilter")} className="mb-3 space-y-1.5" data-testid="auto-field-filter-chips">
      <p className="text-xs font-medium text-fg-muted">{t("search.answerDetails.autoFieldFilter")}</p>
      <div className="flex flex-wrap gap-1.5">
        {names.map((name) => {
          const label = auto.conditions
            .filter((condition) => condition.name === name)
            .map(extractionFieldConditionLabel)
            .join(" / ");
          return (
            <span
              key={name}
              data-surface-tint="info"
              className="inline-flex max-w-full items-center gap-0.5 rounded-full border border-border bg-info-subtle py-0.5 pl-2 text-xs font-medium leading-snug text-fg"
            >
              <span className="break-all">{t("search.autoFieldFilter.chip", { label })}</span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                iconOnly
                icon={X}
                disabled={disabled}
                aria-label={t("search.autoFieldFilter.remove", { label })}
                onClick={() => onRemove(name)}
              />
            </span>
          );
        })}
      </div>
      {auto.relaxed ? (
        <Banner severity="info">{t("search.answerDetails.autoFieldFilterRelaxed")}</Banner>
      ) : null}
    </div>
  );
}

/** 検索に使った条件（指定した条件と、質問から読み取った「自動」の条件）。 */
function SearchConditionChips({
  appliedFilters,
  meta,
  phase,
  disabled,
  onRemoveAuto,
}: {
  appliedFilters: Record<string, string>;
  meta: Meta | null;
  phase: Phase;
  disabled: boolean;
  onRemoveAuto: (name: string) => void;
}) {
  return (
    <>
      <ActiveFilterChips filters={appliedFilters} />
      {meta && phase === "done" ? (
        <AutoFieldFilterChips diagnostics={meta.diagnostics?.answer} disabled={disabled} onRemove={onRemoveAuto} />
      ) : null}
    </>
  );
}

function ActiveFilterChips({ filters }: { filters: Record<string, string> }) {
  const chips = activeFilterChips(filters);
  if (!chips.length) return null;
  return (
    <div aria-label={t("search.filters.applied")} className="mb-3 space-y-1.5">
      <p className="text-xs font-medium text-fg-muted">{t("search.filters.applied")}</p>
      <div className="flex flex-wrap gap-1.5">
        {chips.map((chip) => (
          <span
            key={chip.key}
            className="max-w-full break-all rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs font-medium leading-snug text-fg"
          >
            {chip.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function activeFilterChips(filters: Record<string, string>) {
  return [
    ...CLASSIFICATION_FILTER_KEYS.map((key) =>
      filters[key]
        ? {
            key,
            label: t("search.filters.appliedClassification", {
              label: t(`search.filters.${key}`),
              value: filters[key],
            }),
          }
        : null
    ),
    ...parseExtractionFieldFilterValue(filters[EXTRACTION_FIELD_FILTER_KEY]).map(
      (condition, index) => ({
        key: `${EXTRACTION_FIELD_FILTER_KEY}-${index}`,
        label: extractionFieldConditionLabel(condition),
      })
    ),
  ].flatMap((chip) => (chip ? [chip] : []));
}

function buildSearchFilters({
  classification,
  extractionFields,
}: {
  classification: ClassificationFilterValues;
  extractionFields: string;
}): Record<string, string> {
  const filters: Record<string, string> = {};
  for (const key of CLASSIFICATION_FILTER_KEYS) {
    if (classification[key].trim()) filters[key] = classification[key].trim();
  }
  if (extractionFields) filters[EXTRACTION_FIELD_FILTER_KEY] = extractionFields;
  return filters;
}

/** 質問の候補は分類だけで絞る（基準日は候補に関係しない）。 */

function isClassificationFilterValues(value: unknown): value is ClassificationFilterValues {
  return (
    typeof value === "object" &&
    value !== null &&
    CLASSIFICATION_FILTER_KEYS.every(
      (key) => typeof (value as Record<string, unknown>)[key] === "string"
    )
  );
}

const SEARCH_ANSWER_PROFILE_SCOPE_INPUT_ID = "search-search-answer-profile-scope";
