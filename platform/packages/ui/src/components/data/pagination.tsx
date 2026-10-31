import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";
import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { isImeComposing } from "../../lib/keyboard";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { CONTROL_MIN_HEIGHT_CLASS, fieldControlClass } from "../ui/control-size";
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "./page-size";
import { PageSizeSelect } from "./page-size-select";

export { DEFAULT_PAGE_SIZE };

export interface PaginationRange {
  /** 1-based の表示開始行（空なら 0）。 */
  start: number;
  /** 表示終了行（含む）。 */
  end: number;
  /** 全件数。 */
  total: number;
}

/**
 * Pagination の文言。パッケージは i18n に依存しないため、既定は日本語（`DEFAULT_PAGINATION_LABELS`）で、
 * 製品は i18n の文字列で上書きできる（`labels` に部分だけ渡してもよい）。数値は `formatPaginationNumber`
 * （桁区切り）で書き、製品ごとに書式を変えない（#1891）。
 */
export interface PaginationLabels {
  /** 件数の範囲（例: 「1–25 / 123 件」。区切りは en dash）。 */
  summary: (range: PaginationRange) => string;
  /**
   * 「N / M ページ」。advanced では今のページの読み上げ（`role="status"`）に、カーソル型では見える表示に使う。
   */
  pageIndicator: (page: number, totalPages: number) => string;
  /** ページ番号の入力欄の後ろの「/ M ページ」。 */
  pageCount: (totalPages: number) => string;
  /** 総数の分からないカーソル型の「N ページ目」。 */
  cursorPage: (page: number) => string;
  /** ページ番号の入力欄の名前（読み上げ。例「ページ番号」）。 */
  pageInput: string;
  /** 範囲の外・数でない値を入れたときの理由（入力欄は直前のページに戻る）。 */
  pageOutOfRange: (totalPages: number) => string;
  /** 最初のページへ（アイコンだけのボタンの名前と Tooltip）。 */
  first: string;
  prev: string;
  next: string;
  /** 最後のページへ（アイコンだけのボタンの名前と Tooltip）。 */
  last: string;
  /** 表示件数の選択のラベル。 */
  pageSize: string;
  /** 表示件数の選択肢（例「25 件」）。 */
  pageSizeOption: (size: number) => string;
  /** nav の aria-label（一覧ごとに `ariaLabel` で上書きできる）。 */
  ariaLabel: string;
}

const numberFormat = new Intl.NumberFormat("ja-JP");

/** 共通の数値の書式（桁区切り）。件数・ページ番号はすべてこれで書く（製品で書式を変えない。#1891）。 */
export function formatPaginationNumber(value: number) {
  return numberFormat.format(value);
}

/** Pagination の既定の文言（日本語。3 製品で同じ形。#1266 / #1891）。 */
export const DEFAULT_PAGINATION_LABELS: PaginationLabels = {
  summary: ({ start, end, total }) =>
    `${formatPaginationNumber(start)}–${formatPaginationNumber(end)} / ${formatPaginationNumber(total)} 件`,
  pageIndicator: (page, totalPages) =>
    `${formatPaginationNumber(page)} / ${formatPaginationNumber(totalPages)} ページ`,
  pageCount: (totalPages) => `/ ${formatPaginationNumber(totalPages)} ページ`,
  cursorPage: (page) => `${formatPaginationNumber(page)} ページ目`,
  pageInput: "ページ番号",
  pageOutOfRange: (totalPages) => `1〜${formatPaginationNumber(totalPages)} のページ番号を入力してください`,
  first: "最初のページ",
  prev: "前へ",
  next: "次へ",
  last: "最後のページ",
  pageSize: "表示件数",
  pageSizeOption: (size) => `${formatPaginationNumber(size)} 件`,
  ariaLabel: "ページ送り",
};

/**
 * ページ番号の入力を読む（#1891）。全角の数字（IME）も受け付ける。数でない・範囲の外は null。
 */
export function parsePageInput(raw: string, totalPages: number): number | null {
  const text = raw.normalize("NFKC").replace(/[,，\s]/g, "");
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < 1 || value > Math.max(1, totalPages)) return null;
  return value;
}

