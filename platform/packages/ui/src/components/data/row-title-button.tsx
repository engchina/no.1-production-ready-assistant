import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type AnchorHTMLAttributes,
  type ButtonHTMLAttributes,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from "react";

import { cn } from "../../lib/utils";
import { Tooltip } from "../ui/tooltip";

/** 長い題名を切り詰めるときの最大行数（既定は切り詰めずに折り返す）。 */
export type RowTitleButtonMaxLines = 1 | 2 | 3;

/**
 * 切り詰めた題名の Tooltip に出す最大文字数。Tooltip は短い文のための部品（README §4「`Tooltip`」）なので、
 * 長い内容（メモ・質問の全文など）は先頭だけを出し、全文は詳細で見せる。
 */
export const ROW_TITLE_TOOLTIP_MAX_CHARS = 120;

/** Tooltip の文言。長すぎるときは先頭 `ROW_TITLE_TOOLTIP_MAX_CHARS` 文字 + 「…」にする（サロゲートペアを割らない）。 */
export function rowTitleTooltipText(text: string, maxChars = ROW_TITLE_TOOLTIP_MAX_CHARS) {
  const chars = Array.from(text.trim());
  return chars.length > maxChars ? `${chars.slice(0, maxChars).join("")}…` : chars.join("");
}

/** リンクの形（`href`）のときに `<a>` へ渡さない、ボタンだけの属性。`onClick` は画面内で開くときに呼ぶ。 */
const BUTTON_ONLY_PROPS = new Set([
  "onClick",
  "disabled",
  "form",
  "formAction",
  "formEncType",
  "formMethod",
  "formNoValidate",
  "formTarget",
  "name",
  "value",
]);

const LINE_CLAMP_CLASS: Record<RowTitleButtonMaxLines, string> = {
  1: "line-clamp-1",
  2: "line-clamp-2",
  3: "line-clamp-3",
};

export interface RowTitleButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "title" | "children" | "type" | "aria-current"> {
  /** 対象名。文字列のほか、識別子の折り返しなど製品の表示部品も渡せる（色・太さは部品の既定にそろえる）。 */
  title: ReactNode;
  /** 対象名の下の補足（ID・日時・説明など）。`--color-fg-muted` の 12px。 */
  subtitle?: ReactNode;
  /**
   * 詳細に表示中の対象か。`aria-current="true"` を付ける（読み上げで「現在の項目」と伝える）。
   * 見た目の選択の状態は、行（`DataTable` の `selectedRowKey`）やカードが持つ。
   */
  current?: boolean;
  /**
   * 対象名を切り詰める最大行数。既定は切り詰めず、単語の途中でも折り返す。
   * 切り詰めたときは、ホバーとキーボードのフォーカスで全文の Tooltip を出す（読み上げは全文のまま。
   * 長い内容は先頭 `ROW_TITLE_TOOLTIP_MAX_CHARS` 文字まで）。
   */
  maxLines?: RowTitleButtonMaxLines;
  /** 切り詰めたときの Tooltip の全文。`title` が文字列なら省略できる。 */
  fullTitle?: string;
  /**
   * 開く先の URL（#583。例: `/search-answer-profiles?id=bv-1`）。渡すと `<a href>` のリンクになり、
   * Ctrl / ⌘ / Shift + クリック・中クリック・コンテキストメニューで新しいタブ・ウィンドウに開ける。
   * 修飾キーの無いクリック（Enter を含む）は既定の遷移を止めて `onClick` を呼ぶ（画面内で開く。
   * 作業中の状態を保つため、ページを読み直さない）。`onClick` が無ければ通常のリンクとして移る。
   * `disabled` のときはリンクにしない（押せないボタンのまま）。
   */
  href?: string;
  ref?: Ref<HTMLButtonElement | HTMLAnchorElement>;
}

function assignRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (typeof ref === "function") ref(value);
  else if (ref && typeof ref === "object") (ref as { current: T | null }).current = value;
}

/**
 * リンクのクリックを画面内で開くか（修飾キーの無い左クリックだけ）。Ctrl / ⌘ / Shift / Alt との組み合わせ・
 * 左以外のボタンはブラウザの既定（新しいタブ・ウィンドウ・ダウンロード）に任せる。
 */
