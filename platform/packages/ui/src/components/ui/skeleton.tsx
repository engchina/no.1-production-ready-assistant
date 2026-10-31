import { INFORMATION_TABLE_VISIBLE_ROWS } from "../../lib/list-density";
import { cn } from "../../lib/utils";

/**
 * 読み込み中のプレースホルダの見た目（3 製品で共通、#265）。
 * 地は `--color-surface-hover`、`prefers-reduced-motion` では点滅を止める。
 * `Skeleton`・`TableSkeleton`・`ListSkeleton`・`DataTable` の `loading` の行が同じ見た目を使う。
 */
export const SKELETON_CLASS = "animate-pulse rounded-md bg-surface-hover motion-reduce:animate-none";

/** ローディング用スケルトン（プレースホルダ）。寸法は className で内容の形に合わせる。 */
export function Skeleton({ className, testId }: { className?: string; testId?: string }) {
  return <div className={cn(SKELETON_CLASS, className)} aria-hidden="true" data-testid={testId} />;
}

/** 行数。`{ base, md }` は md（48rem）以上で `md` を使う（DataTable の `visibleRows` と同じ形）。 */
export type SkeletonRows = number | { base: number; md?: number };

function resolveRowCounts(rows: SkeletonRows) {
  if (typeof rows === "number") return { base: Math.max(0, rows), max: Math.max(0, rows) };
  const base = Math.max(0, rows.base);
  return { base, max: Math.max(base, rows.md ?? base) };
}

/** 列の棒の幅。行ごとにずらして、同じ長さの棒が縦に並ばないようにする。 */
/** md 未満で出す列の数。狭い幅では実際の表も横スクロールで先頭の数列しか見えないため、棒が点にならないよう絞る。 */
const NARROW_COLUMNS = 3;
function columnClass(column: number) {
  return cn("min-w-0 flex-1", column >= NARROW_COLUMNS && "hidden md:block");
}

const COLUMN_BAR_WIDTHS = ["w-3/4", "w-1/2", "w-2/3", "w-1/3", "w-3/5", "w-2/5"] as const;

export interface TableSkeletonProps {
  /** 行数。既定は一覧の表示行数（md 未満 5 行・md 以上 8 行）で、読み込み後の DataTable の高さを予約する。 */
  rows?: SkeletonRows;
  /** 列数（既定 4）。読み込み後の表の列数に合わせる。 */
  columns?: number;
  className?: string;
  testId?: string;
}

/**
 * 表の形のスケルトン。DataTable と同じ枠・表頭の地（`surface-sunken`、2.5rem）・行の高さ（3.5rem）で、
 * 列ごとの棒を並べる。`TimedLoadingState` の子に置き、読み込み中の文言と経過時間はそちらで示す。
 * md 以上だけの行は CSS で出し分けるため、幅の判定を待たずに最初の描画で高さが決まる（CLS を出さない）。
 */
