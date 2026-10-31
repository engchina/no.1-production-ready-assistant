import {
  AlertCircle,
  File,
  FileImage,
  FileSpreadsheet,
  FileText,
  Paperclip,
  Presentation,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";
import { useRef, useState, type DragEvent, type ReactNode } from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { Spinner } from "../ui/spinner";

/** 添付の種類（backend の `pr_system_settings.chat_attachments` の `kind`）。 */
export type ChatAttachmentKind = "pdf" | "word" | "powerpoint" | "excel" | "csv" | "text" | "image";

/** 添付の 1 件の状態（入力欄の添付）。`uploading` はアップロード中（保存だけ。解析は送信の後。#2652）、`failed` は添付できなかった。 */
export type ChatAttachmentStatus = "uploading" | "ready" | "failed";

export interface ChatAttachmentItem {
  /** 画面の中の ID（添付の前から使う）。 */
  id: string;
  fileName: string;
  sizeBytes: number;
  kind?: ChatAttachmentKind;
  /** 既定 `ready`（発話の下の表示は常に ready）。 */
  status?: ChatAttachmentStatus;
  /** 添付できなかった理由（翻訳済み。`failed` のとき）。 */
  error?: string;
  /** 読み取りの注意（翻訳済み。例:「マクロは実行していません」）。 */
  notes?: string[];
}

export interface ChatAttachmentListLabels {
  /** 一覧の名前（翻訳済み。例:「添付ファイル」）。読み上げだけ。 */
  list: string;
  /** 外すボタンの名前（翻訳済み。ファイル名を受け取る。例: (name) => `${name} を外す`）。 */
  remove?: (fileName: string) => string;
  /** アップロード中の文（翻訳済み。例:「アップロード中」）。 */
  uploading?: string;
}

const KIND_ICON: Record<ChatAttachmentKind, LucideIcon> = {
  pdf: FileText,
  word: FileText,
  text: FileText,
  powerpoint: Presentation,
  excel: FileSpreadsheet,
  csv: FileSpreadsheet,
  image: FileImage,
};

/** 大きさの表示（「12 KB」「1.5 MB」。1024 の倍数）。 */
export function formatAttachmentSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const size = bytes / (1024 * 1024);
    return `${size >= 10 || Number.isInteger(size) ? size.toFixed(0) : size.toFixed(1)} MB`;
  }
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

export interface ChatAttachmentListProps {
  items: ChatAttachmentItem[];
  labels: ChatAttachmentListLabels;
  /** 外す（入力欄の添付だけ。発話の下の表示には渡さない）。 */
  onRemove?: (id: string) => void;
  /** 右寄せ（発話の下）。 */
  align?: "start" | "end";
  className?: string;
  testId?: string;
}

/**
 * 添付ファイルの一覧（入力欄の添付と、発話の下の表示。3 製品共通。#2517）。
 *
 * - 1 件 = 種類のアイコン・名前（長ければ省略し、全体は読み上げと `title` で持つ）・大きさ。
 * - 状態は色だけでなくアイコンと文で示す（アップロード中は `Spinner` と文、失敗は `AlertCircle` と理由）。
 * - 外すボタンはアイコンだけ（`X`）で、名前にファイル名を入れる。
 */
