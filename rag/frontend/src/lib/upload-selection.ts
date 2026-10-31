/** 文書アップロードで送る前に選んでおくファイルの扱い（#701）。 */

/** ファイル選択の `accept` と、送る前の形式の確認に使う拡張子・MIME type。 */
export const ACCEPTED_UPLOAD_TYPES = [
  ".pdf",
  ".gif",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".tif",
  ".tiff",
  ".txt",
  ".md",
  ".markdown",
  ".rst",
  ".org",
  ".csv",
  ".tsv",
  ".json",
  ".jsonl",
  ".xml",
  ".ndjson",
  ".html",
  ".htm",
  ".xhtml",
  ".eml",
  ".msg",
  ".doc",
  ".docx",
  ".docm",
  ".ppt",
  ".pptx",
  ".pptm",
  ".xls",
  ".xlsx",
  ".xlsm",
  ".odt",
  ".rtf",
  "application/pdf",
  "image/gif",
  "image/jpeg",
  "image/jpg",
  "image/png",
  "image/webp",
  "image/tif",
  "image/tiff",
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/tab-separated-values",
  "text/x-rst",
  "text/prs.fallenstein.rst",
  "text/org",
  "text/x-org",
  "text/html",
  "application/xhtml+xml",
  "application/json",
  "application/jsonl",
  "application/jsonlines",
  "application/ndjson",
  "application/xml",
  "text/xml",
  "application/csv",
  "application/x-ndjson",
  "message/rfc822",
  "application/eml",
  "application/vnd.ms-outlook",
  "application/x-msg",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-word.document.macroEnabled.12",
  "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
  "application/vnd.ms-excel.sheet.macroEnabled.12",
  "application/vnd.oasis.opendocument.text",
  "application/rtf",
  "application/x-rtf",
  "text/rtf",
].join(",");

const ACCEPTED_EXTENSIONS = new Set(
  ACCEPTED_UPLOAD_TYPES.split(",").filter((item) => item.startsWith("."))
);

/** 案内に出す拡張子（ドット無し・大文字）。 */
export const ACCEPTED_UPLOAD_EXTENSION_LABELS = [...ACCEPTED_EXTENSIONS].map((ext) =>
  ext.slice(1).toUpperCase()
);

/**
 * 受け付けない形式のうち、変換の方法を案内する形式（#3351）。解析に要る依存（HEIC / HEIF は pi-heif の
 * libheif・EPUB は pandoc）をライセンスの理由で入れないため、送らずに止める。音声・動画は取り込まない
 * 形式で、同じく送らずに止める（#3369）。backend の `app.rag.source_profile.REJECTED_UPLOAD_EXTENSIONS`
 * と同じ拡張子（backend のテストで照合する）。
 */
export const REJECTED_UPLOAD_FORMATS = {
  ".heic": "heicImage",
  ".heif": "heicImage",
  ".epub": "epubBook",
  ".aac": "audioVideo",
  ".aif": "audioVideo",
  ".aiff": "audioVideo",
  ".amr": "audioVideo",
  ".flac": "audioVideo",
  ".m4a": "audioVideo",
  ".mp3": "audioVideo",
  ".oga": "audioVideo",
  ".ogg": "audioVideo",
  ".opus": "audioVideo",
  ".wav": "audioVideo",
  ".weba": "audioVideo",
  ".wma": "audioVideo",
  ".3gp": "audioVideo",
  ".avi": "audioVideo",
  ".m4v": "audioVideo",
  ".mkv": "audioVideo",
  ".mov": "audioVideo",
  ".mp4": "audioVideo",
  ".mpeg": "audioVideo",
  ".mpg": "audioVideo",
  ".ogv": "audioVideo",
  ".webm": "audioVideo",
  ".wmv": "audioVideo",
} as const;

export type RejectedUploadFormat = (typeof REJECTED_UPLOAD_FORMATS)[keyof typeof REJECTED_UPLOAD_FORMATS];

export type UploadFileProblem = "unsupported" | "tooLarge" | "empty" | RejectedUploadFormat;

function isRejectedUploadExtension(extension: string): extension is keyof typeof REJECTED_UPLOAD_FORMATS {
  return Object.hasOwn(REJECTED_UPLOAD_FORMATS, extension);
}

/** 送れないファイルの理由。送れるなら null。ドロップしたファイルは `accept` を通らないので、ここで確かめる。 */
export function uploadFileProblem(
  file: Pick<File, "name" | "size">,
  maxUploadBytes: number
): UploadFileProblem | null {
  const dot = file.name.lastIndexOf(".");
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (isRejectedUploadExtension(extension)) return REJECTED_UPLOAD_FORMATS[extension];
  if (!ACCEPTED_EXTENSIONS.has(extension)) return "unsupported";
  if (file.size === 0) return "empty";
  if (file.size > maxUploadBytes) return "tooLarge";
  return null;
}

function fileKey(file: Pick<File, "name" | "size" | "lastModified">): string {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
}

/** 選んだファイルに足す。同じファイル（名前・サイズ・更新日時が同じ）は重ねない。 */
export function mergeUploadSelection(current: readonly File[], added: readonly File[]): File[] {
  const seen = new Set(current.map(fileKey));
  const merged = [...current];
  for (const file of added) {
    const key = fileKey(file);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(file);
  }
  return merged;
}
