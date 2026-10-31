import { ChevronDown, type LucideIcon } from "lucide-react";
import {
  type KeyboardEvent,
  type UIEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { isImeComposing } from "../../lib/keyboard";
import { cn } from "../../lib/utils";

import { useFieldWidthScope } from "./field-width-scope";
import { CONTROL_MIN_HEIGHT_CLASS, type ControlSize, type FieldWidth, labeledFieldWidthClasses } from "./control-size";
import { FieldError } from "./field-error";
import { useFloatingMenuPosition } from "./floating-menu";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "./required-badge";
import { SearchField } from "./search-field";
import {
  DEFAULT_SEARCHABLE_SELECT_LABELS,
  filterSearchableOptions,
  moveActiveIndex,
  SearchableOptionRow,
  searchableOptionId,
  type SearchableSelectLabels,
  type SearchableSelectOption,
  type SearchableSelectRemote,
} from "./searchable-options";
import { Spinner } from "./spinner";
import { isInsideAny, nearestScrollTop, selectPortalContainer } from "./select-field";
import { SelectListEmpty } from "./select-list-empty";

/** 一覧の下端からこの距離（px）まで来たら続きを読む。 */
const LOAD_MORE_THRESHOLD_PX = 48;

export interface SearchableSelectFieldProps {
  /** 開閉ボタンの id（外の要素と結ぶとき用）。 */
  id: string;
  /** 翻訳済みラベル。 */
  label: string;
  /** 選択中の値。 */
  value: string;
  /** 候補（画面側で絞り込むときは全件。サーバー側で検索するときは返った分）。 */
  options: readonly SearchableSelectOption[];
  onValueChange: (value: string) => void;
  /**
   * 選択中の候補（任意）。サーバー側で検索していて `options` に選択中が無いときも、ボタンに名前を出すために渡す。
   */
  selectedOption?: SearchableSelectOption | null;
  /** 検索語が確定したとき（SearchField と同じ: 300ms・Enter・消去。IME の変換中は呼ばない）。 */
  onQueryChange?: (query: string) => void;
  /** サーバー側で検索するとき（#578）。渡さなければ `options` を画面側で絞り込む。 */
  remote?: SearchableSelectRemote;
  helper?: string;
  error?: string;
  /**
   * 欄の外にある説明の要素の id（空白区切りで複数可。#647）。`helper`・`error` の id と合わせて
   * ボタンの `aria-describedby` に渡す（SelectField の `describedBy` と同じ）。
   */
  describedBy?: string;
  /**
   * 必須の欄。ラベルの後ろに「必須」のタグを出す。ボタン（role=button）は aria-required を持てないので、
   * タグをラベルの一部として読み上げる（FieldLabel の `requiredAnnouncedByControl={false}` と同じ。#1595）。
   */
  required?: boolean;
  requiredLabel?: string;
  /** 何も選んでいないときのボタンの文言。 */
  placeholder?: string;
  /**
   * ボタンの先頭のアイコン（任意。lucide-react のコンポーネント。例: `leadingIcon={Database}`）。16px・読み上げない。
   * 対象の種類を表すアイコンにだけ使う。虫眼鏡（`Search` 系）は渡さない: 閉じたボタンは選んだ値と開閉の印だけを出し、
   * 虫眼鏡は開いたパネルの中の検索欄（SearchField）だけが持つ（shadcn/ui の Combobox・MUI の Select と同じ。
   * adherence の lint が検出する。README §4「`SearchableSelectField` の閉じたボタン」、#1771）。
   */
  leadingIcon?: LucideIcon;
  disabled?: boolean;
  labels?: Partial<SearchableSelectLabels>;
  /** 高さの最小（既定 md = 36px）。選択中の名前は折り返すので、長い名前では高くなる。同じ行の Button と同じ size にする（#613）。 */
  size?: ControlSize;
  /** 幅（候補の名前の長さで選ぶ。既定は親の幅いっぱい）。sm（640px）未満は全幅（#613）。 */
  width?: FieldWidth;
  className?: string;
  buttonClassName?: string;
  /**
   * ラベルの置き方（#1933）。`top`（既定）は欄の上、`inline` はボタンの中の先頭に「ナレッジベース: すべて」の形で出す
   * （一覧のツールバーの絞り込みで、数百件から選ぶ対象。`SelectField` の `labelPlacement` と同じ）。`inline` では
   * 選択中の名前を 1 行で切り（全文は `title`）、ツールバーの行の高さを欄にそろえる。
   */
  /** compact はチャットのツールバー用。選択値だけを 1 行で表示し、ラベルは読み上げ名に残す。 */
  labelPlacement?: "top" | "inline" | "compact";
  /** 絞り込みが掛かっている（`inline` のときだけ効く。値を太字・アクセント、枠をアクセントにする。`SelectField` の `active` と同じ）。 */
  active?: boolean;
}

/**
 * 検索できる単一選択（#578）。ボタン（選択中の名前を切らずに出す）を押すと、検索欄と候補の一覧を重ねて開く。
 * 選択肢が数十〜数百件の選択に使う（十数件までの固定の選択肢は SelectField）。
 *
 * - 開くと検索欄へフォーカスが移る。検索欄は SearchField（debounce 300ms・Enter はすぐ・IME の変換中は絞り込まない）。
 * - ↑↓ / PageUp / PageDown で候補を強調（aria-activedescendant）、Enter で選んで閉じ、ボタンへ戻る。
 *   Esc・Tab・外側のクリックで閉じる（Esc と Tab はボタンへ戻る）。IME の変換中の Enter・矢印は候補の操作に使わない。
 * - 候補が多いとき（数百件）は `remote` でサーバー側の検索に切り替え、一覧の下端までスクロールするか
 *   最後の候補から ↓ で続きを読む。
 */
export function SearchableSelectField({
  id,
  label,
  value,
  options,
  onValueChange,
  selectedOption,
  onQueryChange,
  remote,
  helper,
  error,
  describedBy: externalDescribedBy,
  required,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  placeholder = "",
  leadingIcon: LeadingIcon,
  disabled = false,
  labels: labelOverrides,
  size = "md",
  width,
  className,
  buttonClassName,
  labelPlacement = "top",
  active: filterActive = false,
}: SearchableSelectFieldProps) {
  const widthScope = useFieldWidthScope();
  const compactLabel = labelPlacement === "compact";
  const inlineLabel = labelPlacement !== "top";
  const labels = { ...DEFAULT_SEARCHABLE_SELECT_LABELS, ...labelOverrides };
  const reactId = useId();
  const labelId = `${id}-${reactId}-label`;
  const valueId = `${id}-${reactId}-value`;
  const hintId = `${id}-${reactId}-hint`;
  const errorId = `${id}-${reactId}-error`;
  const popoverId = `${id}-${reactId}-popover`;
  const listboxId = `${id}-${reactId}-listbox`;
  const searchId = `${id}-${reactId}-search`;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const listboxRef = useRef<HTMLUListElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const revealActiveRef = useRef(false);
  // 閉じた後に、外れた検索欄が待っていた検索語を確定しても（SearchField の unmount）受け取らない。
  const openRef = useRef(false);

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const [portalContainer, setPortalContainer] = useState<HTMLElement | null>(null);
  const [minWidth, setMinWidth] = useState(0);
  const position = useFloatingMenuPosition({
    align: "start",
    boundary: "viewport",
    menuRef: popoverRef,
    open,
    triggerRef: buttonRef,
  });

  const visible = useMemo(
    () => (remote ? options : filterSearchableOptions(options, query)),
    [options, query, remote]
  );
  const selectedIndex = visible.findIndex((option) => option.value === value);
  const active =
    visible.length === 0
      ? -1
      : activeIndex >= 0
        ? Math.min(activeIndex, visible.length - 1)
        : Math.max(0, selectedIndex);
  const current = options.find((option) => option.value === value) ?? selectedOption ?? null;
  const searching = Boolean(remote?.searching);
  const countText = remote
    ? labels.count(options.length, remote.total)
    : labels.count(visible.length, options.length);
  const describedBy =
    [helper ? hintId : "", error ? errorId : "", externalDescribedBy ?? ""].filter(Boolean).join(" ") || undefined;

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (!isInsideAny(event.target as Node | null, [rootRef.current, popoverRef.current])) close(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // 開いているあいだに無効になったら閉じる（SelectField と同じ。#631）。
  useEffect(() => {
    if (disabled && open) close(false);
  }, [disabled, open]);

  // 開いたら検索欄へ（位置が決まってから。決まる前は画面外にあり、フォーカスでスクロールさせない）。
  const positioned = Boolean(position);
  useEffect(() => {
    if (open && positioned) inputRef.current?.focus({ preventScroll: true });
  }, [open, positioned]);

  const popoverTop = position?.style.top;
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
  }, [open, active, popoverTop]);

  // 続きはスクロールか最後の候補からの ↓ で読むが、読み込んだ候補が一覧の高さに満たないと一覧がスクロールせず、
  // マウスでは続きに届かない（サーバーが短いページを返したとき。#1708）。そのときは続きを自動で読む。
  // 読み込み中・検索中は待ち、続きが無くなるか一覧があふれたら止まる。続きを読んでも候補が増えなければ
  // 自動ではもう読まない（サーバーが続きを返さないときに繰り返さない。↓ では読める）。
  const remoteHasMore = Boolean(remote?.hasMore);
  const remoteLoadingMore = Boolean(remote?.loadingMore);
  const autoLoadedCountRef = useRef<number | null>(null);
  useEffect(() => {
    if (!open || searching) {
      autoLoadedCountRef.current = null; // 閉じた・検索し直したら、新しい一覧として数え直す
      return;
    }
    if (!remoteHasMore || remoteLoadingMore || autoLoadedCountRef.current === visible.length) return;
    const listbox = listboxRef.current;
    if (listbox && listbox.scrollHeight - listbox.clientHeight > LOAD_MORE_THRESHOLD_PX) return;
    autoLoadedCountRef.current = visible.length;
    // remote.onLoadMore は呼び出し側で毎回作り直されうるので、依存に入れない。
    remote?.onLoadMore();
  }, [open, remoteHasMore, remoteLoadingMore, searching, visible.length]);

  function openPopover() {
    if (disabled) return;
    revealActiveRef.current = true;
    setActiveIndex(-1);
    setPortalContainer(selectPortalContainer(buttonRef.current));
    setMinWidth(Math.round(buttonRef.current?.getBoundingClientRect().width ?? 0));
    openRef.current = true;
    setOpen(true);
  }

  function close(returnFocus: boolean) {
    openRef.current = false;
    setOpen(false);
    setActiveIndex(-1);
    if (query) {
      setQuery("");
      onQueryChange?.("");
    }
    if (returnFocus) buttonRef.current?.focus({ preventScroll: true });
  }

  function choose(option: SearchableSelectOption) {
    onValueChange(option.value);
    close(true);
  }

  function loadMoreIfNeeded() {
    if (remote?.hasMore && !remote.loadingMore) remote.onLoadMore();
  }

  function moveActive(delta: number) {
    revealActiveRef.current = true;
    const next = moveActiveIndex(active, delta, visible.length);
    setActiveIndex(next);
    if (delta > 0 && active === visible.length - 1) loadMoreIfNeeded();
  }

  function handleSearchKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Tab") {
      // 開いた選択はボタンに付属する一時的な層。Tab では閉じてボタンへ戻り、そこから次へ進む
      // （Shift+Tab はボタンに止まる）。
      if (event.shiftKey) event.preventDefault();
      close(true);
      return;
    }
    // IME の変換中の Enter・矢印は変換の確定・候補の移動なので、一覧の操作に使わない（#535）。
    if (isImeComposing(event)) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveActive(-1);
        return;
      case "PageDown":
        event.preventDefault();
        moveActive(10);
        return;
      case "PageUp":
        event.preventDefault();
        moveActive(-10);
        return;
      case "Enter": {
        const option = active >= 0 && !searching ? visible[active] : undefined;
        if (!option) return; // SearchField が検索語をすぐ確定する
        event.preventDefault();
        choose(option);
        return;
      }
      case "Escape":
        event.preventDefault();
        // 開いた選択だけを閉じる。囲むモーダルの Esc まで届かせない。
        event.stopPropagation();
        close(true);
        return;
      default:
        return;
    }
  }

  function handleButtonKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openPopover();
    }
  }

  function handleListScroll(event: UIEvent<HTMLUListElement>) {
    const list = event.currentTarget;
    if (list.scrollHeight - list.scrollTop - list.clientHeight <= LOAD_MORE_THRESHOLD_PX) loadMoreIfNeeded();
  }

  const popover =
    open && portalContainer
      ? createPortal(
          <div
            ref={popoverRef}
            id={popoverId}
            role="dialog"
            aria-label={label}
            data-floating-menu-placement={position?.placement}
            data-searchable-select-popover=""
            className={cn(
              // 候補の一覧は自分の高さの上限（bounded-scroll-area）の中でスクロールする。画面の空きが足りないときだけ
              // 全体を縮めてスクロールにする（高さは中身の実寸で測るので、全体は overflow-y-auto にしておく）。
              "fixed z-[var(--z-dropdown)] w-[min(24rem,calc(100vw-2rem))] overflow-y-auto overscroll-contain [scrollbar-gutter:stable] rounded-md border border-border bg-surface-raised shadow-[var(--shadow-popover)]",
              !position && "opacity-0"
            )}
            style={{ minWidth: minWidth || undefined, ...(position?.style ?? { left: -9999, top: -9999 }) }}
            // Portal でも React のイベントは祖先（表の行など）へ伝わるため、ここで止める。
            onClick={(event) => event.stopPropagation()}
          >
            <div className="border-b border-border p-2">
              <SearchField
                ref={inputRef}
                id={searchId}
                label={labels.searchLabel(label)}
                labelHidden
                value={query}
                onSearch={(next) => {
                  if (!openRef.current) return;
                  setQuery(next);
                  setActiveIndex(-1);
                  onQueryChange?.(next);
                }}
                clearLabel={labels.clearSearch}
                resultCountLabel={searching ? labels.searching : countText}
                placeholder={labels.searchPlaceholder}
                role="combobox"
                aria-expanded
                aria-controls={listboxId}
                aria-autocomplete="list"
                aria-activedescendant={active >= 0 ? searchableOptionId(listboxId, active) : undefined}
                onKeyDown={handleSearchKeyDown}
              />
            </div>
            {visible.length > 0 ? (
              <ul
                ref={listboxRef}
                id={listboxId}
                role="listbox"
                aria-label={label}
                aria-busy={searching || undefined}
                onScroll={handleListScroll}
                className="bounded-scroll-area overscroll-contain py-1"
              >
                {visible.map((option, index) => (
                  <SearchableOptionRow
                    key={option.value}
                    id={searchableOptionId(listboxId, index)}
                    option={option}
                    selected={option.value === value}
                    active={index === active}
                    multiple={false}
                    onSelect={() => choose(option)}
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
            <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-1.5 text-xs text-fg-muted">
              <span className="tnum">{searching ? labels.searching : countText}</span>
              {remote?.loadingMore ? (
                <span className="inline-flex items-center gap-1.5">
                  <Spinner size={14} aria-hidden />
                  {labels.loadMore}
                </span>
              ) : null}
            </div>
          </div>,
          portalContainer
        )
      : null;

  const widthClasses = labeledFieldWidthClasses(width, widthScope, Boolean(helper || error));

  return (
    // 段の幅（width）は選択欄のボタンに付け、ラベルと必須の印はボタンの幅で折り返さない（#1746）。
    // data-field-label: 上にラベルを出している欄の目印（一覧のツールバーが、移行前の上のラベルの絞り込みを下端でそろえる。#1933）。
    <div
      ref={rootRef}
      className={cn("space-y-1.5", widthClasses.box, className)}
      data-field-label={inlineLabel ? undefined : "top"}
      data-filter-active={inlineLabel ? String(filterActive) : undefined}
    >
      <label
        id={labelId}
        htmlFor={id}
        className={cn("flex items-center gap-2 text-sm font-medium text-fg", inlineLabel && "sr-only")}
      >
        {label}
        {/* ボタンは aria-required を持てない（axe の aria-allowed-attr）ので、タグを読み上げ対象に残す（#1595）。 */}
        {required && requiredLabel ? <RequiredBadge label={requiredLabel} /> : null}
      </label>
      <button
        ref={buttonRef}
        id={id}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        aria-labelledby={`${labelId} ${valueId}`}
        aria-describedby={describedBy}
        aria-invalid={Boolean(error) || undefined}
        disabled={disabled}
        onClick={() => (open ? close(false) : openPopover())}
        onKeyDown={handleButtonKeyDown}
        className={cn(
          CONTROL_MIN_HEIGHT_CLASS[size],
          "flex w-full cursor-pointer items-center justify-between gap-3 rounded-control border bg-surface px-3 py-1.5 text-left text-sm text-fg outline-none transition-colors",
          "enabled:hover:bg-surface-hover forced-colors:border-[CanvasText] focus-visible:border-focus-ring focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring",
          "disabled:cursor-not-allowed disabled:bg-surface-disabled disabled:text-fg-disabled",
          error ? "border-danger-fg" : inlineLabel && filterActive ? "border-accent-fg" : "border-border-control",
          widthClasses.control,
          compactLabel && "py-0",
          buttonClassName
        )}
      >
        {/* 選択中の名前は切らずに折り返す（長い名前も全体を読める）。 */}
        <span className={cn("flex min-w-0 items-center gap-2", inlineLabel && "gap-1")}>
          {LeadingIcon ? (
            <LeadingIcon
              size={16}
              aria-hidden
              className={cn("shrink-0", disabled ? "text-fg-disabled" : "text-fg-muted")}
            />
          ) : null}
          {inlineLabel && !compactLabel ? (
            // 読み上げ名はラベル（aria-labelledby）で読むので、ボタンの中のラベルは見た目だけにする（SelectField と同じ）。
            <span aria-hidden className="shrink-0 whitespace-nowrap text-fg-muted" data-filter-select-label="">
              {label}:
            </span>
          ) : null}
          <span
            id={valueId}
            title={inlineLabel ? (current?.label ?? undefined) : undefined}
            className={cn(
              "min-w-0",
              inlineLabel ? "truncate" : "[overflow-wrap:anywhere]",
              !current && !disabled && "text-fg-muted",
              inlineLabel && filterActive && !disabled && "font-semibold text-accent-fg-strong"
            )}
            data-filter-select-value={inlineLabel ? "" : undefined}
          >
            {current?.label ?? (value || placeholder)}
          </span>
        </span>
        {compactLabel && !value && required && requiredLabel ? <span aria-hidden className="shrink-0"><RequiredBadge label={requiredLabel} /></span> : null}
        <ChevronDown
          size={16}
          aria-hidden
          className={cn(
            "shrink-0 transition-transform duration-150",
            disabled ? "text-fg-disabled" : "text-fg-muted",
            open && "rotate-180 text-accent-fg"
          )}
        />
      </button>
      {popover}
      {helper ? (
        <p id={hintId} className={cn("text-xs leading-relaxed text-fg-muted", widthClasses.text)}>
          {helper}
        </p>
      ) : null}
      <FieldError id={errorId} message={error} className={widthClasses.text} />
    </div>
  );
}
