import {
  useEffect,
  useId,
  useRef,
  useState,
  type DragEvent,
} from "react";
import {
  FileSpreadsheet,
  FileText,
  Upload,
} from "lucide-react";

import { t } from "./messages";
import { validateFileDropzoneSelection, type FileDropzoneRejectReason } from "./fileDropzone";
import { cn, ClearActionButton } from "@production-ready/ui";
import {
  FieldError,
  FieldLabel,
  Spinner,
} from "@production-ready/ui";

export type FileDropzoneIcon = "file" | "spreadsheet" | "upload";

const fileDropzoneIcons: Record<FileDropzoneIcon, typeof FileText> = {
  file: FileText,
  spreadsheet: FileSpreadsheet,
  upload: Upload,
};

export interface FileDropzoneProps {
  label: string;
  ariaLabel?: string;
  accept: string;
  formatLabel: string;
  multiple?: boolean;
  selectedText?: string;
  selectedCount?: number;
  actionText?: string;
  activeText?: string;
  replaceText?: string;
  addText?: string;
  loadingText?: string;
  clearText?: string;
  clearAriaLabel?: string;
  hint?: string;
  errorText?: string;
  icon?: FileDropzoneIcon;
  required?: boolean;
  disabled?: boolean;
  loading?: boolean;
  clearDisabled?: boolean;
  resetSignal?: string | number;
  className?: string;
  dataTestId?: string;
  onFiles: (files: File[]) => void | Promise<void>;
  onClear?: () => void;
  onReject?: (reason: FileDropzoneRejectReason) => void;
}