export interface UsePaginationOptions {
  /**
   * 制御式の現在ページ（1-based）。ページ番号を作業状態（sessionStorage・URL 等）に保持するときに渡す
   * （UX 契約 workspace-state.md）。渡すと、再取得で items が変わってもページを戻さない（範囲外は表示だけ末尾に寄せる）。
   */
  page?: number;
  /** 制御式のときのページ変更。`setPage` と Pagination の操作から呼ばれる。 */
  onPageChange?: (page: number) => void;
  /**
   * 1 ページ目へ戻す契機（検索語・絞り込み・並べ替え等）。この値が変わったときだけ 1 ページ目へ戻す。
   * 省略時: 非制御なら items が変わるたびに戻す（従来どおり）、制御式なら戻さない。
   */
  resetKey?: unknown;
}

/**
 * 一覧のページング状態（slice・totalPages・clamp）を 1 箇所に集約するフック。
 * 各ページで重複していた「PAGE_SIZE / setPage / slice / clamp」を置き換える。
 * 既定では items が変わったら 1 ページ目へ戻す。ページを保持する一覧は `options.page` / `onPageChange` /
 * `resetKey` を渡す。
 */
export function usePagination<T>(
  items: readonly T[],
  pageSize: number = DEFAULT_PAGE_SIZE,
  options: UsePaginationOptions = {}
) {
  const controlled = options.page !== undefined;
  const [innerPage, setInnerPage] = useState(1);
  const requestedPage = controlled ? Math.max(1, Math.floor(options.page ?? 1)) : innerPage;
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(requestedPage, totalPages);

  const onPageChangeRef = useRef(options.onPageChange);
  useLayoutEffect(() => {
    onPageChangeRef.current = options.onPageChange;
  });
  const setPage = useCallback(
    (next: number) => {
      if (controlled) onPageChangeRef.current?.(next);
      else setInnerPage(next);
    },
    [controlled]
  );

  // 前回の契機と比べて変わったときだけ戻す（StrictMode の effect の再実行や復元直後の初回で戻さない）。
  const resetTrigger = "resetKey" in options ? options.resetKey : controlled ? CONTROLLED_WITHOUT_RESET : items;
  const previousResetTrigger = useRef(resetTrigger);
  useEffect(() => {
    if (Object.is(previousResetTrigger.current, resetTrigger)) return;
    previousResetTrigger.current = resetTrigger;
    setPage(1);
  }, [resetTrigger, setPage]);

  const start = total === 0 ? 0 : (currentPage - 1) * pageSize;
  const pageItems = useMemo(
    () => items.slice(start, start + pageSize),
    [items, start, pageSize]
  );

  const range = paginationRange(currentPage, pageSize, pageItems.length, total);

  return { page: currentPage, setPage, totalPages, pageItems, range };
}

/** 制御式で resetKey が無いときの、変わらない契機。 */
const CONTROLLED_WITHOUT_RESET = Symbol("controlled-without-reset");

/**
 * 件数のサマリの範囲（「a - b / n 件」の a・b・n）。`count` は今のページで出している件数。
 * 0 件のときは start / end とも 0。
 */
export function paginationRange(page: number, pageSize: number, count: number, total: number): PaginationRange {
  const size = Math.max(1, pageSize);
  const offset = (Math.max(1, Math.floor(page)) - 1) * size;
  const safeTotal = Math.max(0, total);
  return {
    start: count === 0 || safeTotal === 0 ? 0 : offset + 1,
    end: count === 0 ? 0 : offset + count,
    total: safeTotal,
  };
}

/**
 * サーバー側のページング（offset / limit / total）を Pagination の page / totalPages / range に直す。
 * `count` は今のページで返った件数。ページの移動は `offsetForPage(page, limit)` で offset に戻す。
 */
export function offsetPagination({
  offset,
  limit,
  total,
  count,
}: {
  offset: number;
  limit: number;
  total: number;
  count: number;
}) {
  const size = Math.max(1, limit);
  const page = Math.floor(Math.max(0, offset) / size) + 1;
  const totalPages = Math.max(1, Math.ceil(Math.max(0, total) / size));
  const range: PaginationRange = {
    start: total === 0 || count === 0 ? 0 : offset + 1,
    end: count === 0 ? 0 : offset + count,
    total,
  };
  return { page, totalPages, range };
}

/** 1-based のページ番号を offset に直す。 */
export function offsetForPage(page: number, limit: number) {
  return (Math.max(1, Math.floor(page)) - 1) * Math.max(1, limit);
}

/** 最後のページの offset（0 件なら 0）。 */
export function lastPageOffset(total: number, limit: number) {
  if (total <= 0) return 0;
  const size = Math.max(1, limit);
  return Math.floor((total - 1) / size) * size;
}

