import { Check } from "lucide-react";
import {
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { isImeComposing } from "../../lib/keyboard";
import { INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_VISIBLE_ROWS } from "../../lib/list-density";
import { prefixOffsets, scrollTopToReveal, visibleRange } from "../../lib/list-window";
import { cn } from "../../lib/utils";
import { EmptyState, ErrorState } from "../feedback/state-views";
import { TimedLoadingState } from "../feedback/processing-state";
import { BulkSelectionActions } from "../ui/bulk-selection-actions";
import { ClearActionButton } from "../ui/clear-action-button";
import { SearchField, type SearchFieldProps } from "../ui/search-field";
import { ListSkeleton } from "../ui/skeleton";
import { ToggleChip } from "../ui/toggle-chip";
import { ListToolbar } from "./list-toolbar";
import { LoadMoreFooter } from "./load-more-footer";

/** 候補の 1 行。 */
export interface ListPickerItem {
  /** 選択の突合に使う一意の値（文書 ID・`OWNER.OBJECT` など）。 */
  key: string;
  /** 行の名前（1 行で省略表示。全文は title）。選択肢の読み上げの名前にもなる。 */
  label: ReactNode;
  /** 名前の文字列（title・読み上げ・「選択中だけ表示」での絞り込みに使う）。 */
  textValue: string;
  /** 名前の下の補足（1 行で省略表示。例: 論理名・種類・更新日）。 */
  description?: ReactNode;
  /** 行の右端の補足（例: 状態の StatusBadge）。読み上げでは選択肢の説明になる。 */
  meta?: ReactNode;
  /** 選べない候補（例: すでに追加済み）。フォーカスは移れるが、選択は切り替わらない。 */
  disabled?: boolean;
  /** 選べない理由（行の右端に出し、読み上げの説明にもする。例:「追加済み」）。 */
  disabledReason?: string;
  /** 属するグループ（`groups` の key）。 */
  groupKey?: string;
}

/** 候補のグループ（例: スキーマ）。グループの見出しと、グループ単位の一括選択を持つ。 */
export interface ListPickerGroup {
  key: string;
  label: ReactNode;
  /** グループ名の文字列（一覧の読み上げの名前・一括操作の読み上げに使う）。 */
  textValue: string;
  /** 見出しに並べる件数（例:「選択 3 / 全 32 件」）。 */
  countLabel?: string;
  /** グループの候補をすべて選ぶ（サーバーから全件を取って選ぶなど、非同期でよい）。 */
  onSelectAll?: () => void | Promise<void>;
  /** グループの選択をすべて解除する。 */
  onClearAll?: () => void | Promise<void>;
  selectAllDisabled?: boolean;
  clearAllDisabled?: boolean;
  /** data-testid の接尾辞（既定は `group-<key>`。見出しは `<testId>-<接尾辞>-heading`、一括操作は `-bulk-actions`）。 */
  testId?: string;
}

/** 文言（翻訳済み）。パッケージは i18n を持たないので、製品が `t()` で渡す。 */
export interface ListPickerLabels {
  /** フッターの件数（例:「100 / 3,000 件を表示、選択 3 件」）。 */
  resultCount: (counts: { visible: number; total: number; selected: number }) => string;
  /** 検索欄の件数の読み上げ（例:「12 件が一致しました」）。 */
  searchResultCount: (total: number) => string;
  loading: string;
  refreshing: string;
  loadMore: string;
  retry: string;
  emptyTitle: string;
  emptyHint?: string;
  noResultsTitle: string;
  noResultsHint?: string;
  clearSearch: string;
  /** 「選択中だけ表示」の切り替え（件数付き）。 */
  showSelected: (selected: number) => string;
  selectedEmpty: string;
  selectVisible: string;
  clearSelection: string;
  groupSelectAll: string;
  groupClearAll: string;
  groupSelectAllAria: (group: string) => string;
  groupClearAllAria: (group: string) => string;
  /** 一覧の操作の説明（読み上げ）。 */
  keyboardHint: string;
}

const formatCount = (value: number) => value.toLocaleString("ja-JP");

export const DEFAULT_LIST_PICKER_LABELS: ListPickerLabels = {
  resultCount: ({ visible, total, selected }) =>
    `${formatCount(visible)} / ${formatCount(total)} 件を表示、選択 ${formatCount(selected)} 件`,
  searchResultCount: (total) => `${formatCount(total)} 件が一致しました`,
  loading: "候補を読み込んでいます",
  refreshing: "候補を更新しています",
  loadMore: "さらに読み込む",
  retry: "再試行",
  emptyTitle: "選べる候補がありません",
  noResultsTitle: "検索に一致する候補がありません",
  noResultsHint: "検索語を変えるか、クリアしてください。",
  clearSearch: "検索語をクリア",
  showSelected: (selected) => `選択中だけ表示（${formatCount(selected)}）`,
  selectedEmpty: "選択している候補はありません",
  selectVisible: "表示中をすべて選択",
  clearSelection: "選択をすべて解除",
  groupSelectAll: "すべて選択",
  groupClearAll: "選択をすべて解除",
  groupSelectAllAria: (group) => `${group} をすべて選択`,
  groupClearAllAria: (group) => `${group} の選択をすべて解除`,
  keyboardHint: "上下の矢印キーで移動し、Space キーで選択を切り替えます。",
};

export type ListPickerSearch = Omit<SearchFieldProps, "id" | "clearLabel" | "resultCountLabel"> & {
  id?: string;
  clearLabel?: string;
};

export interface ListPickerProps {
  /** 要素の id の接頭辞。 */
  id: string;
  /** 候補の一覧の名前（listbox の読み上げの名前。例:「追加する文書の候補」）。 */
  label: string;
  /** 見出し（任意）。無ければ `label` を読み上げの名前にする。 */
  title?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  /** 見出しの下の説明（任意）。 */
  description?: ReactNode;
  /** 読み込んだ候補（サーバーの検索とページングの結果を積んだもの）。 */
  items: readonly ListPickerItem[];
  /** グループ（任意）。渡すと、この順にグループの見出しと候補を並べる。 */
  groups?: readonly ListPickerGroup[];
  /** 選択中の key。 */
  selectedKeys: ReadonlySet<string>;
  /** 1 件の選択を切り替えたとき。 */
  onToggle: (item: ListPickerItem, selected: boolean) => void;
  /** 「表示中をすべて選択」（任意）。読み込んだ候補のうち、選べて未選択のものを渡す。 */
  onSelectMany?: (items: readonly ListPickerItem[]) => void;
  /** 「選択をすべて解除」（任意）。 */
  onClearSelection?: () => void;
  /**
   * 選択中の候補（任意）。渡すと「選択中だけ表示」の切り替えが出て、検索や読み込みの範囲の外にある選択も確かめられる。
   */
  selectedItems?: readonly ListPickerItem[];
  /**
   * 条件に一致する全件数（サーバーの total）。省略時は読み込んだ件数。
   * サーバー側のページングの状態（`total` / `hasMore` / `loadingMore` / `onLoadMore`）は、
   * 検索できる選択欄（`SearchableSelectRemote`、#578）と同じ名前で受ける。
   */
  total?: number;
  /** 候補の検索欄（任意）。ツールバーの左端に置く。検索は入力に合わせてサーバーで絞る（#535）。 */
  search?: ListPickerSearch;
  /** 検索欄の右に並べる絞り込み（任意）。 */
  filters?: ReactNode;
  /** 検索語以外の絞り込みが効いているか（0 件の文言を「一致しない」にする）。 */
  hasActiveFilter?: boolean;
  /** 最初の読み込み中。 */
  loading?: boolean;
  /** 条件を変えて取り直している間（前の候補を出したまま）。 */
  refreshing?: boolean;
  /** 最初の読み込みの失敗。 */
  error?: string;
  onRetry?: () => void;
  /** 続きがあるか（「さらに読み込む」を出す）。 */
  hasMore?: boolean;
  loadingMore?: boolean;
  loadMoreError?: string;
  onLoadMore?: () => void;
  /**
   * 選択を切り替えられない間（保存中など）。選択肢は `aria-disabled` になり、クリック・キーで切り替わらない。
   * 囲む `<fieldset disabled>` の中でも同じく切り替えない（ボタンはネイティブに無効になる）。
   */
  disabled?: boolean;
  /**
   * 選択を確定する操作（任意。例: `FormActionBar` の「選択した N 件を追加」「閉じる」）。一覧のフッターの下に置く
   * （design-system README §4「カード内の操作行」: 区切り線の下に左寄せ、primary → secondary）。
   */
  actions?: ReactNode;
  /** 一覧の高さを 5 / 8 行に固定する（並べた 2 つの一覧の高さをそろえるとき）。既定は候補の数に合わせて 5 / 8 行まで。 */
  fixedHeight?: boolean;
  labels?: Partial<ListPickerLabels>;
  className?: string;
  testId?: string;
}

type Row =
  | { kind: "header"; group: ListPickerGroup; groupIndex: number }
  | {
      kind: "item";
      item: ListPickerItem;
      groupIndex: number;
      /** グループの中の位置（1 始まり。aria-posinset）。 */
      position: number;
      setSize: number;
    };

interface Segment {
  groupIndex: number;
  group: ListPickerGroup | null;
  items: ListPickerItem[];
}

/** 行の高さ（rem）。CSS の class と同じ値。実際の高さは描いた行から測り直す。 */
const ITEM_ROW_REM = 3.5;
const HEADER_ROW_REM = 5.5;
/** 見えている範囲の上下に余分に描く行数。 */
const OVERSCAN_ROWS = 6;
/**
 * これを超える行数のときだけ見えている行に絞って描く（仮想スクロール）。少ないときは全部描き、
 * ブラウザのページ内検索（Ctrl+F）・読み上げの一覧の移動でも全部の候補に届くようにする。
 */
const VIRTUALIZE_ABOVE_ROWS = 100;
const ITEM_ROW_CLASS = "h-[3.5rem]";
const FIXED_VIEWPORT_CLASS = "h-[17.5rem] overflow-auto md:h-[28rem]";

function rootFontSize() {
  if (typeof window === "undefined") return 14;
  const size = Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize);
  return Number.isFinite(size) && size > 0 ? size : 14;
}

