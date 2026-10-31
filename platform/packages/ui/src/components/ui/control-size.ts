/*
 * 操作部品（入力欄・選択欄・ボタン）の高さと、入力欄・選択欄の幅の段（#613）。
 * 規則は docs/design-system/README.md §4「操作部品の高さと幅」。
 *
 * - 高さは 3 段（sm 32px / md 36px / lg 40px、既定 md）。Button・TextField・SearchField・SecretField・
 *   SelectField・SearchableSelectField で同じ値（--control-height-*）を使い、同じ行の部品は同じ size にする。
 *   タッチ端末（pointer: coarse）では 3 段とも 44px（--control-height-touch）になるので、行の高さはそろったまま。
 * - 幅は値の種類で決める 5 段（xs 8rem / sm 12rem / md 20rem / lg 28rem / full）。sm（640px）未満は全幅（#1891）。
 *   フォームの grid のセルでも、種類の段を渡せば段の幅が上限になる（セルが狭ければセルの幅）。`full` の欄は
 *   `FormGrid` の読みやすい最大幅（--form-max-width 60rem）まで。
 *   段の幅は入力欄・選択欄そのものに付け、ラベルと必須の印は段の幅で折り返さず 1 行で出す（#1746）。
 *   補足・エラーの文は欄の幅ではなく、欄を置いた枠（grid のセル・FormGrid の段・カード）の幅で折り返す（#2063）。
 *   横に並べる行（flex）の中では min(20rem, 親の幅) より狭くは折り返さない（#1891 段 5）。
 *
 * Tailwind が dist からクラス名を拾えるように、クラスは文字列のまま書く（組み立てない）。
 */
import { cn } from "../../lib/utils";

/** 操作部品の高さ。sm 32px（表・密な行）/ md 36px（既定）/ lg 40px（工程を進める主操作の行・主な問い合わせの入力の行）。 */
export type ControlSize = "sm" | "md" | "lg";

/**
 * 入力欄・選択欄の幅。入る値の種類で選ぶ（sm 未満の画面では全幅。#613 / #1891）。
 * - `xs` 8rem: 数値・短いコード（〜6 文字）・表示件数（「10」「DEFAULT」「100 件」）
 * - `sm` 12rem: 日付・期間・短い選択（状態・種類・言語）・有効期限・リージョン
 * - `md` 20rem: 名前・ID・メール・一般の選択（ユーザー名・ロール名・表名）
 * - `lg` 28rem: 長い名前・対象の選択（検索・回答プロファイル・ナレッジベース・業務 Agent・モデル）・一覧の検索欄
 * - `full`: 親の幅いっぱい（URL・OCID・パス・文章・SQL・プロンプト・`TextareaField`。フォームでは `FormGrid` の
 *   最大幅 60rem まで）
 */
export type FieldWidth = "xs" | "sm" | "md" | "lg" | "full";

/** 幅の段の値（rem。トークン --field-width-* と同じ）。e2e の「欄の幅が段のどれか」の検査が使う（#1891）。 */
export const FIELD_WIDTH_REM: Record<Exclude<FieldWidth, "full">, number> = { xs: 8, sm: 12, md: 20, lg: 28 };

/** 高さ（固定）。1 行で切り詰める選択欄のボタン（SelectField）が使う。 */
export const CONTROL_HEIGHT_CLASS: Record<ControlSize, string> = {
  sm: "h-[var(--control-height-sm)]",
  md: "h-[var(--control-height-md)]",
  lg: "h-[var(--control-height-lg)]",
};

/** 高さ（最小）。入力欄と、値を折り返す選択欄（SearchableSelectField）が使う。 */
export const CONTROL_MIN_HEIGHT_CLASS: Record<ControlSize, string> = {
  sm: "min-h-[var(--control-height-sm)]",
  md: "min-h-[var(--control-height-md)]",
  lg: "min-h-[var(--control-height-lg)]",
};

/**
 * 幅。sm（640px）未満は全幅、sm 以上は段の幅（親より広くはしない）。
 * ラベルを持たない欄（`fieldControlClassName` のネイティブの `<select>` / `<input>`）と、ラベルを持つ欄の
 * 入力欄・選択欄のボタンそのもの（`FIELD_CONTROL_WIDTH_CLASS`）に付ける。
 */
