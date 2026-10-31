import type { CSSProperties, SVGProps } from "react";

import { cn } from "../../lib/utils";

export interface SpinnerProps extends Omit<SVGProps<SVGSVGElement>, "width" | "height" | "style"> {
  /** アイコン寸法(px)。周囲のアイコンと揃える(14 / 16 / 20 / 24)。既定は 16px。 */
  size?: number;
  /** 外側の箱(`span.pr-spinner`)の style。寸法は `size` が決める。 */
  style?: CSSProperties;
}

/** 線の実寸(px)。大きさによらず 2px にそろえる(#395)。 */
export const SPINNER_STROKE_PX = 2;

const VIEWBOX = 24;
/** 線の外縁の半径(viewBox 単位)。線を太くしても外形(直径 20/24)を変えない。 */
const OUTER_RADIUS = 10;

const round = (value: number) => Math.round(value * 1000) / 1000;

/**
 * 大きさごとの線幅(viewBox 単位)と円の半径。
 *
 * 旧実装は viewBox 24 に `strokeWidth={2}` 固定で、16px では実寸 1.33px、14px では 1.17px まで細り、
 * 等倍の画面ではアンチエイリアスで線がかすれていた。線の実寸を 2px に固定する(小さいほど viewBox 上の比率を
 * 太くする光学補正。GitHub Primer・Fluent 2 の 16〜28px のスピナーも 2px)。外縁の半径は固定し、太くした分だけ
 * 円の半径を内側へ寄せる(大きさを変えても外形は同じ)。
 */
export function spinnerGeometry(size: number) {
  const strokeWidth = round((SPINNER_STROKE_PX * VIEWBOX) / size);
  return { strokeWidth, radius: round(OUTER_RADIUS - strokeWidth / 2) };
}

/**
 * 2 本のアーク（90 度 × 2。12 時→3 時と、180 度回した 6 時→9 時）の `d`。
 * 180 度の回転対称なので、アーク（濃い筆画）の見た目の重心は常に図形の中心にある。
 */
export function spinnerArcPath(radius: number) {
  const c = VIEWBOX / 2;
  const r = round(radius);
  return `M${c} ${round(c - r)}a${r} ${r} 0 0 1 ${r} ${r}M${c} ${round(c + r)}a${r} ${r} 0 0 1-${r}-${r}`;
}

/**
 * 読み込み中スピナー（処理中を示す回転アイコンは 3 製品でこれ 1 つ。#395 / #1180）。
 *
 * - **形**: 全周のトラックの上を、180 度対称の 2 本のアーク（90 度 × 2）が回る。
 *   lucide の `Loader2`（288 度の欠けた円弧）や旧形（270 度の 1 本のアーク）は、濃い筆画の重心が中心から外れ、
 *   回転するとその重心が中心のまわりを回るため「上下・左右に揺れている」ように見えた（16px で 1.8px。#1180）。
 *   対称の 2 本なら重心は回転角によらず中心に残る（NL2SQL の旧 `StableLoadingIcon` と同じ考え方）。
 * - **箱**: 回転しない固定の正方形（`span.pr-spinner`。`base.css`）の中で、内側の svg だけを回す。箱は
 *   寸法・レイアウト・描画を閉じ込める（`contain: strict`）ので、回転した正方形の外接矩形（最大 1.41 倍）が
 *   周りの行の位置・高さ・スクロールの領域に入らない。`className`（色など）は箱に付け、svg は `currentColor` で受ける。
 * - トラックの色は `--color-spinner-track`(`currentColor` をライト 30% / ダーク 35% で透かす)。
 *   アークとトラックの境目の 3:1 を保つ上限の濃さ(README §4「Spinner」)。
 * - 回転は `transform` だけ(合成スレッドで回るので、メインスレッドが詰まっても止まらない)。等速(linear)。
 * - `prefers-reduced-motion` でも回転を止めない(#440。`base.css`)。その場で回るだけの小さな動きで処理中を伝える
 *   本質的な表示のため(OS 標準の処理中表示も止まらない)。止めると処理が固まったように見える。
 */
export function Spinner({ size = 16, className, style, ...props }: SpinnerProps) {
  const { strokeWidth, radius } = spinnerGeometry(size);
  const center = VIEWBOX / 2;
  return (
    <span
      className={cn("pr-spinner", className)}
      style={{ ...style, width: size, height: size }}
      aria-hidden="true"
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${VIEWBOX} ${VIEWBOX}`}
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        aria-hidden="true"
        className="animate-spin"
        {...props}
      >
        {/* シルエットを一定に保つトラック(全周) */}
        <circle className="pr-spinner-track" cx={center} cy={center} r={radius} />
        {/* 回転を知覚させる 2 本のアーク(180 度対称。重心が中心から動かない) */}
        <path className="pr-spinner-arc" d={spinnerArcPath(radius)} strokeLinecap="round" />
      </svg>
    </span>
  );
}