/**
 * 表示中のページが範囲外になったとき（最後のページの最後の 1 件を削除した・保存していたページが
 * 他の操作で無くなった）に移るべき offset を返す。範囲内なら null。
 * 1 件もない（total=0）ときは先頭（0）へ戻す。DB の縮退で空になったときも 0 に戻るだけで、回復後は先頭から表示する。
 */
export function outOfRangeOffset({
  offset,
  total,
  limit,
}: {
  offset: number;
  total: number;
  limit: number;
}): number | null {
  if (offset === 0 || offset < total) return null;
  return lastPageOffset(total, limit);
}

/**
 * カーソル型の API（`next_cursor` と `total` を返す）のページを、共通の Pagination の
 * page / totalPages / range に直す（#403。NL2SQL から共通にした）。前へ戻るためのカーソルは画面が積んで持つ
 * （`useCursorPages`）。
 * - `depth`: 今までに「次へ」で進んだ回数（積んだカーソルの数）。
 * - `total` がカーソルの位置と合わない（取得の間に件数が変わった）ときも、次のカーソルがあれば次のページを数える。
 */
export function cursorPagination({
  depth,
  limit,
  total,
  count,
  hasNext,
}: {
  depth: number;
  limit: number;
  /** 全件数。API が返さない（数えるのが重い）ときは省く（「N ページ目」の縮約版になる。#1891）。 */
  total?: number | null;
  count: number;
  hasNext: boolean;
}): { page: number; totalPages: number; range: PaginationRange | undefined; totalKnown: boolean } {
  const size = Math.max(1, limit);
  const page = Math.max(0, depth) + 1;
  const offset = (page - 1) * size;
  const totalKnown = typeof total === "number" && Number.isFinite(total);
  const knownTotal = totalKnown ? Math.max(0, total as number) : 0;
  const totalPages = Math.max(page + (hasNext ? 1 : 0), Math.ceil(knownTotal / size), 1);
  return {
    page,
    totalPages,
    range: totalKnown
      ? {
          start: count === 0 ? 0 : offset + 1,
          end: count === 0 ? 0 : offset + count,
          total: Math.max(knownTotal, offset + count),
        }
      : undefined,
    totalKnown,
  };
}

interface CursorPagesState {
  cursor: string | null;
  stack: Array<string | null>;
}

const EMPTY_CURSOR_PAGES: CursorPagesState = { cursor: null, stack: [] };

/**
 * カーソル型の API の「前へ / 次へ」の状態（今のカーソルと、前へ戻るために積んだカーソル）。
 * `next(nextCursor)` で進み、`prev()` で 1 つ戻り、`reset()`（または `resetKey` の変化）で先頭へ戻す。
 * `cursor` は最初のページで `null`（API には送らない）。
 */
export function useCursorPages(options: { resetKey?: unknown } = {}) {
  const [state, setState] = useState<CursorPagesState>(EMPTY_CURSOR_PAGES);
  const next = useCallback((nextCursor: string) => {
    setState((current) => ({ cursor: nextCursor, stack: [...current.stack, current.cursor] }));
  }, []);
  const prev = useCallback(() => {
    setState((current) => {
      if (current.stack.length === 0) return current;
      const stack = current.stack.slice(0, -1);
      return { cursor: current.stack[current.stack.length - 1] ?? null, stack };
    });
  }, []);
  const reset = useCallback(() => {
    setState((current) => (current.cursor === null && current.stack.length === 0 ? current : EMPTY_CURSOR_PAGES));
  }, []);

  // 検索語・絞り込みが変わったら先頭へ（usePagination の resetKey と同じ規則）。
  const resetTrigger = "resetKey" in options ? options.resetKey : CONTROLLED_WITHOUT_RESET;
  const previousResetTrigger = useRef(resetTrigger);
  useEffect(() => {
    if (Object.is(previousResetTrigger.current, resetTrigger)) return;
    previousResetTrigger.current = resetTrigger;
    reset();
  }, [resetTrigger, reset]);

  return {
    cursor: state.cursor,
    depth: state.stack.length,
    page: state.stack.length + 1,
    canGoPrevious: state.stack.length > 0,
    next,
    prev,
    reset,
  };
}