export const FIELD_WIDTH_CLASS: Record<FieldWidth, string> = {
  xs: "w-full sm:w-[var(--field-width-xs)] sm:max-w-full",
  sm: "w-full sm:w-[var(--field-width-sm)] sm:max-w-full",
  md: "w-full sm:w-[var(--field-width-md)] sm:max-w-full",
  lg: "w-full sm:w-[var(--field-width-lg)] sm:max-w-full",
  full: "w-full",
};

/** 幅の段のクラス。指定が無ければ何も付けない（外枠はブロック要素なので親の幅いっぱい）。 */
export function fieldWidthClass(width: FieldWidth | undefined, scope: FieldWidthScope = "viewport") {
  if (!width) return undefined;
  return scope === "container" ? FIELD_WIDTH_CONTAINER_CLASS[width] : FIELD_WIDTH_CLASS[width];
}

/**
 * ラベルを持つ欄（TextField・SelectField・SearchableSelectField・SecretField）の外枠の幅（#1746）。
 * sm 以上は内容に合わせた幅（`w-fit`）= 「入力欄の段の幅」と「ラベルと必須の印の 1 行の幅」の広いほうで、
 * ラベルを入力欄の幅で折り返さない。親より広くはしない（`sm:max-w-full`。入らないときだけラベルが折り返す）。
 * sm 未満はどの段も全幅（ラベルは画面の幅に入らないときだけ折り返す）。
 */
export const FIELD_BOX_WIDTH_CLASS: Record<FieldWidth, string> = {
  xs: "w-full sm:w-fit sm:max-w-full",
  sm: "w-full sm:w-fit sm:max-w-full",
  md: "w-full sm:w-fit sm:max-w-full",
  lg: "w-full sm:w-fit sm:max-w-full",
  full: "w-full",
};

/**
 * ラベルを持つ欄の、入力欄・選択欄のボタンそのものの幅（#1746）。sm 以上は段の幅で、外枠（ラベルの幅まで広がる）
 * より広くはしない。`full` は外枠の幅いっぱい（何も付けない）。
 */
export const FIELD_CONTROL_WIDTH_CLASS: Record<FieldWidth, string | undefined> = {
  xs: "sm:w-[var(--field-width-xs)] sm:max-w-full",
  sm: "sm:w-[var(--field-width-sm)] sm:max-w-full",
  md: "sm:w-[var(--field-width-md)] sm:max-w-full",
  lg: "sm:w-[var(--field-width-lg)] sm:max-w-full",
  full: undefined,
};

/**
 * 段の幅を持つ欄の補足・エラーなどの文（#1746）。外枠の幅を広げず（`contain: inline-size` で文の長さを外枠の幅の
 * 計算に入れない）、外枠の幅で折り返す。補足・エラーのある外枠は置いた枠の幅まで広がる（`FIELD_BOX_TEXT_WIDTH_CLASS`。
 * #2063）ので、文は枠の幅で折り返す。枠が広くても 1 行はフォームの読みやすい最大幅（`--form-max-width` 60rem）まで。
 */
export const FIELD_TEXT_CONTAIN_CLASS = "sm:[contain:inline-size] sm:max-w-[var(--form-max-width)]";

/** 補足・エラーの文を折り返す最小の幅（rem。`md` の段と同じ 20rem = 12px の文で約 23 文字）。#1891 段 5 */
export const FIELD_TEXT_MIN_WIDTH_REM = FIELD_WIDTH_REM.md;

/**
 * 補足・エラーの文を持つ、段の幅の欄の外枠の幅（#2063）。sm 以上は `width: auto` で、置いた枠（grid のセル・
 * `FormGrid` の段・カード）の幅いっぱいに広がり、補足・エラーの文はその幅で折り返す（`xs` の数値の欄の 1 文の補足が
 * 欄の幅や 20rem で 2 行に折り返さない）。grid のセルの中に収まり、隣の欄にははみ出さない。入力欄の幅は段のまま。
 * 横に並べる行（flex）の中では `auto` は中身の幅（入力欄とラベル）になり、行の並びを変えない
 * （下の最小の幅 `FIELD_BOX_TEXT_MIN_WIDTH_CLASS` まで）。
 */
export const FIELD_BOX_TEXT_WIDTH_CLASS = "w-full sm:w-auto sm:max-w-full";