/**
 * 大量の候補から複数を選ぶ一覧（#600）。数千〜数万件の候補から、検索して複数を選ぶ画面の標準形。
 * NL2SQL の業務プロファイルの「許可する表・ビュー」の型（検索・件数・チェックの一覧・サーバーの検索と追加読み込み）を共通にした。
 *
 * - **ツールバー**（`ListToolbar`）: 左に検索欄（`SearchField`。入力に合わせてサーバーで絞る）と絞り込み。
 * - **選択の行**（一覧の直上）: 左に一括選択（表示中をすべて選択・選択をすべて解除）、右に「選択中だけ表示（K）」。
 * - **一覧**: 選択肢の listbox（`aria-multiselectable`、`aria-checked`）。グループごとに見出し（一括選択のボタン）と listbox を置く。
 *   Tab で listbox に入り、↑↓ / Home / End / PageUp / PageDown で移動、Space（Enter）で選択を切り替える。
 *   グループの端で ↑↓ を押すと、隣のグループへ移る。
 *   表示は 5 / 8 行の高さで中をスクロールし、読み込んだ候補が 100 行を超えると見えている行だけを描く（仮想スクロール）。
 * - **フッター**: 「N / M 件を表示、選択 K 件」と「さらに読み込む」（読み込みの失敗は再試行付きの Banner）。
 *
 * 少数（数十件まで）から選ぶ場合はこの部品ではなく、検索できる選択欄（combobox）を使う（UX 契約 page-archetypes.md）。
 */
