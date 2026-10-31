/**
 * 本文の中から引用（根拠の抜粋）の位置を探す（テキストのプレビューで引用した箇所へ移って強調する。#2512）。
 *
 * 抜粋は本文そのままとは限らない（取り込みで改行・空白・記号・全角半角が変わる、JSON の値だけを並べる、など）。
 * そのため文字と数字だけを NFKC と小文字にそろえて比べ、全体が見つからなければ抜粋を行に分け、本文の中で順に
 * 近くに並ぶ行のまとまりを探す。特定の文書の形には合わせない。見つからなければ null（プレビューは先頭のまま）。
 */

/** 本文の範囲（元の文字列の UTF-16 の位置。end は含まない）。 */
export interface TextQuoteRange {
  start: number;
  end: number;
}

/** 正規化した 1 行が、照合に使える最小の文字数（短い行は本文のあちこちに当たるので使わない）。 */
const MIN_SEGMENT_LENGTH = 4;
/** 行のまとまりの起点に試す行の数（抜粋の先頭から）。 */
const MAX_ANCHORS = 12;
/** 行と行の間に許す本文の文字数（正規化後）の下限。抜粋が長いほど広げる。 */
const MIN_GAP = 64;

const WORD_CHAR = /[\p{L}\p{N}]/u;

interface NormalizedText {
  chars: string;
  /** 正規化した文字ごとの、元の文字列での開始と終了の位置。 */
  starts: number[];
  ends: number[];
}

function normalize(text: string): NormalizedText {
  let chars = "";
  const starts: number[] = [];
  const ends: number[] = [];
  let index = 0;
  for (const codePoint of text) {
    const end = index + codePoint.length;
    for (const char of codePoint.normalize("NFKC").toLowerCase()) {
      if (WORD_CHAR.test(char)) {
        chars += char;
        starts.push(index);
        ends.push(end);
      }
    }
    index = end;
  }
  return { chars, starts, ends };
}

/** 本文の中の引用の範囲。抜粋が短すぎる・見つからないときは null。 */
export function findTextQuote(text: string, quote: string | null | undefined): TextQuoteRange | null {
  if (!quote || !text) return null;
  const whole = normalize(quote).chars;
  if (whole.length < MIN_SEGMENT_LENGTH) return null;
  const body = normalize(text);
  const toRange = (start: number, end: number): TextQuoteRange => ({
    start: body.starts[start],
    end: body.ends[end - 1],
  });

  const exact = body.chars.indexOf(whole);
  if (exact >= 0) return toRange(exact, exact + whole.length);

  const segments = quote
    .split(/\n+/)
    .map((line) => normalize(line).chars)
    .filter((line) => line.length >= MIN_SEGMENT_LENGTH);
  const maxGap = Math.max(MIN_GAP, whole.length);
  let best: { start: number; end: number; matched: number } | null = null;
  for (let anchor = 0; anchor < Math.min(segments.length, MAX_ANCHORS); anchor += 1) {
    const start = body.chars.indexOf(segments[anchor]);
    if (start < 0) continue;
    let end = start + segments[anchor].length;
    let matched = segments[anchor].length;
    for (const segment of segments.slice(anchor + 1)) {
      const found = body.chars.indexOf(segment, end);
      if (found < 0 || found - end > maxGap) continue;
      end = found + segment.length;
      matched += segment.length;
    }
    if (!best || matched > best.matched) best = { start, end, matched };
  }
  return best ? toRange(best.start, best.end) : null;
}
