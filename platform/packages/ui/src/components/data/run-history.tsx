import { CalendarRange } from "lucide-react";
import type { ReactNode } from "react";

import { displayDateTimeParts, displayTimeZone } from "../../lib/date-time";
import { INFORMATION_TABLE_VISIBLE_ROWS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { formatChatProgressDuration } from "../feedback/chat-progress-format";
import { EmptyState } from "../feedback/state-views";
import { Button } from "../ui/button";
import { DataTable, type DataTableColumn } from "./data-table";
import { FilterSelect, isFilterActive } from "./list-filters";
import type { ListCountProps } from "./list-count";
import { ListToolbar, type ListToolbarLabels } from "./list-toolbar";
import { RowActionMenu, type EntityAction } from "./object-actions";
import { formatPaginationNumber } from "./pagination";
import { RowTitleButton } from "./row-title-button";
import { DEFAULT_RUN_STATUS_LABELS, RunStatusBadge, type RunStatus, type RunStatusLabels } from "./run-status";
import type { SelectFieldOption } from "../ui/select-field";

/*
 * 実行履歴の一覧（#1955）。3 製品（RAG・NL2SQL・Agent）の「実行履歴」の一覧を同じ列・同じ順・同じ狭い画面の形にする。
 * 製品はデータの取得・絞り込みの状態・行の操作の定義を持ち、行を `RunHistoryItem` に写して渡すだけにする。
 * 規則は UX 契約 run-history.md。
 */

// ---------------------------------------------------------------------------
// 書式

/** 所要時間の表記（「1.2 秒」「12 秒」「2 分 5 秒」）。記録が無ければ `notRecorded`（既定「—」）。処理の経過・チャットの経過と同じ書式。 */
export function formatRunDuration(ms: number | null | undefined, notRecorded = "—"): string {
  if (ms == null || !Number.isFinite(ms)) return notRecorded;
  return formatChatProgressDuration(ms);
}

function pad2(value: number) {
  return String(value).padStart(2, "0");
}

/**
 * 日時の表記（システムの設定の時間帯。`setDisplayTimeZone`。「2026/10/15 09:12」、`seconds` で「09:12:05」）。
 * 一覧・詳細で相対時刻（「3 分前」）を使わない（時刻を見比べ、監査の記録と照らすため。run-history.md）。
 * 読めない値はそのまま返す。
 */
export function formatRunDateTime(iso: string | null | undefined, options: { seconds?: boolean; timeZone?: boolean } = {}): string {
  if (!iso) return "—";
  const parts = displayDateTimeParts(iso);
  if (!parts) return iso;
  const day = `${parts.year}/${pad2(parts.month)}/${pad2(parts.day)}`;
  const time = `${pad2(parts.hour)}:${pad2(parts.minute)}${options.seconds ? `:${pad2(parts.second)}` : ""}`;
  return `${day} ${time}${options.timeZone ? ` (${displayTimeZone()})` : ""}`;
}

/** token の数の表記（「1,234 token」）。 */
export function formatRunTokens(tokens: number | null | undefined, notRecorded = "—"): string {
  if (tokens == null || !Number.isFinite(tokens)) return notRecorded;
  return `${formatPaginationNumber(tokens)} token`;
}

// ---------------------------------------------------------------------------
// 実行のモデルの利用量（RAG・NL2SQL の実行履歴の一覧の「token」と詳細の概要。#3419・#3466・#3543）

/**
 * 計測の状態。complete: すべての呼び出しを計測した。partial: 一部の呼び出しの利用量が無い。
 * unmeasured: アプリが利用量を受け取れない呼び出し（DB の中でモデルを呼ぶ呼び出しなど）だけで、数を持たない。
 */
export type RunUsageMeasurement = "complete" | "partial" | "unmeasured";

/** 1 回の実行のモデルの利用量（backend の `pr_backend_core.observability.model_usage.RunModelUsage`）。 */
export interface RunModelUsageRecord {
  /** 呼び出したモデルの ID（主のモデルが先頭）。 */
  models: string[];
  requests: number;
  input_tokens: number;
  output_tokens: number;
  /** 入力 + 出力（cached は入力、reasoning は出力の内数）。 */
  total_tokens: number;
  cached_input_tokens?: number | null;
  reasoning_output_tokens?: number | null;
  measurement: RunUsageMeasurement;
  measured_requests: number;
  /** 利用量を計測できない呼び出しの出どころ（`db_model` など。回数は数えない）。古い記録は無い。 */
  unmeasured_sources?: string[];
}

/** 利用量の文言（製品の i18n で上書きできる）。 */
export interface RunUsageLabels {
  unmeasured: string;
  partial: string;
  /** 「モデルの呼び出し」の値（すべて計測）。数は桁区切り済み。 */
  modelCalls: (values: { requests: string; input: string; output: string }) => string;
  /** 「モデルの呼び出し」の値（一部計測）。 */
  modelCallsPartial: (values: { requests: string; measured: string; input: string; output: string }) => string;
}

export const DEFAULT_RUN_USAGE_LABELS: RunUsageLabels = {
  unmeasured: "未計測",
  partial: "一部計測",
  modelCalls: ({ requests, input, output }) => `${requests} 回（入力 ${input} / 出力 ${output} token）`,
  modelCallsPartial: ({ requests, measured, input, output }) =>
    `${requests} 回（うち ${measured} 回を計測。入力 ${input} / 出力 ${output} token）`,
};

/** 詳細の概要の「モデル」「token」「モデルの呼び出し」と、未計測の呼び出しの出どころ。 */
export interface RunUsageView {
  /** 呼び出したモデルの ID（複数は「、」で並べる）。記録が無ければ null（並べない）。 */
  model: string | null;
  /** `RunSummary` の `usage`（数があれば数の下に、無ければ注記だけ）。 */
  usage: { tokens: number | null; note: string | null };
  modelCalls: string;
  /** 利用量を計測できない呼び出しの出どころ（表示名を「、」で並べる）。無ければ null。 */
  unmeasuredCalls: string | null;
}

/**
 * 実行のモデルと利用量の表示（3 製品で同じ規則）。モデルは実行のときに記録した ID だけを出し、今の設定から推測しない。
 *
 * - 利用量の記録が無い（この機能より前の記録・trace の始まる前に失敗した実行）: token と呼び出しは「未計測」（0 にしない）。
 * - 計測できない呼び出しだけ（それだけの実行）: token と呼び出しは「未計測」で、出どころを `unmeasuredCalls` に出す。
 * - 一部の呼び出しの利用量が無い・計測できない呼び出しが混ざる: 計測できた分の数に「一部計測」を添える。
 * - LLM を呼ばなかった実行: 実測の 0。
 */
export function runUsageView(
  usage: RunModelUsageRecord | null | undefined,
  options: {
    /** 利用量の記録に無いときに出すモデル（実行の記録の列）。 */
    fallbackModel?: string | null;
    labels?: Partial<RunUsageLabels>;
    /** 未計測の出どころの表示名（無ければ ID のまま）。 */
    sourceLabel?: (source: string) => string;
  } = {}
): RunUsageView {
  const labels = { ...DEFAULT_RUN_USAGE_LABELS, ...options.labels };
  const models = usage?.models.length ? usage.models : options.fallbackModel ? [options.fallbackModel] : [];
  const model = models.length ? models.join("、") : null;
  const sources = usage?.unmeasured_sources ?? [];
  const unmeasuredCalls = sources.length
    ? sources.map((source) => options.sourceLabel?.(source) ?? source).join("、")
    : null;
  if (!usage || usage.measurement === "unmeasured") {
    return {
      model,
      usage: { tokens: null, note: labels.unmeasured },
      modelCalls: labels.unmeasured,
      unmeasuredCalls,
    };
  }
  const partial = usage.measurement === "partial";
  const values = {
    requests: formatPaginationNumber(usage.requests),
    measured: formatPaginationNumber(usage.measured_requests),
    input: formatPaginationNumber(usage.input_tokens),
    output: formatPaginationNumber(usage.output_tokens),
  };
  return {
    model,
    usage: { tokens: usage.total_tokens, note: partial ? labels.partial : null },
    modelCalls: partial ? labels.modelCallsPartial(values) : labels.modelCalls(values),
    unmeasuredCalls,
  };
}

/**
 * 一覧の「token」の列（`RunHistoryItem.usage`）。一覧の API は利用量の全体ではなく合計と計測の状態だけを返す。詳細の概要
 * （`runUsageView`）と同じ規則: 記録の無い実行と計測できない呼び出しだけの実行は「未計測」（0 にしない）、一部計測は数に
 * 「一部計測」を添え、LLM を呼ばなかった実行は実測の 0。
 */
export function runListUsage(
  totalTokens: number | null | undefined,
  measurement: RunUsageMeasurement | null | undefined,
  labels: Partial<Pick<RunUsageLabels, "unmeasured" | "partial">> = {}
): { tokens: number | null; note: string | null } {
  const resolved = { ...DEFAULT_RUN_USAGE_LABELS, ...labels };
  if (!measurement || measurement === "unmeasured") return { tokens: null, note: resolved.unmeasured };
  return { tokens: totalTokens ?? 0, note: measurement === "partial" ? resolved.partial : null };
}

// ---------------------------------------------------------------------------
// 絞り込みの値（部品は #1933 の `FilterSelect` を使う。ここは 3 製品で同じ選択肢と既定の文言）

/** 期間の絞り込み。既定は `DEFAULT_RUN_PERIOD`（過去 7 日）。 */
export type RunPeriod = "all" | "24h" | "7d" | "30d";

export const RUN_PERIODS: readonly RunPeriod[] = ["all", "24h", "7d", "30d"] as const;

/**
 * 期間の既定（#3386）。3 製品の実行履歴の初期値・「条件をクリア」の戻り先・「既定と違う条件か」の基準。
 * 直近の実行を見る一覧なので過去 7 日にし、それより前は「すべて」を選ぶと出す（件数の「N 件（全 M 件）」と
 * `RunHistoryPeriodEmptyState` で、隠れている実行があることを伝える）。
 */
export const DEFAULT_RUN_PERIOD: RunPeriod = "7d";

export const DEFAULT_RUN_PERIOD_LABELS: Record<RunPeriod, string> = {
  all: "すべて",
  "24h": "過去 24 時間",
  "7d": "過去 7 日",
  "30d": "過去 30 日",
};

const PERIOD_MS: Record<Exclude<RunPeriod, "all">, number> = {
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
};

/** 期間の開始（ISO 8601。backend の `since` に渡す）。`all` は null（条件を付けない）。 */
export function runPeriodSince(period: RunPeriod, now: Date = new Date()): string | null {
  if (period === "all") return null;
  return new Date(now.getTime() - PERIOD_MS[period]).toISOString();
}

/** 期間の空の状態の既定の文言（`RunHistoryPeriodEmptyState`。#3386）。 */
export const DEFAULT_RUN_PERIOD_EMPTY_LABELS = {
  /** 「〈期間〉の実行はありません」。 */
  title: (period: string) => `${period}の実行はありません`,
  hint: "それより前の実行は、期間を「すべて」にすると表示します。",
  showAll: "すべての期間を表示",
} as const;

export interface RunHistoryPeriodEmptyStateProps {
  /** 今の期間（`all` 以外）。 */
  period: Exclude<RunPeriod, "all">;
  /** 期間を「すべて」にする（ページも 1 ページ目へ）。 */
  onShowAll: () => void;
  labels?: {
    period?: Partial<Record<RunPeriod, string>>;
    title?: string;
    hint?: string;
    showAll?: string;
  };
  testId?: string;
}

/**
 * 期間の中に実行が無く、それより前の実行はあるときの空の状態（#3386）。「まだ実行がありません」と分け、
 * 期間で隠れていることと、期間を「すべて」にする操作を出す。3 製品の実行履歴で同じ文言・同じ形にする。
 */
export function RunHistoryPeriodEmptyState({ period, onShowAll, labels, testId }: RunHistoryPeriodEmptyStateProps) {
  const periodLabel = { ...DEFAULT_RUN_PERIOD_LABELS, ...labels?.period }[period];
  return (
    <div data-testid={testId} data-run-history-period-empty={period}>
      <EmptyState
        title={labels?.title ?? DEFAULT_RUN_PERIOD_EMPTY_LABELS.title(periodLabel)}
        hint={labels?.hint ?? DEFAULT_RUN_PERIOD_EMPTY_LABELS.hint}
        action={
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={CalendarRange}
            onClick={onShowAll}
            data-testid={testId ? `${testId}-show-all` : undefined}
          >
            {labels?.showAll ?? DEFAULT_RUN_PERIOD_EMPTY_LABELS.showAll}
          </Button>
        }
      />
    </div>
  );
}

/** 実行者の絞り込み（#1954）。既定は `all`。他の人の実行が見える利用者（system_admin・会話の持ち主）だけに出す。 */
export type RunExecutorFilter = "all" | "self" | "others";

export const RUN_EXECUTOR_FILTERS: readonly RunExecutorFilter[] = ["all", "self", "others"] as const;

export const DEFAULT_RUN_EXECUTOR_FILTER_LABELS: Record<RunExecutorFilter, string> = {
  all: "すべて",
  self: "自分が実行",
  others: "他の人が実行",
};

/** 絞り込みの名前（`FilterSelect` の `label`）。 */
export const DEFAULT_RUN_FILTER_NAMES = {
  status: "状態",
  kind: "種類",
  period: "期間",
  executor: "実行者",
} as const;

/** 状態の絞り込みの値（`all` = すべて）。 */
export type RunStatusFilter = RunStatus | "all";

/** 絞り込みの 1 つ（値と変更）。 */
export interface RunHistoryFilter<T extends string> {
  value: T;
  onChange: (value: T) => void;
}

export interface RunHistoryToolbarProps {
  /** 検索欄（`SearchField`。質問・実行 ID）。 */
  search?: ReactNode;
  /** 検索語が入っている（「条件をクリア」を出す）。 */
  searchActive?: boolean;
  /** 状態の絞り込み。`options` は製品が写せる共通の状態だけ（既定 `RUN_STATUSES` の全部ではない）。 */
  status?: RunHistoryFilter<RunStatusFilter> & { options: readonly RunStatus[] };
  /**
   * 種類の絞り込み。既定の値は `defaultValue`（無ければ `options` の先頭）。品質評価の実行を既定で隠す製品は
   * 「品質評価以外」を既定にし、先頭に置く（「すべて」はその次。#1983）。「条件をクリア」の数え方と
   * 選択中の強調は、この既定の値を基準にする。
   */
  kind?: RunHistoryFilter<string> & { options: readonly SelectFieldOption<string>[]; defaultValue?: string };
  period?: RunHistoryFilter<RunPeriod>;
  /** 実行者の絞り込み（他の人の実行が見える利用者だけ。#1954）。 */
  executor?: RunHistoryFilter<RunExecutorFilter>;
  /** 検索語とすべての絞り込みを既定に戻す（ページも 1 ページ目へ）。 */
  onClearFilters?: () => void;
  count?: ListCountProps;
  labels?: {
    all?: string;
    names?: Partial<Record<keyof typeof DEFAULT_RUN_FILTER_NAMES, string>>;
    status?: Partial<RunStatusLabels>;
    period?: Partial<Record<RunPeriod, string>>;
    executor?: Partial<Record<RunExecutorFilter, string>>;
    toolbar?: Partial<ListToolbarLabels>;
  };
  /** 絞り込みの id の接頭辞（画面の中で一意）。 */
  idPrefix?: string;
  className?: string;
  testId?: string;
}

/**
 * 実行履歴の一覧のツールバー（#1955）。`ListToolbar` に検索 → 状態 → 種類 → 期間 → 実行者の順で `FilterSelect` を並べ、
 * 3 製品で並び・既定・「条件をクリア」・狭い画面の形（絞り込みが 3 つ以上ならシート）をそろえる。
 */
export function RunHistoryToolbar({
  search,
  searchActive,
  status,
  kind,
  period,
  executor,
  onClearFilters,
  count,
  labels,
  idPrefix = "run-history",
  className,
  testId,
}: RunHistoryToolbarProps) {
  const all = labels?.all ?? DEFAULT_RUN_PERIOD_LABELS.all;
  const names = { ...DEFAULT_RUN_FILTER_NAMES, ...labels?.names };
  const statusLabels = { ...DEFAULT_RUN_STATUS_LABELS, ...labels?.status };
  const periodLabels = { ...DEFAULT_RUN_PERIOD_LABELS, ...labels?.period };
  const executorLabels = { ...DEFAULT_RUN_EXECUTOR_FILTER_LABELS, ...labels?.executor };

  const filters: ReactNode[] = [];
  let active = 0;
  if (status) {
    active += Number(isFilterActive(status.value, "all"));
    filters.push(
      <FilterSelect<RunStatusFilter>
        key="status"
        id={`${idPrefix}-status`}
        label={names.status}
        value={status.value}
        options={[{ value: "all", label: all }, ...status.options.map((value) => ({ value, label: statusLabels[value] }))]}
        onValueChange={status.onChange}
        defaultValue="all"
        data-testid={testId ? `${testId}-status` : undefined}
      />
    );
  }
  if (kind) {
    const defaultKind = kind.defaultValue ?? kind.options[0]?.value ?? "";
    active += Number(isFilterActive(kind.value, defaultKind));
    filters.push(
      <FilterSelect<string>
        key="kind"
        id={`${idPrefix}-kind`}
        label={names.kind}
        value={kind.value}
        options={kind.options}
        onValueChange={kind.onChange}
        defaultValue={defaultKind}
        // 既定を「品質評価以外」にする製品（#1983・#1986）は既定の文言が長く、sm では「種類: 品質評価…」と
        // 切れるので md にする（実行者と同じ段）。
        width={kind.defaultValue ? "md" : undefined}
        data-testid={testId ? `${testId}-kind` : undefined}
      />
    );
  }
  if (period) {
    active += Number(isFilterActive(period.value, DEFAULT_RUN_PERIOD));
    filters.push(
      <FilterSelect<RunPeriod>
        key="period"
        id={`${idPrefix}-period`}
        label={names.period}
        value={period.value}
        options={RUN_PERIODS.map((value) => ({ value, label: periodLabels[value] }))}
        onValueChange={period.onChange}
        defaultValue={DEFAULT_RUN_PERIOD}
        data-testid={testId ? `${testId}-period` : undefined}
      />
    );
  }
  if (executor) {
    active += Number(isFilterActive(executor.value, "all"));
    filters.push(
      <FilterSelect<RunExecutorFilter>
        key="executor"
        id={`${idPrefix}-executor`}
        label={names.executor}
        value={executor.value}
        options={RUN_EXECUTOR_FILTERS.map((value) => ({ value, label: executorLabels[value] }))}
        onValueChange={executor.onChange}
        defaultValue="all"
        width="md"
        data-testid={testId ? `${testId}-executor` : undefined}
      />
    );
  }

  return (
    <ListToolbar
      search={search}
      searchActive={searchActive}
      filters={filters.length > 0 ? filters : undefined}
      activeFilterCount={active}
      onClearFilters={onClearFilters}
      filtersLayout={filters.length >= 3 ? "sheet" : "wrap"}
      count={count}
      labels={labels?.toolbar}
      className={className}
      testId={testId}
    />
  );
}

// ---------------------------------------------------------------------------
// 一覧

/** 狭い画面の 1 行の値を「 · 」でつなぐ（区切りは読み上げない）。空の値は飛ばす。 */
function joinMeta(parts: ReactNode[]): ReactNode[] {
  const present = parts.filter((part) => part !== null && part !== undefined && part !== "" && part !== false);
  return present.flatMap((part, index): ReactNode[] =>
    index === 0
      ? [<span key="part-0">{part}</span>]
      : [
          <span key={`sep-${index}`} aria-hidden="true">
            {" · "}
          </span>,
          <span key={`part-${index}`}>{part}</span>,
        ]
  );
}

/** 一覧の 1 行（製品の行をこの形に写す）。文言はすべて翻訳済み。 */
export interface RunHistoryItem {
  /** 実行 ID（行の key・`data-run-id`）。 */
  id: string;
  /** 質問（Agent は依頼の文）。長い文は 2 行で切り詰め、全文は詳細で読む。 */
  title: string;
  /** 対象（業務 Agent・検索・回答プロファイル・業務プロファイルの名前）。題名の下に出す。 */
  target?: string | null;
  /** 詳細の URL（`?id=<run_id>` / `/history/<run_id>`）。リンクの形にして新しいタブでも開けるようにする。 */
  href: string;
  status: RunStatus;
  /** 製品に固有の状態の文言（例: waiting →「承認待ち」）。 */
  statusLabel?: string;
  /** 種類（RAG の「チャット / RAG 検索」、NL2SQL の「チャット / SQL 生成」、Agent の「チャット / API」）。 */
  kind?: string | null;
  /** 実行者（#1954）。`isSelf` は「自分」と出す。 */
  executor?: { name: string; isSelf?: boolean } | null;
  /** 開始日時（ISO 8601）。 */
  startedAt: string;
  /** 所要時間（ミリ秒）。実行中・記録なしは null。 */
  durationMs?: number | null;
  /**
   * 利用量（token の数・費用の表記）。記録が無い製品は渡さない。`note` は計測の状態の注記（「一部計測」「未計測」など。
   * 翻訳済み）で、詳細の概要（`RunSummary` の `usage.note`）と同じ規則で出す: 数があれば数の下に、無ければ注記だけ
   * （不明を 0 と出さない。#3419・#3466）。
   */
  usage?: { tokens?: number | null; note?: string | null; cost?: string | null } | null;
}

/** 一覧の文言。製品の i18n で上書きできる（部分で渡す）。 */
export interface RunHistoryLabels {
  /** 表の名前（読み上げ。例「実行履歴」）。 */
  table: string;
  columns: {
    title: string;
    status: string;
    kind: string;
    executor: string;
    startedAt: string;
    duration: string;
    usage: string;
    actions: string;
  };
  /** 実行者が自分のときの表記。 */
  self: string;
  /** 記録が無い値。 */
  notRecorded: string;
  /** 題名のリンクの読み上げ名（見える文字を含める。WCAG 2.5.3）。 */
  openDetail: (title: string) => string;
  /** 行の操作のメニューの読み上げ名。 */
  rowActions: (title: string) => string;
  status: RunStatusLabels;
}

export const DEFAULT_RUN_HISTORY_LABELS: RunHistoryLabels = {
  table: "実行履歴",
  columns: {
    title: "質問",
    status: "状態",
    kind: "種類",
    executor: "実行者",
    startedAt: "開始日時",
    duration: "所要時間",
    usage: "token / 費用",
    actions: "操作",
  },
  self: "自分",
  notRecorded: "—",
  openDetail: (title) => `${title}（詳細を開く）`,
  rowActions: (title) => `${title}の操作`,
  status: DEFAULT_RUN_STATUS_LABELS,
};

/** 部分の上書きを既定に重ねる（`columns`・`status` は項目ごと）。 */
export function resolveRunHistoryLabels(labels?: RunHistoryLabelsInput): RunHistoryLabels {
  return {
    ...DEFAULT_RUN_HISTORY_LABELS,
    ...labels,
    columns: { ...DEFAULT_RUN_HISTORY_LABELS.columns, ...labels?.columns },
    status: { ...DEFAULT_RUN_HISTORY_LABELS.status, ...labels?.status },
  };
}

export type RunHistoryLabelsInput = Partial<Omit<RunHistoryLabels, "columns" | "status">> & {
  columns?: Partial<RunHistoryLabels["columns"]>;
  status?: Partial<RunStatusLabels>;
};

export interface RunHistoryTableProps<T> {
  /** 主一覧で広い画面の下の余白を活用する（DataTable と同じ基準）。 */
  growToViewport?: boolean;
  rows: readonly T[];
  /** 製品の行を共通の形に写す。 */
  toItem: (row: T) => RunHistoryItem;
  /** 修飾キーの無いクリック・Enter で詳細を開く（画面の中で移る。作業状態を保つ）。 */
  onOpen: (row: T) => void;
  /** 「種類」の列を出す（既定 true。種類が 1 つしかない一覧は false）。 */
  showKind?: boolean;
  /** 「実行者」の列を出す（他の人の実行が見える利用者のとき。#1954）。 */
  showExecutor?: boolean;
  /** 「token / 費用」の列を出す（利用量を記録している製品）。 */
  showUsage?: boolean;
  /**
   * 製品に固有の列（RAG の回答の対応・NL2SQL の行数など）。所要時間の右（操作の左）に置く。
   * 2 列までにし、狭い置き場所（56rem 未満）では出さない（詳細で見る）。
   */
  extraColumns?: DataTableColumn<T>[];
  /** 行の操作（`RowActionMenu`）。詳細の `ObjectActionBar` と同じ定義を渡す。 */
  actionsFor?: (row: T) => readonly EntityAction[];
  /** 詳細に表示中（戻ったとき）の実行。行に `aria-current` と選択の面を付ける。 */
  selectedRunId?: string | null;
  labels?: RunHistoryLabelsInput;
  /** 日時の表記（既定 `formatRunDateTime`）。 */
  formatDateTime?: (iso: string) => string;
  /** 読み込み中（行の代わりにスケルトン）。初回の読み込みは `TimedLoadingState` + `TableSkeleton` を使う。 */
  loading?: boolean;
  /** 0 件のとき（`EmptyState`。絞り込み中は条件のクリア付き）。 */
  empty?: ReactNode;
  className?: string;
  testId?: string;
}

/** 広い一覧（表の置き場所が 56rem 以上）だけに出す列。狭いと題名の下の 1 行にまとめる。 */
const WIDE_ONLY = "hidden @4xl:table-cell";

/**
 * 実行履歴の表（#1955）。
 *
 * ```text
 * | 質問（リンク・2 行）/ 対象 | 状態 | 種類 | 実行者 | 開始日時 | 所要時間 | (token / 費用) | (製品の列) | 操作 |
 * ```
 *
 * - 列の順は 3 製品で同じ（run-history.md「一覧」）。先頭の題名は `RowTitleButton`（`href` 付きのリンク）で、
 *   キーボードの導線（Tab → Enter）と新しいタブで開く導線を持つ。行のクリックは補助。
 * - 置き場所が 56rem 未満（375px・狭いパネル・タブレット）は、題名と操作の 2 列にし、状態・日時・所要時間・実行者を
 *   題名の下の 1 行にまとめる（container query。横スクロールさせない）。
 * - 表頭の固定と 5 / 8 行の高さ（`stickyHeader` + `visibleRows`）は一覧の基準どおり。ページ送りは表の直下に製品が置く
 *   （サーバーの offset 型 `OffsetPagination`。既定の件数は `DEFAULT_PAGE_SIZE`）。
 */
export function RunHistoryTable<T>({
  rows,
  growToViewport = false,
  toItem,
  onOpen,
  showKind = true,
  showExecutor = false,
  showUsage = false,
  extraColumns = [],
  actionsFor,
  selectedRunId = null,
  labels: labelsInput,
  formatDateTime = formatRunDateTime,
  loading,
  empty,
  className,
  testId,
}: RunHistoryTableProps<T>) {
  const labels = resolveRunHistoryLabels(labelsInput);
  const executorText = (item: RunHistoryItem) =>
    item.executor ? (item.executor.isSelf ? labels.self : item.executor.name) : labels.notRecorded;

  const columns: DataTableColumn<T>[] = [
    {
      key: "title",
      header: labels.columns.title,
      rowHeader: true,
      className: "min-w-0 @4xl:min-w-[14rem]",
      render: (row) => {
        const item = toItem(row);
        return (
          <div className="grid min-w-0 gap-1">
            <RowTitleButton
              title={item.title}
              fullTitle={item.title}
              subtitle={item.target ?? undefined}
              href={item.href}
              maxLines={2}
              current={item.id === selectedRunId}
              aria-label={labels.openDetail(item.title)}
              data-run-id={item.id}
              onClick={() => onOpen(row)}
            />
            {/* 狭い置き場所だけ: 状態・開始日時・所要時間・実行者を 1 行にまとめる（広いと列で出す）。 */}
            <div
              className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted @4xl:hidden"
              data-run-history-compact-meta=""
            >
              <RunStatusBadge status={item.status} label={item.statusLabel} labels={labels.status} />
              <span className="min-w-0 tnum">
                {joinMeta([
                  <time key="startedAt" dateTime={item.startedAt}>
                    {formatDateTime(item.startedAt)}
                  </time>,
                  formatRunDuration(item.durationMs, labels.notRecorded),
                  showKind ? item.kind : null,
                  showExecutor && item.executor ? executorText(item) : null,
                ])}
              </span>
            </div>
          </div>
        );
      },
    },
    {
      key: "status",
      header: labels.columns.status,
      className: WIDE_ONLY,
      headerClassName: WIDE_ONLY,
      render: (row) => {
        const item = toItem(row);
        return <RunStatusBadge status={item.status} label={item.statusLabel} labels={labels.status} />;
      },
    },
  ];
  if (showKind) {
    columns.push({
      key: "kind",
      header: labels.columns.kind,
      className: cn(WIDE_ONLY, "whitespace-nowrap text-fg"),
      headerClassName: WIDE_ONLY,
      render: (row) => toItem(row).kind || labels.notRecorded,
    });
  }
  if (showExecutor) {
    columns.push({
      key: "executor",
      header: labels.columns.executor,
      className: cn(WIDE_ONLY, "min-w-[6rem] break-words text-fg"),
      headerClassName: WIDE_ONLY,
      render: (row) => executorText(toItem(row)),
    });
  }
  columns.push(
    {
      key: "startedAt",
      header: labels.columns.startedAt,
      className: cn(WIDE_ONLY, "whitespace-nowrap tnum text-fg-muted"),
      headerClassName: WIDE_ONLY,
      render: (row) => {
        const item = toItem(row);
        return <time dateTime={item.startedAt}>{formatDateTime(item.startedAt)}</time>;
      },
    },
    {
      key: "duration",
      header: labels.columns.duration,
      align: "right",
      className: cn(WIDE_ONLY, "whitespace-nowrap tnum text-fg-muted"),
      headerClassName: WIDE_ONLY,
      render: (row) => formatRunDuration(toItem(row).durationMs, labels.notRecorded),
    }
  );
  if (showUsage) {
    columns.push({
      key: "usage",
      header: labels.columns.usage,
      align: "right",
      className: cn(WIDE_ONLY, "whitespace-nowrap tnum text-fg-muted"),
      headerClassName: WIDE_ONLY,
      render: (row) => {
        const usage = toItem(row).usage;
        if (!usage || (usage.tokens == null && !usage.cost && !usage.note)) return labels.notRecorded;
        // 数の無い実行（未計測など）は注記だけを 1 行で出す（「—」と注記を重ねない）。
        if (usage.tokens == null && !usage.cost) return <span data-run-usage-note="">{usage.note}</span>;
        return (
          <span className="grid justify-items-end">
            <span>{formatRunTokens(usage.tokens, labels.notRecorded)}</span>
            {usage.note ? (
              <span className="text-xs" data-run-usage-note="">
                {usage.note}
              </span>
            ) : null}
            {usage.cost ? <span className="text-xs">{usage.cost}</span> : null}
          </span>
        );
      },
    });
  }
  for (const column of extraColumns) {
    columns.push({
      ...column,
      className: cn(WIDE_ONLY, column.className),
      headerClassName: cn(WIDE_ONLY, column.headerClassName),
    });
  }
  if (actionsFor) {
    columns.push({
      key: "actions",
      // not-sr-only は white-space: normal に戻すので、広い置き場所でも 1 行に保つ（「操 / 作」にしない。#3427）。
      header: <span className="sr-only @4xl:not-sr-only @4xl:whitespace-nowrap">{labels.columns.actions}</span>,
      align: "right",
      className: "w-px whitespace-nowrap",
      render: (row) => {
        const item = toItem(row);
        // 操作の無い行（終わった実行）のセルは空。列はメニューのボタン（iconOnly の sm）の幅を保ち、操作の無い行だけの
        // ページでも縮めない（ページ・再取得で列の幅を変えない。#3427）。
        return (
          <div className="flex min-w-[var(--button-height-sm)] justify-end" data-run-history-actions="">
            <RowActionMenu
              actions={actionsFor(row)}
              ariaLabel={labels.rowActions(item.title)}
              testId={testId ? `${testId}-row-actions-${item.id}` : undefined}
            />
          </div>
        );
      },
    });
  }

  return (
    <div className={cn("@container min-w-0", className)} data-run-history-table="">
      <DataTable
        columns={columns}
        rows={rows}
        getRowKey={(row) => toItem(row).id}
        onRowClick={onOpen}
        selectedRowKey={selectedRunId}
        stickyHeader
        visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
        growToViewport={growToViewport}
        loading={loading}
        empty={empty}
        ariaLabel={labels.table}
        scrollAriaLabel={labels.table}
        testId={testId}
        scrollTestId={testId ? `${testId}-scroll-region` : undefined}
        tableClassName="w-full text-sm"
        rowProps={(row) => {
          const item = toItem(row);
          return {
            className: "align-top",
            "data-testid": testId ? `${testId}-row-${item.id}` : undefined,
          };
        }}
      />
    </div>
  );
}