/** 全画面共通のコンパクトなファイル選択 / drag & drop 入力。 */
export function FileDropzone({
  label,
  ariaLabel = label,
  accept,
  formatLabel,
  multiple = false,
  selectedText = "",
  selectedCount = 0,
  actionText = t("common.fileDropzone.action"),
  activeText = t("common.fileDropzone.active"),
  replaceText = t("common.fileDropzone.replace"),
  addText = t("common.fileDropzone.add"),
  loadingText = t("common.fileDropzone.loading"),
  clearText = t("common.fileDropzone.clear"),
  clearAriaLabel,
  hint = "",
  errorText = "",
  icon = "upload",
  required = false,
  disabled = false,
  loading = false,
  clearDisabled,
  resetSignal = 0,
  className,
  dataTestId,
  onFiles,
  onClear,
  onReject,
}: FileDropzoneProps) {
  const inputId = useId();
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const Icon = fileDropzoneIcons[icon];
  const hasSelection = Boolean(selectedText) || selectedCount > 0;
  const interactionDisabled = disabled || loading;
  const clearIsDisabled = clearDisabled ?? !hasSelection;
  const dragDepthRef = useRef(0);
  const [isDragActive, setIsDragActive] = useState(false);
  const [validationError, setValidationError] = useState("");
  const visibleError = errorText || validationError;
  const describedBy = [hint ? hintId : "", visibleError ? errorId : ""]
    .filter(Boolean)
    .join(" ");

  useEffect(() => {
    if (interactionDisabled) {
      dragDepthRef.current = 0;
      setIsDragActive(false);
    }
  }, [interactionDisabled]);

  useEffect(() => {
    dragDepthRef.current = 0;
    setIsDragActive(false);
    setValidationError("");
  }, [resetSignal]);

  const acceptFiles = (files: FileList | File[]) => {
    const candidates = Array.from(files);
    if (candidates.length === 0) return;
    const result = validateFileDropzoneSelection(candidates, { accept, multiple });
    if (!result.accepted) {
      if (onReject) {
        onReject(result.reason);
      } else {
        setValidationError(
          result.reason === "multiple-files"
            ? t("common.fileDropzone.error.multiple", { formats: formatLabel })
            : t("common.fileDropzone.error.unsupported", { formats: formatLabel })
        );
      }
      return;
    }
    setValidationError("");
    void onFiles(result.files);
  };

  const handleDragEnter = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (interactionDisabled) return;
    dragDepthRef.current += 1;
    setIsDragActive(true);
  };

  const handleDragOver = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (!interactionDisabled) event.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (interactionDisabled) return;
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setIsDragActive(false);
  };

  const handleDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = 0;
    setIsDragActive(false);
    if (interactionDisabled) return;
    acceptFiles(event.dataTransfer.files);
  };

  const displayText = loading
    ? loadingText
    : isDragActive
      ? activeText
      : selectedText ||
        (selectedCount > 0
          ? t("common.fileDropzone.selectedCount", { count: selectedCount })
          : actionText);

  return (
    <div
      className={cn("grid min-w-0 gap-1 text-sm font-medium text-fg", className)}
      data-testid={dataTestId}
    >
      <FieldLabel htmlFor={inputId} label={label} required={required} />
      <div className="grid min-w-0 grid-cols-1 gap-[8px] sm:grid-cols-[minmax(0,1fr)_auto]">
        <label
          htmlFor={inputId}
          data-testid={dataTestId ? `${dataTestId}-dropzone` : undefined}
          data-drag-active={String(isDragActive)}
          // 淡い面（ドラッグ中・選択済みは accent、エラーは danger）の上の文字を 4.5:1 にする（#1671）。
          data-surface-tint={
            interactionDisabled
              ? undefined
              : isDragActive || (!visibleError && hasSelection)
                ? "accent"
                : visibleError
                  ? "danger"
                  : undefined
          }
          aria-busy={loading}
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          className={cn(
            // 入力欄と同じ md の高さ（タッチ端末は 44px）。隣のクリアも同じ size（#613）。
            "group flex h-[var(--control-height-md)] min-w-0 touch-manipulation items-center gap-2 rounded-md border border-dashed bg-surface-sunken px-3 py-1 text-left",
            "transition-[border-color,background-color,box-shadow] duration-200 ease-out motion-reduce:transition-none",
            "focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus-ring",
            interactionDisabled
              ? "cursor-not-allowed border-border opacity-60"
              : isDragActive
                ? "cursor-copy border-accent-emphasis bg-accent-subtle ring-2 ring-focus-ring"
                : visibleError
                  ? "cursor-pointer border-danger-border bg-danger-subtle hover:border-danger-fg"
                  : hasSelection
                    ? "cursor-pointer border-accent-emphasis bg-accent-subtle hover:border-accent-emphasis hover:bg-accent-subtle"
                    : "cursor-pointer border-border hover:border-accent-emphasis hover:bg-accent-subtle"
          )}
        >
          <span
            data-surface-tint="accent"
            className={cn(
              "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
              hasSelection || isDragActive
                ? "bg-accent-subtle text-accent-fg"
                : "bg-surface-hover text-fg-muted group-hover:bg-accent-subtle group-hover:text-accent-fg"
            )}
            aria-hidden="true"
          >
            {loading ? (
              <Spinner size={16} />
            ) : (
              <Icon size={16} />
            )}
          </span>
          <span className="min-w-0 flex-1">
            <span
              className="block truncate text-sm font-semibold text-fg"
              title={displayText}
            >
              {displayText}
            </span>
          </span>
          <span
            data-surface-tint={hasSelection ? "accent" : undefined}
            className={cn(
              "inline-block max-w-24 shrink-0 truncate rounded-md border px-2 py-1 text-xs font-semibold sm:max-w-56",
              hasSelection
                ? "border-accent-emphasis bg-accent-subtle text-accent-fg"
                : "border-border bg-surface-sunken text-fg-muted"
            )}
            title={hasSelection ? (multiple ? addText : replaceText) : formatLabel}
          >
            {hasSelection ? (multiple ? addText : replaceText) : formatLabel}
          </span>
          <input
            id={inputId}
            data-testid={dataTestId ? `${dataTestId}-input` : undefined}
            className="sr-only"
            type="file"
            accept={accept}
            multiple={multiple}
            // 読み込み中（loading）はネイティブの disabled にせず aria-disabled にし、選んだ入力にフォーカスを保つ。
            // ファイルの選択の画面は click の既定動作を止めて開かない（#355 / #835）。
            disabled={disabled || undefined}
            aria-disabled={loading && !disabled ? true : undefined}
            onClick={(event) => {
              if (loading) event.preventDefault();
            }}
            required={required}
            aria-label={ariaLabel}
            aria-required={required}
            aria-invalid={Boolean(visibleError)}
            aria-describedby={describedBy || undefined}
            onChange={(event) => {
              const input = event.currentTarget;
              if (!input.files?.length) return;
              acceptFiles(input.files);
              queueMicrotask(() => {
                input.value = "";
              });
            }}
          />
        </label>
        {onClear ? (
          <ClearActionButton
            size="md"
            disabled={clearIsDisabled || interactionDisabled}
            ariaLabel={clearAriaLabel ?? clearText}
            onClick={() => {
              setValidationError("");
              onClear();
            }}
            label={clearText}
          />
        ) : null}
      </div>
      {hint ? (
        <p id={hintId} className="text-xs font-normal leading-5 text-fg-muted">
          {hint}
        </p>
      ) : null}
      <FieldError id={errorId} message={visibleError} />
    </div>
  );
}
