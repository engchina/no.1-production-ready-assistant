/**
 * 抽出本文 / chunk テキストから埋め込みバイナリ(base64)を分離する。
 *
 * parser が `element.text` / `chunk.text` / `raw_text` / table cell に
 * `data:image/...;base64,...`(および分割で生じた prefix 無し base64 断片)を
 * 流し込むことがあり、そのまま描画すると本文が判読不能になる。表示前に
 * テキストとメディアへ分け、メディアはサムネイル/チップへ畳む。
 */

export interface ExtractedMedia {
  kind: "image" | "blob";
  /** image の場合のみ。`<img src>` に使える完全な data URI。 */
  src?: string;
  /** data URI 由来の mime。bare 断片では undefined。 */
  mime?: string;
  /** 概算バイト数(base64 長 × 3/4)。 */
  approxBytes: number;
  /** data URI が分割で途中から切れた bare base64 断片か。 */
  fragment: boolean;
}

export interface SplitExtractedText {
  text: string;
  media: ExtractedMedia[];
}

/** `<img>` で安全に描画できるラスタ画像 mime。svg は除外(描画しない=安全側)。 */
const RASTER_IMAGE = /^image\/(png|jpe?g|gif|webp)$/i;

/** `data:<mime>;base64,<payload>` 形式。 */
const DATA_URI = /data:([\w.+-]+\/[\w.+-]+);base64,([A-Za-z0-9+/]+={0,2})/g;

// ponytail: 200 文字閾値の素朴ヒューリスティック。誤検出(長い hash/JWT 等)が
// 問題化したら mime/文脈判定へ上げる。
/** prefix の無い長い base64 断片(分割で生じた中間チャンク)。 */
const BARE_BASE64 = /[A-Za-z0-9+/]{200,}={0,2}/g;

function approxBytesFromBase64(payload: string): number {
  const clean = payload.replace(/=+$/, "");
  return Math.floor((clean.length * 3) / 4);
}

/** 表示用にテキストと埋め込みメディアを分離する。 */
export function splitExtractedText(raw: string): SplitExtractedText {
  if (!raw) return { text: "", media: [] };
  const media: ExtractedMedia[] = [];

  // 1. data URI を抽出し、本文からは除去する。
  let working = raw.replace(DATA_URI, (match, mime: string, payload: string) => {
    const approxBytes = approxBytesFromBase64(payload);
    if (RASTER_IMAGE.test(mime)) {
      media.push({ kind: "image", src: match, mime, approxBytes, fragment: false });
    } else {
      media.push({ kind: "blob", mime, approxBytes, fragment: false });
    }
    return " ";
  });

  // 2. data URI で消えなかった bare base64 断片を畳む。
  working = working.replace(BARE_BASE64, (match: string) => {
    media.push({ kind: "blob", approxBytes: approxBytesFromBase64(match), fragment: true });
    return " ";
  });

  const text = working.replace(/[ \t]{2,}/g, " ").trim();
  return { text, media };
}