function resolveLabels(labels: Partial<PaginationLabels> | undefined): PaginationLabels {
  return labels ? { ...DEFAULT_PAGINATION_LABELS, ...labels } : DEFAULT_PAGINATION_LABELS;
}

/**
 * ページの送り方。
 * - `advanced`（既定）: 最初へ・前へ・[ページ番号の入力] / N ページ・次へ・最後へ（任意のページへ移れる一覧）
 * - `cursor`: 前へ・「N / M ページ」か「N ページ目」・次へ（カーソル型。隣のページにしか移れない）
 */
export type PaginationMode = "advanced" | "cursor";

export interface PaginationProps {
  /** 1-based の現在ページ。 */
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  /** 文言（部分だけでもよい。省略は `DEFAULT_PAGINATION_LABELS`）。製品は `paginationLabels()` を渡す。 */
  labels?: Partial<PaginationLabels>;
  /** 件数の範囲（`usePagination` / `offsetPagination` / `cursorPagination` の `range`）。左に「1–25 / 123 件」を出す。 */
  range?: PaginationRange;
  /** 今の 1 ページの件数。`onPageSizeChange` と一緒に渡すと、範囲の右に表示件数の選択を出す。 */
  pageSize?: number;
  /** 表示件数の選択肢（10 / 25 / 50 / 100。既定 25）。 */
  pageSizeOptions?: readonly number[];
  /** 表示件数を変えたとき。呼び出し側は 1 ページ目へ戻す（`usePagination` は `resetKey` に件数を含める）。 */
  onPageSizeChange?: (size: number) => void;
  /** 送り方（既定 `advanced`）。 */
  mode?: PaginationMode;
  /** 1 ページでも範囲とページ番号を出す（結果シートなど）。通常の一覧では省略する。 */
  showSinglePage?: boolean;
  /** 取得中など、ページを移らせない間（前のページを出したまま）。 */
  disabled?: boolean;
  /** nav の aria-label（任意）。省略時は labels.ariaLabel。 */
  ariaLabel?: string;
  /** テスト用 data-testid（任意）。 */
  testId?: string;
  className?: string;
}

/** 表示件数の選択を出すか。1 ページに収まっていても、最も少ない段より件数が多ければ出す（戻せるように）。 */
export function shouldShowPageSize({
  totalPages,
  total,
  options,
  enabled,
}: {
  totalPages: number;
  total: number | undefined;
  options: readonly number[];
  enabled: boolean;
}) {
  if (!enabled || options.length === 0) return false;
  return totalPages > 1 || (total ?? 0) > Math.min(...options);
}

/**
 * 共通のページ送り（#265 / #1266 / #1891。Carbon の advanced pagination・Material のデータ表・ICDS の型）。
 *
 * ```text
 * [1–25 / 123 件] [表示件数 25 件▾]           [«] [‹ 前へ] [ 1 ] / 5 ページ [次へ ›] [»]
 * ```
 *
 * - 左: 件数の範囲（`tnum`）と、`onPageSizeChange` を渡したときだけ表示件数の選択。
 * - 右: 最初へ・前へ・ページ番号の入力（Enter か欄を離れたときに移る。範囲の外・数でない値は直前のページに
 *   戻し、理由を下に出す）/ 総ページ数・次へ・最後へ。
 * - 置き場所の幅（container query）が 28rem 未満（375px の画面・会話の履歴のパネル）では、最初へ・最後へを隠し、
 *   範囲と「前へ・入力・次へ」を 2 行に折り返す。タッチ端末では操作部品が 44px になる（トークン）。
 * - `nav` のランドマーク（aria-label）。今のページは `role="status"` で読み上げる（「2 / 13 ページ」）。
 * - 1 ページに収まるときはページ送りを出さない（件数は `ListToolbar` の `ListCount` が出す）。表示件数の選択が
 *   あるときだけ、範囲と選択を出す。
 */