/**
 * 補足・エラーの文を持つ、段の幅の欄の外枠の最小の幅（#1891 段 5）。sm 以上は `min(20rem, 親の幅)`。
 * 横に並べる行（flex）の中で、`xs`（8rem）・`sm`（12rem）の欄の補足が欄の幅で 3〜6 行に折り返さないよう、文だけを
 * 読みやすい幅まで広げる（入力欄の幅は段のまま）。親より広くはならない（狭いセルではセルの幅）。
 */
export const FIELD_BOX_TEXT_MIN_WIDTH_CLASS = "sm:min-w-[min(var(--field-width-md),100%)]";

/**
 * 幅の段を、画面（viewport）の幅で切り替えるか、いちばん近い container（`@container`）の幅で切り替えるか（#1933）。
 * - `viewport`（既定）: sm（640px）未満は全幅、それ以上は段の幅。
 * - `container`: いちばん近い `@container` の幅が 28rem（`@md`）未満は全幅、それ以上は段の幅。一覧のツールバー
 *   （`ListToolbar`）の中の欄が使う。ツールバーの検索欄・並べ方と同じ container の幅で決まるので、狭い分割のペインでも
 *   検索欄と絞り込みの欄の幅がそろう（viewport の `sm:` だと、ペインが狭くても画面が広いと段の幅のままだった）。
 *   `@container` の外（シートなど）では全幅。
 */
export type FieldWidthScope = "viewport" | "container";

/** container の幅で切り替える幅（`FIELD_WIDTH_CLASS` の container 版）。 */
export const FIELD_WIDTH_CONTAINER_CLASS: Record<FieldWidth, string> = {
  xs: "w-full @md:w-[var(--field-width-xs)] @md:max-w-full",
  sm: "w-full @md:w-[var(--field-width-sm)] @md:max-w-full",
  md: "w-full @md:w-[var(--field-width-md)] @md:max-w-full",
  lg: "w-full @md:w-[var(--field-width-lg)] @md:max-w-full",
  full: "w-full",
};

/** container の幅で切り替える外枠の幅（`FIELD_BOX_WIDTH_CLASS` の container 版）。 */
export const FIELD_BOX_WIDTH_CONTAINER_CLASS: Record<FieldWidth, string> = {
  xs: "w-full @md:w-fit @md:max-w-full",
  sm: "w-full @md:w-fit @md:max-w-full",
  md: "w-full @md:w-fit @md:max-w-full",
  lg: "w-full @md:w-fit @md:max-w-full",
  full: "w-full",
};

/** container の幅で切り替える入力欄の幅（`FIELD_CONTROL_WIDTH_CLASS` の container 版）。 */
export const FIELD_CONTROL_WIDTH_CONTAINER_CLASS: Record<FieldWidth, string | undefined> = {
  xs: "@md:w-[var(--field-width-xs)] @md:max-w-full",
  sm: "@md:w-[var(--field-width-sm)] @md:max-w-full",
  md: "@md:w-[var(--field-width-md)] @md:max-w-full",
  lg: "@md:w-[var(--field-width-lg)] @md:max-w-full",
  full: undefined,
};

/** container の幅で切り替える補足の文（`FIELD_TEXT_CONTAIN_CLASS` の container 版）。 */
export const FIELD_TEXT_CONTAIN_CONTAINER_CLASS = "@md:[contain:inline-size] @md:max-w-[var(--form-max-width)]";

/** container の幅で切り替える、補足・エラーのある外枠の幅（`FIELD_BOX_TEXT_WIDTH_CLASS` の container 版）。 */
export const FIELD_BOX_TEXT_WIDTH_CONTAINER_CLASS = "w-full @md:w-auto @md:max-w-full";

/** container の幅で切り替える、補足・エラーのある外枠の最小の幅（`FIELD_BOX_TEXT_MIN_WIDTH_CLASS` の container 版）。 */
export const FIELD_BOX_TEXT_MIN_WIDTH_CONTAINER_CLASS = "@md:min-w-[min(var(--field-width-md),100%)]";

