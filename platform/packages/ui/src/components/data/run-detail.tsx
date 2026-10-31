import { traceOrderedSteps } from "../../lib/trace-explorer";
import { RUN_TIME_TRACK, RUN_TIME_FILL, RUN_TIME_COLOR, runTimelineNameClass } from "./run-timeline-style";
import { DEFAULT_TRACE_DETAILS_LABELS } from "../../lib/trace-labels";
import { CheckCircle2, Circle, CirclePause, CirclePlay, Clock3, MinusCircle, Waypoints, XCircle, type LucideIcon } from "lucide-react";
import { createContext, useContext, useEffect, useId, useMemo, useState, type ReactNode, type CSSProperties } from "react";

import { DEFAULT_TRACE_EXPLORER_LABELS, TraceExplorer, type TraceExplorerLabels } from "./trace-explorer";
import { Button } from "../ui/button";
import { CopyButton } from "../ui/copy-button";
import type { TraceDetailsLabels } from "../../lib/trace-labels";
import { useTraceDetailsLabels } from "./trace-labels-provider";
import { traceTimeline } from "../../lib/trace-tree";
import { operationTimestampMs, type OperationTimestamp } from "../../lib/operation-timing";
import { cn } from "../../lib/utils";
import type { ChatProgressStepStatus } from "../feedback/chat-progress";
import { ProcessingIndicator, useOperationTiming, type ProcessingIndicatorProps } from "../feedback/processing-state";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Disclosure } from "../ui/disclosure";
import { DisclosureChevron } from "../ui/disclosure-chevron";
import { TEXT_ICON_SIZE } from "../ui/text-icon";
import { Skeleton } from "../ui/skeleton";
import { Spinner } from "../ui/spinner";
import { formatRunDateTime, formatRunDuration, formatRunTokens } from "./run-history";
import { hasRunStepDetails, RunStepDetailsView, runStepContent, runStepResultSummary, type RunStepDetails } from "./run-step-details";
import { DEFAULT_RUN_STATUS_LABELS, RunStatusBadge, type RunStatus, type RunStatusLabels } from "./run-status";

/*
 * 実行履歴の詳細（#1955）。3 製品の詳細を同じ順の節にそろえる:
 *   概要（状態・質問・日時・所要時間・実行者・会話の持ち主・対象・モデル・利用量）
 *   → 処理の経過（段・ツールの呼び出し・時間の線。回答の上。#2126）→ 回答 → 根拠 / SQL / 検証（製品の中身）
 *   → フィードバック → 技術的な詳細（畳む）
 * 製品は節の中身（RAG の根拠・NL2SQL の SQL・Agent のツールの結果）だけを持つ。規則は UX 契約 run-history.md「詳細」。
 */

// ---------------------------------------------------------------------------
// 文言

export interface RunDetailLabels {
  summary: string;
  question: string;
  answer: string;
  timeline: string;
  /** 処理の経過の節の中の「処理の詳細」（`RunProcessDetails` の見出し。#2325）。 */
  processDetails: string;
  facts: {
    startedAt: string;
    finishedAt: string;
    duration: string;
    kind: string;
    executor: string;
    owner: string;
    model: string;
    tokens: string;
    cost: string;
    runId: string;
    /** 取り消した人と入口（#3348）。 */
    cancellation: string;
  };
  /** 実行者・持ち主が自分のときの表記。 */
  self: string;
  /** 取消の入口の表記（`formatRunCancellation`。#3348）。 */
  cancellation: RunCancellationLabels;
  notRecorded: string;
  /** 処理の経過の要約（「5 ステップ・合計 12.3 秒」）。終わった実行の閉じた見出し。 */
  timelineSummary: (count: number, duration: string) => string;
  /**
   * 実行中の処理の経過の閉じた見出し（「処理中（4 ステップ完了）」。チャットの処理の経過 `ChatProgress` と同じ文言。
   * 経過時間は見出しの右の時計に出す。#3445）。
   */
  timelineRunning: (completed: number) => string;
  /** 実行中の処理の経過の見出しの右の、処理全体の経過時間の見出し（「経過時間」）。 */
  timelineElapsed: string;
  /** 段の状態（アイコンに添える読み上げ・失敗とスキップは画面にも出す）。 */
  stepStatus: Record<RunTimelineStepStatus, string>;
  /** 段の詳細（`RunTimelineStep.details`）の長さを切ったときの印。 */
  stepDetailsTruncated: string;
  status: RunStatusLabels;
}

// ---------------------------------------------------------------------------
// 取消（取り消した人と入口。#3348）

/**
 * 取消の入口。画面の操作（`screen`）・外部の MCP クライアント（`mcp`。API キー）・Agent の実行
 * （`agentRun`。NL2SQL のジョブを Agent の Run が止めた）・記録の無い自動の取消（`system`）。
 */
export type RunCancellationVia = "screen" | "mcp" | "agentRun" | "system";

export interface RunCancellation {
  via: RunCancellationVia;
  /** 取り消した人（`system` は渡さない）。自分なら `isSelf`。 */
  actor?: { name: string; isSelf?: boolean } | null;
  /** MCP の API キーの名前（見ている利用者が見られるキーだけ。見られなければ渡さない）。 */
  apiKeyName?: string | null;
}

export interface RunCancellationLabels {
  screen: string;
  mcp: string;
  /** MCP と API キーの名前（「MCP（〈キーの名前〉）」）。 */
  mcpKey: (name: string) => string;
  agentRun: string;
  system: string;
  /** 取り消した人と入口の区切り。 */
  separator: string;
}

export const DEFAULT_RUN_CANCELLATION_LABELS: RunCancellationLabels = {
  screen: "画面",
  mcp: "MCP",
  mcpKey: (name) => `MCP（${name}）`,
  agentRun: "Agent の実行から",
  system: "システム",
  separator: " · ",
};

/**
 * 取消の事実の文（「〈利用者〉 · MCP（〈キーの名前〉）」「〈利用者〉 · 画面」「システム」）。3 製品で同じ形にする
 * （#3348）。自分の取消は `self`（「自分」）。
 */
export function formatRunCancellation(
  cancellation: RunCancellation,
  labels: Partial<RunCancellationLabels> & { self?: string } = {}
): string {
  const text = { ...DEFAULT_RUN_CANCELLATION_LABELS, ...labels };
  if (cancellation.via === "system") return text.system;
  const keyName = cancellation.apiKeyName?.trim();
  const via =
    cancellation.via === "mcp"
      ? keyName
        ? text.mcpKey(keyName)
        : text.mcp
      : cancellation.via === "agentRun"
        ? text.agentRun
        : text.screen;
  const actor = cancellation.actor;
  const name = actor ? (actor.isSelf ? labels.self ?? DEFAULT_RUN_DETAIL_LABELS.self : actor.name.trim()) : "";
  return name ? `${name}${text.separator}${via}` : via;
}

export const DEFAULT_RUN_DETAIL_LABELS: RunDetailLabels = {
  summary: "概要",
  question: "質問",
  answer: "回答",
  timeline: "処理の経過",
  processDetails: "処理の詳細",
  facts: {
    startedAt: "開始日時",
    finishedAt: "終了日時",
    duration: "所要時間",
    kind: "種類",
    executor: "実行者",
    owner: "会話の持ち主",
    model: "モデル",
    tokens: "token",
    cost: "費用",
    runId: "実行 ID",
    cancellation: "取消",
  },
  self: "自分",
  cancellation: DEFAULT_RUN_CANCELLATION_LABELS,
  notRecorded: "—",
  timelineSummary: (count, duration) => `${count} ステップ・合計 ${duration}`,
  timelineRunning: (completed) => (completed > 0 ? `処理中（${completed} ステップ完了）` : "処理中"),
  timelineElapsed: "経過時間",
  stepStatus: {
    pending: "未実行",
    running: "実行中",
    waiting: "応答待ち",
    done: "完了",
    failed: "失敗",
    skipped: "スキップ",
  },
  stepDetailsTruncated: "長い内容は先頭だけを表示しています。",
  status: DEFAULT_RUN_STATUS_LABELS,
};