export function Pagination({
  page,
  totalPages,
  onPageChange,
  labels: labelOverrides,
  range,
  pageSize,
  pageSizeOptions = PAGE_SIZE_OPTIONS,
  onPageSizeChange,
  mode = "advanced",
  disabled = false,
  showSinglePage = false,
  ariaLabel,
  testId,
  className,
}: PaginationProps) {
  const labels = resolveLabels(labelOverrides);
  const pages = Math.max(1, Math.floor(totalPages));
  const current = Math.min(Math.max(1, Math.floor(page)), pages);
  const showPager = pages > 1 || showSinglePage;
  const showPageSize = shouldShowPageSize({
    totalPages: pages,
    total: range?.total,
    options: pageSizeOptions,
    enabled: Boolean(onPageSizeChange) && pageSize !== undefined,
  });
  if (!showPager && !showPageSize) return null;
  const summary = range ? labels.summary(range) : null;

  return (
    <nav
      className={cn("@container grid min-w-0 gap-1 text-xs text-fg-muted", className)}
      aria-label={ariaLabel ?? labels.ariaLabel}
      data-testid={testId}
      data-pagination={mode}
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2">
        {summary || showPageSize ? (
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2" data-pagination-start="">
            {summary ? (
              <span className="tnum" data-pagination-range="">
                {summary}
              </span>
            ) : null}
            {showPageSize && onPageSizeChange && pageSize !== undefined ? (
              <span className="flex items-center gap-2">
                {/* 見えるラベル。選択欄はラベルを読み上げに持つので、ここは読ませない（二重に読ませない）。 */}
                <span aria-hidden="true">{labels.pageSize}</span>
                <PageSizeSelect
                  value={pageSize}
                  onValueChange={onPageSizeChange}
                  options={pageSizeOptions}
                  labels={{ label: labels.pageSize, option: labels.pageSizeOption }}
                  size="sm"
                  labelHidden
                  disabled={disabled}
                  // 横に並べる行の中の欄。狭い画面でも全幅にせず、数値の段（xs）のまま並べる。
                  className="w-[var(--field-width-xs)] shrink-0"
                />
              </span>
            ) : null}
          </div>
        ) : null}
        {showPager ? (
          mode === "cursor" ? (
            <CursorPager page={current} totalPages={pages} labels={labels} disabled={disabled} onPageChange={onPageChange} />
          ) : (
            <AdvancedPager
              page={current}
              totalPages={pages}
              labels={labels}
              disabled={disabled}
              onPageChange={onPageChange}
            />
          )
        ) : null}
      </div>
    </nav>
  );
}

/**
 * 前へ / 次へ の文字。置き場所（Pagination の container）が 20rem 未満（一覧 + 詳細の一覧の列の下端など。#2282）では
 * 見た目をアイコンだけにし、文字は読み上げに残す（ボタンの名前は変わらない）。
 */
const PAGER_LABEL_CLASS = "sr-only @xs:not-sr-only";

interface PagerProps {
  page: number;
  totalPages: number;
  labels: PaginationLabels;
  disabled: boolean;
  onPageChange: (page: number) => void;
}

