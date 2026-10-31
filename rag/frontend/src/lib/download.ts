import type { DownloadedFile } from "@/lib/api";

/** 取得したファイルを利用者の端末へ保存させる（#2065。Excel のテンプレートなど）。 */
export function saveDownloadedFile({ blob, fileName }: DownloadedFile): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
