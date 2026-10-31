import { Check, ChevronDown, ChevronsDown, ChevronUp, X } from "lucide-react";
import {
  type FocusEvent,
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { isImeComposing } from "../../lib/keyboard";
import { cn } from "../../lib/utils";

import { type FieldWidth, fieldWidthClass } from "./control-size";
import { useFieldWidthScope } from "./field-width-scope";
import { Button } from "./button";
import { FieldLabel } from "./field-label";
import { SearchField } from "./search-field";
import { TEXT_FIELD_TRAILING_BUTTON_CLASS } from "./text-field";
import {
  DEFAULT_SEARCHABLE_SELECT_LABELS,
  filterSearchableOptions,
  moveActiveIndex,
  SearchableOptionRow,
  searchableOptionId,
  type SearchableMultiSelectLabels,
  type SearchableSelectOption,
  type SearchableSelectRemote,
} from "./searchable-options";
import { nearestScrollTop } from "./select-field";
import { SelectListEmpty } from "./select-list-empty";

/**
 * 開いた一覧（パネル）の読みやすい最小の幅（rem）。候補の名前（全角 24 字ほど）と右端の文書数などが 1 行に入り、
 * 下部の「N 件選択中」と操作（表示中をすべて選択・クリア・完了）も 1 行に入る（#2091）。欄の幅（lg 28rem）より広い。
 */
export const SEARCHABLE_PANEL_MIN_WIDTH_REM = 36;

export interface SearchablePanelLayoutInput {
  /** 欄（部品の外枠）の左端と幅（px）。 */
  fieldLeft: number;
  fieldWidth: number;
  /** 読みやすい幅（px）。`softRight` までの右の空きの分だけ使う。 */
  preferred: number;
  /** 下部の行（「N 件選択中」と操作の組）が 1 行に入る幅（px）。`softRight` までに入らなければ、`boundLeft`〜`boundRight` の中で右へ広げ、左へずらしてでも確保する。 */
  required: number;
  /** パネルを置いてよい左右の端（画面・スクロールの箱の余白の内側、横に並ぶ要素の手前。px）。 */
  boundLeft: number;
  boundRight: number;
  /** 読みやすい幅に広げるときの右の端（いちばん近い枠のある箱（Card など）の内容の端。px）。省くと `boundRight`。 */
  softRight?: number;
}

/**
 * 開いた一覧の幅と左へのずらし（#2091）。欄の幅より狭くはしない。
 *
 * 1. 欄の左端から右へ、いちばん近い枠のある箱（Card など）の内容の端までの中で、読みやすい幅（`preferred`）まで広げる。
 * 2. それでも下部の行が 1 行に入らない（`required` に足りない）ときだけ、置いてよい範囲（画面の余白・横の要素の手前）まで
 *    右へ広げ、足りなければ左へずらす（375px では画面の幅に合わせる）。範囲が足りなければ範囲いっぱい（下部は 2 段になる）。
 *
 * 測れない（幅 0 の環境）ときは null（欄の幅のまま）。
 */
export function computeSearchablePanelLayout({
  fieldLeft,
  fieldWidth,
  preferred,
  required,
  boundLeft,
  boundRight,
  softRight = boundRight,
}: SearchablePanelLayoutInput): { width: number; offsetLeft: number } | null {
  if (!(fieldWidth > 0) || !(boundRight > boundLeft)) return null;
  const softSpace = Math.min(softRight, boundRight) - fieldLeft;
  const toRight = Math.max(fieldWidth, Math.min(Math.max(preferred, required), softSpace));
  if (toRight >= required) return { width: Math.ceil(toRight), offsetLeft: 0 };
  const width = Math.max(fieldWidth, Math.min(required, boundRight - boundLeft));
  const rightSpace = boundRight - fieldLeft;
  const offsetLeft = width > rightSpace ? Math.min(width - rightSpace, Math.max(0, fieldLeft - boundLeft)) : 0;
  // 幅は切り上げる（切り捨てると 1 行に入る幅にわずかに足りず折り返す）。
  return { width: Math.ceil(width), offsetLeft: Math.round(offsetLeft) };
}

/**
 * パネルを置いてよい左右の端（px）を測る。画面とスクロールする祖先（overflow が visible でない箱）の内側から
 * `gutter` を引き、部品とその祖先の横に並ぶ要素（パネルと縦に重なるもの。grid の隣の欄・サイドナビなど）の手前までにする。
 * パネルは通常の流れの中にあるので、隣の欄に重ねない。読みやすい幅に広げるときは、いちばん近い枠のある箱（Card など）の
 * 内容の端（`softRight`）で止める（カードからはみ出さない。#1794）。
 */
function measurePanelBounds(root: HTMLElement, panel: HTMLElement, gutter: number) {
  const fieldRect = root.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  let left = 0;
  let right = document.documentElement.clientWidth;
  let softRight = Number.POSITIVE_INFINITY;
  for (let el = root.parentElement; el && el !== document.body; el = el.parentElement) {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (!Number.isFinite(softRight) && (Number.parseFloat(style.borderRightWidth) || 0) > 0) {
      softRight =
        rect.right - (Number.parseFloat(style.borderRightWidth) || 0) - (Number.parseFloat(style.paddingRight) || 0);
    }
    if (style.overflowX === "visible") continue;
    left = Math.max(left, rect.left + el.clientLeft);
    right = Math.min(right, rect.left + el.clientLeft + el.clientWidth);
  }
  left += gutter;
  right -= gutter;
  for (let node: HTMLElement = root; node.parentElement && node.parentElement !== document.body; node = node.parentElement) {
    const parent = node.parentElement;
    const gap = Number.parseFloat(getComputedStyle(parent).columnGap) || 0;
    for (const sibling of Array.from(parent.children)) {
      if (sibling === node || !(sibling instanceof HTMLElement)) continue;
      const rect = sibling.getBoundingClientRect();
      if (rect.width <= 1 || rect.height <= 1) continue;
      if (rect.bottom <= panelRect.top || rect.top >= panelRect.bottom) continue;
      const beside = rect.left >= fieldRect.right - 1 ? "right" : rect.right <= fieldRect.left + 1 ? "left" : null;
      if (!beside) continue;
      const position = getComputedStyle(sibling).position;
      if (position === "fixed" || position === "absolute") continue;
      if (beside === "right") right = Math.min(right, rect.left - gap);
      else left = Math.max(left, rect.right + gap);
    }
  }
  return {
    fieldLeft: fieldRect.left,
    fieldWidth: fieldRect.width,
    boundLeft: left,
    boundRight: right,
    softRight: Math.min(softRight, right),
  };
}

/** 下部の行（件数の文と操作の組）を 1 行に並べるのに要る幅（px。パネルの枠線と左右の余白を含む）。 */
function measureFooterWidth(footer: HTMLElement, panel: HTMLElement) {
  const style = getComputedStyle(footer);
  const columnGap = Number.parseFloat(style.columnGap) || 0;
  let width =
    (Number.parseFloat(style.paddingLeft) || 0) +
    (Number.parseFloat(style.paddingRight) || 0) +
    panel.offsetWidth -
    panel.clientWidth;
  const parts = Array.from(footer.children).filter((child): child is HTMLElement => child instanceof HTMLElement);
  parts.forEach((part, index) => {
    if (index > 0) width += columnGap;
    if (part.dataset.searchablePanelActions === undefined) {
      width += part.scrollWidth;
      return;
    }
    // 操作の組は折り返せる flex なので、中のボタンの幅と間隔の和（1 行に並べた幅）で数える。
    const gap = Number.parseFloat(getComputedStyle(part).columnGap) || 0;
    const buttons = Array.from(part.children).filter((child): child is HTMLElement => child instanceof HTMLElement);
    width += buttons.reduce((sum, button) => sum + button.offsetWidth, 0) + gap * Math.max(0, buttons.length - 1);
  });
  return Math.ceil(width);
}

export interface SearchableMultiSelectProps {
  /** 検索欄（role=combobox）の id。 */
  id: string;
  /** 翻訳済みラベル。検索欄の名前・候補の一覧の名前・選択済みの一覧の名前に使う。 */
  label: string;
  /** ラベルを画面に出さず読み上げだけにする（見出しなどで目的が分かる場所だけ）。 */
  labelHidden?: boolean;
  /**
   * 画面に出している見出し（カードの題名の FieldLabel など）の id。`labelHidden` と一緒に渡すと、
   * 検索欄の名前をその見出しにする（同じ名前を二重に読み上げない）。
   */
  labelledBy?: string;
  /** 翻訳済みの補足（任意）。 */
  helper?: ReactNode;
  /** 翻訳済みのエラー（任意）。 */
  error?: string;
  /** 1 件以上の選択が必須。ラベルに「必須」、検索欄に aria-required（検証はアプリ側）。 */
  required?: boolean;
  requiredLabel?: string;
  /** 外に出しているエラー・補足と結ぶ（検索欄の aria-describedby に足す）。 */
  describedBy?: string;
  /** 外に出しているエラー中（検索欄を aria-invalid にする）。`error` を渡すときは不要。 */
  invalid?: boolean;
  /** 候補（画面側で絞り込むときは全件。サーバー側で検索するときは返った分）。 */
  options: readonly SearchableSelectOption[];
  /** 選択中の値（選んだ順）。 */
  value: readonly string[];
  onValueChange: (value: string[]) => void;
  /**
   * chip に出す選択済みの候補（任意）。`options`（検索結果のページ）に無い選択済みも名前と状態で出すために渡す。
   * どちらにも無い値は値そのものを名前として出す（外せるように）。
   */
  selectedOptions?: readonly SearchableSelectOption[];
  /** 検索語が確定したとき（SearchField と同じ: 300ms・Enter・消去。IME の変換中は呼ばない）。 */
  onQueryChange?: (query: string) => void;
  /** サーバー側で検索するとき（#578）。渡さなければ `options` を画面側で絞り込む。 */
  remote?: SearchableSelectRemote;
  disabled?: boolean;
  labels?: Partial<SearchableMultiSelectLabels>;
  /**
   * 幅（既定は親の幅いっぱい）。対象の選択（ナレッジベース・プロファイルなど）は `lg`（#1891）。
   * 検索欄・選択済みの chip がこの幅に収まる。sm（640px）未満は全幅。開いた候補の一覧はこの幅以上で、
   * 下部の行が 1 行に入る幅・読みやすい幅（36rem）まで広がる（#2091）。
   */
  width?: FieldWidth;
  className?: string;
}

/**
 * 検索できる複数選択（#578）。「検索欄（combobox）＋ 候補の一覧（listbox、開いている間だけ）＋ 選択済みの chip」。
 *
 * - 検索欄は SearchField（debounce 300ms・Enter はすぐ・IME の変換中は絞り込まない・消去）。
 * - ↑↓ で候補を強調（aria-activedescendant）、Enter で選択を切り替える（IME の変換中の Enter・矢印は候補の操作に使わない）。
 *   Esc で一覧を閉じ、もう一度の Esc で検索語を消す。フォーカスが部品の外へ出たら閉じる。
 * - 選んでも一覧は閉じない（続けて選べる）。「完了」で閉じて検索欄へ戻る。
 * - 候補が多いとき（数百件）は `remote` でサーバー側の検索に切り替え、「さらに表示」で続きを読む。
 * - 選択済みは chip で、名前を切らずに出す。chip の「×」で外す。
 */
export function SearchableMultiSelect({
  id,
  label,
  labelHidden = false,
  labelledBy,
  helper,
  error,
  required = false,
  requiredLabel,
  describedBy,
  invalid = false,
  options,
  value,
  onValueChange,
  selectedOptions,
  onQueryChange,
  remote,
  disabled = false,
  labels: labelOverrides,
  width,
  className,
}: SearchableMultiSelectProps) {
  const widthScope = useFieldWidthScope();
  const labels = { ...DEFAULT_SEARCHABLE_SELECT_LABELS, ...labelOverrides };
  const reactId = useId();
  const listboxId = `${id}-${reactId}-listbox`;
  const labelId = `${id}-${reactId}-label`;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listboxRef = useRef<HTMLUListElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const footerRef = useRef<HTMLDivElement | null>(null);
  // 閉じて検索欄へフォーカスを戻すとき、検索欄の onFocus で開き直さないための印。
  const suppressOpenRef = useRef(false);
  // キーボードで動かしたときだけ強調中の候補へスクロールする（マウスの移動では動かさない）。
  const revealActiveRef = useRef(false);

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [hideHideable, setHideHideable] = useState(true);
  const [activeIndex, setActiveIndex] = useState(0);
  const [panelLayout, setPanelLayout] = useState<{ width: number; offsetLeft: number } | null>(null);

  const selected = useMemo(() => new Set(value), [value]);
  const matched = useMemo(
    () => (remote ? options : filterSearchableOptions(options, query)),
    [options, query, remote]
  );
  const canHide =
    Boolean(labels.hideHideable) &&
    !remote &&
    options.some((option) => option.hideable) &&
    options.some((option) => !option.hideable);
  const hiding = canHide && hideHideable;
  const visible = useMemo(
    () => (hiding ? matched.filter((option) => !option.hideable || selected.has(option.value)) : matched),
    [hiding, matched, selected]
  );
  const hiddenCount = hiding ? matched.length - visible.length : 0;
  // 候補が減ったときも強調を範囲に収める（state を同期させず、描画のたびに求める）。
  const active = visible.length === 0 ? -1 : Math.min(activeIndex, visible.length - 1);

  const chips = value.map(
    (current) =>
      selectedOptions?.find((option) => option.value === current) ??
      options.find((option) => option.value === current) ?? { value: current, label: current }
  );

  const countText = remote
    ? labels.count(options.length, remote.total)
    : labels.count(visible.length, options.length);
  const searching = Boolean(remote?.searching);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  useLayoutEffect(() => {
    const listbox = listboxRef.current;
    if (!open || !listbox || active < 0 || !revealActiveRef.current) return;
    const item = listbox.children.item(active);
    if (!(item instanceof HTMLElement)) return;
    listbox.scrollTop = nearestScrollTop({
      scrollTop: listbox.scrollTop,
      viewportHeight: listbox.clientHeight,
      itemTop: item.offsetTop,
      itemHeight: item.offsetHeight,
    });
  }, [open, active]);

  // 開いた一覧の幅（#2091）。欄の幅に縛らず、下部の行が 1 行に入る幅（と読みやすい最小の幅）まで広げ、
  // 画面・スクロールの箱・隣の欄を超えない。描画の前に測る（広がる前の形を見せない）。
  const footerText =
    hiddenCount > 0 && labels.hiddenCount ? labels.hiddenCount(hiddenCount) : labels.selectedCount(value.length);
  useLayoutEffect(() => {
    if (!open) {
      setPanelLayout(null);
      return undefined;
    }
    const rootEl = rootRef.current;
    const panel = panelRef.current;
    const footer = footerRef.current;
    if (!rootEl || !panel || !footer) return undefined;
    const measure = () => {
      const rem = Number.parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      const required = measureFooterWidth(footer, panel);
      const next = computeSearchablePanelLayout({
        ...measurePanelBounds(rootEl, panel, rem),
        preferred: SEARCHABLE_PANEL_MIN_WIDTH_REM * rem,
        required,
      });
      setPanelLayout((current) =>
        current?.width === next?.width && current?.offsetLeft === next?.offsetLeft ? current : next
      );
    };
    measure();
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(rootEl);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [open, footerText]);

  function openList() {
    if (disabled) return;
    setOpen(true);
  }

  /** 一覧を閉じ、検索欄へフォーカスを戻す（完了・Esc・開閉ボタン）。 */
  function closeAndFocusInput() {
    setOpen(false);
    suppressOpenRef.current = true;
    try {
      inputRef.current?.focus();
    } finally {
      suppressOpenRef.current = false;
    }
  }

  function toggle(optionValue: string) {
    if (disabled) return;
    onValueChange(
      selected.has(optionValue)
        ? value.filter((current) => current !== optionValue)
        : [...value, optionValue]
    );
  }

  function selectAllVisible() {
    const next = [...value];
    for (const option of visible) if (!selected.has(option.value)) next.push(option.value);
    onValueChange(next);
  }

  function moveActive(delta: number) {
    revealActiveRef.current = true;
    const next = moveActiveIndex(active, delta, visible.length);
    setActiveIndex(next < 0 ? 0 : next);
    // 最後の候補から ↓ で、続きのページを読む（キーボードでも「さらに表示」まで行かずに続けられる）。
    if (delta > 0 && active === visible.length - 1 && remote?.hasMore && !remote.loadingMore) {
      remote.onLoadMore();
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    // IME の変換中の Enter・矢印は変換の確定・候補の移動なので、一覧の操作に使わない（#535）。
    if (disabled || isImeComposing(event)) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) {
          openList();
          return;
        }
        moveActive(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        if (open) moveActive(-1);
        return;
      case "Enter": {
        if (!open || active < 0 || searching) return;
        const option = visible[active];
        if (!option) return;
        // SearchField の Enter（検索語の確定）とフォームの送信をさせない。
        event.preventDefault();
        toggle(option.value);
        return;
      }
      case "Escape":
        if (!open) return; // 閉じているときは SearchField が検索語を消す
        event.preventDefault();
        // 囲むダイアログまで Esc を伝えない（一覧だけを閉じる）。
        event.stopPropagation();
        setOpen(false);
        return;
      default:
        return;
    }
  }

  // 一覧の中の操作（さらに表示・完了など）にフォーカスがあるときも Esc で閉じて検索欄へ戻る。
  function handleRootKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Escape" || !open || event.defaultPrevented) return;
    if (event.target === inputRef.current) return;
    event.preventDefault();
    event.stopPropagation();
    closeAndFocusInput();
  }

  // Tab / Shift+Tab でフォーカスが部品の外へ移ったら閉じる。移り先が無い（ウィンドウの切り替え）ときは閉じない。
  function handleRootBlur(event: FocusEvent<HTMLDivElement>) {
    if (!open) return;
    const next = event.relatedTarget;
    if (next instanceof Node && !rootRef.current?.contains(next)) setOpen(false);
  }

  const toggleButton = (
    <Button
      type="button"
      variant="ghost"
      iconOnly
      icon={open ? ChevronUp : ChevronDown}
      aria-label={labels.toggleList}
      aria-expanded={open}
      aria-controls={listboxId}
      disabled={disabled}
      // 押しても検索欄からフォーカスを外さない。
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => {
        if (open) {
          closeAndFocusInput();
        } else {
          setOpen(true);
          inputRef.current?.focus();
        }
      }}
      // 検索欄と同じ高さ（md 36px・タッチ端末 44px。TextField の後置スロットの形）。消去のボタンが右に
      // 並ぶ（検索語がある）ときは右の角を丸めず、強制カラーモードで消去の左の区切りと二重にしない。
      className={cn(
        TEXT_FIELD_TRAILING_BUTTON_CLASS,
        draft && "rounded-r-none forced-colors:border-r-[Canvas]"
      )}
    />
  );

  return (
    <div
      ref={rootRef}
      // 親が grid / flex でも親の幅に収める（候補の説明は 1 行で省略するため、min-content が説明の全文の幅になる。#1794）。
      className={cn("min-w-0 space-y-2", fieldWidthClass(width, widthScope), className)}
      onKeyDown={handleRootKeyDown}
      onBlur={handleRootBlur}
      data-searchable-multi-select=""
    >
      {/* 必須のタグを出すため、見えるラベルは FieldLabel で出す（SearchField は必須を持たない）。 */}
      {labelHidden ? null : (
        <FieldLabel
          id={labelId}
          htmlFor={id}
          label={label}
          required={required}
          requiredLabel={requiredLabel}
          className="block"
        />
      )}
      <SearchField
        ref={inputRef}
        id={id}
        label={label}
        labelHidden
        aria-labelledby={labelHidden ? labelledBy : labelId}
        aria-required={required || undefined}
        helper={helper}
        error={error}
        value={query}
        onSearch={(next) => {
          setQuery(next);
          setActiveIndex(0);
          onQueryChange?.(next);
        }}
        onDraftChange={(next) => {
          setDraft(next);
          if (next) openList();
        }}
        clearLabel={labels.clearSearch}
        resultCountLabel={searching ? labels.searching : countText}
        placeholder={labels.searchPlaceholder}
        disabled={disabled}
        trailing={toggleButton}
        role="combobox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-activedescendant={open && active >= 0 ? searchableOptionId(listboxId, active) : undefined}
        aria-describedby={describedBy}
        aria-invalid={Boolean(error) || invalid || undefined}
        onFocus={() => {
          if (!suppressOpenRef.current) openList();
        }}
        onKeyDown={handleKeyDown}
      />

      {open ? (
        <div
          ref={panelRef}
          // 幅は欄の幅以上（上の useLayoutEffect）。欄より広いとき、通常の流れのまま右へはみ出し、右に入らなければ左へずらす。
          className="overflow-hidden rounded-md border border-border bg-surface shadow-sm"
          style={panelLayout ? { width: panelLayout.width, marginLeft: -panelLayout.offsetLeft } : undefined}
          data-searchable-panel=""
        >
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-border px-3 py-1.5">
            <span className="tnum text-xs text-fg-muted">{searching ? labels.searching : countText}</span>
            {canHide && labels.hideHideable ? (
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-fg-muted">
                <input
                  type="checkbox"
                  checked={hideHideable}
                  onChange={(event) => setHideHideable(event.target.checked)}
                  className="cursor-pointer accent-accent-emphasis"
                />
                {labels.hideHideable}
              </label>
            ) : null}
          </div>

          {visible.length > 0 ? (
            <ul
              ref={listboxRef}
              id={listboxId}
              role="listbox"
              aria-label={label}
              aria-multiselectable
              aria-busy={searching || undefined}
              className="bounded-scroll-area py-1"
            >
              {visible.map((option, index) => (
                <SearchableOptionRow
                  key={option.value}
                  id={searchableOptionId(listboxId, index)}
                  option={option}
                  selected={selected.has(option.value)}
                  active={index === active}
                  multiple
                  onSelect={() => {
                    toggle(option.value);
                    setActiveIndex(index);
                  }}
                  onHover={() => {
                    revealActiveRef.current = false;
                    setActiveIndex(index);
                  }}
                />
              ))}
            </ul>
          ) : (
            <SelectListEmpty searching={searching} searchingLabel={labels.searching}>
              {query ? labels.noMatch(query) : labels.empty}
            </SelectListEmpty>
          )}

          {remote?.hasMore ? (
            <div className="border-t border-border px-3 py-1.5">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                icon={ChevronsDown}
                loading={remote.loadingMore}
                disabled={disabled}
                onClick={remote.onLoadMore}
                className="w-full"
              >
                {labels.loadMore}
              </Button>
            </div>
          ) : null}

          {/* 下部の行（#2091）: 左に「N 件選択中」（折り返さない）、右に操作の組（表示中をすべて選択・クリア・完了）を 1 行。
              幅が足りない（375px など）ときは件数の文と操作の組を 2 段にし、操作の組は分けない。 */}
          <div
            ref={footerRef}
            className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-t border-border px-3 py-1.5"
            data-searchable-panel-footer=""
          >
            <span className="tnum shrink-0 whitespace-nowrap text-xs text-fg-muted">{footerText}</span>
            <span
              className="ml-auto flex flex-wrap items-center justify-end gap-1"
              data-searchable-panel-actions=""
            >
              <Button
                type="button"
                size="sm"
                variant="ghost"
                // 検索語を変えている間は古い結果が出ているため、まとめて選ばせない。
                disabled={disabled || visible.length === 0 || searching}
                onClick={selectAllVisible}
                className="shrink-0"
              >
                {labels.selectAllVisible}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={disabled || value.length === 0}
                onClick={() => onValueChange([])}
                className="shrink-0"
              >
                {labels.clear}
              </Button>
              {/* 選択は選ぶたびに反映済み。「完了」は一覧を閉じて検索欄へ戻る、目に見える閉じ方。 */}
              <Button
                type="button"
                size="sm"
                variant="secondary"
                icon={Check}
                onClick={closeAndFocusInput}
                className="shrink-0"
              >
                {labels.done}
              </Button>
            </span>
          </div>
        </div>
      ) : null}

      {chips.length > 0 ? (
        <ul
          aria-label={labels.selectedList(label)}
          className="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto [scrollbar-gutter:stable]"
          data-searchable-chips=""
        >
          {chips.map((option) => (
            <li
              key={option.value}
              data-surface-tint="info"
              className="inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-md border border-accent-emphasis bg-info-subtle py-0.5 pl-2 pr-0.5 text-xs font-medium text-fg"
            >
              {/* 名前は切らずに折り返す（選択の完全な表示）。 */}
              <span className="min-w-0 [overflow-wrap:anywhere]">{option.label}</span>
              {option.badge ? (
                <span className="shrink-0 rounded-sm bg-surface-sunken px-1 py-0.5 text-xs font-medium text-accent-fg">
                  {option.badge}
                </span>
              ) : null}
              <button
                type="button"
                onClick={() => toggle(option.value)}
                disabled={disabled}
                aria-label={labels.removeChip(option.label)}
                className="pr-touch-target relative flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-sm text-fg-muted transition-colors hover:bg-surface-hover hover:text-fg disabled:cursor-not-allowed focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring"
              >
                <X size={14} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