export type RunDetailLabelsInput = Partial<Omit<RunDetailLabels, "facts" | "stepStatus" | "status" | "cancellation">> & {
  facts?: Partial<RunDetailLabels["facts"]>;
  cancellation?: Partial<RunCancellationLabels>;
  stepStatus?: Partial<RunDetailLabels["stepStatus"]>;
  status?: Partial<RunStatusLabels>;
};

export function resolveRunDetailLabels(labels?: RunDetailLabelsInput): RunDetailLabels {
  return {
    ...DEFAULT_RUN_DETAIL_LABELS,
    ...labels,
    facts: { ...DEFAULT_RUN_DETAIL_LABELS.facts, ...labels?.facts },
    cancellation: { ...DEFAULT_RUN_DETAIL_LABELS.cancellation, ...labels?.cancellation },
    stepStatus: { ...DEFAULT_RUN_DETAIL_LABELS.stepStatus, ...labels?.stepStatus },
    status: { ...DEFAULT_RUN_DETAIL_LABELS.status, ...labels?.status },
  };
}

// ---------------------------------------------------------------------------
// 事実の一覧（概要・技術的な詳細）

export interface RunFact {
  id: string;
  /** 見出し（翻訳済み）。 */
  label: string;
  value: ReactNode;
  /** 識別子（実行 ID・モデル名）を等幅で出す。 */
  mono?: boolean;
  /** 長い値（対象の名前・エラーの要約など）を 1 行の全幅に置く。 */
  wide?: boolean;
}

