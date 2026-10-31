import type { BatchUploadFailedItem, BatchUploadResult, UploadTransferProgress } from "./api";

/**
 * 文書アップロードの送信計画（#280）。
 *
 * backend の上限は「1 ファイル」あたり（`RAG_MAX_UPLOAD_BYTES`、既定 200 MiB）だが、前段の Nginx の
 * `client_max_body_size`（`init_script.sh` が `RAG_MAX_UPLOAD_BYTES` + multipart の余白から作る。#306）は
 * 「1 リクエスト」あたりに効く。複数ファイルを 1 リクエストで送ると、1 件ずつは上限内でも合計が
 * その値を超えた時点で一括アップロード全体が 413 になる。
 * そこで、1 リクエストの合計を 1 ファイルの上限以内に収めるよう分けて送り、上限を超えるファイルは
 * 送る前に失敗として扱う（大きなファイルを最後まで送ってから拒否されるのを避ける）。
 */

/** upload-storage 設定を取得できないときの上限（backend の既定値と同じ）。 */
export const DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

/** 1 リクエストに含めるファイル数の上限（multipart の解析上限 1000 より十分小さくする）。 */
export const MAX_FILES_PER_UPLOAD_REQUEST = 50;

type SizedFile = Pick<File, "size">;

/** バイト数の表示（上限・送信済みの量。200 MiB → "200 MB"。整数にならないときだけ小数 1 桁）。 */
export function formatByteSize(bytes: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = Number.isInteger(value) ? String(value) : value.toFixed(1);
  return `${text} ${units[unit]}`;
}

/**
 * 選択したファイルを、1 リクエストの合計が `maxBytes` 以内になるまとまり（選択した順を保つ）へ分ける。
 * 1 ファイルで `maxBytes` を超えるものは `oversized` に分け、送らない。
 */
export function planUploadRequests<T extends SizedFile>(
  files: readonly T[],
  maxBytes: number,
  maxFilesPerRequest: number = MAX_FILES_PER_UPLOAD_REQUEST,
): { groups: T[][]; oversized: T[] } {
  const limit = Number.isFinite(maxBytes) && maxBytes > 0 ? maxBytes : DEFAULT_MAX_UPLOAD_BYTES;
  const groups: T[][] = [];
  const oversized: T[] = [];
  let current: T[] = [];
  let currentBytes = 0;
  for (const file of files) {
    if (file.size > limit) {
      oversized.push(file);
      continue;
    }
    if (
      current.length > 0 &&
      (currentBytes + file.size > limit || current.length >= maxFilesPerRequest)
    ) {
      groups.push(current);
      current = [];
      currentBytes = 0;
    }
    current.push(file);
    currentBytes += file.size;
  }
  if (current.length > 0) groups.push(current);
  return { groups, oversized };
}

/** 送らずに失敗とするファイルの結果（backend の 413 と同じ形）。 */
export function failedUploadItem(
  file: Pick<File, "name">,
  statusCode: number,
  message: string,
): BatchUploadFailedItem {
  return { file_name: file.name, status_code: statusCode, message, source_profile: null };
}

/** 分けて送った結果と、送らずに失敗としたファイルを 1 つの一括アップロード結果にまとめる。 */
export function mergeBatchUploadResults(
  results: readonly BatchUploadResult[],
  extraFailedItems: readonly BatchUploadFailedItem[] = [],
): BatchUploadResult {
  const items = results.flatMap((result) => result.items);
  const failedItems = [...results.flatMap((result) => result.failed_items), ...extraFailedItems];
  return {
    items,
    failed_items: failedItems,
    total_count: items.length + failedItems.length,
    uploaded_count: items.length,
    failed_count: failedItems.length,
  };
}

/**
 * 送信の進み具合（#306）。選択したファイルのバイト数で数える（multipart の境界などの付加分は含めない）。
 * 上限を超えて送らないファイルは含めない。
 */
export interface UploadProgress {
  sentBytes: number;
  totalBytes: number;
}

/** 送信するファイルの合計バイト数。 */
export function totalUploadBytes(files: readonly SizedFile[]): number {
  return files.reduce((sum, file) => sum + file.size, 0);
}

/**
 * 1 リクエストの送信済みの割合（XHR の `loaded / total`。本文全体に対する値）を、ファイルのバイト数へ
 * 換算して全体の進み具合にする。`doneBytes` はそれまでに送り終えたまとまりのバイト数。
 */
export function uploadProgressOf(
  doneBytes: number,
  requestBytes: number,
  transfer: UploadTransferProgress,
  totalBytes: number,
): UploadProgress {
  const ratio = transfer.total > 0 ? Math.min(1, Math.max(0, transfer.loaded / transfer.total)) : 0;
  const sentBytes = Math.min(totalBytes, doneBytes + Math.round(requestBytes * ratio));
  return { sentBytes, totalBytes };
}

/** 送信済みの割合（0〜100 の整数。合計が 0 のときは 0）。100 は全体を送り終えたときだけ。 */
export function uploadProgressPercent(progress: UploadProgress): number {
  if (progress.totalBytes <= 0) return 0;
  if (progress.sentBytes >= progress.totalBytes) return 100;
  return Math.min(99, Math.floor((progress.sentBytes / progress.totalBytes) * 100));
}

/** ファイルごとの送信の状態（#306）。 */
export type UploadFileSendState = "waiting" | "sending" | "sent";

/** ファイルごとの送信の進み具合（#306）。 */
export interface UploadFileProgress {
  name: string;
  sentBytes: number;
  totalBytes: number;
  state: UploadFileSendState;
}

/**
 * 全体の送信済みバイト数を、送る順に並べたファイルへ割り振る（#306）。
 *
 * multipart の本文はファイルを並べた順に送るため、全体の送信済みバイト数を先頭のファイルから順に
 * 埋めていけば、各ファイルの送信済みの量になる（境界などの付加分は全体の進み具合と同じく含めない）。
 * `files` は送る順（一括アップロードでは分けたまとまりを順に連ねたもの）で、上限を超えて送らない
 * ファイルは含めない。
 */
export function uploadFileProgresses(
  files: readonly Pick<File, "name" | "size">[],
  sentBytes: number,
): UploadFileProgress[] {
  const totalBytes = totalUploadBytes(files);
  let offset = 0;
  return files.map((file) => {
    const start = offset;
    offset += file.size;
    const sent = Math.min(file.size, Math.max(0, sentBytes - start));
    // 0 バイトのファイルは、後ろのファイルを送り始めたか全体を送り終えたときに送信済みとする。
    const finished =
      sentBytes >= start + file.size &&
      (file.size > 0 || sentBytes > start || sentBytes >= totalBytes);
    const state: UploadFileSendState = finished ? "sent" : sentBytes > start ? "sending" : "waiting";
    return { name: file.name, sentBytes: sent, totalBytes: file.size, state };
  });
}