function AdvancedPager({ page, totalPages, labels, disabled, onPageChange }: PagerProps) {
  const reactId = useId();
  const inputId = `pagination-page-${reactId}`;
  const countId = `${inputId}-count`;
  const messageId = `${inputId}-message`;
  const [draft, setDraft] = useState(String(page));
  const [message, setMessage] = useState("");

  // ページが外から変わったら（前へ・次へ・検索で 1 ページ目へ）入力欄を合わせ、理由を消す。
  useEffect(() => {
    setDraft(String(page));
    setMessage("");
  }, [page, totalPages]);

  const go = (target: number) => {
    const next = Math.min(Math.max(1, target), totalPages);
    if (next !== page) onPageChange(next);
  };

  const commit = (raw: string) => {
    if (raw.trim() === "" || raw.normalize("NFKC").trim() === String(page)) {
      setDraft(String(page));
      setMessage("");
      return;
    }
    const target = parsePageInput(raw, totalPages);
    if (target === null) {
      // 範囲の外・数でない値は直前のページに戻し、理由を出す（入力を黙って捨てない）。
      setDraft(String(page));
      setMessage(labels.pageOutOfRange(totalPages));
      return;
    }
    setMessage("");
    setDraft(String(target));
    go(target);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      // 変換を確定する Enter では移らない。囲む form を送信しない。
      if (isImeComposing(event)) return;
      event.preventDefault();
      commit(event.currentTarget.value);
    } else if (event.key === "Escape" && draft !== String(page)) {
      event.preventDefault();
      event.stopPropagation();
      setDraft(String(page));
      setMessage("");
    }
  };

  const atFirst = page <= 1;
  const atLast = page >= totalPages;

  return (
    <div className="grid min-w-0 justify-items-end gap-1" data-pagination-pager="advanced">
      <div className="flex min-w-0 items-center gap-1.5">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          iconOnly
          icon={ChevronsLeft}
          aria-label={labels.first}
          disabled={disabled || atFirst}
          onClick={() => go(1)}
          // 狭い置き場所（375px・会話の履歴のパネル）では畳む（前へ・入力で足りる）。
          className="hidden @md:inline-flex"
          data-pagination-first=""
        />
        <Button
          type="button"
          variant="secondary"
          size="sm"
          icon={ChevronLeft}
          disabled={disabled || atFirst}
          onClick={() => go(page - 1)}
        >
          <span className={PAGER_LABEL_CLASS}>{labels.prev}</span>
        </Button>
        <span className="flex items-center gap-1.5">
          <input
            id={inputId}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            enterKeyHint="go"
            aria-label={labels.pageInput}
            aria-describedby={message ? `${countId} ${messageId}` : countId}
            value={draft}
            disabled={disabled}
            onChange={(event) => {
              setDraft(event.target.value);
              if (message) setMessage("");
            }}
            onKeyDown={handleKeyDown}
            onBlur={(event) => commit(event.currentTarget.value)}
            onFocus={(event) => event.currentTarget.select()}
            className={cn(
              fieldControlClass,
              CONTROL_MIN_HEIGHT_CLASS.sm,
              "border-border-control w-16 px-2 text-center tnum"
            )}
            data-pagination-input=""
          />
          <span id={countId} className="tnum whitespace-nowrap text-fg">
            {labels.pageCount(totalPages)}
          </span>
        </span>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          trailingIcon={ChevronRight}
          disabled={disabled || atLast}
          onClick={() => go(page + 1)}
        >
          <span className={PAGER_LABEL_CLASS}>{labels.next}</span>
        </Button>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          iconOnly
          icon={ChevronsRight}
          aria-label={labels.last}
          disabled={disabled || atLast}
          onClick={() => go(totalPages)}
          className="hidden @md:inline-flex"
          data-pagination-last=""
        />
      </div>
      {/* 範囲の外を入れたときの理由（入力欄の補足。aria-describedby で結ぶ）。 */}
      <p id={messageId} className="text-right text-xs text-fg-muted empty:hidden" data-pagination-message="">
        {message}
      </p>
      {/*
        読み上げ（1 つの Pagination に 1 つだけの live region）。前へ・次へ・入力で移ったら今のページを、
        範囲の外を入れたらその理由を読む。
      */}
      <p role="status" className="sr-only" data-pagination-status="">
        {message || labels.pageIndicator(page, totalPages)}
      </p>
    </div>
  );
}

function CursorPager({
  page,
  totalPages,
  totalKnown = true,
  labels,
  disabled,
  onPageChange,
}: PagerProps & { totalKnown?: boolean }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5" data-pagination-pager="cursor">
      <Button
        type="button"
        variant="secondary"
        size="sm"
        icon={ChevronLeft}
        disabled={disabled || page <= 1}
        onClick={() => onPageChange(page - 1)}
      >
        <span className={PAGER_LABEL_CLASS}>{labels.prev}</span>
      </Button>
      <span
        role="status"
        className="tnum inline-flex min-h-[var(--button-height-sm)] items-center whitespace-nowrap rounded-control border border-border-control px-3 text-fg"
      >
        {totalKnown ? labels.pageIndicator(page, totalPages) : labels.cursorPage(page)}
      </span>
      <Button
        type="button"
        variant="secondary"
        size="sm"
        trailingIcon={ChevronRight}
        disabled={disabled || page >= totalPages}
        onClick={() => onPageChange(page + 1)}
      >
        <span className={PAGER_LABEL_CLASS}>{labels.next}</span>
      </Button>
    </div>
  );
}

export interface OffsetPaginationProps {
  offset: number;
  limit: number;
  total: number;
  /** 今のページで返った件数。 */
  count: number;
  /** ページの移動（1-based のページ番号と、その offset）。 */
  onPageChange: (page: number, offset: number) => void;
  /**
   * 表示件数を変えたとき（渡すと表示件数の選択を出す）。呼び出し側は limit を変え、offset を 0 に戻す。
   */
  onPageSizeChange?: (limit: number) => void;
  /** 表示件数の選択肢（10 / 25 / 50 / 100。既定 25）。 */
  pageSizeOptions?: readonly number[];
  labels?: Partial<PaginationLabels>;
  /** 単一ページでもページ番号と移動操作を表示する。 */
  showSinglePage?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  testId?: string;
  className?: string;
}

