import { Fragment } from "react";

import { cn } from "../../lib/utils";

/** 句読点優先で折り返すメッセージ本文。 */
export interface MessageTextProps {
  text: string;
  className?: string;
}

export interface MessageTextSegment {
  text: string;
  separatorBefore: string;
}

/** 全角の文末の記号。続く閉じ括弧・引用符までを 1 つの文末にする。 */
const FULLWIDTH_TERMINATORS = new Set(["。", "！", "？", "；"]);
const OPENERS = new Set(["「", "『", "（", "【", "〔", "［", "〈", "《", "(", "[", "{"]);
const FULLWIDTH_CLOSERS = new Set(["」", "』", "】", "）", ")", "]", '"', "'", "”", "’"]);
/** 半角の文末の記号。空白か本文の末尾が続くときだけ文末にする（`3.14`・`a!b` は切らない）。 */
const ASCII_TERMINATORS = new Set([".", "!", "?"]);
const ASCII_CLOSERS = new Set([")", "]", '"', "'", "”", "’"]);

/**
 * 通知本文は prose として扱うため、改行・タブ・連続空白を 1 つの空白へ正規化する。
 * 構造化された複数行コンテンツは string ではなく ReactNode で各コンポーネントへ渡す。
 */
export function normalizeMessageText(text: string): string {
  return text.replace(/\s+/gu, " ").trim();
}

function skipWhile(text: string, start: number, chars: ReadonlySet<string>): number {
  let index = start;
  while (index < text.length && chars.has(text[index] ?? "")) index += 1;
  return index;
}

/**
 * 句読点の境界を 1 回の走査で求める。正規表現（`[.!?]+…(?=\s|$)`）は `!` の連続で
 * 開始位置ごとに読み直して O(n²) になるため、記号の連続の先頭からだけ読む。
 */
function collectFallbackBoundaries(text: string): number[] {
  const boundaries: number[] = [];
  let index = 0;
  while (index < text.length) {
    const char = text[index] ?? "";
    if (FULLWIDTH_TERMINATORS.has(char)) {
      const end = skipWhile(text, skipWhile(text, index, FULLWIDTH_TERMINATORS), FULLWIDTH_CLOSERS);
      boundaries.push(end);
      index = end;
    } else if (ASCII_TERMINATORS.has(char)) {
      const end = skipWhile(text, skipWhile(text, index, ASCII_TERMINATORS), ASCII_CLOSERS);
      if (end === text.length || /\s/u.test(text[end] ?? "")) boundaries.push(end);
      index = end;
    } else {
      index += 1;
    }
  }
  return boundaries;
}

function collectIntlBoundaries(text: string, locale: string): number[] {
  if (!("Segmenter" in Intl)) return [];
  try {
    const segmenter = new Intl.Segmenter(locale, { granularity: "sentence" });
    const boundaries: number[] = [];
    for (const item of segmenter.segment(text)) {
      let end = item.index + item.segment.length;
      while (end > item.index && /\s/u.test(text[end - 1] ?? "")) end -= 1;
      if (end > item.index && end < text.length) boundaries.push(end);
    }
    return boundaries;
  } catch {
    return [];
  }
}

/**
 * 文末単位へ分割する。Intl.Segmenter の結果へ決定論的 fallback 境界を重ね、
 * 全角セミコロンや Segmenter 非対応環境でも同じ折返し機会を作る。
 */
export function segmentMessageText(text: string, locale = "ja"): MessageTextSegment[] {
  const normalized = normalizeMessageText(text);
  if (!normalized) return [];

  const boundaries = Array.from(
    new Set([
      ...collectIntlBoundaries(normalized, locale),
      ...collectFallbackBoundaries(normalized),
      normalized.length,
    ].map((boundary) => {
      if (boundary === normalized.length) return boundary;
      let start = boundary;
      let opening = false;
      while (start > 0 && (OPENERS.has(normalized[start - 1] ?? "") || normalized[start - 1] === "\u2060")) {
        opening ||= OPENERS.has(normalized[start - 1] ?? "");
        start -= 1;
      }
      return opening ? start : boundary;
    }))
  ).sort((left, right) => left - right);

  const segments: MessageTextSegment[] = [];
  let start = 0;
  for (const end of boundaries) {
    if (end <= start) continue;
    const raw = normalized.slice(start, end);
    const leading = raw.match(/^\s+/u)?.[0] ?? "";
    const value = raw.slice(leading.length).trimEnd();
    if (value) segments.push({ text: value, separatorBefore: leading });
    start = end;
  }

  return segments;
}

/**
 * 文全体を atomic inline box として描画し、幅不足時は文末を優先して折り返す。
 * 1 文が 1 行に入らないときは、文の中を文節で折り返す（`.pr-message-text` の `word-break: auto-phrase` /
 * `text-wrap: pretty`。#899）。単独の長い文節・URL は `overflow-wrap: anywhere` で max-width の中で折り返し、
 * 横方向へはみ出さない。
 */
export function MessageText({ text, className }: MessageTextProps) {
  const segments = segmentMessageText(text);
  if (segments.length === 0) return null;

  return (
    <span className={cn("pr-message-text min-w-0 whitespace-normal", className)} data-message-text>
      {segments.map((segment, index) => (
        <Fragment key={`${index}-${segment.text}`}>
          {segment.separatorBefore}
          <span
            className="inline-block max-w-full align-baseline"
            data-message-sentence
          >
            {segment.text}
          </span>
        </Fragment>
      ))}
    </span>
  );
}