export function TableSkeleton({
  rows = INFORMATION_TABLE_VISIBLE_ROWS,
  columns = 4,
  className,
  testId,
}: TableSkeletonProps) {
  const { base, max } = resolveRowCounts(rows);
  const columnCount = Math.max(1, columns);
  return (
    <div
      className={cn("overflow-hidden rounded-md border border-border bg-surface", className)}
      aria-hidden="true"
      data-testid={testId}
      data-skeleton="table"
    >
      <div className="flex h-[2.5rem] items-center gap-4 bg-surface-sunken px-3" data-skeleton-part="header">
        {Array.from({ length: columnCount }, (_, column) => (
          <div key={column} className={columnClass(column)}>
            <div className={cn(SKELETON_CLASS, "h-3 w-1/3")} />
          </div>
        ))}
      </div>
      {Array.from({ length: max }, (_, row) => (
        <div
          key={row}
          data-skeleton-row={row < base ? "base" : "md"}
          className={cn(
            "h-[3.5rem] items-center gap-4 border-t border-border/70 px-3",
            row < base ? "flex" : "hidden md:flex"
          )}
        >
          {Array.from({ length: columnCount }, (_, column) => (
            <div key={column} className={columnClass(column)}>
              <div className={cn(SKELETON_CLASS, "h-4", COLUMN_BAR_WIDTHS[(column + row) % COLUMN_BAR_WIDTHS.length])} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export interface ListSkeletonProps {
  /** 行数。既定は一覧の表示行数（md 未満 5 行・md 以上 8 行）。 */
  rows?: SkeletonRows;
  /** 1 行の高さの class（既定は一覧の行の高さ 3.5rem）。行が高いカードの一覧は `h-20` などを渡す。 */
  rowClassName?: string;
  className?: string;
  testId?: string;
}

/**
 * 表ではない行リスト（カードの行・履歴など）の形のスケルトン。角丸の塊を gap で並べる。
 * `TimedLoadingState` の子に置く。
 */
export function ListSkeleton({
  rows = INFORMATION_TABLE_VISIBLE_ROWS,
  rowClassName = "h-[3.5rem]",
  className,
  testId,
}: ListSkeletonProps) {
  const { base, max } = resolveRowCounts(rows);
  return (
    <div className={cn("grid gap-2", className)} aria-hidden="true" data-testid={testId} data-skeleton="list">
      {Array.from({ length: max }, (_, row) => (
        <div
          key={row}
          data-skeleton-row={row < base ? "base" : "md"}
          className={cn(SKELETON_CLASS, rowClassName, row < base ? "block" : "hidden md:block")}
        />
      ))}
    </div>
  );
}

export interface FormSkeletonProps {
  /** 入力欄の数（既定 4）。 */
  fields?: number;
  /** 見出しの棒を出すか（既定 true）。 */
  title?: boolean;
  /** 保存などの操作行の棒を出すか（既定 true）。 */
  actions?: boolean;
  className?: string;
  testId?: string;
}

/**
 * 設定カード・エディタのフォームの形のスケルトン。見出し → ラベル + 入力欄 × N → 右寄せの操作行。
 * 入力欄の高さはコントロールの高さ（`--button-height-md`）にそろえる。`TimedLoadingState` の子に置く。
 */
export function FormSkeleton({ fields = 4, title = true, actions = true, className, testId }: FormSkeletonProps) {
  return (
    <div
      className={cn("grid gap-5 rounded-md border border-border bg-surface p-4", className)}
      aria-hidden="true"
      data-testid={testId}
      data-skeleton="form"
    >
      {title ? <div className={cn(SKELETON_CLASS, "h-5 w-48 max-w-full")} data-skeleton-part="title" /> : null}
      {Array.from({ length: Math.max(0, fields) }, (_, field) => (
        <div key={field} className="grid gap-1.5" data-skeleton-part="field">
          <div className={cn(SKELETON_CLASS, "h-3", field % 2 === 0 ? "w-32" : "w-24")} />
          <div className={cn(SKELETON_CLASS, "h-[var(--button-height-md)] w-full")} />
        </div>
      ))}
      {actions ? (
        <div className="flex justify-end" data-skeleton-part="actions">
          <div className={cn(SKELETON_CLASS, "h-[var(--button-height-md)] w-24")} />
        </div>
      ) : null}
    </div>
  );
}

export interface ChatSkeletonProps {
  /** 質問と回答の組の数（既定 2。1 未満でも 1 組は出す）。 */
  turns?: number;
  className?: string;
  testId?: string;
}

/** 質問（右寄せの吹き出し）と回答（左の塊）の幅。組ごとにずらして同じ長さを縦に並べない。 */
const CHAT_QUESTION_WIDTHS = ["w-2/3", "w-1/2"] as const;
const CHAT_ANSWER_WIDTHS = ["w-5/6", "w-3/4"] as const;

/**
 * チャットの会話の形のスケルトン（#1153）。右寄せの質問の吹き出し（`ChatUserMessage` の形）と、
 * 左の回答の塊を交互に並べる。3 製品のチャットの会話の欄で、画面の前提（対象の一覧・開いている会話の内容）を
 * 読み込んでいる間に、空の状態（はじめの案内）の代わりに出す（UX 契約 messaging.md §11.7）。
 * 会話の内容を取得している間は `TimedLoadingState` の子に置く。対象の一覧の経過時間を上のカードが出している間は、
 * 経過時間を重ねず、このスケルトンだけを出す。
 */
export function ChatSkeleton({ turns = 2, className, testId }: ChatSkeletonProps) {
  return (
    <div className={cn("grid gap-4", className)} aria-hidden="true" data-testid={testId} data-skeleton="chat">
      {Array.from({ length: Math.max(1, turns) }, (_, turn) => (
        <div key={turn} className="grid gap-2" data-skeleton-part="turn">
          <div
            className={cn(
              SKELETON_CLASS,
              "ml-auto h-12 max-w-[85%]",
              CHAT_QUESTION_WIDTHS[turn % CHAT_QUESTION_WIDTHS.length]
            )}
            data-skeleton-part="question"
          />
          <div
            className={cn(SKELETON_CLASS, "h-32", CHAT_ANSWER_WIDTHS[turn % CHAT_ANSWER_WIDTHS.length])}
            data-skeleton-part="answer"
          />
        </div>
      ))}
    </div>
  );
}