export function ListPicker({
  id,
  label,
  title,
  headingLevel = 3,
  description,
  items,
  groups,
  selectedKeys,
  onToggle,
  onSelectMany,
  onClearSelection,
  selectedItems,
  total: totalProp,
  search,
  filters,
  hasActiveFilter = false,
  loading = false,
  refreshing = false,
  error,
  onRetry,
  hasMore = false,
  loadingMore = false,
  loadMoreError,
  onLoadMore,
  actions,
  disabled = false,
  fixedHeight = false,
  labels: labelOverrides,
  className,
  testId,
}: ListPickerProps) {
  const labels = { ...DEFAULT_LIST_PICKER_LABELS, ...labelOverrides };
  const reactId = useId();
  const baseId = `${id}-${reactId.replace(/:/g, "")}`;
  const titleId = `${baseId}-title`;
  const hintId = `${baseId}-hint`;

  const [showSelectedOnly, setShowSelectedOnly] = useState(false);
  const selectedView = showSelectedOnly && selectedItems !== undefined;
  const searchValue = search?.value ?? "";

  // 表示する候補。「選択中だけ表示」では選択中の候補を、検索語で絞って出す（読み込みの範囲の外の選択も確かめられる）。
  const visibleItems = useMemo(() => {
    if (!selectedView) return items;
    const query = searchValue.trim().toLowerCase();
    const selected = selectedItems ?? [];
    return query ? selected.filter((item) => item.textValue.toLowerCase().includes(query)) : selected;
  }, [items, searchValue, selectedItems, selectedView]);

  const segments = useMemo<Segment[]>(() => buildSegments(visibleItems, groups), [visibleItems, groups]);
  const rows = useMemo<Row[]>(() => {
    const result: Row[] = [];
    segments.forEach((segment, groupIndex) => {
      if (segment.group) result.push({ kind: "header", group: segment.group, groupIndex });
      segment.items.forEach((item, index) =>
        result.push({ kind: "item", item, groupIndex, position: index + 1, setSize: segment.items.length })
      );
    });
    return result;
  }, [segments]);

  // --- 行の高さの測定（行の高さは CSS が決め、描いた行から測る。測るまでは rem からの見積もり） ---
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [metrics, setMetrics] = useState(() => {
    const root = rootFontSize();
    return {
      item: ITEM_ROW_REM * root,
      header: HEADER_ROW_REM * root,
      headers: {} as Record<string, number>,
      viewport: INFORMATION_TABLE_VISIBLE_ROWS.md * ITEM_ROW_REM * root,
    };
  });
  const [scrollTop, setScrollTop] = useState(0);

  const heights = useMemo(
    () =>
      rows.map((row) =>
        row.kind === "item" ? metrics.item : metrics.headers[row.group.key] ?? metrics.header
      ),
    [metrics, rows]
  );
  const offsets = useMemo(() => prefixOffsets(heights), [heights]);
  const totalHeight = offsets[offsets.length - 1] ?? 0;

  const measure = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    setMetrics((current) => {
      let next = current;
      const viewportHeight = viewport.clientHeight;
      if (viewportHeight > 0 && Math.abs(viewportHeight - current.viewport) >= 1) {
        next = { ...next, viewport: viewportHeight };
      }
      const itemRow = viewport.querySelector<HTMLElement>('[data-list-picker-row="item"]');
      const itemHeight = itemRow?.offsetHeight ?? 0;
      if (itemHeight > 0 && Math.abs(itemHeight - current.item) >= 0.5) {
        next = { ...next, item: itemHeight };
      }
      let headers = next.headers;
      viewport.querySelectorAll<HTMLElement>('[data-list-picker-row="header"]').forEach((header) => {
        const key = header.dataset.groupKey ?? "";
        const height = header.offsetHeight;
        if (height > 0 && Math.abs((headers[key] ?? -1) - height) >= 0.5) {
          headers = { ...headers, [key]: height };
        }
      });
      if (headers !== next.headers) next = { ...next, headers };
      return next;
    });
  }, []);

  useLayoutEffect(() => {
    measure();
  });

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === "undefined") return undefined;
    // 幅が変わると見出しの折り返しが変わるので測り直す。
    const observer = new ResizeObserver(() => measure());
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [measure, loading, error]);

  // 条件（検索語・表示の切り替え）が変わったら、一覧の先頭へ戻す。
  const resetKey = `${searchValue}\u0000${selectedView ? "selected" : "all"}`;
  const previousResetKey = useRef(resetKey);
  useEffect(() => {
    if (previousResetKey.current === resetKey) return;
    previousResetKey.current = resetKey;
    setScrollTop(0);
    if (viewportRef.current) viewportRef.current.scrollTop = 0;
  }, [resetKey]);

  // --- キーボードの位置（aria-activedescendant）---
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [focusedGroup, setFocusedGroup] = useState<number | null>(null);
  const [pendingFocusGroup, setPendingFocusGroup] = useState<number | null>(null);
  const listboxRefs = useRef(new Map<number, HTMLDivElement>());

  const itemRowIndex = useMemo(() => {
    const map = new Map<string, number>();
    rows.forEach((row, index) => {
      if (row.kind === "item") map.set(row.item.key, index);
    });
    return map;
  }, [rows]);

  const activeRowIndex = activeKey === null ? -1 : itemRowIndex.get(activeKey) ?? -1;
  const activeRow = activeRowIndex >= 0 ? rows[activeRowIndex] : undefined;

  // 描く行: 見えている範囲 + キーボードの位置の行（スクロールで外れてもフォーカスを失わないよう残す）。
  const range =
    rows.length > VIRTUALIZE_ABOVE_ROWS
      ? visibleRange(offsets, scrollTop, metrics.viewport, OVERSCAN_ROWS * metrics.item)
      : { start: 0, end: rows.length };
  const rendered: number[] = [];
  for (let index = range.start; index < range.end; index += 1) rendered.push(index);
  if (activeRowIndex >= 0 && focusedGroup !== null && (activeRowIndex < range.start || activeRowIndex >= range.end)) {
    rendered.push(activeRowIndex);
    rendered.sort((a, b) => a - b);
  }

  const reveal = useCallback(
    (rowIndex: number) => {
      const viewport = viewportRef.current;
      if (!viewport) return;
      const next = scrollTopToReveal(offsets, rowIndex, viewport.scrollTop, viewport.clientHeight || metrics.viewport);
      if (next !== viewport.scrollTop) {
        viewport.scrollTop = next;
        setScrollTop(next);
      }
    },
    [metrics.viewport, offsets]
  );

  useEffect(() => {
    if (pendingFocusGroup === null) return;
    const node = listboxRefs.current.get(pendingFocusGroup);
    if (node) {
      node.focus({ preventScroll: true });
      setPendingFocusGroup(null);
    }
  });

  const moveTo = (rowIndex: number) => {
    const row = rows[rowIndex];
    if (!row || row.kind !== "item") return;
    setActiveKey(row.item.key);
    reveal(rowIndex);
    if (row.groupIndex !== focusedGroup) {
      setFocusedGroup(row.groupIndex);
      setPendingFocusGroup(row.groupIndex);
    }
  };

  /** 候補の行だけを数えて、`from` から `step` 行（負なら上）動いた先の行。 */
  const stepItem = (from: number, step: number) => {
    let index = from;
    let remaining = Math.abs(step);
    const direction = step < 0 ? -1 : 1;
    let last = from;
    while (remaining > 0) {
      index += direction;
      if (index < 0 || index >= rows.length) break;
      if (rows[index].kind === "item") {
        last = index;
        remaining -= 1;
      }
    }
    return last;
  };

  const isDisabled = () => disabled || Boolean(viewportRef.current?.closest("fieldset:disabled"));
  const toggle = (item: ListPickerItem) => {
    if (item.disabled || isDisabled()) return;
    onToggle(item, !selectedKeys.has(item.key));
  };

  const handleListboxKeyDown = (groupIndex: number) => (event: KeyboardEvent<HTMLDivElement>) => {
    const current =
      activeRow && activeRow.kind === "item" && activeRow.groupIndex === groupIndex ? activeRowIndex : -1;
    const segment = segments[groupIndex];
    if (!segment || segment.items.length === 0) return;
    const firstInGroup = itemRowIndex.get(segment.items[0].key) ?? 0;
    const lastInGroup = itemRowIndex.get(segment.items[segment.items.length - 1].key) ?? firstInGroup;
    const pageRows = Math.max(1, Math.floor(metrics.viewport / metrics.item) - 1);
    let handled = true;
    switch (event.key) {
      case "ArrowDown":
        moveTo(current < 0 ? firstInGroup : stepItem(current, 1));
        break;
      case "ArrowUp":
        moveTo(current < 0 ? firstInGroup : stepItem(current, -1));
        break;
      case "PageDown":
        moveTo(current < 0 ? firstInGroup : stepItem(current, pageRows));
        break;
      case "PageUp":
        moveTo(current < 0 ? firstInGroup : stepItem(current, -pageRows));
        break;
      case "Home":
        moveTo(firstInGroup);
        break;
      case "End":
        moveTo(lastInGroup);
        break;
      case " ":
      case "Enter": {
        if (isImeComposing(event)) {
          handled = false;
          break;
        }
        const row = rows[current >= 0 ? current : firstInGroup];
        if (row?.kind === "item") {
          if (current < 0) setActiveKey(row.item.key);
          toggle(row.item);
        }
        break;
      }
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  };

  const handleListboxFocus = (groupIndex: number) => () => {
    setFocusedGroup(groupIndex);
    if (activeRow && activeRow.kind === "item" && activeRow.groupIndex === groupIndex) return;
    // 位置が無ければ、このグループの見えている最初の候補から始める。
    const firstVisible = rendered.find((index) => {
      const row = rows[index];
      return row.kind === "item" && row.groupIndex === groupIndex && offsets[index] >= scrollTop - 1;
    });
    const fallback = segments[groupIndex]?.items[0];
    const target = firstVisible !== undefined ? rows[firstVisible] : undefined;
    const key = target?.kind === "item" ? target.item.key : fallback?.key;
    if (key !== undefined) setActiveKey(key);
  };

  const handleListboxBlur = (groupIndex: number) => () => {
    setFocusedGroup((current) => (current === groupIndex ? null : current));
  };

  // --- グループの一括操作（非同期のあいだは同じグループのボタンを止める） ---
  const [busyGroup, setBusyGroup] = useState<string | null>(null);
  const runGroupAction = async (group: ListPickerGroup, action?: () => void | Promise<void>) => {
    if (!action) return;
    setBusyGroup(group.key);
    try {
      await action();
    } finally {
      setBusyGroup(null);
    }
  };

  const selectedCount = selectedItems?.length ?? selectedKeys.size;
  const total = totalProp ?? items.length;
  const hasQuery = searchValue.trim() !== "";
  const selectableVisible = items.filter((item) => !item.disabled && !selectedKeys.has(item.key));

  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";

  // 選択の操作の行（一覧の直上）: 左に一括選択、右に「選択中だけ表示」。検索の行とは分け、狭いパネルでも折り返しを少なくする。
  const hasBulk = Boolean(onSelectMany || onClearSelection);
  const selectionBar =
    selectedItems !== undefined || hasBulk ? (
      <div
        className="flex min-w-0 flex-wrap items-center justify-between gap-2"
        data-testid={testId ? `${testId}-selection-bar` : undefined}
      >
        {hasBulk ? (
          <BulkSelectionActions
            selectLabel={labels.selectVisible}
            clearLabel={labels.clearSelection}
            selectDisabled={!onSelectMany || selectedView || selectableVisible.length === 0}
            clearDisabled={!onClearSelection || selectedCount === 0}
            onSelectAll={() => onSelectMany?.(selectableVisible)}
            onClearAll={() => onClearSelection?.()}
            dataTestId={testId ? `${testId}-bulk-actions` : undefined}
          />
        ) : (
          <span />
        )}
        {selectedItems !== undefined ? (
          <ToggleChip
            selected={selectedView}
            onClick={() => setShowSelectedOnly((current) => !current)}
            data-testid={testId ? `${testId}-show-selected` : undefined}
          >
            {labels.showSelected(selectedCount)}
          </ToggleChip>
        ) : null}
      </div>
    ) : null;

  const renderBody = () => {
    if (loading) {
      return (
        <TimedLoadingState
          label={labels.loading}
          operationKey={`${id}-load`}
          framed={false}
          className="content-start"
          testId={testId ? `${testId}-loading` : undefined}
        >
          <ListSkeleton rows={INFORMATION_TABLE_VISIBLE_ROWS} rowClassName={ITEM_ROW_CLASS} />
        </TimedLoadingState>
      );
    }
    if (error) {
      return <ErrorState message={error} onRetry={onRetry} retryLabel={labels.retry} />;
    }
    if (rows.length === 0) {
      if (selectedView) return <EmptyState title={labels.selectedEmpty} />;
      const filtered = hasQuery || hasActiveFilter;
      return (
        <div data-testid={testId ? `${testId}-empty` : undefined}>
          <EmptyState
            title={filtered ? labels.noResultsTitle : labels.emptyTitle}
            hint={filtered ? labels.noResultsHint : labels.emptyHint}
            action={
              hasQuery && search ? (
                <ClearActionButton label={labels.clearSearch} onClick={() => search.onSearch("")} />
              ) : undefined
            }
          />
        </div>
      );
    }

    // 描く行を、グループの見出しとグループの listbox に分ける。listbox は描いた最初の候補から最後の候補までを占める。
    const headerRows: number[] = [];
    const listboxRows = new Map<number, number[]>();
    for (const index of rendered) {
      const row = rows[index];
      if (row.kind === "header") headerRows.push(index);
      else listboxRows.set(row.groupIndex, [...(listboxRows.get(row.groupIndex) ?? []), index]);
    }
    // フォーカスのある listbox は、候補の行が見えていなくても要素を残す（フォーカスを body へ落とさない）。
    return (
      <div className="relative" style={{ height: totalHeight }}>
        {headerRows.map((index) => {
          const row = rows[index] as Extract<Row, { kind: "header" }>;
          return (
            <GroupHeader
              key={`header:${row.group.key}`}
              group={row.group}
              top={offsets[index]}
              busy={busyGroup === row.group.key}
              anyBusy={busyGroup !== null}
              labels={labels}
              testId={testId}
              onSelectAll={() => void runGroupAction(row.group, row.group.onSelectAll)}
              onClearAll={() => void runGroupAction(row.group, row.group.onClearAll)}
            />
          );
        })}
        {[...listboxRows.entries()].map(([groupIndex, indexes]) => {
          const segment = segments[groupIndex];
          const top = offsets[indexes[0]];
          const bottom = offsets[indexes[indexes.length - 1] + 1];
          const listboxName = segment.group ? `${label}: ${segment.group.textValue}` : label;
          const activeInGroup =
            activeRow && activeRow.kind === "item" && activeRow.groupIndex === groupIndex ? activeRowIndex : -1;
          return (
            <div
              key={`listbox:${segment.group?.key ?? ""}`}
              ref={(node) => {
                if (node) listboxRefs.current.set(groupIndex, node);
                else listboxRefs.current.delete(groupIndex);
              }}
              role="listbox"
              aria-multiselectable="true"
              aria-label={listboxName}
              aria-describedby={hintId}
              aria-activedescendant={activeInGroup >= 0 ? `${baseId}-option-${activeInGroup}` : undefined}
              aria-busy={refreshing || undefined}
              aria-disabled={disabled || undefined}
              tabIndex={disabled ? -1 : 0}
              className="group/listbox absolute inset-x-0 focus-visible:-outline-offset-2"
              style={{ top, height: bottom - top }}
              data-list-picker-listbox=""
              onKeyDown={handleListboxKeyDown(groupIndex)}
              onFocus={handleListboxFocus(groupIndex)}
              onBlur={handleListboxBlur(groupIndex)}
            >
              {indexes.map((index) => {
                const row = rows[index] as Extract<Row, { kind: "item" }>;
                return (
                  <PickerOption
                    key={row.item.key}
                    id={`${baseId}-option-${index}`}
                    item={row.item}
                    top={offsets[index] - top}
                    position={row.position}
                    setSize={row.setSize}
                    selected={selectedKeys.has(row.item.key)}
                    disabled={disabled || Boolean(row.item.disabled)}
                    active={index === activeInGroup && focusedGroup === groupIndex}
                    onSelect={() => {
                      setActiveKey(row.item.key);
                      toggle(row.item);
                    }}
                  />
                );
              })}
            </div>
          );
        })}
      </div>
    );
  };

  const footerCount = selectedView
    ? labels.resultCount({ visible: visibleItems.length, total: selectedCount, selected: selectedCount })
    : labels.resultCount({ visible: items.length, total, selected: selectedCount });

  return (
    <section
      className={cn("grid min-w-0 gap-2 rounded-md border border-border bg-surface p-3", className)}
      aria-labelledby={title ? titleId : undefined}
      aria-label={title ? undefined : label}
      data-testid={testId}
      data-list-picker=""
    >
      {title || description ? (
        <div className="grid min-w-0 gap-0.5">
          {title ? (
            <Heading id={titleId} className="text-sm font-semibold text-fg">
              {title}
            </Heading>
          ) : null}
          {description ? <p className="text-xs leading-5 text-fg-muted">{description}</p> : null}
        </div>
      ) : null}
      <ListToolbar
        search={
          search ? (
            <SearchField
              {...search}
              id={search.id ?? `${baseId}-search`}
              clearLabel={search.clearLabel ?? labels.clearSearch}
              resultCountLabel={labels.searchResultCount(total)}
            />
          ) : undefined
        }
        filters={filters}
        testId={testId ? `${testId}-toolbar` : undefined}
      />
      {selectionBar}
      <p id={hintId} className="sr-only">
        {labels.keyboardHint}
      </p>
      <div
        ref={viewportRef}
        className={cn(
          "relative min-w-0 rounded-md border border-border bg-surface overscroll-contain [scrollbar-gutter:stable]",
          fixedHeight ? FIXED_VIEWPORT_CLASS : INFORMATION_LIST_SCROLL_CLASS,
          (loading || error || rows.length === 0) && "p-2"
        )}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
        data-testid={testId ? `${testId}-scroll-region` : undefined}
      >
        {renderBody()}
      </div>
      <LoadMoreFooter
        summary={loading ? "" : footerCount}
        hasMore={!selectedView && hasMore}
        loadingMore={loadingMore}
        loadMoreError={selectedView ? undefined : loadMoreError}
        onLoadMore={onLoadMore}
        loadMoreLabel={labels.loadMore}
        retryLabel={labels.retry}
        refreshing={refreshing && !loading}
        refreshingLabel={labels.refreshing}
        testId={testId ? `${testId}-footer` : undefined}
      />
      {actions}
    </section>
  );
}

