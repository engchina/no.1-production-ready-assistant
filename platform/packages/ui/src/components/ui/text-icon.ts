/**
 * 文字の横のアイコンの大きさ（px。#2371）。文字の大きさの token（`--font-size-xs` 12px / `--font-size-sm` 14px）に
 * そろえる。置き場所は `.pr-icon-line`（文字の 1 行目の行の高さの箱の縦の中央）。
 *
 * 開閉の印・処理の経過の段の状態・補足の行の印など、文字に添えるアイコンに使う。状態を知らせる `Banner` / `Toast` /
 * `FormStatus` のアイコンと `Button` のアイコンは、目立たせるため `--icon-md`（16px）のまま（README §3「文字の横のアイコン」）。
 */
export const TEXT_ICON_SIZE = { xs: 12, sm: 14 } as const satisfies Record<"xs" | "sm", number>;

export type TextIconSize = keyof typeof TEXT_ICON_SIZE;