export function isPlainLeftClick(
  event: Pick<globalThis.MouseEvent, "button" | "metaKey" | "ctrlKey" | "shiftKey" | "altKey" | "defaultPrevented">
) {
  return (
    !event.defaultPrevented &&
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

/** 行数で切り詰めた要素が、実際に切り詰められているか（1px の丸め誤差は無視する）。 */
export function isClampedOverflow(element: Pick<HTMLElement, "scrollHeight" | "clientHeight">) {
  return element.scrollHeight - element.clientHeight > 1;
}

/**
 * 一覧の行の先頭セル（または一覧のカード）に置く対象名のボタン（#421）。
 * 行のクリックと同じ「選ぶ / 開く」をキーボード（Tab → Enter / Space）でも行えるようにする
 * （UX 契約 page-archetypes.md §0-7。選択だけの導線は RowActionMenu に入れない）。
 *
 * - 情報一覧の構造コントロールなので、アクションボタン（共有 Button）ではなく文字だけのボタンにする。
 *   ホバーで対象名に下線、押せる範囲は行全体（`DataTable` の `onRowClick`）とこのボタン。
 * - 選択中は `aria-current="true"`（APG: 詳細を切り替えるだけで押しても解除しないので `aria-pressed` ではなく、
 *   開閉する領域を持たないので `aria-expanded` でもない。`DataTable` の行の `aria-current` と同じ意味）。
 * - フォーカスはグローバルの `:focus-visible`（outline 2px）。タッチ端末では当たり判定を 44px 以上にする
 *   （`pr-touch-target`、#364。見た目の大きさは変えない）。
 * - URL で開く対象（`?id=` のエディタ・詳細）は `href` を渡してリンクにする（#583。新しいタブで開ける）。
 *   見た目はボタンの形と同じ。行のクリック（`onRowClick`）はリンクのクリックを重ねて扱わない（`DataTable`）。
 */
export function RowTitleButton({
  title,
  subtitle,
  current = false,
  maxLines,
  fullTitle,
  href,
  className,
  ref,
  ...props
}: RowTitleButtonProps) {
  const contentId = useId();
  const subtitleId = useId();
  const generatedId = useId();
  const controlId = props.id ?? generatedId;
  const actionLabel = props["aria-label"];
  // 自己参照は aria-label の操作説明を使い、表示文の参照と併せる（DOM の文は複製しない）。
  const labelledBy = props["aria-labelledby"] ?? (actionLabel && subtitle ? `${controlId} ${contentId} ${subtitleId}` : undefined);
  const titleRef = useRef<HTMLSpanElement | null>(null);
  const [clamped, setClamped] = useState(false);
  const fullText = maxLines ? (fullTitle ?? (typeof title === "string" ? title : undefined)) : undefined;
  const tooltipText = fullText ? rowTitleTooltipText(fullText) : undefined;

  // 切り詰めたときだけ全文の Tooltip を出す（幅が変わるたびに測り直す）。
  useLayoutEffect(() => {
    const element = titleRef.current;
    if (!tooltipText || !element) {
      setClamped(false);
      return undefined;
    }
    const measure = () => setClamped(isClampedOverflow(element));
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [tooltipText, maxLines]);

  const asLink = href !== undefined && !props.disabled;
  const rootClass = cn(
    "group/row-title pr-touch-target relative block min-w-0 max-w-full cursor-pointer rounded-sm text-left text-fg disabled:cursor-not-allowed disabled:text-fg-disabled",
    className
  );
  const content = (
    <>
      <span
        id={contentId}
        ref={titleRef}
        className={cn(
          "block break-words text-sm font-medium leading-5 underline-offset-2 [overflow-wrap:anywhere]",
          asLink ? "group-hover/row-title:underline" : "group-enabled/row-title:group-hover/row-title:underline",
          maxLines && LINE_CLAMP_CLASS[maxLines]
        )}
      >
        {title}
      </span>
      {subtitle ? (
        <span id={subtitleId} className="mt-0.5 block break-words text-xs leading-5 text-fg-muted [overflow-wrap:anywhere]">
          {subtitle}
        </span>
      ) : null}
    </>
  );

  let button;
  if (asLink) {
    // ボタンだけの属性（form・value 等）はリンクに渡さない。読み上げ名・data-*・id・ほかのイベントは渡す。
    const { onClick } = props;
    const linkProps = Object.fromEntries(
      Object.entries(props).filter(([key]) => !BUTTON_ONLY_PROPS.has(key))
    ) as AnchorHTMLAttributes<HTMLAnchorElement>;
    button = (
      <a
        {...linkProps}
        id={controlId}
        aria-labelledby={labelledBy}
        ref={(node) => assignRef(ref, node)}
        href={href}
        aria-current={current ? "true" : undefined}
        data-row-title-button=""
        className={rootClass}
        onClick={(event) => {
          if (!onClick || !isPlainLeftClick(event)) return;
          // 修飾キーの無いクリック（Enter も click になる）は画面内で開く。ページを読み直さない。
          event.preventDefault();
          onClick(event as unknown as MouseEvent<HTMLButtonElement>);
        }}
      >
        {content}
      </a>
    );
  } else {
    button = (
      <button
        {...props}
        id={controlId}
        aria-labelledby={labelledBy}
        ref={(node) => assignRef(ref, node)}
        type="button"
        aria-current={current ? "true" : undefined}
        data-row-title-button=""
        className={rootClass}
      >
        {content}
      </button>
    );
  }

  if (!tooltipText) return button;
  // 読み上げ名には全文が入っているので、吹き出しは説明として結び付けない（二重に読み上げない）。
  return (
    <Tooltip content={tooltipText} disabled={!clamped} describe={false}>
      {button}
    </Tooltip>
  );
}
