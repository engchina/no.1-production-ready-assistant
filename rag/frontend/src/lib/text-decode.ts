/**
 * テキスト原本プレビューの文字コード処理。
 *
 * Response.text() は仕様上常に UTF-8 でデコードするため、非 UTF-8 テキスト
 * (Shift_JIS / EUC-JP / GB18030 等) が文字化けする。サーバが Content-Type の
 * charset で検出結果を返すので、それに従って arrayBuffer を TextDecoder で
 * デコードする。
 */

/** Content-Type ヘッダから charset を取り出す（既定は utf-8）。 */
export function charsetFromContentType(contentType: string | null): string {
  const match = /charset=([^;]+)/i.exec(contentType ?? "");
  return match?.[1]?.trim().replace(/^["']|["']$/g, "") || "utf-8";
}

/** 原本 bytes を charset に従ってデコードする。未知ラベルは UTF-8 にフォールバック。 */
export function decodeText(buffer: ArrayBuffer, charset: string): string {
  try {
    return new TextDecoder(charset).decode(buffer);
  } catch {
    return new TextDecoder("utf-8").decode(buffer);
  }
}