/** 見出しと値の並び（`<dl>`）。置き場所の幅に合わせて段を増やす（375px は 1 段）。 */
export function RunFactList({ facts, className, testId }: { facts: readonly RunFact[]; className?: string; testId?: string }) {
  if (facts.length === 0) return null;
  return (
    <dl
      className={cn(
        "grid min-w-0 gap-x-6 gap-y-3 text-sm [grid-template-columns:repeat(auto-fill,minmax(min(100%,11rem),1fr))]",
        className
      )}
      data-testid={testId}
    >
      {facts.map((fact) => (
        <div key={fact.id} className={cn("min-w-0", fact.wide && "col-span-full")} data-run-fact={fact.id}>
          <dt className="text-xs text-fg-muted">{fact.label}</dt>
          <dd
            className={cn(
              "mt-0.5 break-words text-fg [overflow-wrap:anywhere]",
              fact.mono ? "font-mono text-xs leading-5" : "tnum"
            )}
          >
            {fact.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// 概要

export interface RunSummaryProps {
  /** 補助用途のバッジ表示。実行履歴の概要は既定で表示しない。 */
  showBadges?: boolean;
  status: RunStatus;
  /** 製品に固有の状態の文言（例: waiting →「承認待ち」）。 */
  statusLabel?: string;
  /** 状態の横に並べる製品のバッジ（RAG の回答の対応・検証、NL2SQL の安全性など）。`StatusBadge` を渡す。 */
  badges?: ReactNode;
  /** 質問（入力）。全文を出す（一覧は 2 行で切り詰めるため、ここで全文を読む）。 */
  question: ReactNode;
  startedAt: string;
  finishedAt?: string | null;
  durationMs?: number | null;
  kind?: string | null;
  /** 実行者（#1954）。 */
  executor?: { name: string; isSelf?: boolean } | null;
  /** 会話の持ち主（実行者と違うときだけ渡す。#1954）。 */
  owner?: { name: string; isSelf?: boolean } | null;
  /** 取り消した人と入口（取り消した実行だけ渡す。#3348）。 */
  cancellation?: RunCancellation | null;
  /** 対象（業務 Agent・検索・回答プロファイル・業務プロファイル）。見出しは製品が渡す。 */
  target?: { label: string; value: ReactNode } | null;
  model?: string | null;
  /**
   * 利用量。`note` は計測の状態の注記（「一部計測」「未計測」など。翻訳済み）で、`tokens` があれば数の後ろに括弧で、
   * 無ければ単独で出す（不明を 0 と出さない。#3419）。
   */
  usage?: { tokens?: number | null; note?: string | null; cost?: string | null } | null;
  runId: string;
  /** 製品に固有の事実（NL2SQL の実行エンジン・行数など）。共通の事実の後、実行 ID の前に並べる。 */
  extraFacts?: readonly RunFact[];
  /** 対象への操作（`ObjectActionBar`。再実行・停止・フィードバックなど）。見出しの右に 1 か所。 */
  actions?: ReactNode;
  /** 失敗・ブロックの理由（danger / warning の `Banner`）。質問の上、見出しの直下に出す。 */
  alert?: ReactNode;
  labels?: RunDetailLabelsInput;
  formatDateTime?: (iso: string) => string;
  testId?: string;
}

/**
 * 詳細の先頭のカード「概要」（#1955）。状態 → 質問 → 事実の並び（開始日時・所要時間・種類・実行者・会話の持ち主・
 * 取消（取り消した実行だけ。#3348）・対象・モデル・token・費用・製品の事実・実行 ID）。記録の無い事実は並べない（「—」の並びにしない）。
 */
export function RunSummary({
  showBadges = false,
  status,
  statusLabel,
  badges,
  question,
  startedAt,
  finishedAt,
  durationMs,
  kind,
  executor,
  owner,
  cancellation,
  target,
  model,
  usage,
  runId,
  extraFacts = [],
  actions,
  alert,
  labels: labelsInput,
  formatDateTime = (iso) => formatRunDateTime(iso, { seconds: true }),
  testId,
}: RunSummaryProps) {
  const labels = resolveRunDetailLabels(labelsInput);
  const person = (value: { name: string; isSelf?: boolean }) => (value.isSelf ? labels.self : value.name);
  const facts: RunFact[] = [
    { id: "startedAt", label: labels.facts.startedAt, value: <time dateTime={startedAt}>{formatDateTime(startedAt)}</time> },
  ];
  if (finishedAt) {
    facts.push({ id: "finishedAt", label: labels.facts.finishedAt, value: <time dateTime={finishedAt}>{formatDateTime(finishedAt)}</time> });
  }
  facts.push({ id: "duration", label: labels.facts.duration, value: formatRunDuration(durationMs, labels.notRecorded) });
  if (kind) facts.push({ id: "kind", label: labels.facts.kind, value: kind });
  if (executor) facts.push({ id: "executor", label: labels.facts.executor, value: person(executor) });
  if (owner) facts.push({ id: "owner", label: labels.facts.owner, value: person(owner) });
  if (cancellation) {
    facts.push({
      id: "cancellation",
      label: labels.facts.cancellation,
      value: formatRunCancellation(cancellation, { ...labels.cancellation, self: labels.self }),
    });
  }
  if (target) facts.push({ id: "target", label: target.label, value: target.value });
  if (model) facts.push({ id: "model", label: labels.facts.model, value: model, mono: true });
  if (usage?.tokens != null || usage?.note) {
    const tokens = usage.tokens != null ? formatRunTokens(usage.tokens) : null;
    const value = tokens && usage.note ? `${tokens}（${usage.note}）` : (tokens ?? usage.note);
    facts.push({ id: "tokens", label: labels.facts.tokens, value });
  }
  if (usage?.cost) facts.push({ id: "cost", label: labels.facts.cost, value: usage.cost });
  facts.push(...extraFacts);
  facts.push({ id: "runId", label: labels.facts.runId, value: runId, mono: true });

  return (
    <Card className="min-w-0" data-run-summary="" data-testid={testId}>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2" data-run-summary-heading="">
          <CardTitle>{labels.summary}</CardTitle>
          {showBadges ? <><RunStatusBadge status={status} label={statusLabel} labels={labels.status} />{badges}</> : null}
        </div>
        {actions ? <div className="min-w-0">{actions}</div> : null}
      </CardHeader>
      <CardContent className="grid min-w-0 gap-4">
        {alert}
        <div className="min-w-0" data-run-question="">
          <p className="text-xs text-fg-muted">{labels.question}</p>
          <div className="mt-1 whitespace-pre-wrap break-words text-sm text-fg [overflow-wrap:anywhere]">{question}</div>
        </div>
        <RunFactList facts={facts} />
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 節

export interface RunDetailSectionProps {
  /** 節の見出し（翻訳済み）。 */
  title: string;
  description?: string;
  /** 見出しの右の件数・状態のバッジ（操作は置かない）。 */
  meta?: ReactNode;
  /** 内容への操作（コピー・CSV など。`ContentActionBar`）。開閉できる節では内容の先頭に置く。 */
  actions?: ReactNode;
  /** 開閉できる節（技術的な詳細・長い根拠の一覧）。既定は閉じる（`defaultOpen` で開く）。 */
  collapsible?: boolean;
  defaultOpen?: boolean;
  /**
   * 開閉を製品が持つ（受控。`collapsible` のときだけ）。開いたときだけ中身を読む節（Agent の監査ログ）に使う。
   */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  icon?: LucideIcon;
  children: ReactNode;
  testId?: string;
}

/** 詳細の 1 つの節（カード）。見出しの大きさと余白を 3 製品で同じにする。 */
export function RunDetailSection({
  title,
  description,
  meta,
  actions,
  collapsible = false,
  defaultOpen = false,
  open,
  onOpenChange,
  icon,
  children,
  testId,
}: RunDetailSectionProps) {
  if (collapsible) {
    return (
      <Disclosure
        summary={title}
        description={description}
        meta={meta}
        icon={icon}
        defaultOpen={defaultOpen}
        open={open}
        onOpenChange={onOpenChange}
        className="bg-surface shadow-[var(--shadow-card)]"
        contentClassName="grid min-w-0 grid-cols-1 gap-3 border-t border-border p-5"
        data-run-detail-section=""
        data-testid={testId}
      >
        {actions ? <div className="flex justify-end">{actions}</div> : null}
        {children}
      </Disclosure>
    );
  }
  return (
    <Card className="min-w-0" data-run-detail-section="" data-testid={testId}>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <CardTitle>{title}</CardTitle>
            {meta}
          </div>
          {description ? <CardDescription className="mt-1">{description}</CardDescription> : null}
        </div>
        {actions ? <div className="min-w-0">{actions}</div> : null}
      </CardHeader>
      <CardContent className="grid min-w-0 grid-cols-1 gap-3">{children}</CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 処理の詳細

export interface RunProcessMetadataProps {
  /** 保存された会話の ID。会話のない実行は null。 */
  conversationId: string | null;
  /** 1 往復を識別するメッセージ・Run・Job の ID。未取得は null。 */
  messageId: string | null;
}

export interface RunProcessDetailsProps {
  metadata?: RunProcessMetadataProps;
  /** 実測の trace（「トレースの詳細」）。文言は製品の root の `TraceDetailsLabelsProvider` から取る。 */
  traceSummary?: unknown;
  /**
   * 処理の経過が trace の投影のとき（#3226）の trace id。「処理の詳細」の先頭に Trace ID とコピーのボタンを出す。
   * `traceSummary` は渡さない（処理の経過と同じ記録を 2 回出さない）。
   */
  traceId?: string;
  /** 見出し（翻訳済み）。既定「処理の詳細」。 */
  title?: ReactNode;
  /** 開いた中身の先頭の補足。 */
  description?: ReactNode;
  defaultOpen?: boolean;
  children?: ReactNode;
  /** 開閉の要素（`<details>`）の `data-testid`。 */
  testId?: string;
  /** 見出し（`<summary>`）の `data-testid`。 */
  summaryTestId?: string;
  className?: string;
}

/**
 * 処理の詳細（#2325）。処理の経過に属する技術的な詳細（回答の記録の詳細・識別子・生成した SQL・イベントの記録など）を、
 * 処理の経過のタイムラインの後ろに既定で閉じて置く控えめな開閉（`Disclosure` の plain・sm）。
 *
 * チャット・RAG 検索の処理の経過（`ChatProgress` の `footer`）と、実行の詳細の「処理の経過」の節（`RunTimeline` の後ろ）で
 * 同じ名前・同じ形・同じ位置にする。処理の経過の外（回答・根拠の後ろやページの末尾）に別の節として置かない
 * （回答の補足と読み違える。ユーザーの指摘）。生の JSON などさらに深い内容は、中で `<pre data-surface="code">` や
 * 入れ子の `Disclosure` に畳む。
 */
export function RunProcessDetails({
  metadata,
  traceSummary,
  traceId,
  title = DEFAULT_RUN_DETAIL_LABELS.processDetails,
  description,
  defaultOpen = false,
  children,
  testId,
  summaryTestId,
  className,
}: RunProcessDetailsProps) {
  const traceLabels = useTraceDetailsLabels();
  return (
    <Disclosure
      variant="plain"
      size="sm"
      summary={title}
      defaultOpen={defaultOpen}
      summaryProps={{ "data-testid": summaryTestId }}
      summaryClassName="text-fg-muted"
      contentClassName="grid min-w-0 grid-cols-1 gap-3"
      className={className}
      data-run-process-details=""
      data-testid={testId}
    >
      <section aria-label={typeof title === "string" ? title : DEFAULT_RUN_DETAIL_LABELS.processDetails} className="grid min-w-0 gap-3">
      {description ? <p className="text-xs text-fg-muted">{description}</p> : null}
      {metadata ? <>
        <ProcessIdentifierRow label="Trace ID" value={traceId ?? null} trace />
        <ProcessIdentifierRow label={(traceLabels.processMetadata ?? DEFAULT_TRACE_DETAILS_LABELS.processMetadata!).conversationId} value={metadata.conversationId} />
        <ProcessIdentifierRow label={(traceLabels.processMetadata ?? DEFAULT_TRACE_DETAILS_LABELS.processMetadata!).messageId} value={metadata.messageId} />
      </> : traceId ? <TraceIdRow traceId={traceId} labels={traceLabels} /> : null}
      <TraceDetails summary={traceSummary} />
      {children}
      </section>
    </Disclosure>
  );
}

// ---------------------------------------------------------------------------
// 処理の経過

/** 段の状態（チャットの処理の経過 `ChatProgressStepStatus` と同じ値 + 応答待ち）。 */
export type RunTimelineStepStatus = ChatProgressStepStatus | "waiting";

/**
 * 段の名前の後ろの製品名のタグ（別の製品の処理の行。#3272）。名前の前に置くと、その行の名前だけが兄弟の段の名前より
 * 右にずれるので、名前の後ろの控えめな枠つきの文字にする（#3447）。色だけに頼らず文字で出し、読み上げは名前の後ろ。
 */
export function RunTimelineProduct({ product }: { product: string }) {
  return (
    <>
      {" "}
      <span
        className="inline-block rounded-sm border border-border px-1 align-baseline leading-4 text-fg-muted"
        data-run-timeline-product=""
      >
        {product}
      </span>
    </>
  );
}

export interface RunTimelineStep {
  /** 記録された operation 名。軌跡の分類と検索に使う。 */
  operationName?: string;
  id: string;
  /** 段の名前（翻訳済み。例「SQL を生成」「ツール: search_documents」）。 */
  label: string;
  status: RunTimelineStepStatus;
  /** ISO 8601。開始と終了があれば時間の線を実際の位置に描く。 */
  startedAt?: string | null;
  finishedAt?: string | null;
  /** 所要時間（ミリ秒）。無ければ開始と終了から求める。開始が無い段は前の段の後ろに続けて描く。 */
  durationMs?: number | null;
  /** 短い補足（対象の表・ツール名・件数・LLM の回数）。名前の後ろの括弧に出す。SQL 全文・エラーの全文は `children` に入れる。 */
  detail?: string;
  /** 段の右に並べるバッジ（token・試行の回数など）。 */
  meta?: ReactNode;
  /**
   * 開いて見る詳細（見出しと値・行・SQL や JSON の箱・省略の印。#2294）。中身があれば行に開閉のボタンが付く。
   * 製品は記録をこの形に写すだけにし、描き方は部品が持つ（チャットの処理の経過と実行の詳細で同じ形）。
   */
  details?: RunStepDetails;
  /**
   * 別の製品の処理の行の製品名（例「RAG」。#3272）。名前の前に控えめな文字で出す（色だけに頼らない）。製品の境目の行だけに
   * 渡す（子の行は字下げで分かる）。
   */
  product?: string;
  /** 開いて見る自由な中身（結果の表など。`details` の下に出す）。渡すと行に開閉のボタンが付く。 */
  children?: ReactNode;
  /**
   * 入れ子の深さ（0 = 最上位）。1 段 1rem で全文字下げし、深い木は一覧内で横へ移動する。
   * 時刻の無い段の線は親の始まりから積む。
   */
  level?: number;
  /** 記録にある実際の親 ID。親不在でも推測で置き換えない。 */
  parentId?: string;
}

const STEP_ICON: Record<Exclude<RunTimelineStepStatus, "running">, { icon: LucideIcon; className: string }> = {
  done: { icon: CheckCircle2, className: "text-success-fg" },
  failed: { icon: XCircle, className: "text-danger-fg" },
  skipped: { icon: MinusCircle, className: "text-fg-muted" },
  pending: { icon: Circle, className: "text-fg-subtle" },
  waiting: { icon: CirclePause, className: "text-warning-fg" },
};



/** 深い木で一覧内のスクロールに切り替える深さ。深さそのものは制限しない。 */
export const RUN_TIMELINE_SCROLL_DEPTH = 6;
/** @deprecated 字下げの上限ではなく、一覧内スクロールに切り替える閾値。 */
export const RUN_TIMELINE_MAX_VISIBLE_DEPTH = RUN_TIMELINE_SCROLL_DEPTH;

function stepLevel(level: number | undefined): number {
  return typeof level === "number" && Number.isFinite(level) ? Math.max(0, Math.floor(level)) : 0;
}

/** 実際の木の深さを 1 段 1rem で表示する。固定の上限へ丸めない。 */
export function runTimelineStepIndentStyle(level: number | undefined): CSSProperties {
  return { paddingInlineStart: `${stepLevel(level)}rem` };
}

/** 深い木はページではなく一覧の中で横へ移動する。浅い一覧の幅は変えない。 */
export function RunTimelineViewport({ depth, label, children }: { depth: number; label: string; children: ReactNode }) {
  const deep = depth > RUN_TIMELINE_SCROLL_DEPTH;
  if (!deep) return <>{children}</>;
  return (
    <div className="min-w-0 overflow-x-auto" tabIndex={0}
      role="region" aria-label={label} data-run-timeline-scroll="true">
      <div className="min-w-0" style={{ minWidth: `${depth + 16}rem` }}>
        {children}
      </div>
    </div>
  );
}

/**
 * 段ごとに、子の段を持つか（一覧で直後の段が 1 段以上深い `level`）を返す（#3436）。子の段を持つ段は親の工程で、
 * 実行中でも回さない実行中の印（`CirclePlay`）で出す。回すのは実行中の末端の段（子を持たない段）だけ。
 * 親子の判定は `level` の値そのもので行う。
 */
export function runTimelineHasChildren(steps: readonly { level?: number | null }[]): boolean[] {
  return steps.map(
    (step, index) => index + 1 < steps.length && stepLevel(steps[index + 1].level ?? undefined) > stepLevel(step.level ?? undefined)
  );
}

/**
 * 今の段（最後に始まった実行中の末端の段。子の段を持たない段）の位置。無ければ -1（#3445）。一覧は開始順に並ぶので、
 * 後ろから探す。チャットの処理の経過（`ChatProgress`）の今の段階と同じ規則で、実行の詳細の一覧が回す段と、概要の
 * 進行表示の文言に使う段はこれ 1 つ（並行して実行中の末端の段がほかにあっても、回すのはこの段だけ）。
 */
export function runTimelineCurrentStepIndex(
  steps: readonly { status: string; level?: number | null }[]
): number {
  const hasChildren = runTimelineHasChildren(steps);
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (steps[index].status === "running" && !hasChildren[index]) return index;
  }
  return -1;
}

/**
 * 回さない実行中の印（子の段を持つ実行中の段（親の工程）と、`spinRunning` が false の一覧の実行中の段。チャットの
 * 処理の経過の、今の段階を包む親の工程）。`RunStatusBadge` の実行中と同じ形（`CirclePlay`）にする。
 */
const RUNNING_STATIC_ICON = { icon: CirclePlay, className: "text-accent-fg" } as const;

/** 段の状態のアイコン。大きさは行の文字の大きさ（`sm` 12px / `md` 14px。#2371）。 */
export function RunStepStatusIcon({
  status,
  size = TEXT_ICON_SIZE.sm,
  spin = true,
}: {
  status: RunTimelineStepStatus;
  size?: number;
  /** 実行中の段を `Spinner` で回すか（false は `RUNNING_STATIC_ICON`）。 */
  spin?: boolean;
}) {
  if (status === "running" && spin) return <Spinner size={size} className="text-accent-fg" />;
  const { icon: Icon, className } = status === "running" ? RUNNING_STATIC_ICON : STEP_ICON[status];
  return <Icon size={size} className={cn("shrink-0", className)} aria-hidden="true" />;
}

export interface RunTimelineBar {
  /** 線の始まり（全体に対する %）。 */
  offset: number;
  /** 線の長さ（%。0.5% 未満は 0.5% にして見えるようにする）。 */
  width: number;
}

/**
 * 段ごとの時間の線の位置（%）を求める。開始・終了の時刻がある段はその位置、無い段は所要時間を前の段の後ろに積む
 * （入れ子の段は親の段の始まりから積む。時刻を持たない記録でも、子の線が親の線の中に入る）。
 * 全体の長さが分からない（どの段にも所要時間が無い）ときは null（線を描かない）。
 */
export function runTimelineBars(steps: readonly RunTimelineStep[]): (RunTimelineBar | null)[] {
  const spans = steps.map((step) => {
    const start = operationTimestampMs(step.startedAt ?? undefined);
    const end = operationTimestampMs(step.finishedAt ?? undefined);
    const duration = step.durationMs ?? (start !== null && end !== null ? Math.max(0, end - start) : null);
    return { start, duration, level: stepLevel(step.level) };
  });
  const starts = spans.map((span) => span.start).filter((value): value is number => value !== null);
  const origin = starts.length ? Math.min(...starts) : null;
  // 深さごとの、直前の段の始まり（子の起点）と、同じ親の中で次の段を置く位置。
  const levelStart: (number | undefined)[] = [];
  const levelCursor: (number | undefined)[] = [];
  const placed = spans.map((span) => {
    const { level } = span;
    const parentStart = level > 0 ? (levelStart[level - 1] ?? 0) : 0;
    const offset =
      span.start !== null && origin !== null ? span.start - origin : (levelCursor[level] ?? parentStart);
    // 新しい段を置いたら、より深い段の起点を作り直す（次の子はこの段の始まりから積む）。
    levelStart.length = level;
    levelCursor.length = level + 1;
    levelStart[level] = offset;
    if (span.duration === null) return null;
    levelCursor[level] = Math.max(levelCursor[level] ?? 0, offset + span.duration);
    return { offset, duration: span.duration };
  });
  const total = Math.max(0, ...placed.map((item) => (item ? item.offset + item.duration : 0)));
  if (total <= 0) return placed.map(() => null);
  return placed.map((item) => {
    if (!item) return null;
    const offset = Math.min(100, (item.offset / total) * 100);
    const width = Math.max(0.5, Math.min(100 - offset, (item.duration / total) * 100));
    return { offset, width };
  });
}

export function stepDurationMs(step: RunTimelineStep): number | null {
  if (step.durationMs != null) return step.durationMs;
  const start = operationTimestampMs(step.startedAt ?? undefined);
  const end = operationTimestampMs(step.finishedAt ?? undefined);
  return start !== null && end !== null ? Math.max(0, end - start) : null;
}

/**
 * 段の開閉の印（`DisclosureChevron`）の大きさ（px）。行の名前の文字の大きさにそろえる（#2338）: `sm` は
 * `--font-size-xs`（12px）、`md` は `--font-size-sm`（14px。`--icon-sm` と同じ）。指定しないと lucide の既定の
 * 24px になり、行の文字より大きく見える。印は行の 1 行目（`leading-5`）の縦の中央に置き、押せる範囲は
 * ボタンの `pr-touch-target` が保つ。
 */
export const RUN_TIMELINE_CHEVRON_SIZE = {
  sm: TEXT_ICON_SIZE.xs,
  md: TEXT_ICON_SIZE.sm,
} as const satisfies Record<"sm" | "md", number>;

/** 並列の段は経過の範囲で数え、呼び出し時間を足し合わせない。 */
export function runTimelineDuration(steps: readonly RunTimelineStep[]): number | null {
  const roots = steps.filter(step => !step.level);
  const intervals = roots.map(step => [operationTimestampMs(step.startedAt), operationTimestampMs(step.finishedAt)]);
  if (intervals.length && intervals.every(([start, end]) => start != null && end != null && end >= start)) {
    return Math.max(...intervals.map(([, end]) => end!)) - Math.min(...intervals.map(([start]) => start!));
  }
  const sum = roots.reduce((total, step) => total + (stepDurationMs(step) ?? 0), 0);
  return sum > 0 ? sum : null;
}

export interface RunTimelineProps {
  explorerLabels?: Partial<TraceExplorerLabels>;
  steps: readonly RunTimelineStep[];
  /** 処理全体の所要時間（ミリ秒）。要約の行に出す（無ければ段の合計の範囲）。 */
  totalDurationMs?: number | null;
  /** 中身のある段を最初から開く（失敗した段は既定で開く）。 */
  defaultExpanded?: boolean;
  /**
   * 要約の行（「N ステップ・合計 M 秒」）を出すか。既定 true。チャットの処理の経過（`ChatProgress`）は、開閉の見出し
   * （「処理の経過（N ステップ・M 秒）」）が同じ情報を持つので出さない。
   */
  showSummary?: boolean;
  /**
   * 時間の線を出すか。既定 true。実行中の一覧（全体の長さが伸び続け、線が毎回伸び縮みする）は出さない。
   */
  showBars?: boolean;
  /**
   * 今の段（最後に始まった実行中の末端の段。`runTimelineCurrentStepIndex`）を `Spinner` で回すか。既定 true。false は
   * 回さない実行中の印（`CirclePlay`）で出す。子の段を持つ実行中の段（親の工程）と、今の段より前に始まって並行して
   * 実行中の末端の段は、この値にかかわらず回さない（動くスピナーは 1 つ。#3436 / #3445）。チャットの処理の経過
   * （`ChatProgress`）は、今の段階（最後に始まった実行中の末端の段）の行を自分で描き、その行だけを回す。今の段階を包む実行中の
   * 親の工程は、この一覧の中で開始順の位置に回さずに出す（同じ処理のスピナーは 1 つ。messaging.md §3.7。#2840）。
   */
  spinRunning?: boolean;
  /** 文字の大きさ。`sm`（既定。全製品の操作画面・履歴で共通）/ `md`（大きな表示）。 */
  size?: "sm" | "md";
  /** 段の所要時間の表記。既定は `formatRunDuration`（「0.4 秒」「1 分 5 秒」）。 */
  formatDuration?: (ms: number) => string;
  /**
   * 行に `data-testid="<stepTestIdPrefix>-step-<id>"` を付ける（`ChatProgress` は自分の testId を渡す）。開閉の
   * ボタンは `<…>-step-<id>-toggle`、開いた詳細は `<…>-step-<id>-details`。
   */
  stepTestIdPrefix?: string;
  labels?: RunDetailLabelsInput;
  testId?: string;
}

/**
 * 処理の経過（#1955）。段とツールの呼び出しを時間の順に 1 行ずつ並べ、状態のアイコン・名前・所要時間（文字）と、
 * 全体の中の位置と長さの線（ウォーターフォール）を出す。線は飾りで、時間は必ず文字でも出す。
 * 実行の詳細の「処理の経過」と、チャットの回答の「処理の経過」（`ChatProgress` の一覧）の 1 つの部品（#2265）。
 *
 * - 行の中身（ツールの引数と結果・SQL・エラー）は、行の開閉のボタン（`aria-expanded` + `DisclosureChevron`）で開く。
 *   失敗した段は最初から開く。
 * - 入れ子の段は実際の `level` で字下げし、深い一覧は内部で横へ移動する。補足（LLM の回数など）は名前の後ろの括弧に出す。
 * - 狭い置き場所（36rem 未満）では線を隠し、名前と所要時間だけにする（container query）。
 * - 状態はアイコンの形と読み上げの文言で伝え、失敗・スキップ・応答待ちは画面にも文字で出す（色だけにしない）。
 */
export function RunTimeline({
  steps: inputSteps,
  explorerLabels,
  totalDurationMs,
  defaultExpanded = false,
  showSummary = true,
  showBars = true,
  spinRunning = true,
  size = "sm",
  formatDuration = formatRunDuration,
  stepTestIdPrefix,
  labels: labelsInput,
  testId,
}: RunTimelineProps) {
  const steps = traceOrderedSteps(inputSteps, "hierarchy");
  const traceLabels = useTraceDetailsLabels();
  const [exploring, setExploring] = useState(false);
  const labels = resolveRunDetailLabels(labelsInput);
  const bars = runTimelineBars(steps);
  // 回すのは今の段（最後に始まった実行中の末端の段）1 つだけ（#3445）。
  const spinIndex = spinRunning ? runTimelineCurrentStepIndex(steps) : -1;
  const idPrefix = useId();
  const [expanded, setExpanded] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      steps
        .filter((step) => step.children != null || hasRunStepDetails(step.details))
        .map((step) => [step.id, defaultExpanded || step.status === "failed"])
    )
  );
  const total = totalDurationMs ?? runTimelineDuration(steps);
  const nameText = runTimelineNameClass(size);

  if (exploring) return <TraceExplorer steps={steps} onClose={() => { setExploring(false); requestAnimationFrame(() => document.getElementById(`${idPrefix}-explore`)?.focus()); }} labels={labelsInput} explorerLabels={explorerLabels} size={size} />;

  return (
    <div
      className="@container grid min-w-0 gap-2"
      data-run-timeline=""
      data-run-timeline-bars={showBars ? "true" : "false"}
      data-testid={testId}
    >
      {steps.length ? <div className="flex justify-end"><Button id={`${idPrefix}-explore`} variant="secondary" size="sm" icon={Waypoints} onClick={() => setExploring(true)}>{explorerLabels?.open ?? DEFAULT_TRACE_EXPLORER_LABELS.open}</Button></div> : null}
      {showBars && steps.some(step => stepDurationMs(step) != null && (!step.startedAt || !step.finishedAt)) ? (
        <p className="text-xs text-fg-muted">{traceLabels.reconstructed}</p>
      ) : null}
      {showSummary ? (
        <p className="text-xs text-fg-muted tnum">
          {labels.timelineSummary(steps.length, formatRunDuration(total, labels.notRecorded))}
        </p>
      ) : null}
      <RunTimelineViewport depth={Math.max(0, ...steps.map(step => stepLevel(step.level)))} label={labels.timeline}>
      <ol className={cn("grid min-w-0", size === "sm" ? "gap-1.5" : "gap-1")} aria-label={labels.timeline}>
        {steps.map((step, index) => {
          const duration = stepDurationMs(step);
          const bar = showBars ? bars[index] : null;
          const content = runStepContent(step.details);
          const details = hasRunStepDetails(content) ? content : undefined;
          const hasDetails = step.children != null || details !== undefined;
          const stepTestId = stepTestIdPrefix ? `${stepTestIdPrefix}-step-${step.id}` : undefined;
          const open = hasDetails && Boolean(expanded[step.id]);
          const regionId = `${idPrefix}-step-${index}`;
          // 失敗・スキップ・応答待ち・未実行（失敗の後に実行しなかった段。#2342）は状態を文字でも出す。
          const visibleStatus = true;
          const level = stepLevel(step.level);
          const name = (
            <span className={cn("min-w-0 break-words [overflow-wrap:anywhere]", !hasDetails && "flex-1")}>
              <span
                className={
                  step.status === "failed" ? "text-danger-fg" : step.status === "pending" ? "text-fg-muted" : "text-fg"
                }
                data-run-timeline-label=""
              >
                {step.label}
              </span>
              {step.product ? <RunTimelineProduct product={step.product} /> : null}

              {visibleStatus ? null : <span className="sr-only">{` ${labels.stepStatus[step.status]}`}</span>}
            </span>
          );
          return (
            <li
              key={step.id}
              className="min-w-0 rounded-md"
              data-status={step.status}
              data-level={level > 0 ? String(level) : undefined}
              data-parent-id={step.parentId}
              data-run-timeline-step={step.id}
              data-testid={stepTestId}
            >
              {/* 線の列と時間の列の幅を全行で同じにし、線の始まりをそろえる（字下げは名前の列の中だけ）。 */}
              <div
                className={cn(
                  "grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3",
                  showBars && "@xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)_7rem] @xl:items-center"
                )}
              >
                <div className={cn("flex min-w-0 items-start gap-2 leading-5", nameText)} style={runTimelineStepIndentStyle(level)}>
                  <span className="pr-icon-line">
                    <RunStepStatusIcon
                      status={step.status}
                      size={RUN_TIMELINE_CHEVRON_SIZE[size]}
                      // 回すのは今の段だけ。親の工程（#3436）と並行して実行中のほかの段（#3445）は回さない。
                      spin={index === spinIndex}
                    />
                  </span>
                  {hasDetails ? (
                    <button
                      type="button"
                      className="pr-touch-target relative inline-flex min-w-0 max-w-full cursor-pointer items-start gap-1 rounded-sm text-left hover:underline hover:underline-offset-2"
                      aria-expanded={open}
                      aria-controls={regionId}
                      data-run-timeline-toggle=""
                      data-testid={stepTestId ? `${stepTestId}-toggle` : undefined}
                      onClick={() => setExpanded((current) => ({ ...current, [step.id]: !current[step.id] }))}
                    >
                      {name}
                      <span className="pr-icon-line text-fg-muted">
                        <DisclosureChevron expanded={open} size={RUN_TIMELINE_CHEVRON_SIZE[size]} />
                      </span>
                    </button>
                  ) : (
                    name
                  )}
                  {step.meta ? <span className="flex shrink-0 flex-wrap items-center gap-1">{step.meta}</span> : null}
                </div>
                {/* 時間の線（飾り）。広い置き場所だけ。 */}
                {showBars ? (
                  <div
                    className={cn(RUN_TIME_TRACK, "hidden overflow-hidden @xl:block")}
                    aria-hidden="true"
                    data-run-timeline-bar=""
                  >
                    {bar ? (
                      <span
                        className={cn(
                          RUN_TIME_FILL,
                          RUN_TIME_COLOR[step.status]
                        )}
                        style={{ left: `${bar.offset}%`, width: `${bar.width}%` }}
                      />
                    ) : null}
                  </div>
                ) : null}
                <span data-run-step-status-duration="" className="whitespace-nowrap text-right text-xs leading-5 text-fg-muted tnum">
                  {visibleStatus ? labels.stepStatus[step.status] : null}
                  {visibleStatus && duration !== null ? " · " : null}
                  {duration !== null ? formatDuration(duration) : visibleStatus ? null : labels.notRecorded}
                </span>
              </div>
              {runStepResultSummary(step.details, step.detail) ? <p data-run-result-summary="" className="mt-1 ml-6 text-xs leading-5 text-fg-muted">{runStepResultSummary(step.details, step.detail)}</p> : null}
              {hasDetails ? (
                <div
                  id={regionId}
                  hidden={!open}
                  className="mt-2 mb-1 ml-6 grid min-w-0 gap-2 rounded-md border border-border bg-surface-sunken p-3 text-sm"
                  data-run-timeline-details=""
                  data-testid={stepTestId ? `${stepTestId}-details` : undefined}
                >
                  {open ? (
                    <>
                      {details ? (
                        <RunStepDetailsView details={details} labels={{ truncated: labels.stepDetailsTruncated }} timing={{ startedAt: step.startedAt, finishedAt: step.finishedAt, durationMs: duration }} />
                      ) : null}
                      {step.children}
                    </>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      </RunTimelineViewport>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 処理の経過の節

export interface RunTimelineSectionProps {
  traceSummary?: unknown;
  /** 処理の経過が trace の投影のときの trace id（「処理の詳細」に出す。#3226）。 */
  traceId?: string;
  /** 節の見出し（翻訳済み。「処理の経過」）。 */
  title: string;
  steps: readonly RunTimelineStep[];
  /** 処理全体の所要時間（ミリ秒）。開閉の見出しに出す（無ければ段の合計）。 */
  totalDurationMs?: number | null;
  /** 段が無いときの表示（開閉の見出しを出さずに置く）。 */
  empty?: ReactNode;
  /** 処理の詳細の中身。共通の開閉内に trace とまとめる。 */
  processDetails?: ReactNode;
  processDetailsProps?: Omit<RunProcessDetailsProps, "children" | "traceSummary" | "traceId">;
  /** 段の一覧を最初から開く。既定は閉じる（#2473）。 */
  defaultOpen?: boolean;
  /**
   * 実行中か（#3445）。true の間は閉じた見出しを「処理中（N ステップ完了）」と処理全体の経過時間（`startedAt` から。
   * 同じ `RunDetailLayout` に概要の進行表示 `RunJobProgress` があれば、経過時間はそちらの 1 か所だけ）にする（チャットの処理の経過と同じ。終わった段の所要時間の合計を「合計」として出さない）。既定 false。実行中の
   * 実行を詳細に出す製品（Agent の Run）は、実行の状態（終端でない）から渡す。終わった実行だけを出す製品（RAG・NL2SQL
   * の実行履歴）は渡さない（記録の段の状態からは推し量らない）。
   */
  active?: boolean;
  /** 実行の開始時刻（実行中の経過時間の起点。無ければ最初の段の開始）。 */
  startedAt?: OperationTimestamp;
  /**
   * 表に書けなかった span がある（一部だけの記録。#3226）。段の一覧の後ろ（段が無いときは `empty` の後ろ）に案内を
   * 出す。製品は `useTraceProgressSnapshot` の `partial` をそのまま渡す。
   */
  partial?: boolean;
  labels?: RunDetailLabelsInput;
  /** 節の `data-testid`。開閉の見出しは `<testId>-summary`、一部だけの記録の案内は `<testId>-partial`。 */
  testId?: string;
}

/**
 * 実行の詳細の「処理の経過」の節（#2473）。節の中を「N ステップ・合計 M 秒」の 1 行の開閉（`Disclosure` の plain。
 * 回答の根拠の「AI に渡した N 件 ／ 回答で引用 M 件」と同じ形）にし、既定で閉じる。開くと段の一覧（`RunTimeline`）と
 * その後ろの処理の詳細が出る。段の数はチャットの処理の経過（`ChatProgress`）と同じく入れ子の段も数える。
 * 3 製品の実行の詳細はこの部品を使い、節の中の並びを自分で組まない。
 */
export function RunTimelineSection({
  traceSummary,
  traceId,
  title,
  steps,
  totalDurationMs,
  empty,
  processDetails,
  processDetailsProps,
  defaultOpen = false,
  active = false,
  startedAt,
  partial = false,
  labels: labelsInput,
  testId,
}: RunTimelineSectionProps) {
  const labels = resolveRunDetailLabels(labelsInput);
  const traceLabels = useTraceDetailsLabels();
  const [open, setOpen] = useState(defaultOpen);
  // 開いた一覧が今の段を回している間は、同じ実行の概要の進行表示（`RunJobProgress`）を回さない（#3445）。
  const timelineSpins = open && steps.length > 0 && runTimelineCurrentStepIndex(steps) >= 0;
  useReportTimelineSpinning(timelineSpins);
  // 同じ実行の経過時間は 1 か所（messaging.md §3.7「同じ timer を複数のパネルに重ねない」）。概要の進行表示が出して
  // いれば、見出しには出さない。
  const jobProgress = useContext(RunDetailActivityContext)?.jobProgress ?? false;
  const partialNote = partial ? (
    <p className="text-xs text-fg-muted" data-testid={testId ? `${testId}-partial` : undefined}>
      {traceLabels.partial}
    </p>
  ) : null;
  const total = totalDurationMs ?? runTimelineDuration(steps);
  const failed = steps.some((step) => step.status === "failed");
  return (
    <RunDetailSection title={title} testId={testId}>
      {steps.length ? (
        <Disclosure
          variant="plain"
          size="sm"
          summary={
            <span className="tnum">
              {active
                ? labels.timelineRunning(steps.filter((step) => FINISHED_STEP_STATUSES.has(step.status)).length)
                : labels.timelineSummary(steps.length, formatRunDuration(total, labels.notRecorded))}
            </span>
          }
          meta={
            active && !jobProgress ? (
              <RunTimelineElapsed
                startedAt={startedAt ?? firstStepStartedAt(steps)}
                label={labels.timelineElapsed}
                testId={testId ? `${testId}-timer` : undefined}
              />
            ) : undefined
          }
          // 失敗した段がある実行は、色だけでなく失敗のアイコンでも伝える（チャットの処理の経過と同じ）。
          icon={failed ? XCircle : undefined}
          open={open}
          onOpenChange={setOpen}
          summaryClassName={failed ? "text-danger-fg [&>span:first-child]:text-danger-fg" : undefined}
          summaryProps={{ "data-testid": testId ? `${testId}-summary` : undefined, "data-run-progress-summary": "" }}
          contentClassName="grid min-w-0 grid-cols-1 gap-3 pt-2"
          data-run-timeline-section=""
        >
          <RunTimeline
            steps={steps}
            totalDurationMs={totalDurationMs}
            showSummary={false}
            // 実行中は全体の長さが伸び続けるので線を出さない（チャットの実行中の一覧と同じ）。
            showBars={!active}
            labels={labelsInput}
          />
          {partialNote}
          {processDetails != null || traceSummary != null || traceId ? <RunProcessDetails {...processDetailsProps} traceSummary={traceSummary} traceId={traceId}>{processDetails}</RunProcessDetails> : null}
        </Disclosure>
      ) : (
        <>
          {empty}
          {partialNote}
          {processDetails != null || traceSummary != null || traceId ? <RunProcessDetails {...processDetailsProps} traceSummary={traceSummary} traceId={traceId}>{processDetails}</RunProcessDetails> : null}
        </>
      )}
    </RunDetailSection>
  );
}

const FINISHED_STEP_STATUSES: ReadonlySet<RunTimelineStepStatus> = new Set(["done", "failed", "skipped"]);

function firstStepStartedAt(steps: readonly RunTimelineStep[]): number | undefined {
  const starts = steps
    .map((step) => operationTimestampMs(step.startedAt ?? undefined))
    .filter((value): value is number => value !== null);
  return starts.length ? Math.min(...starts) : undefined;
}

/**
 * 実行中の処理の経過の見出しの右の、処理全体の経過時間（「経過時間 00:12」。#3445）。チャットの処理の経過の見出しの
 * 時計と同じ形（`role="timer"`・読み上げない・桁をそろえる）。
 */
function RunTimelineElapsed({
  startedAt,
  label,
  testId,
}: {
  startedAt: OperationTimestamp;
  label: string;
  testId?: string;
}) {
  const timing = useOperationTiming({ active: true, operationKey: "run-timeline-total", startedAt });
  return (
    <span
      className="ml-auto inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap pl-3 text-xs font-normal leading-5 text-fg-muted"
      role="timer"
      aria-live="off"
      aria-label={`${label} ${timing.elapsedClock}`}
      data-testid={testId}
    >
      <Clock3 size={TEXT_ICON_SIZE.xs} aria-hidden="true" />
      <span>{label}</span>
      <span className="min-w-[3.25rem] text-right tabular-nums text-fg">{timing.elapsedClock}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// 動くスピナーの調停（#3445）

/**
 * 実行の詳細の中で、処理の経過の一覧が今の段を回しているか。`RunDetailLayout` が持ち、`RunTimelineSection` が知らせ、
 * 概要の進行表示（`RunJobProgress`）が読む（同じ処理の動くスピナーは 1 つ。messaging.md §3.7）。
 */
interface RunDetailActivity {
  timelineSpins: boolean;
  setTimelineSpins: (spins: boolean) => void;
  /** 概要の進行表示（`RunJobProgress`）が出ているか。出ていれば処理の経過の見出しに経過時間を重ねない。 */
  jobProgress: boolean;
  setJobProgress: (shown: boolean) => void;
}

const RunDetailActivityContext = createContext<RunDetailActivity | null>(null);

function useReportTimelineSpinning(spins: boolean): void {
  const setTimelineSpins = useContext(RunDetailActivityContext)?.setTimelineSpins;
  useEffect(() => {
    if (!setTimelineSpins) return undefined;
    setTimelineSpins(spins);
    return () => setTimelineSpins(false);
  }, [setTimelineSpins, spins]);
}

export type RunJobProgressProps = Omit<ProcessingIndicatorProps, "placement" | "activityIcon">;

/**
 * 実行の詳細の概要の進行表示（durable job。`ProcessingIndicator placement="job"`。#3445）。処理の経過を開いて一覧が今の
 * 段を回している間は、回さない実行中の印（`activityIcon="static"`）にし、文言・経過時間・遅延の案内だけを出す（動く
 * スピナーは今の段の 1 つ。閉じている・回す段が無いときは、この表示が回す）。3 製品の実行の詳細の進行表示はこの部品
 * を使い、`RunDetailLayout` の中に置く。文言は処理の経過の今の段の名前（`runTimelineCurrentStepIndex`）にし、内部名・
 * 種類のコードを出さない。
 */
export function RunJobProgress(props: RunJobProgressProps) {
  const activity = useContext(RunDetailActivityContext);
  const timelineSpins = activity?.timelineSpins ?? false;
  const setJobProgress = activity?.setJobProgress;
  const shown = props.active;
  useEffect(() => {
    if (!setJobProgress) return undefined;
    setJobProgress(shown);
    return () => setJobProgress(false);
  }, [setJobProgress, shown]);
  return <ProcessingIndicator {...props} placement="job" activityIcon={timelineSpins ? "static" : "spinner"} />;
}

// ---------------------------------------------------------------------------
// 並び

export interface RunDetailLayoutProps {
  /** 概要（`RunSummary`）。 */
  summary: ReactNode;
  /** 回答（最終の出力。`RunDetailSection` + 製品の回答の表示）。 */
  answer?: ReactNode;
  /** 処理の経過（`RunTimelineSection`。開閉の中にタイムラインと `RunProcessDetails`〔処理の詳細〕。#2325 / #2473）。 */
  timeline?: ReactNode;
  /** 根拠・SQL と結果・検証・成果物（製品の節。複数可）。 */
  evidence?: ReactNode;
  /** フィードバック。 */
  feedback?: ReactNode;
  /**
   * 処理の経過に属さない技術的な節（Agent の監査ログ。`RunDetailSection collapsible`。既定で閉じる）。処理の識別子・
   * 生成した SQL・イベントの記録などの技術的な詳細は、処理の経過の節の中の `RunProcessDetails` に置く（#2325）。
   */
  technical?: ReactNode;
  testId?: string;
}

/**
 * 詳細の節の順を固定する並び（#1955）。概要 → 処理の経過（タイムラインと処理の詳細。#2325）→ 回答 → 根拠 / SQL / 検証 →
 * フィードバック → 監査ログ（処理の経過は回答の上。チャットと同じ並び。#2126）。
 * タブで隠さず、1 枚のページを縦に読む（ページ内検索・印刷・読み上げで全部に届く。run-history.md「詳細」）。
 */
export function RunDetailLayout({ summary, answer, timeline, evidence, feedback, technical, testId }: RunDetailLayoutProps) {
  const [timelineSpins, setTimelineSpins] = useState(false);
  const [jobProgress, setJobProgress] = useState(false);
  const activity = useMemo(
    () => ({ timelineSpins, setTimelineSpins, jobProgress, setJobProgress }),
    [timelineSpins, jobProgress]
  );
  return (
    <RunDetailActivityContext.Provider value={activity}>
      <div className="grid min-w-0 content-start gap-4 [&>*]:min-w-0" data-run-detail="" data-testid={testId}>
        {summary}
        {timeline}
        {answer}
        {evidence}
        {feedback}
        {technical}
      </div>
    </RunDetailActivityContext.Provider>
  );
}

/** 詳細の読み込み中の形（概要のカード + 2 つの節）。`TimedLoadingState` の子に置く。 */
export function RunDetailSkeleton({ testId }: { testId?: string }) {
  return (
    <div className="grid min-w-0 gap-4" aria-hidden="true" data-testid={testId}>
      <div className="grid gap-4 rounded-lg border border-border bg-surface p-5">
        <div className="flex items-center gap-2">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-5 w-20 rounded-full" />
        </div>
        <Skeleton className="h-4 w-3/4" />
        <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(min(100%,11rem),1fr))]">
          {Array.from({ length: 6 }, (_, index) => (
            <div key={index} className="grid gap-1">
              <Skeleton className="h-3 w-16" />
              <Skeleton className="h-4 w-28" />
            </div>
          ))}
        </div>
      </div>
      {Array.from({ length: 2 }, (_, index) => (
        <div key={index} className="grid gap-3 rounded-lg border border-border bg-surface p-5">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ))}
    </div>
  );
}

/** 識別子は全文を表示し、コピーの対象はラベルを含まない ID だけにする。 */
function ProcessIdentifierRow({ label, value, trace = false }: { label: string; value: string | null; trace?: boolean }) {
  const labels = useTraceDetailsLabels().processMetadata ?? DEFAULT_TRACE_DETAILS_LABELS.processMetadata!;
  return <div className="flex min-w-0 items-center gap-2 text-xs" data-process-identifier={label} data-trace-id-row={trace ? "" : undefined}>
    <span className="shrink-0 text-fg-muted">{label}:{" "}</span>
    <span className="min-w-0 break-all font-mono text-fg">{value || "—"}</span>
    {value ? <CopyButton type="button" variant="ghost" size="sm" iconOnly className="shrink-0" text={value}
      label={labels.copy.replace("{label}", label)} copiedMessage={labels.copied.replace("{label}", label)} failedMessage={labels.copyFailed.replace("{label}", label)} /> : null}
  </div>;
}

/** 使用したモデルの記録。識別子の後ろに置く共通の表示。 */
export function RunUsedModels({ models, events = [] }: { models: readonly { label: string; value: string; title?: string }[]; events?: readonly { type: string; attributes?: Record<string, unknown> }[] }) {
  const recorded = events.flatMap(event => {
    if (event.type !== "step") return [];
    const value = event.attributes?.["gen_ai.request.model"];
    if (typeof value !== "string" || !value.trim()) return [];
    const operation = event.attributes?.["gen_ai.operation.name"];
    return [{ label: operation === "embeddings" ? "Embedding" : operation === "rerank" ? "Rerank" : "LLM", value }];
  });
  const used: readonly { label: string; value: string; title?: string }[] = [...models, ...recorded].filter((model, index, all) => all.findIndex(other => other.value === model.value) === index);
  const labels = useTraceDetailsLabels().processMetadata ?? DEFAULT_TRACE_DETAILS_LABELS.processMetadata!;
  return <div className="grid min-w-0 gap-2 text-xs" data-testid="answer-models" data-run-used-models="">
    <span className="font-medium text-fg-muted">{labels.models}</span>
    {used.length ? <dl className="flex min-w-0 flex-wrap gap-x-4 gap-y-2">{used.map((model, index) =>
      <div key={`${model.label}-${index}`} className="flex min-w-0 flex-wrap items-baseline gap-1.5">
        {model.label ? <dt className="text-fg-muted">{model.label}</dt> : null}
        <dd className="min-w-0 break-all font-medium text-fg" title={"title" in model ? model.title : undefined}>{model.value}</dd>
      </div>)}</dl> : <span className="text-fg-muted">—</span>}
  </div>;
}

/** Trace ID とコピーのボタン。ID の文字列だけを折り返し、ボタンを同じ行に保つ（375px。#3219）。 */
function TraceIdRow({ traceId, labels }: { traceId: string; labels: TraceDetailsLabels }) {
  return <div className="flex min-w-0 items-center gap-2" data-trace-id-row="">
    <span className="min-w-0 break-all font-mono text-xs text-fg-muted">Trace ID: {traceId}</span>
    <CopyButton type="button" variant="ghost" size="sm" iconOnly className="shrink-0" text={traceId} label={labels.copy} copiedMessage={labels.copied} failedMessage={labels.copyFailed} />
  </div>;
}

function TraceSegmentContent({ summary, labels }: { summary: unknown; labels: TraceDetailsLabels }) {
  const trace = traceTimeline(summary, labels);
  const notSampled = summary && typeof summary === "object" && "sampled" in summary && summary.sampled === false;
  return trace ? <div className="grid min-w-0 gap-2">
    <p className="text-xs text-fg-muted">{trace.steps.length === 0 ? labels.empty : trace.partial ? labels.partial : labels.measured}</p>
    {notSampled ? <p className="text-xs text-fg-muted">{labels.notSampled}</p> : null}
    {trace.traceId ? <TraceIdRow traceId={trace.traceId} labels={labels} /> : null}
    <RunTimeline steps={trace.steps} />
  </div> : null;
}

function TraceSegmentDetails({ summary, labels, title, defaultOpen }: { summary: unknown; labels: TraceDetailsLabels; title: string; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return <Disclosure variant="plain" size="sm" summary={title} open={open} onOpenChange={setOpen}>
    {open ? <TraceSegmentContent summary={summary} labels={labels} /> : null}
  </Disclosure>;
}

/** 5 つの質問の入口で使う、安全な実測 trace の詳細。実行区間の追加でも開閉を保つ。 */
export function TraceDetails({ summary }: { summary: unknown }) {
  const labels = useTraceDetailsLabels();
  const [open, setOpen] = useState(false);
  if (summary == null) return null;
  const segments = Array.isArray(summary)
    ? summary.filter(segment => segment && typeof segment === "object" && !Array.isArray(segment)).slice(0, 1000)
    : [summary];
  return <Disclosure variant="plain" size="sm" summary={labels.title} open={open} onOpenChange={setOpen} summaryProps={{ "data-testid": "trace-details-toggle" }}>
    {open ? <div className="grid min-w-0 grid-cols-1 gap-3" data-testid="trace-details">
      {segments.length === 0 ? <p className="text-xs text-fg-muted">{labels.empty}</p> : null}
      {segments.length === 1 ? <TraceSegmentContent summary={segments[0]} labels={labels} /> : segments.map((segment, index) => <div key={segment && typeof segment === "object" && "root_span_id" in segment && typeof segment.root_span_id === "string" ? segment.root_span_id : index}>
        <TraceSegmentDetails summary={segment} labels={labels} title={labels.segment(index + 1, segments.length)} defaultOpen={index === segments.length - 1} />
      </div>)}
    </div> : null}
  </Disclosure>;
}