function buildSegments(items: readonly ListPickerItem[], groups?: readonly ListPickerGroup[]): Segment[] {
  if (!groups || groups.length === 0) {
    return items.length > 0 ? [{ groupIndex: 0, group: null, items: [...items] }] : [];
  }
  const byGroup = new Map<string, ListPickerItem[]>();
  const ungrouped: ListPickerItem[] = [];
  const known = new Set(groups.map((group) => group.key));
  for (const item of items) {
    if (item.groupKey !== undefined && known.has(item.groupKey)) {
      byGroup.set(item.groupKey, [...(byGroup.get(item.groupKey) ?? []), item]);
    } else {
      ungrouped.push(item);
    }
  }
  const segments: Segment[] = [];
  for (const group of groups) {
    const groupItems = byGroup.get(group.key);
    if (groupItems?.length) segments.push({ groupIndex: segments.length, group, items: groupItems });
  }
  if (ungrouped.length) segments.push({ groupIndex: segments.length, group: null, items: ungrouped });
  return segments;
}

function GroupHeader({
  group,
  top,
  busy,
  anyBusy,
  labels,
  testId,
  onSelectAll,
  onClearAll,
}: {
  group: ListPickerGroup;
  top: number;
  busy: boolean;
  anyBusy: boolean;
  labels: ListPickerLabels;
  testId?: string;
  onSelectAll: () => void;
  onClearAll: () => void;
}) {
  const suffix = group.testId ?? `group-${group.key}`;
  const hasBulk = Boolean(group.onSelectAll || group.onClearAll);
  return (
    <div
      data-surface-tint="neutral"
      className="absolute inset-x-0 grid content-center gap-1.5 border-b border-border bg-surface-hover px-3 py-2"
      style={{ top }}
      data-list-picker-row="header"
      data-group-key={group.key}
      data-testid={testId ? `${testId}-${suffix}` : undefined}
    >
      <div
        className="flex min-w-0 flex-wrap items-center gap-2"
        data-testid={testId ? `${testId}-${suffix}-heading` : undefined}
      >
        <span className="min-w-0 truncate text-sm font-semibold text-fg" title={group.textValue}>
          {group.label}
        </span>
        {group.countLabel ? <span className="text-xs text-fg-muted tnum">{group.countLabel}</span> : null}
      </div>
      {hasBulk ? (
        <BulkSelectionActions
          selectLabel={labels.groupSelectAll}
          clearLabel={labels.groupClearAll}
          selectAriaLabel={labels.groupSelectAllAria(group.textValue)}
          clearAriaLabel={labels.groupClearAllAria(group.textValue)}
          selectDisabled={!group.onSelectAll || Boolean(group.selectAllDisabled) || anyBusy}
          clearDisabled={!group.onClearAll || Boolean(group.clearAllDisabled) || anyBusy}
          busy={busy}
          onSelectAll={onSelectAll}
          onClearAll={onClearAll}
          dataTestId={testId ? `${testId}-${suffix}-bulk-actions` : undefined}
        />
      ) : null}
    </div>
  );
}