/**
 * サーバー側（offset / limit / total）でページングする一覧の直下に置く Pagination（#794 / #1266。Agent の
 * `ServerPagination` と RAG の `outOfRangeOffset` を共通にした）。
 * 1 ページしかないときは出さない（表示件数の選択があるときは範囲と選択だけ）。表示中のページが範囲外（削除・期間の変更・
 * 保存していたページが無くなった）になって 0 件で返ったら、最後のページへ寄せる（`onPageChange` を呼ぶ）。
 */
export function OffsetPagination({
  offset,
  limit,
  total,
  count,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions,
  labels,
  showSinglePage,
  disabled,
  ariaLabel,
  testId,
  className,
}: OffsetPaginationProps) {
  const paging = offsetPagination({ offset, limit, total, count });
  const onPageChangeRef = useRef(onPageChange);
  useLayoutEffect(() => {
    onPageChangeRef.current = onPageChange;
  });
  const fallbackOffset = count === 0 ? outOfRangeOffset({ offset, total, limit }) : null;
  useEffect(() => {
    if (fallbackOffset === null) return;
    onPageChangeRef.current(Math.floor(fallbackOffset / Math.max(1, limit)) + 1, fallbackOffset);
  }, [fallbackOffset, limit]);
  return (
    <Pagination
      page={paging.page}
      totalPages={paging.totalPages}
      range={paging.range}
      onPageChange={(page) => onPageChange(page, offsetForPage(page, limit))}
      pageSize={limit}
      pageSizeOptions={pageSizeOptions}
      onPageSizeChange={onPageSizeChange}
      labels={labels}
      showSinglePage={showSinglePage}
      disabled={disabled}
      ariaLabel={ariaLabel}
      testId={testId}
      className={className}
    />
  );
}

export interface CursorPaginationProps {
  /** 「次へ」で進んだ回数（`useCursorPages().depth`）。 */
  depth: number;
  /** 1 ページの件数（既定 25 件）。 */
  limit?: number;
  /** 全件数。API が返さないときは省く（「前へ・N ページ目・次へ」の縮約版になる）。 */
  total?: number | null;
  /** 今のページで返った件数。 */
  count: number;
  /** API が返した次のカーソル（無ければ null / undefined / 空文字）。 */
  nextCursor: string | null | undefined;
  onPrevious: () => void;
  onNext: (nextCursor: string) => void;
  labels?: Partial<PaginationLabels>;
  disabled?: boolean;
  ariaLabel?: string;
  testId?: string;
  className?: string;
}

/**
 * カーソル型の API（`next_cursor`、と分かれば `total`）のページ送り（#403 / #1266 / #1891）。
 * 前へ戻るカーソルは `useCursorPages` が積んで持つので、移動は隣のページだけ（前へ / 次へ）。ページ番号の入力・最初へ・
 * 最後へは持たない。総数が分かれば範囲「1–25 / 123 件」と「N / M ページ」、分からなければ「N ページ目」。
 * 任意のページへ移る必要がある一覧は、サーバーを offset 型にして `OffsetPagination` を使う。
 */
export function CursorPagination({
  depth,
  limit = DEFAULT_PAGE_SIZE,
  total,
  count,
  nextCursor,
  onPrevious,
  onNext,
  labels: labelOverrides,
  disabled = false,
  ariaLabel,
  testId,
  className,
}: CursorPaginationProps) {
  const next = nextCursor || null;
  const { page, totalPages, range, totalKnown } = cursorPagination({
    depth,
    limit,
    total,
    count,
    hasNext: next !== null,
  });
  const labels = resolveLabels(labelOverrides);
  if (totalPages <= 1) return null;
  const summary = range ? labels.summary(range) : null;
  return (
    <nav
      className={cn("@container grid min-w-0 gap-1 text-xs text-fg-muted", className)}
      aria-label={ariaLabel ?? labels.ariaLabel}
      data-testid={testId}
      data-pagination="cursor"
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2">
        {summary ? (
          <span className="tnum" data-pagination-range="">
            {summary}
          </span>
        ) : null}
        <CursorPager
          page={page}
          totalPages={totalPages}
          totalKnown={totalKnown}
          labels={labels}
          disabled={disabled}
          onPageChange={(target) => {
            if (target < page) onPrevious();
            else if (target > page && next !== null) onNext(next);
          }}
        />
      </div>
    </nav>
  );
}
