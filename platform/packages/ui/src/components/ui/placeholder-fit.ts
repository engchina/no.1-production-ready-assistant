/*
 * placeholder の切れの検出（#1891）。
 *
 * placeholder は欄の幅に収める（収まらないときは短い例にし、説明は欄の下の補足 `helper` に移す）。部品は欄の内側の
 * 幅（padding・後置スロットを除く）と placeholder の文字の幅を比べ、収まらないときは
 * - `data-placeholder-clipped` を付ける（e2e の共通の検査 `findClippedPlaceholders` と開発者の目印）
 * - `title` に placeholder の全文を入れる（ホバーで全文を読める。`title` を渡した欄は上書きしない）
 * を行う。直すのは文言（短い例 + helper）で、`title` は直すまでの補い。
 *
 * 文字の幅は canvas の measureText で、欄の computed の font で測る（DOM を足さない）。jsdom など、
 * 幅を測れない環境（clientWidth が 0）では何もしない。
 */
import { type Ref, useCallback, useLayoutEffect, useState } from "react";

let measureContext: CanvasRenderingContext2D | null | undefined;

function canvasContext(): CanvasRenderingContext2D | null {
  if (measureContext !== undefined) return measureContext;
  try {
    measureContext = typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d");
  } catch {
    measureContext = null;
  }
  return measureContext;
}

export interface PlaceholderFitInput {
  /** placeholder の各行の文字の幅（px。改行で分けた行ごと）。 */
  lineWidths: readonly number[];
  /** 欄の内側の幅（px。padding を除く）。 */
  availableWidth: number;
  /** 複数行の欄（textarea）か。 */
  multiline: boolean;
  /** 1 行の高さ（px。multiline のとき）。 */
  lineHeight?: number;
  /** 欄の内側の高さ（px。padding を除く。multiline のとき）。 */
  availableHeight?: number;
}

/**
 * placeholder が欄に収まらないか（1px は丸めの許容）。1 行の欄は幅、複数行の欄は折り返した行の高さで判定する。
 */
export function placeholderOverflows({
  lineWidths,
  availableWidth,
  multiline,
  lineHeight = 0,
  availableHeight = 0,
}: PlaceholderFitInput): boolean {
  if (availableWidth <= 0 || lineWidths.length === 0) return false;
  if (!multiline) return Math.max(...lineWidths) > availableWidth + 1;
  if (lineHeight <= 0 || availableHeight <= 0) return false;
  const lines = lineWidths.reduce((sum, width) => sum + Math.max(1, Math.ceil((width - 1) / availableWidth)), 0);
  return lines * lineHeight > availableHeight + 1;
}

function px(value: string) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** 欄の placeholder が収まらないかを測る（測れない環境では false）。 */
export function measurePlaceholderClip(element: HTMLInputElement | HTMLTextAreaElement): boolean {
  const placeholder = element.placeholder;
  if (!placeholder || element.clientWidth <= 0) return false;
  const style = getComputedStyle(element);
  const availableWidth = element.clientWidth - px(style.paddingLeft) - px(style.paddingRight);
  if (availableWidth <= 0) return false;
  const context = canvasContext();
  if (!context) return false;
  context.font = style.font || `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const lineWidths = placeholder.split("\n").map((line) => context.measureText(line).width);
  const multiline = element instanceof HTMLTextAreaElement;
  const lineHeight = px(style.lineHeight) || px(style.fontSize) * 1.5;
  return placeholderOverflows({
    lineWidths,
    availableWidth,
    multiline,
    lineHeight,
    availableHeight: element.clientHeight - px(style.paddingTop) - px(style.paddingBottom),
  });
}

/**
 * 欄の placeholder の切れを見張る（TextField・SearchField・TextareaField が使う）。
 * 返す `ref` を欄に付け、`clipped` を `data-placeholder-clipped`、`title` を欄の `title` に渡す。
 */
export function usePlaceholderFit<T extends HTMLInputElement | HTMLTextAreaElement>(
  externalRef: Ref<T> | undefined,
  placeholder: string | undefined,
  title: string | undefined
) {
  const [node, setNode] = useState<T | null>(null);
  const [clipped, setClipped] = useState(false);
  const ref = useCallback(
    (element: T | null) => {
      setNode(element);
      if (typeof externalRef === "function") externalRef(element);
      else if (externalRef) (externalRef as { current: T | null }).current = element;
    },
    [externalRef]
  );

  useLayoutEffect(() => {
    if (!node || !placeholder) {
      setClipped(false);
      return undefined;
    }
    let cancelled = false;
    const measure = () => {
      if (!cancelled) setClipped(measurePlaceholderClip(node));
    };
    measure();
    // Web フォントの読み込みの後に測り直す（読み込む前は代わりの書体の幅）。
    void document.fonts?.ready.then(measure);
    if (typeof ResizeObserver === "undefined") {
      return () => {
        cancelled = true;
      };
    }
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => {
      cancelled = true;
      observer.disconnect();
    };
  }, [node, placeholder]);

  return { ref, clipped, title: title ?? (clipped ? placeholder : undefined) };
}
