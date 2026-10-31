/** Blob をブラウザーのダウンロードとして開始する。 */
export function downloadBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Content-Disposition から安全な basename を取り出す。 */
export function downloadFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const quoted = disposition.match(/filename="([^"]+)"/i)?.[1];
  const plain = disposition.match(/filename=([^;\s]+)/i)?.[1];
  let candidate = quoted ?? plain ?? fallback;
  if (encoded) {
    try {
      candidate = decodeURIComponent(encoded);
    } catch {
      candidate = fallback;
    }
  }
  const basename = candidate.split(/[\\/]/).pop()?.replace(/[\r\n]/g, "").trim();
  return basename || fallback;
}
