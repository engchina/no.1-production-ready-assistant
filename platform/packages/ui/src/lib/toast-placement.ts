/**
 * Toast（一時通知）の置き場所（#1728。#411 の「PageHeader の操作の位置を測ってその左に置く」規則を置き換えた）。
 *
 * 通知は画面ごとに動かさず、画面幅（md 以上 / md 未満）ごとに決まった 1 か所に出す。ページの内容（PageHeader の操作の
 * 有無・幅・スクロール）では位置を変えない（利用者が毎回同じ所を見ればよい。Sonner・Fluent 2 などの
 * 「位置は 1 か所に固定し、内容で動かさない」作り）。
 *
 * - md 以上（`bottom-right`）: 画面の右下。右端から 1rem、下端から `--toast-offset-bottom`（8rem）。新しい通知は下に足し、
 *   古い通知が上へ押し上げられる。下端からの距離は、画面の下端に固定された主操作（3 製品のチャットの入力欄と送信の行。
 *   #2008 の前は desktop の画面の下端から約 6.7rem まで）と、ページの末尾の操作（スクロールしきると画面の下端から約 7rem まで）の
 *   上に出るように決めた共通の定数で、製品・画面では変えない。ページの操作（PageHeader の右上）は上端にあるので覆わない。
 *   画面の下端に固定された入力欄（チャット。質問の入力欄を 3.5 行にした #2008 の後は下端から約 10rem）がこの高さを
 *   超えるときは、入力欄の上端から 1rem 上に持ち上げる（#2103。`--toast-clear-bottom` は `ChatLayout` が
 *   `useToastClearance` で書く）。右下の角に出すことと、ページの内容（PageHeader の操作・スクロール）で動かないことは変えない。
 * - md 未満（`top-bar`）: 上端の全幅。上端のバー（メニューのボタンと製品名）に重ね、メニューのボタン（左の 0.5rem + 44px）の
 *   後ろから右端の 1rem まで。375px では画面の下端が親指の届く範囲で、全幅のページの末尾の操作・チャットの入力欄・
 *   ソフトウェアキーボードが来るため、下端には置かない。上端のバーにはメニューのボタンのほかに操作が無い。バーを超える通知の高さを AppShell が確保し、本文の操作を覆わない。通知が多い場合は40dvhを上限に通知の中をスクロールする。
 *
 * 幅（#899）: md 以上は内容に合わせて広がり、`--toast-width-min`（22rem）〜`--toast-width-max`（32rem）の間で決まる
 * （画面がそれより狭ければ画面の幅まで）。md 未満は左右の間隔の内側いっぱい。
 */

/** 画面の端との間隔。 */
const EDGE = "1rem";
/** md 以上の下端からの距離（`tokens/spacing.css`。#1728）。 */
const OFFSET_BOTTOM = "var(--toast-offset-bottom)";
/** md 未満の上端の間隔（上端のバーに重ねる）。 */
const NARROW_TOP = "0.5rem";
/**
 * md 未満で、上端のバーのメニューのボタンを覆わないための左の間隔。
 * ボタンはバーの左の余白（`px-2` = 0.5rem）の後ろに 44px（`--control-height-touch`）。その後ろに 0.5rem 空ける。
 */
const NARROW_LEFT = "calc(1rem + var(--control-height-touch))";
/** md 以上の幅の下限と上限（`tokens/spacing.css`。#899）。この間で内容に合わせて広がる。 */
const MIN_WIDTH = "var(--toast-width-min)";
const MAX_WIDTH = "var(--toast-width-max)";

export type ToastPlacementMode = "bottom-right" | "top-bar";

export interface ToastPlacementInput {
  /** md 未満（`NAV_DRAWER_QUERY`）。 */
  narrow: boolean;
}

export interface ToastPlacement {
  mode: ToastPlacementMode;
  style: {
    top?: string;
    bottom?: string;
    left?: string;
    right: string;
    /** md 以上だけ。内容に合わせた幅（shrink-to-fit）の下限・上限。md 未満は left / right で画面の幅に広げる。 */
    minWidth?: string;
    maxWidth?: string;
    maxHeight: string;
    /** 登場の向き（`toast-in` の始まりの縦のずれ）。下端の通知は下から、上端の通知は上から現れる。 */
    "--toast-enter-y": string;
  };
}

const SAFE_RIGHT = `max(${EDGE}, env(safe-area-inset-right))`;
/** 下端に固定された入力欄の上端から画面の下端までの高さ（`useToastClearance`。#2103）。無ければ 0。 */
const CLEAR_BOTTOM = `calc(var(--toast-clear-bottom, 0px) + ${EDGE})`;
const SAFE_BOTTOM = `max(${OFFSET_BOTTOM}, ${CLEAR_BOTTOM}, env(safe-area-inset-bottom))`;
const SAFE_NARROW_TOP = `max(${NARROW_TOP}, env(safe-area-inset-top))`;
const AVAILABLE_WIDTH = `calc(100vw - 2 * ${EDGE})`;

/** 画面幅ごとの決まった置き場所。ページの内容は読まない。 */
export function resolveToastPlacement({ narrow }: ToastPlacementInput): ToastPlacement {
  if (narrow) {
    return {
      mode: "top-bar",
      style: {
        top: SAFE_NARROW_TOP,
        left: NARROW_LEFT,
        right: SAFE_RIGHT,
        maxHeight: `min(var(--toast-max-height-narrow), calc(100dvh - ${SAFE_NARROW_TOP} - ${EDGE}))`,
        "--toast-enter-y": "-0.5rem",
      },
    };
  }
  return {
    mode: "bottom-right",
    style: {
      bottom: SAFE_BOTTOM,
      right: SAFE_RIGHT,
      minWidth: `min(${MIN_WIDTH}, ${AVAILABLE_WIDTH})`,
      maxWidth: `min(${MAX_WIDTH}, ${AVAILABLE_WIDTH})`,
      maxHeight: `calc(100dvh - ${SAFE_BOTTOM} - ${EDGE})`,
      "--toast-enter-y": "0.5rem",
    },
  };
}