/**
 * ラベルを持つ欄の幅のクラス（外枠・入力欄・補足の文）。`width` が無いか `full` なら何も付けない
 * （外枠は親の幅いっぱい、入力欄は外枠の幅いっぱい）。`scope` は幅を切り替える基準（既定 viewport。#1933）。
 * `hasText`（補足・エラーなどの文がある）なら、外枠を置いた枠の幅まで広げ（`width: auto`。#2063）、文を枠の幅で
 * 折り返す。横に並べる行（flex）の中では、外枠は中身の幅と読みやすい最小の幅（min(20rem, 親の幅)。#1891 段 5）の
 * 広いほう。
 */
export function labeledFieldWidthClasses(
  width: FieldWidth | undefined,
  scope: FieldWidthScope = "viewport",
  hasText = false
) {
  if (!width || width === "full") {
    return { box: fieldWidthClass(width, scope), control: undefined, text: undefined };
  }
  if (scope === "container") {
    return {
      box: hasText
        ? cn(FIELD_BOX_TEXT_WIDTH_CONTAINER_CLASS, FIELD_BOX_TEXT_MIN_WIDTH_CONTAINER_CLASS)
        : FIELD_BOX_WIDTH_CONTAINER_CLASS[width],
      control: FIELD_CONTROL_WIDTH_CONTAINER_CLASS[width],
      text: FIELD_TEXT_CONTAIN_CONTAINER_CLASS,
    };
  }
  return {
    box: hasText ? cn(FIELD_BOX_TEXT_WIDTH_CLASS, FIELD_BOX_TEXT_MIN_WIDTH_CLASS) : FIELD_BOX_WIDTH_CLASS[width],
    control: FIELD_CONTROL_WIDTH_CLASS[width],
    text: FIELD_TEXT_CONTAIN_CLASS,
  };
}

/**
 * 入力欄の見た目（枠線は secondary ボタンと同じ --color-border-control、角丸は Button・SelectField と同じ --radius-control）。
 * TextField・TextareaField・SecretField と、ネイティブの `<select>` / `<input>`（`fieldControlClassName`）が共有する。
 */
export const fieldControlClass = cn(
  "w-full min-h-[var(--control-height-md)] rounded-control border bg-surface px-3 text-sm text-fg outline-none transition-colors",
  // プレースホルダも「文字」。透過で薄めると 3:1 を割るので fg-muted のまま使う
  "placeholder:text-fg-muted placeholder:opacity-100",
  "focus-visible:border-focus-ring focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus-ring",
  "disabled:cursor-not-allowed disabled:bg-surface-disabled disabled:text-fg-disabled",
  "read-only:bg-surface-sunken",
  "forced-colors:border-[CanvasText]",
  // type="search" のブラウザ既定のクリアボタン（Chromium / Safari の ×）と装飾を出さない。
  // キーボードで届かず読み上げ名も訳せないため、クリアは onClear の共有ボタン（または trailing）で出す（#384）。
  "[&::-webkit-search-cancel-button]:appearance-none [&::-webkit-search-decoration]:appearance-none"
);

export interface FieldControlClassNameOptions {
  /** 高さ（既定 md）。同じ行の Button と同じ size にする。 */
  size?: ControlSize;
  /** 幅（既定は親の幅いっぱい）。 */
  width?: FieldWidth;
  /** 幅の段を切り替える基準（既定 viewport。一覧のツールバーの中は `useFieldWidthScope()` の値を渡す。#1933）。 */
  scope?: FieldWidthScope;
  className?: string;
}

/**
 * ネイティブの `<select>` / `<input>` に、TextField・SelectField と同じ見た目・高さ・幅を付けるクラス（#613）。
 * 新しい画面は TextField / SelectField を使う。ネイティブの要素を残す画面（`<optgroup>` を使う選択・
 * ラベルを周りの部品が持つ欄など）だけがこれを使い、`h-*` / `w-*` / 枠線・角丸を手書きしない。
 * エラーは `aria-invalid="true"` を付ければ枠線が danger になる。
 */
export function fieldControlClassName({ size = "md", width, scope, className }: FieldControlClassNameOptions = {}) {
  return cn(
    fieldControlClass,
    CONTROL_MIN_HEIGHT_CLASS[size],
    "border-border-control aria-[invalid=true]:border-danger-fg",
    // ネイティブの select は押せることを示す（入力欄は既定の I ビーム）。
    "[&:is(select)]:cursor-pointer",
    fieldWidthClass(width, scope),
    className
  );
}
