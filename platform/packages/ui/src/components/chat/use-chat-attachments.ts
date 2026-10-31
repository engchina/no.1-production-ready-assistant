import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  formatAttachmentSize,
  type ChatAttachmentItem,
  type ChatAttachmentKind,
  type ChatComposerAttachments,
} from "./chat-attachments";

/** 添付の API の結果（製品の API の応答を詰め替えて返す）。 */
export interface ChatAttachmentUploadResult {
  attachmentId: string;
  fileName: string;
  sizeBytes: number;
  kind?: ChatAttachmentKind;
  /** 読み取りの注意（翻訳済み）。 */
  notes?: string[];
}

export interface ChatAttachmentMessages {
  /** 大きすぎる（上限の表示を受け取る。例: "20 MB"）。 */
  tooLarge: (maxSize: string) => string;
  /** 件数の上限。 */
  tooMany: (maxFiles: number) => string;
  /** 受け付けない種類。 */
  unsupported: string;
  /** API の失敗の理由（製品の API のエラーから作る）。 */
  uploadFailed: (error: unknown) => string;
}

export interface UseChatAttachmentsOptions {
  upload: (file: File) => Promise<ChatAttachmentUploadResult>;
  /** 外す（API の失敗は無視する。期限で消える）。 */
  remove: (attachmentId: string) => Promise<unknown>;
  /** 1 つの大きさの上限（bytes）。無ければ確かめない。 */
  maxBytes?: number;
  /** 件数の上限（アップロード中・添付済みの合計）。 */
  maxFiles?: number;
  /** 受け付ける拡張子（".pdf" など）。無ければ確かめない。 */
  accept?: readonly string[];
  messages: ChatAttachmentMessages;
  formatSize?: (bytes: number) => string;
}

interface Entry extends ChatAttachmentItem {
  attachmentId?: string;
}

let localCounter = 0;
function localId() {
  localCounter += 1;
  return `chat-attachment-${localCounter}`;
}

function extensionOf(name: string) {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}

/**
 * 入力欄の添付の状態（3 製品共通。#2517）。選んだファイルを 1 つずつ API で添付し、送信に使う
 * `readyIds`（API の添付の ID）を返す。送信が受け付けられたら `clear()` で入力欄から外す
 * （添付は会話に残る）。上限・種類は API でも確かめるが、送る前に分かるものはここで失敗にする。
 */
