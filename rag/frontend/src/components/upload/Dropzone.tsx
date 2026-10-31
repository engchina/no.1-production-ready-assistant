"use client";

import { UploadCloud } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";

import { cn } from "@/lib/utils";
import { t } from "@/lib/i18n";
import { DEFAULT_MAX_UPLOAD_BYTES, formatByteSize } from "@/lib/upload-requests";
import { ACCEPTED_UPLOAD_EXTENSION_LABELS, ACCEPTED_UPLOAD_TYPES } from "@/lib/upload-selection";

/** ドラッグ＆ドロップ + クリック選択のファイル入力。 */
export function Dropzone({
  onFiles,
  disabled,
  maxUploadBytes = DEFAULT_MAX_UPLOAD_BYTES,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  /** 1 ファイルの上限（upload-storage 設定の `max_upload_bytes`）。案内に表示する。 */
  maxUploadBytes?: number;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    if (disabled) return;
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length) onFiles(files);
  };

  return (
    <div
      data-surface-tint={dragOver ? "info" : undefined}
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onClick={() => !disabled && inputRef.current?.click()}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
      className={cn(
        "flex min-h-52 cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed bg-surface px-4 py-6 text-center transition-colors",
        dragOver ? "border-accent-emphasis bg-info-subtle" : "border-border hover:border-accent-emphasis",
        disabled && "cursor-not-allowed opacity-60"
      )}
    >
      <UploadCloud size={24} className="text-accent-fg" aria-hidden />
      <p className="text-sm font-medium text-fg">{t("upload.dropzone")}</p>
      <p className="text-xs text-fg-muted">{t("upload.dropzoneHint", { size: formatByteSize(maxUploadBytes) })}</p>
      <p className="max-w-2xl text-xs text-fg-muted">
        {t("upload.dropzoneFormats", { formats: ACCEPTED_UPLOAD_EXTENSION_LABELS.join(", ") })}
      </p>
      <p className="max-w-2xl text-xs text-fg-muted" data-testid="upload-dropzone-unsupported">
        {t("upload.dropzoneUnsupported")}
      </p>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        accept={ACCEPTED_UPLOAD_TYPES}
        disabled={disabled}
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          if (files.length) onFiles(files);
          e.target.value = "";
        }}
      />
    </div>
  );
}