export function ChatAttachmentList({
  items,
  labels,
  onRemove,
  align = "start",
  className,
  testId,
}: ChatAttachmentListProps) {
  if (items.length === 0) return null;
  return (
    <ul
      aria-label={labels.list}
      className={cn("flex min-w-0 flex-wrap gap-2", align === "end" && "justify-end", className)}
      data-testid={testId}
    >
      {items.map((item) => {
        const status = item.status ?? "ready";
        const Icon = item.kind ? KIND_ICON[item.kind] : File;
        return (
          <li
            key={item.id}
            className={cn(
              "flex min-w-0 max-w-full flex-col gap-0.5 rounded-md border bg-surface py-1 pl-2 text-xs text-fg",
              onRemove ? "pr-0.5" : "pr-2",
              status === "failed" ? "border-danger-fg" : "border-border"
            )}
            data-status={status}
            data-chat-attachment=""
          >
            <span className="flex min-w-0 items-center gap-1.5">
              {status === "uploading" ? (
                <Spinner size={14} className="shrink-0 text-fg-muted" />
              ) : status === "failed" ? (
                <AlertCircle size={14} aria-hidden className="shrink-0 text-danger-fg" />
              ) : (
                <Icon size={14} aria-hidden className="shrink-0 text-fg-muted" />
              )}
              <span className="min-w-0 max-w-[16rem] truncate font-medium" title={item.fileName}>
                {item.fileName}
              </span>
              <span className="shrink-0 tabular-nums text-fg-muted">{formatAttachmentSize(item.sizeBytes)}</span>
              {status === "uploading" && labels.uploading ? (
                <span className="shrink-0 text-fg-muted">{labels.uploading}</span>
              ) : null}
              {onRemove ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  iconOnly
                  icon={X}
                  aria-label={labels.remove ? labels.remove(item.fileName) : item.fileName}
                  onClick={() => onRemove(item.id)}
                  className="shrink-0"
                />
              ) : null}
            </span>
            {status === "failed" && item.error ? (
              <span className="pr-2 text-danger-fg" role="alert">
                {item.error}
              </span>
            ) : null}
            {status !== "failed" && item.notes?.length ? (
              <span className="pr-2 text-fg-muted">{item.notes.join(" ")}</span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export interface ChatComposerAttachmentLabels extends ChatAttachmentListLabels {
  /** 「ファイルを添付」（翻訳済み）。 */
  add: string;
  /** ドロップの案内（翻訳済み。例:「ここにドロップして添付」）。 */
  drop: string;
}

/** `ChatComposer` の `attachments`（添付の一覧と操作。状態は `useChatAttachments` が持つ）。 */
export interface ChatComposerAttachments {
  items: ChatAttachmentItem[];
  /** 選んだ・ドロップしたファイル（受け付けるかは呼び出し側が判定する）。 */
  onAddFiles: (files: File[]) => void;
  onRemove: (id: string) => void;
  /** 選べる種類（`input[type=file]` の `accept`。例: ".pdf,.docx"）。 */
  accept?: string;
  /** 添付できない間（権限が無い・設定の読み込み中）。 */
  disabled?: boolean;
  labels: ChatComposerAttachmentLabels;
  /** 「ファイルを添付」の横の案内（翻訳済み。期限・上限）。 */
  hint?: ReactNode;
  /** 添付の操作の testid の接頭辞（既定 `chat-attachments`）。 */
  testIdPrefix?: string;
}

function hasFiles(event: DragEvent<HTMLElement>): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

/**
 * 入力欄の領域のドロップ（ファイルを重ねている間は破線の枠と案内を出す）と、「ファイルを添付」の行。
 * `ChatComposer` が `attachments` を受け取ったときだけ使う。
 */
export function ChatComposerAttachmentArea({
  attachments,
  children,
}: {
  attachments: ChatComposerAttachments;
  children: ReactNode;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);
  const prefix = attachments.testIdPrefix ?? "chat-attachments";
  const disabled = Boolean(attachments.disabled);

  const onDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (disabled || !hasFiles(event)) return;
    event.preventDefault();
    depth.current += 1;
    setDragging(true);
  };
  const onDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (disabled || !hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };
  const onDragLeave = () => {
    depth.current = Math.max(0, depth.current - 1);
    if (depth.current === 0) setDragging(false);
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    if (disabled || !hasFiles(event)) return;
    event.preventDefault();
    depth.current = 0;
    setDragging(false);
    const files = Array.from(event.dataTransfer.files ?? []);
    if (files.length) attachments.onAddFiles(files);
  };

  return (
    <div
      className={cn(
        "relative flex flex-col gap-2 rounded-md",
        dragging && "outline-2 outline-offset-2 outline-dashed outline-accent-fg"
      )}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      data-dragging={dragging || undefined}
      data-testid={`${prefix}-dropzone`}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          icon={Paperclip}
          disabled={disabled}
          onClick={() => inputRef.current?.click()}
          data-testid={`${prefix}-add`}
        >
          <span>{attachments.labels.add}</span>
        </Button>
        {attachments.hint ? <span className="min-w-0 text-xs text-fg-muted">{attachments.hint}</span> : null}
        {/* ファイルの選択はネイティブの input（共有部品で表せない）。見せずにボタンから開く。 */}
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={attachments.accept}
          className="sr-only"
          tabIndex={-1}
          aria-hidden
          data-testid={`${prefix}-input`}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            event.target.value = "";
            if (files.length) attachments.onAddFiles(files);
          }}
        />
      </div>
      <ChatAttachmentList
        items={attachments.items}
        labels={attachments.labels}
        onRemove={attachments.onRemove}
        testId={`${prefix}-list`}
      />
      {children}
      {dragging ? (
        <div
          className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2 rounded-md bg-surface/90 text-sm font-medium text-fg"
          data-testid={`${prefix}-drop-hint`}
        >
          <Upload size={16} aria-hidden />
          {attachments.labels.drop}
        </div>
      ) : null}
    </div>
  );
}