export function useChatAttachments(options: UseChatAttachmentsOptions) {
  const [items, setItems] = useState<Entry[]>([]);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  // アップロード中に外したもの（終わったら API でも消す）。
  const removedWhileUploading = useRef(new Set<string>());

  const update = useCallback((id: string, patch: Partial<Entry>) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  // 副作用（API の呼び出し）は state の更新関数の外で行う（StrictMode は更新関数を 2 回呼ぶ）。
  const itemsRef = useRef<Entry[]>([]);
  itemsRef.current = items;

  const startUpload = useCallback(
    (id: string, file: File) => {
      void optionsRef.current
        .upload(file)
        .then((result) => {
          if (removedWhileUploading.current.delete(id)) {
            void optionsRef.current.remove(result.attachmentId).catch(() => undefined);
            return;
          }
          update(id, {
            status: "ready",
            attachmentId: result.attachmentId,
            fileName: result.fileName,
            sizeBytes: result.sizeBytes,
            kind: result.kind,
            notes: result.notes,
          });
        })
        .catch((reason: unknown) => {
          if (removedWhileUploading.current.delete(id)) return;
          update(id, { status: "failed", error: optionsRef.current.messages.uploadFailed(reason) });
        });
    },
    [update]
  );

  const add = useCallback(
    (files: File[]) => {
      const { maxBytes, maxFiles, accept, messages, formatSize } = optionsRef.current;
      let active = itemsRef.current.filter((item) => item.status !== "failed").length;
      const added: Entry[] = [];
      const uploads: [string, File][] = [];
      for (const file of files) {
        const id = localId();
        const base: Entry = { id, fileName: file.name, sizeBytes: file.size };
        let error: string | undefined;
        if (accept && accept.length > 0 && !accept.includes(extensionOf(file.name))) {
          error = messages.unsupported;
        } else if (maxBytes !== undefined && file.size > maxBytes) {
          error = messages.tooLarge((formatSize ?? String)(maxBytes));
        } else if (maxFiles !== undefined && active >= maxFiles) {
          error = messages.tooMany(maxFiles);
        }
        if (error) {
          added.push({ ...base, status: "failed", error });
          continue;
        }
        active += 1;
        added.push({ ...base, status: "uploading" });
        uploads.push([id, file]);
      }
      itemsRef.current = [...itemsRef.current, ...added];
      setItems((current) => [...current, ...added]);
      for (const [id, file] of uploads) startUpload(id, file);
    },
    [startUpload]
  );

  const remove = useCallback((id: string) => {
    const target = itemsRef.current.find((item) => item.id === id);
    if (target?.status === "uploading") removedWhileUploading.current.add(id);
    if (target?.attachmentId) void optionsRef.current.remove(target.attachmentId).catch(() => undefined);
    itemsRef.current = itemsRef.current.filter((item) => item.id !== id);
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  /** 送信が受け付けられた後に、入力欄から外す（API の添付は会話に残る）。 */
  const clear = useCallback(() => {
    setItems((current) => current.filter((item) => item.status === "uploading"));
  }, []);

  /** 送る前の添付を API の一覧から戻す（画面を開き直したとき）。 */
  const restore = useCallback((results: ChatAttachmentUploadResult[]) => {
    setItems(
      results.map((result) => ({
        id: localId(),
        attachmentId: result.attachmentId,
        fileName: result.fileName,
        sizeBytes: result.sizeBytes,
        kind: result.kind,
        notes: result.notes,
        status: "ready" as const,
      }))
    );
  }, []);

  const readyIds = useMemo(
    () => items.flatMap((item) => (item.status === "ready" && item.attachmentId ? [item.attachmentId] : [])),
    [items]
  );
  /** 送信に使う添付（発話の表示に使う）。 */
  const readyItems = useMemo(
    () => items.filter((item) => item.status === "ready" && item.attachmentId),
    [items]
  );
  const uploading = items.some((item) => item.status === "uploading");

  return { items: items as ChatAttachmentItem[], add, remove, clear, restore, readyIds, readyItems, uploading };
}

/** 3 製品の添付の API（`/api/chat-attachments`）が返す添付（backend の `ChatAttachmentData`）。 */
export interface ChatAttachmentApiItem {
  attachment_id: string;
  file_name: string;
  kind: ChatAttachmentKind;
  size_bytes: number;
  warnings?: string[];
  /** 解析の状態（#2652。アップロードの直後は pending で、解析は送信した後に読むとき）。 */
  parse_status?: "pending" | "parsing" | "ready" | "failed";
}

/** 添付の上限（backend の `ChatAttachmentLimitsData`）。 */
export interface ChatAttachmentApiLimits {
  max_bytes: number;
  max_files: number;
  ttl_hours: number;
  accept: string[];
  /** 画像を読み取れるモデルがあるか（画像は無くても添付でき、読むときにモデルが無いことを示す）。 */
  image_enabled: boolean;
  /** 製品の「運用設定 › チャットの添付」で有効か。無効なら入力欄に添付の操作を出さない。 */
  enabled: boolean;
}

/** 発話に残る添付（backend の `ChatAttachmentReference`）。 */
export interface ChatAttachmentApiReference {
  attachment_id: string;
  file_name: string;
  kind: ChatAttachmentKind;
  size_bytes?: number;
}

/** API の添付を `useChatAttachments` の結果にする（警告の code を製品の文言にする）。 */
export function chatAttachmentUploadResult(
  item: ChatAttachmentApiItem,
  warningLabel: (code: string) => string | undefined
): ChatAttachmentUploadResult {
  return {
    attachmentId: item.attachment_id,
    fileName: item.file_name,
    sizeBytes: item.size_bytes,
    kind: item.kind,
    notes: (item.warnings ?? []).flatMap((code) => {
      const label = warningLabel(code);
      return label ? [label] : [];
    }),
  };
}

/** 発話に残る添付を表示の項目にする（`ChatTurn` の `questionAttachments`）。 */
export function chatAttachmentItems(references: readonly ChatAttachmentApiReference[] | undefined): ChatAttachmentItem[] {
  return (references ?? []).map((reference) => ({
    id: reference.attachment_id,
    fileName: reference.file_name,
    sizeBytes: reference.size_bytes ?? 0,
    kind: reference.kind,
  }));
}

/** 製品の添付の API（3 製品で同じ `/api/chat-attachments`。fetch の作法は製品ごと）。 */
export interface ChatComposerAttachmentsApi {
  /** 送る前の添付と上限（`GET /api/chat-attachments`）。 */
  list: () => Promise<{ items: ChatAttachmentApiItem[]; limits: ChatAttachmentApiLimits }>;
  upload: (file: File) => Promise<ChatAttachmentApiItem>;
  remove: (attachmentId: string) => Promise<unknown>;
}

/** 入力欄の添付の文言（製品の i18n。3 製品で同じ文）。 */
export interface ChatComposerAttachmentTexts {
  add: string;
  drop: string;
  list: string;
  remove: (fileName: string) => string;
  uploading: string;
  hint: (limits: { size: string; count: number; hours: number }) => string;
  tooLarge: (size: string) => string;
  tooMany: (count: number) => string;
  unsupported: string;
  uploadFailed: (error: unknown) => string;
  /** 読み取りの注意の code（`macros_not_executed` など）の文。無ければ出さない。 */
  warning: (code: string) => string | undefined;
}

/**
 * チャットの入力欄の添付（3 製品共通。#2517）。開いたときに送る前の添付と上限を読み（製品の「運用設定 ›
 * チャットの添付」で無効なら `composer` は undefined で、入力欄に添付の操作を出さない）、`ChatComposer` の
 * `attachments` と、送信に使う添付（`take`）を返す。設定の変更は画面を開き直すと反映される。
 */
export function useChatComposerAttachments(api: ChatComposerAttachmentsApi, texts: ChatComposerAttachmentTexts) {
  const [limits, setLimits] = useState<ChatAttachmentApiLimits | null>(null);
  const apiRef = useRef(api);
  apiRef.current = api;
  const textsRef = useRef(texts);
  textsRef.current = texts;
  const attachments = useChatAttachments({
    upload: async (file) => chatAttachmentUploadResult(await apiRef.current.upload(file), textsRef.current.warning),
    remove: (attachmentId) => apiRef.current.remove(attachmentId),
    maxBytes: limits?.max_bytes,
    maxFiles: limits?.max_files,
    accept: limits?.accept,
    formatSize: formatAttachmentSize,
    messages: {
      tooLarge: (size) => textsRef.current.tooLarge(size),
      tooMany: (count) => textsRef.current.tooMany(count),
      unsupported: texts.unsupported,
      uploadFailed: (error) => textsRef.current.uploadFailed(error),
    },
  });
  const { restore } = attachments;
  useEffect(() => {
    let active = true;
    apiRef.current
      .list()
      .then((data) => {
        if (!active) return;
        setLimits(data.limits);
        // 開き直したときは、送る前の添付を戻す。
        if (data.items.length) {
          restore(data.items.map((item) => chatAttachmentUploadResult(item, textsRef.current.warning)));
        }
      })
      // 読めない（権限が無い・無効・未作成の表）ときは添付の操作を出さない。
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [restore]);

  const enabled = Boolean(limits?.enabled);
  const composer = useMemo<ChatComposerAttachments | undefined>(
    () =>
      enabled && limits
        ? {
            items: attachments.items,
            onAddFiles: attachments.add,
            onRemove: attachments.remove,
            accept: limits.accept.join(","),
            hint: textsRef.current.hint({
              size: formatAttachmentSize(limits.max_bytes),
              count: limits.max_files,
              hours: limits.ttl_hours,
            }),
            labels: {
              add: textsRef.current.add,
              drop: textsRef.current.drop,
              list: textsRef.current.list,
              remove: (name) => textsRef.current.remove(name),
              uploading: textsRef.current.uploading,
            },
          }
        : undefined,
    [enabled, limits, attachments.items, attachments.add, attachments.remove]
  );

  /** 送信に使う添付を取り出し、入力欄から外す（送った質問の下に出す）。 */
  const take = useCallback((): { ids: string[]; items: ChatAttachmentItem[] } => {
    if (!enabled) return { ids: [], items: [] };
    const items = attachments.readyItems.map((item) => ({ ...item }));
    const ids = attachments.readyIds;
    if (ids.length) attachments.clear();
    return { ids, items };
  }, [enabled, attachments]);

  return { composer, take, uploading: attachments.uploading, enabled };
}