function PickerOption({
  id,
  item,
  top,
  position,
  setSize,
  selected,
  disabled,
  active,
  onSelect,
}: {
  id: string;
  item: ListPickerItem;
  top: number;
  position: number;
  setSize: number;
  selected: boolean;
  /** 選べない（候補が選べない、または一覧全体が無効）。 */
  disabled: boolean;
  active: boolean;
  onSelect: () => void;
}) {
  const labelId = `${id}-label`;
  const descriptionId = `${id}-description`;
  const reasonId = `${id}-reason`;
  const metaId = `${id}-meta`;
  const describedBy = [
    item.description ? descriptionId : "",
    item.meta ? metaId : "",
    item.disabled && item.disabledReason ? reasonId : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div
      id={id}
      role="option"
      aria-checked={selected}
      aria-disabled={disabled || undefined}
      aria-posinset={position}
      aria-setsize={setSize}
      aria-labelledby={labelId}
      aria-describedby={describedBy || undefined}
      // 淡青の面の上の補足（description / meta）を 4.5:1 にする（tokens/colors.css。#1671）。
      data-surface-tint={selected && !disabled ? "accent" : undefined}
      className={cn(
        "absolute inset-x-0 flex items-center gap-3 border-b border-border px-3",
        ITEM_ROW_CLASS,
        disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-surface-hover",
        selected && !disabled && "bg-accent-subtle hover:bg-accent-subtle",
        // キーボードの位置。フォーカスは listbox にあり（aria-activedescendant）、キーボードで操作しているときだけ行に枠を描く。
        active &&
          "group-focus-visible/listbox:outline-2 group-focus-visible/listbox:-outline-offset-2 group-focus-visible/listbox:outline-focus-ring"
      )}
      style={{ top }}
      data-list-picker-row="item"
      data-active={active || undefined}
      onClick={onSelect}
    >
      {/* 選択の印は名前の 1 行目の中心にそろえる（補足のある行でも 2 行のまとまりの中央にしない。#2371）。 */}
      <span className="flex min-w-0 flex-1 items-start gap-3">
        <span className="pr-icon-line text-sm leading-5">
          <span
            aria-hidden="true"
            className={cn(
              "flex size-4 shrink-0 items-center justify-center rounded-sm border",
              disabled
                ? "border-border-strong bg-surface-sunken text-fg-muted"
                : selected
                  ? "border-accent-emphasis bg-accent-emphasis text-fg-on-accent"
                  : "border-border-control bg-surface"
            )}
          >
            {selected ? <Check size={14} strokeWidth={3} /> : null}
          </span>
        </span>
        <span className="grid min-w-0 flex-1 gap-0.5">
          <span
            id={labelId}
            className={cn("block truncate text-sm leading-5", disabled ? "text-fg-muted" : "text-fg")}
            title={item.textValue}
          >
            {item.label}
          </span>
          {item.description ? (
            <span
              id={descriptionId}
              className="block truncate text-xs leading-4 text-fg-muted"
              title={typeof item.description === "string" ? item.description : undefined}
            >
              {item.description}
            </span>
          ) : null}
        </span>
      </span>
      {item.meta ? (
        <span id={metaId} className="flex shrink-0 items-center gap-2">
          {item.meta}
        </span>
      ) : null}
      {item.disabled && item.disabledReason ? (
        <span id={reasonId} className="shrink-0 text-xs text-fg-muted">
          {item.disabledReason}
        </span>
      ) : null}
    </div>
  );
}
