"use client";

import { FlaskConical, Search as SearchIcon, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { AnswerProgress } from "@/components/search/AnswerProgress";
import { CitationCard } from "@/components/search/CitationCard";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  FieldActionRow,
  TextField,
  RunStopButton,
} from "@production-ready/ui";
import { ApiErrorState, EmptyState } from "@/components/StateViews";
import { type RetrievedChunk } from "@/lib/api";
import { streamSearch, type SearchStageEvent } from "@/lib/search-stream";
import { isSubmitEnter } from "@/lib/keyboard";
import { citationCountLabel } from "@/lib/chunk-metadata";
import { t } from "@/lib/i18n";

type Phase = "idle" | "streaming" | "done" | "cancelled" | "error";

const TEST_TOP_K = 10;

/**
 * KB 詳細の「このナレッジで検索テスト」パネル。
 * 検索・回答プロファイル(Search Answer Profile)を介さず、単一 KB scope で retrieval をその場確認する。
 * backend は search_answer_profile_id が無ければ request 明示 KB scope + global defaults で検索する。
 */
export function KnowledgeBaseSearchTestPanel({
  knowledgeBaseId,
  indexedDocumentCount,
  disabled = false,
}: {
  knowledgeBaseId: string;
  indexedDocumentCount: number;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<RetrievedChunk[]>([]);
  const [meta, setMeta] = useState<{ trace_id: string; elapsed_ms: number } | null>(null);
  const [searchError, setSearchError] = useState<unknown>(null);
  // 処理中の表示（今の工程と経過時間）。ボタンは実行中に「停止」になりスピナーを出さないため、ここで示す（#413）。
  const [stages, setStages] = useState<SearchStageEvent[]>([]);
  const [startedAtMs, setStartedAtMs] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  // パネルを離れたら生成中の検索を止める（#285）。
  useEffect(() => () => abortRef.current?.abort(), []);

  const ready = !disabled && indexedDocumentCount > 0;
  const isStreaming = phase === "streaming";

  const submit = async () => {
    const trimmed = query.trim();
    if (!trimmed || isStreaming || !ready) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase("streaming");
    setStages([]);
    setStartedAtMs(Date.now());
    setAnswer("");
    setCitations([]);
    setMeta(null);
    setSearchError(null);

    try {
      await streamSearch(
        {
          query: trimmed,
          top_k: TEST_TOP_K,
          knowledge_base_ids: [knowledgeBaseId],
          // 引用の候補を確かめるので、回答は作らずに検索だけを行う（LLM を呼ばない。#593）。
          retrieval_only: true,
        },
        {
          onStage: (stage) => setStages((current) => [...current, stage]),
          onMetadata: (m) => setMeta({ trace_id: m.trace_id, elapsed_ms: m.elapsed_ms }),
          onDelta: (text) => setAnswer((prev) => prev + text),
          onReplace: (text) => setAnswer(text),
          onCitations: (list) => setCitations(list),
          onDone: () => {
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
      setPhase("error");
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPhase("cancelled");
    setSearchError(null);
  };

  const noResults = phase === "done" && citations.length === 0;
  const resultMeta =
    meta && phase === "done"
      ? t("knowledgeBases.searchTest.resultMeta", {
          count: citations.length,
          ms: Math.round(meta.elapsed_ms),
        })
      : "";
  const inputId = `kb-search-test-${knowledgeBaseId}`;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <FlaskConical className="size-4 text-fg-muted" aria-hidden />
          {t("knowledgeBases.searchTest.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-fg-muted">{t("knowledgeBases.searchTest.description")}</p>

        {disabled ? (
          // アーカイブ済みは索引済みの文書があっても検索されない。索引の不足と取り違えない理由を出す。
          <EmptyState
            title={t("knowledgeBases.searchTest.archived")}
            hint={t("knowledgeBases.searchTest.archivedHint")}
          />
        ) : !ready ? (
          <EmptyState
            title={t("knowledgeBases.searchTest.needsIndexed")}
            hint={t("knowledgeBases.searchTest.needsIndexedHint")}
          />
        ) : (
          <>
            {/* 検索テストと停止は同じボタン。実行中は同じ位置で「停止」になる（buttons.md §3.1、#413）。
                主な問い合わせの行なので入力欄とボタンを同じ lg にする（README §4「操作部品の高さと幅」、#613）。 */}
            <FieldActionRow
              actions={
                <RunStopButton
                  running={isStreaming}
                  onRun={() => void submit()}
                  onStop={cancel}
                  runLabel={t("knowledgeBases.searchTest.button")}
                  stopLabel={t("knowledgeBases.searchTest.cancel")}
                  runIcon={SearchIcon}
                  size="lg"
                  className="sm:min-w-28"
                  testId="kb-search-test-run-stop"
                />
              }
            >
              {/* 入力欄の Enter は実行だけ（実行中の Enter は submit が無視する）。 */}
              <TextField
                id={inputId}
                label={t("knowledgeBases.searchTest.title")}
                labelHidden
                width="full"
                size="lg"
                value={query}
                onValueChange={setQuery}
                onKeyDown={(e) => {
                  if (isSubmitEnter(e)) void submit();
                }}
                placeholder={t("knowledgeBases.searchTest.placeholder")}
                leadingIcon={SearchIcon}
              />
            </FieldActionRow>

            {phase === "idle" ? (
              <EmptyState title={t("knowledgeBases.searchTest.initialHint")} />
            ) : phase === "error" ? (
              <ApiErrorState
                error={searchError}
                fallback={t("knowledgeBases.searchTest.error")}
                onRetry={() => void submit()}
              />
            ) : (
              <div className="space-y-4">
                {answer || isStreaming ? (
                  <div className="rounded-lg border border-border bg-surface p-4">
                    <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold text-fg">
                      <Sparkles size={16} className="text-accent-fg" aria-hidden />
                      {t("search.answer")}
                    </h3>
                    {isStreaming ? (
                      <AnswerProgress
                        active
                        stages={stages}
                        startedAtMs={startedAtMs}
                        testId="kb-search-test-progress"
                        className="mb-3"
                      />
                    ) : null}
                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-fg">
                      {answer || (phase === "cancelled" ? t("search.cancelledHint") : "")}
                      {isStreaming ? (
                        <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-accent-emphasis align-middle motion-reduce:animate-none" />
                      ) : null}
                    </p>
                    {answer && resultMeta ? (
                      <p className="tnum mt-3 border-t border-border pt-2 text-xs text-fg-muted">
                        {resultMeta}
                      </p>
                    ) : null}
                  </div>
                ) : null}
                {/* 検索だけ（回答の枠が無い）で停止したときも、停止したことを結果の領域に残す。 */}
                {phase === "cancelled" && !answer ? (
                  <p className="text-sm text-fg-muted" data-testid="kb-search-test-cancelled">
                    {t("search.cancelledHint")}
                  </p>
                ) : null}
                {/* 検索だけのとき（回答が無い）は、件数と時間を引用の上に出す（#593）。 */}
                {!answer && resultMeta ? (
                  <p className="tnum text-xs text-fg-muted" data-testid="kb-search-test-meta">
                    {resultMeta}
                  </p>
                ) : null}

                {noResults ? (
                  <EmptyState title={t("search.noResults")} hint={t("search.noResultsHint")} />
                ) : citations.length > 0 ? (
                  <section>
                    <h3 className="mb-3 text-sm font-semibold text-fg">
                      {/* 回答のときは回答の根拠、検索だけのときは検索結果（RAG 検索と同じ。#1208）。 */}
                      {answer
                        ? citationCountLabel(citations)
                        : `${t("search.results")}（${citations.length}）`}
                    </h3>
                    <ul className="bounded-scroll-area-lg space-y-2 pr-1">
                      {citations.map((chunk, i) => (
                        <CitationCard
                          key={chunk.chunk_id}
                          chunk={chunk}
                          index={i}
                          traceId={meta?.trace_id}
                          sourceKind={answer ? "answer" : "search"}
                        />
                      ))}
                    </ul>
                  </section>
                ) : null}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
