import { Check, ChevronDown } from "lucide-react";
import {
  type KeyboardEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "../../lib/utils";
import { StatusBadge } from "../data/status-badge";

import { useFieldWidthScope } from "./field-width-scope";
import { CONTROL_HEIGHT_CLASS, type ControlSize, type FieldWidth, labeledFieldWidthClasses } from "./control-size";
import { FieldError } from "./field-error";
import { useFloatingMenuPosition } from "./floating-menu";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "./required-badge";

export interface SelectFieldOption<T extends string = string> {
  value: T;
  label: string;
  description?: string;
  /**
   * 選べない選択肢（#3374。準備中の機能など）。一覧には灰色で出し、キー操作で強調して読み上げられるが、
   * クリック・Enter・Space では選ばれない（`aria-disabled="true"`。W3C APG の listbox の無効の選択肢）。
   * 隠さずに出し、選べない理由を `disabledReason` で伝える。
   */
  disabled?: boolean;
  /**
   * 選べない理由（翻訳済み）。`disabled` のときだけ、選択肢の下の行に出す（`Tabs` の `disabledReason` と同じ考え方。
   * 一覧の中では Tooltip を出せないため、文字で常に見せる）。
   */
  disabledReason?: string;
  /** ラベルの後ろに出す短い印（翻訳済み。例: `DEFAULT_ROADMAP_LABEL`）。中立色のバッジで出す。 */
  badge?: string;
}

interface SelectFieldProps<T extends string> {
  id: string;
  label: string;
  value: T;
  options: readonly SelectFieldOption<T>[];
  /** 展開した選択肢の長いラベルを省略せず折り返す。既定は単一行。 */
  wrapOptions?: boolean;
  onValueChange: (value: T) => void;
  helper?: string;
  error?: string;
  /** 必須であることを aria-required と中立色の RequiredBadge「必須」で伝える（検証はアプリ側）。 */
  required?: boolean;
  /** 必須バッジの文言。既定「必須」。条件付きの必須だけ上書きする。 */
  requiredLabel?: string;
  /** 未選択（値が空）のときのボタンの文言。 */
  placeholder?: string;
  /**
   * 任意の欄で未選択へ戻す選択肢のラベル（翻訳済み。例:「未選択」。#647）。渡すと一覧の先頭に空の値（`""`）の
   * 選択肢を出し、選ぶと `onValueChange("")` を呼ぶ（ネイティブの select の `<option value="">` と同じ）。
   * 未選択のあいだ、ボタンは `placeholder`（無ければこのラベル）を出す。`required` の欄では出さない
   * （必須の欄を未選択へ戻す操作は要らない）。`options` に空の値が既にあるときも足さない。
   * 値の型 `T` が `""` を含むときだけ渡せる。
   */
  emptyOptionLabel?: "" extends T ? string : never;
  /**
   * 欄の外にある説明の要素の id（空白区切りで複数可。#647）。`helper`・`error` の id と合わせて
   * ボタンの `aria-describedby` に渡す（TextField の `aria-describedby` と同じ）。
   */
  describedBy?: string;
  /**
   * 無効にする（#631）。ボタンはネイティブの disabled（Tab で止まらない・押せない・typeahead も効かない）で、
   * 地と文字は TextField の disabled と同じ --color-surface-disabled / --color-fg-disabled。開いているときに無効になったら閉じる。
   */
  disabled?: boolean;
  /**
   * ラベルを画面に出さず読み上げだけにする（`sr-only`）。表の行・一覧のツールバーのように、周り（列の見出し・隣の文言）で
   * 目的が分かる場所だけに使う。フォームの欄では使わない（TextField の labelHidden と同じ）。
   */
  labelHidden?: boolean;
  /** ボタン（combobox）に付ける `data-testid`（e2e 用）。 */
  "data-testid"?: string;
  /** 高さ（既定 md = 36px）。同じ行に並べる Button・入力欄と同じ size にする（#613）。 */
  size?: ControlSize;
  /**
   * 幅（選択肢のラベルの長さで選ぶ。既定は親の幅いっぱい）。sm（640px）未満は全幅。
   * フォームの grid のセルに置くときは指定しない。grid の外に単独で置く選択欄は必ず指定する（#613）。
   */
  width?: FieldWidth;
  className?: string;
  buttonClassName?: string;
  /**
   * ラベルの置き方（#1933）。`top`（既定）は欄の上、`inline` はボタンの中の先頭に「種類: すべて」の形で出す
   * （一覧のツールバーの絞り込み。`FilterSelect` が使う）。`inline` でも読み上げ名はラベル（`aria-labelledby`）で、
   * ボタンの中のラベルの文字は読み上げない（名前と値を二重に読ませない）。
   */
  labelPlacement?: "top" | "inline";
  /**
   * 絞り込みが掛かっている（既定の「すべて」以外を選んでいる。#1933）。`inline` のときだけ効き、値を太字・
   * アクセントの文字にし、枠をアクセントの線にする。状態は値の文字でも分かる（色だけで伝えない）。
   */
  active?: boolean;
}

/** 入力が途切れてからこの時間で typeahead の入力をリセットする（W3C APG の select-only combobox と同じ 500ms）。 */
export const SELECT_TYPEAHEAD_RESET_MS = 500;
/** 空きが十分でも超えない一覧の高さ（rem）。`max-h-64` と同じ。 */
const LISTBOX_MAX_HEIGHT_REM = 16;
/** PageUp / PageDown で動かす件数。 */
const PAGE_STEP = 10;

function normalizeForTypeahead(text: string) {
  return text.normalize("NFKC").trimStart().toLocaleLowerCase();
}

/**
 * typeahead で強調する選択肢の位置。W3C APG の select-only combobox に合わせる。
 * - `startIndex` から末尾、先頭から `startIndex` の手前の順に、`query` で始まる最初の選択肢を返す
 * - 同じ文字の繰り返し（"aaa"）は、その文字で始まる選択肢を順に巡る
 * - 見つからなければ -1
 */
export function findTypeaheadIndex(labels: readonly string[], query: string, startIndex: number) {
  const count = labels.length;
  const normalizedQuery = normalizeForTypeahead(query);
  if (count === 0 || !normalizedQuery) return -1;
  const start = ((startIndex % count) + count) % count;
  const ordered = Array.from({ length: count }, (_, offset) => (start + offset) % count);
  const normalizedLabels = labels.map(normalizeForTypeahead);
  const match = ordered.find((index) => normalizedLabels[index].startsWith(normalizedQuery));
  if (match !== undefined) return match;
  const chars = Array.from(normalizedQuery);
  if (chars.every((char) => char === chars[0])) {
    return ordered.find((index) => normalizedLabels[index].startsWith(chars[0])) ?? -1;
  }
  return -1;
}

/**
 * typeahead で探し始める位置。1 文字目と同じ文字の繰り返しは次の選択肢から（同じ文字で巡る）、
 * 続けて入力した文字は今の選択肢から（前方一致が続くあいだは動かさない）。
 */
export function typeaheadStartIndex(query: string, activeIndex: number) {
  if (activeIndex < 0) return 0;
  const chars = Array.from(query);
  return chars.every((char) => char === chars[0]) ? activeIndex + 1 : activeIndex;
}

/**
 * 強調中の選択肢を一覧の中で見せるための scrollTop（`scrollIntoView({ block: "nearest" })` と同じ動き）。
 * `scrollIntoView` は一覧の外（ページ本体）までスクロールさせるため、一覧の scrollTop だけを動かす。
 */
export function nearestScrollTop({
  scrollTop,
  viewportHeight,
  itemTop,
  itemHeight,
  padding = 0,
}: {
  scrollTop: number;
  viewportHeight: number;
  itemTop: number;
  itemHeight: number;
  padding?: number;
}) {
  const top = itemTop - padding;
  const bottom = itemTop + itemHeight + padding;
  if (top < scrollTop) return Math.max(0, top);
  if (bottom > scrollTop + viewportHeight) return bottom - viewportHeight;
  return scrollTop;
}

/** 外側クリックの判定。フィールド本体と、Portal で body に出した一覧の両方を内側とみなす。 */
export function isInsideAny(
  target: Node | null,
  containers: readonly (Pick<Node, "contains"> | null | undefined)[]
) {
  if (!target) return false;
  return containers.some((container) => container?.contains(target) ?? false);
}

/**
 * 一覧を描く先。モーダル（`aria-modal="true"` / `<dialog>`）の中なら、そのモーダルの中に描く。
 * モーダルの外に出すと、支援技術がモーダルの外として読まない（aria-modal）・top layer の下に隠れる（`<dialog>`）ため。
 */
export function selectPortalContainer(trigger: Pick<Element, "closest"> | null): HTMLElement | null {
  const modal = trigger?.closest<HTMLElement>('[aria-modal="true"], dialog[open]');
  if (modal) return modal;
  return typeof document === "undefined" ? null : document.body;
}

/**
 * 見た目と操作を統一した選択フィールド（W3C APG の select-only combobox）。
 * 一覧は body へ Portal で描き、親の overflow に切られない。画面の下端では上に反転する。
 */
export function SelectField<T extends string>({
  id,
  label,
  value,
  options,
  onValueChange,
  helper,
  error,
  required,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  placeholder = "",
  emptyOptionLabel,
  describedBy: externalDescribedBy,
  disabled = false,
  labelHidden = false,
  wrapOptions = false,
  "data-testid": testId,
  size = "md",
  width,
  className,
  buttonClassName,
  labelPlacement = "top",
  active = false,
}: SelectFieldProps<T>) {
  const widthScope = useFieldWidthScope();
  const inlineLabel = labelPlacement === "inline";
  const reactId = useId();
  const listboxId = `${id}-${reactId}-listbox`;
  const labelId = `${id}-${reactId}-label`;
  const hintId = `${id}-${reactId}-hint`;
  const errorId = `${id}-${reactId}-error`;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const listboxRef = useRef<HTMLUListElement | null>(null);
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const [portalContainer, setPortalContainer] = useState<HTMLElement | null>(null);
  // キーボード・typeahead・開いた直後だけ強調中の選択肢へスクロールする（マウスの移動では動かさない）。
  const revealHighlightRef = useRef(false);
  const typeaheadRef = useRef<{ query: string; timer: ReturnType<typeof setTimeout> | undefined }>({
    query: "",
    timer: undefined,
  });
  const position = useFloatingMenuPosition({
    align: "stretch",
    boundary: "viewport",
    maxHeightRem: LISTBOX_MAX_HEIGHT_REM,
    menuRef: listboxRef,
    open,
    triggerRef: buttonRef,
  });

  // 任意の欄の「未選択」の選択肢（#647）。一覧の先頭に置き、キー操作・typeahead も他の選択肢と同じに扱う。
  const hasEmptyOption =
    Boolean(emptyOptionLabel) && !required && !options.some((option) => option.value === "");
  const allOptions = useMemo<readonly SelectFieldOption<T>[]>(
    () =>
      hasEmptyOption ? [{ value: "" as T, label: emptyOptionLabel as string }, ...options] : options,
    [hasEmptyOption, emptyOptionLabel, options]
  );
  const selectedIndex = useMemo(
    () => allOptions.findIndex((option) => option.value === value),
    [allOptions, value]
  );
  // 「未選択」の選択肢を選んでいるときは、ボタンには placeholder を出す（選択肢のラベルではない）。
  const selectedOption =
    selectedIndex >= 0 && !(hasEmptyOption && selectedIndex === 0) ? allOptions[selectedIndex] : null;
  const activeIndex = highlightedIndex >= 0 ? highlightedIndex : selectedIndex;
  const describedBy = [
    helper ? hintId : "",
    error ? errorId : "",
    externalDescribedBy ?? "",
  ].filter(Boolean).join(" ") || undefined;

  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (!isInsideAny(event.target as Node | null, [rootRef.current, listboxRef.current])) {
        closeList();
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  // 開いているあいだに無効になったら閉じる（無効な欄の一覧を残さない）。
  useEffect(() => {
    if (disabled && open) closeList();
  }, [disabled, open]);

  useEffect(() => {
    const typeahead = typeaheadRef.current;
    return () => clearTimeout(typeahead.timer);
  }, []);

  // 強調中の選択肢を一覧の表示範囲に入れる。位置計算で一覧の高さが変わったときもやり直す。
  const listboxMaxHeight = position?.style.maxHeight;
  useLayoutEffect(() => {
    const listbox = listboxRef.current;
    if (!open || !listbox || activeIndex < 0 || !revealHighlightRef.current) return;
    const item = listbox.children.item(activeIndex);
    if (!(item instanceof HTMLElement)) return;
    listbox.scrollTop = nearestScrollTop({
      scrollTop: listbox.scrollTop,
      viewportHeight: listbox.clientHeight,
      itemTop: item.offsetTop,
      itemHeight: item.offsetHeight,
      padding: Number.parseFloat(window.getComputedStyle(listbox).paddingTop) || 0,
    });
  }, [open, activeIndex, listboxMaxHeight]);

  function highlight(index: number) {
    revealHighlightRef.current = true;
    setHighlightedIndex(index);
  }

  function openList(nextIndex = selectedIndex >= 0 ? selectedIndex : 0) {
    // 値の選択肢が無いあいだ（候補の取得中など）は、「未選択」だけの一覧を開かない。
    if (disabled || options.length === 0) return;
    highlight(nextIndex);
    setPortalContainer(selectPortalContainer(buttonRef.current));
    setOpen(true);
  }

  function closeList() {
    setOpen(false);
    setHighlightedIndex(-1);
    resetTypeahead();
  }

  function resetTypeahead() {
    clearTimeout(typeaheadRef.current.timer);
    typeaheadRef.current = { query: "", timer: undefined };
  }

  function typeahead(char: string) {
    const typeaheadState = typeaheadRef.current;
    clearTimeout(typeaheadState.timer);
    const query = typeaheadState.query + char;
    const match = findTypeaheadIndex(
      allOptions.map((option) => option.label),
      query,
      typeaheadStartIndex(query, activeIndex)
    );
    typeaheadRef.current = {
      query: match >= 0 ? query : "",
      timer: setTimeout(resetTypeahead, SELECT_TYPEAHEAD_RESET_MS),
    };
    if (!open) {
      openList(match >= 0 ? match : undefined);
    } else if (match >= 0) {
      highlight(match);
    }
  }

  function selectOption(option: SelectFieldOption<T>) {
    // 無効の選択肢は強調・読み上げだけで、選ばない（一覧は閉じない。#3374）。
    if (option.disabled) return;
    onValueChange(option.value);
    closeList();
    window.requestAnimationFrame(() => buttonRef.current?.focus({ preventScroll: true }));
  }

  function moveHighlight(delta: number) {
    if (allOptions.length === 0) return;
    const base = activeIndex >= 0 ? activeIndex : 0;
    const next = (base + delta + allOptions.length) % allOptions.length;
    highlight(next);
  }

  function moveHighlightBy(delta: number) {
    if (allOptions.length === 0) return;
    const base = activeIndex >= 0 ? activeIndex : 0;
    highlight(Math.min(allOptions.length - 1, Math.max(0, base + delta)));
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    // 無効なボタンにはフォーカスが来ないが、プログラムで focus された場合も開かず typeahead もしない。
    if (disabled) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) {
          openList();
        } else {
          moveHighlight(1);
        }
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!open) {
          openList(selectedIndex >= 0 ? selectedIndex : allOptions.length - 1);
        } else {
          moveHighlight(-1);
        }
        break;
      case "Home":
        if (open) {
          event.preventDefault();
          highlight(0);
        }
        break;
      case "End":
        if (open) {
          event.preventDefault();
          highlight(allOptions.length - 1);
        }
        break;
      case "PageUp":
        if (open) {
          event.preventDefault();
          moveHighlightBy(-PAGE_STEP);
        }
        break;
      case "PageDown":
        if (open) {
          event.preventDefault();
          moveHighlightBy(PAGE_STEP);
        }
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        if (!open) {
          openList();
          return;
        }
        if (activeIndex >= 0) {
          selectOption(allOptions[activeIndex]);
        }
        break;
      case "Escape":
        if (open) {
          event.preventDefault();
          // 一覧だけを閉じる。囲むモーダルの Esc（document の keydown）まで届かせない。
          event.stopPropagation();
          closeList();
        }
        break;
      case "Tab":
        closeList();
        break;
      default:
        if (
          event.key.length === 1 &&
          event.key !== " " &&
          !event.altKey &&
          !event.ctrlKey &&
          !event.metaKey
        ) {
          event.preventDefault();
          typeahead(event.key);
        }
        break;
    }
  }

  // 一覧は body（モーダルの中ならそのモーダル）へ Portal で描く。親の overflow に切られず、
  // z-index を持つ層の中では floating-menu が z-index をその層より 1 段上げる。
  const listbox =
    open && portalContainer
      ? createPortal(
          <ul
            ref={listboxRef}
            id={listboxId}
            role="listbox"
            aria-labelledby={labelId}
            data-floating-menu-placement={position?.placement}
            className={cn(
              "fixed z-[var(--z-dropdown)] max-h-64 overflow-auto overscroll-contain [scrollbar-gutter:stable] rounded-md border border-border bg-surface-raised p-1 shadow-[var(--shadow-popover)]",
              !position && "opacity-0"
            )}
            style={position?.style ?? { left: -9999, top: -9999 }}
            // 一覧を押してもフォーカスはボタンに残す（aria-activedescendant で強調を伝える）。
            onMouseDown={(event) => event.preventDefault()}
            // Portal でも React のイベントは祖先（表の行など）へ伝わるため、選択のクリックはここで止める。
            onClick={(event) => event.stopPropagation()}
          >
            {allOptions.map((option, index) => {
              const selected = option.value === value;
              const highlighted = index === activeIndex;
              // 「未選択」の選択肢は、値の選択肢と区別できるよう文字を控えめの色にする（選択中・強調中は他と同じ）。
              const emptyChoice = hasEmptyOption && index === 0;
              const optionDisabled = Boolean(option.disabled);
              const disabledReason = optionDisabled ? option.disabledReason : undefined;
              return (
                <li
                  key={option.value}
                  id={optionId(id, index)}
                  role="option"
                  // e2e が値で選択肢を選べるように、値を data 属性に出す（表示には使わない）。
                  data-value={option.value}
                  aria-selected={selected}
                  aria-disabled={optionDisabled || undefined}
                  // 淡青の面の上の補足（description）を 4.5:1 にする（tokens/colors.css。#1671）。
                  data-surface-tint={!optionDisabled && (selected || highlighted) ? "accent" : undefined}
                  onMouseEnter={() => {
                    revealHighlightRef.current = false;
                    setHighlightedIndex(index);
                  }}
                  // pointerdown で選ぶとタッチで一覧をスクロールしただけで選ばれるため、click で選ぶ。
                  onClick={() => selectOption(option)}
                  className={cn(
                    "flex min-h-10 items-center gap-2 rounded px-2.5 py-2 text-sm transition-colors",
                    optionDisabled
                      ? // 無効の選択肢は灰色の文字と禁止のカーソル。強調は地だけで示す（選べそうに見せない）。
                        cn("cursor-not-allowed text-fg-disabled", highlighted && "bg-surface-hover")
                      : cn(
                          "cursor-pointer",
                          selected
                            ? "bg-accent-muted font-medium text-accent-fg-strong"
                            : emptyChoice
                              ? "text-fg-muted"
                              : "text-fg",
                          highlighted && "bg-accent-subtle text-fg",
                          selected && highlighted && "bg-accent-muted text-accent-fg-strong"
                        )
                  )}
                >
                  <Check
                    size={16}
                    className={cn("shrink-0 text-accent-fg-strong", selected ? "opacity-100" : "opacity-0")}
                    aria-hidden
                  />
                  <span className="min-w-0">
                    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <span className={cn("block", wrapOptions ? "whitespace-normal break-words" : "truncate")}>{option.label}</span>
                      {option.badge ? <StatusBadge variant="neutral" icon={false} label={option.badge} /> : null}
                    </span>
                    {option.description ? (
                      <span className="mt-0.5 block truncate text-xs font-normal text-fg-muted">
                        {option.description}
                      </span>
                    ) : null}
                    {disabledReason ? (
                      // 理由は読める濃さ（fg-muted。4.5:1）にし、折り返して全文を見せる（一覧の中では Tooltip を出せない）。
                      <span className="mt-0.5 block whitespace-normal text-xs font-normal text-fg-muted" data-option-disabled-reason="">
                        {disabledReason}
                      </span>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>,
          portalContainer
        )
      : null;

  const widthClasses = labeledFieldWidthClasses(width, widthScope, Boolean(helper || error));

  return (
    // 段の幅（width）は選択欄のボタンに付け、ラベルと必須の印はボタンの幅で折り返さない（#1746）。
    <div
      ref={rootRef}
      className={cn("space-y-1.5", widthClasses.box, className)}
      // 上にラベルを出している欄の目印（一覧のツールバーが、移行前の上のラベルの絞り込みを下端でそろえる。#1933）。
      data-field-label={labelHidden || inlineLabel ? undefined : "top"}
      data-filter-active={inlineLabel ? String(active) : undefined}
    >
      <label
        id={labelId}
        htmlFor={id}
        className={cn(
          "flex items-center gap-2 text-sm font-medium text-fg",
          (labelHidden || inlineLabel) && "sr-only"
        )}
      >
        {label}
        {required && requiredLabel ? (
          <RequiredBadge label={requiredLabel} aria-hidden />
        ) : null}
      </label>
      <div className={cn("relative", widthClasses.control)}>
        <button
          ref={buttonRef}
          id={id}
          type="button"
          role="combobox"
          aria-controls={listboxId}
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-invalid={Boolean(error)}
          aria-required={required || undefined}
          aria-labelledby={labelId}
          aria-describedby={describedBy}
          aria-activedescendant={open && activeIndex >= 0 ? optionId(id, activeIndex) : undefined}
          disabled={disabled}
          data-testid={testId}
          // e2e が選択中の値を確かめられるように、値を data 属性に出す（表示には使わない）。
          data-value={value}
          onClick={() => (open ? closeList() : openList())}
          onKeyDown={handleKeyDown}
          className={cn(
            CONTROL_HEIGHT_CLASS[size],
            "flex w-full cursor-pointer items-center justify-between gap-3 rounded-control border bg-surface px-3 text-left text-sm text-fg outline-none transition-colors",
            "enabled:hover:bg-surface-hover forced-colors:border-[CanvasText] focus-visible:border-focus-ring focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring",
            // TextField・SearchableSelectField の disabled と同じ地・文字（枠線は残して欄の形を見せる）。
            "disabled:cursor-not-allowed disabled:bg-surface-disabled disabled:text-fg-disabled",
            error ? "border-danger-fg" : inlineLabel && active ? "border-accent-fg" : "border-border-control",
            buttonClassName
          )}
        >
          {inlineLabel ? (
            <span className="flex min-w-0 items-baseline gap-1">
              {/* 読み上げ名はラベル（aria-labelledby）で読むので、ボタンの中のラベルは見た目だけにする。 */}
              <span aria-hidden className="shrink-0 whitespace-nowrap text-fg-muted" data-filter-select-label="">
                {label}:
              </span>
              <span
                className={cn("min-w-0 truncate", active && !disabled && "font-semibold text-accent-fg-strong")}
                data-filter-select-value=""
              >
                {selectedOption?.label ?? (value || placeholder || emptyOptionLabel || "")}
              </span>
            </span>
          ) : (
            <span
              className={cn("min-w-0 truncate", !selectedOption && !value && !disabled && "text-fg-muted")}
              // 未選択の placeholder を出している間の目印（e2e の placeholder の切れの検査が見る。#1891）。
              data-field-placeholder={!selectedOption && !value ? "" : undefined}
            >
              {selectedOption?.label ?? (value || placeholder || emptyOptionLabel || "")}
            </span>
          )}
          <ChevronDown
            size={16}
            className={cn(
              "shrink-0 transition-transform duration-150",
              disabled ? "text-fg-disabled" : "text-fg-muted",
              open && "rotate-180 text-accent-fg"
            )}
            aria-hidden
          />
        </button>

        {listbox}
      </div>
      {helper ? (
        <p id={hintId} className={cn("text-xs leading-relaxed text-fg-muted", widthClasses.text)}>
          {helper}
        </p>
      ) : null}
      <FieldError id={errorId} message={error} className={widthClasses.text} />
    </div>
  );
}

function optionId(id: string, index: number) {
  return `${id}-option-${index}`;
}
