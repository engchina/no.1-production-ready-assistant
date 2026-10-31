import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";

import { apiGet, isAbortError, isTimeoutError } from "@/lib/api";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";
import { parseDbAdminObjectTarget } from "./components/DbObjectManagementShared";
import type { DbAdminObjectDetail } from "./types";

export const DB_OBJECT_DETAIL_TIMEOUT_MS = API_TIMEOUT_MS.interactiveDetail;

interface DbObjectDetailRequestOptions {
  collectionPath: string;
  loadErrorMessage: string;
  timeoutErrorMessage: string;
}

interface DbObjectDetailRequestState {
  selectedName: string;
  detail: DbAdminObjectDetail | null;
  setDetail: Dispatch<SetStateAction<DbAdminObjectDetail | null>>;
  loading: boolean;
  ddlLoading: boolean;
  error: string;
  ddlError: string;
  load: (name: string) => Promise<void>;
  loadDdl: (name: string) => Promise<void>;
  cancel: () => void;
  clear: () => void;
  requestVersion: () => number;
}

function detailLoadError(cause: unknown, fallback: string): string {
  if (!(cause instanceof Error) || !cause.message.trim()) return fallback;
  return `${fallback} ${cause.message}`;
}

function detailUrl(collectionPath: string, name: string, includeDdl: boolean) {
  const target = parseDbAdminObjectTarget(name);
  const params = new URLSearchParams({
    include_ddl: includeDdl ? "1" : "0",
  });
  if (target.owner) params.set("owner", target.owner);
  return `${collectionPath}/${encodeURIComponent(target.name)}?${params.toString()}`;
}

/**
 * テーブル/ビュー詳細の共通 request state。
 *
 * request sequence と AbortController の両方で latest-selection-wins を保証し、
 * DDL の後追い取得も含めて page-level 操作の loading/error state から分離する。
 */
export function useDbObjectDetailRequest({
  collectionPath,
  loadErrorMessage,
  timeoutErrorMessage,
}: DbObjectDetailRequestOptions): DbObjectDetailRequestState {
  // 選んだ対象は画面の URL（useWorkspaceUrlSelection）が持つ。ここは取得中・取得済みの対象の名前だけ（#2281）。
  const [selectedName, setSelectedName] = useState("");
  const [detail, setDetail] = useState<DbAdminObjectDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [ddlLoading, setDdlLoading] = useState(false);
  const [error, setError] = useState("");
  const [ddlError, setDdlError] = useState("");
  const controllerRef = useRef<AbortController | null>(null);
  const ddlControllerRef = useRef<{ name: string; controller: AbortController } | null>(null);
  const sequenceRef = useRef(0);

  const clear = useCallback(() => {
    sequenceRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    ddlControllerRef.current?.controller.abort();
    ddlControllerRef.current = null;
    setSelectedName("");
    setDetail(null);
    setLoading(false);
    setDdlLoading(false);
    setError("");
    setDdlError("");
  }, [setSelectedName]);

  const cancel = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    ddlControllerRef.current?.controller.abort();
    ddlControllerRef.current = null;
    setLoading(false);
    setDdlLoading(false);
  }, []);

  const load = useCallback(
    async (name: string) => {
      const sequence = sequenceRef.current + 1;
      sequenceRef.current = sequence;
      controllerRef.current?.abort();
      ddlControllerRef.current?.controller.abort();
      ddlControllerRef.current = null;
      const controller = new AbortController();
      controllerRef.current = controller;
      setSelectedName(name);
      setDetail((current) => current && (current.qualified_name || `${current.owner}.${current.name}`) === name ? current : null);
      setError("");
      setDdlError("");
      setLoading(true);
      setDdlLoading(false);
      try {
        const nextDetail = await apiGet<DbAdminObjectDetail>(
          detailUrl(collectionPath, name, false),
          {
            signal: controller.signal,
            timeoutMs: DB_OBJECT_DETAIL_TIMEOUT_MS,
          },
        );
        if (sequence === sequenceRef.current && !controller.signal.aborted) {
          setDetail(nextDetail);
        }
      } catch (cause) {
        if (isAbortError(cause)) return;
        if (sequence === sequenceRef.current) {
          setError(
            isTimeoutError(cause)
              ? timeoutErrorMessage
              : detailLoadError(cause, loadErrorMessage),
          );
        }
      } finally {
        if (sequence === sequenceRef.current) {
          controllerRef.current = null;
          setLoading(false);
        }
      }
    },
    [collectionPath, loadErrorMessage, setSelectedName, timeoutErrorMessage],
  );

  const loadDdl = useCallback(
    async (name: string) => {
      const target = parseDbAdminObjectTarget(name);
      const detailKey = parseDbAdminObjectTarget(
        detail?.qualified_name || detail?.name || "",
        detail?.owner,
      ).qualifiedName;
      if (!detail || detailKey !== target.qualifiedName || detail.ddl || ddlControllerRef.current?.name === target.qualifiedName) {
        return;
      }
      const sequence = sequenceRef.current + 1;
      sequenceRef.current = sequence;
      ddlControllerRef.current?.controller.abort();
      const controller = new AbortController();
      ddlControllerRef.current = { name: target.qualifiedName, controller };
      setDdlError("");
      setDdlLoading(true);
      try {
        const nextDetail = await apiGet<DbAdminObjectDetail>(
          detailUrl(collectionPath, target.qualifiedName, true),
          {
            signal: controller.signal,
            timeoutMs: DB_OBJECT_DETAIL_TIMEOUT_MS,
          },
        );
        if (sequence === sequenceRef.current && !controller.signal.aborted) {
          setDetail((current) =>
            current &&
            parseDbAdminObjectTarget(
              current.qualified_name || current.name,
              current.owner,
            ).qualifiedName === target.qualifiedName
              ? { ...current, ddl: nextDetail.ddl }
              : current,
          );
        }
      } catch (cause) {
        if (isAbortError(cause)) return;
        if (sequence === sequenceRef.current) {
          setDdlError(
            isTimeoutError(cause)
              ? timeoutErrorMessage
              : detailLoadError(cause, loadErrorMessage),
          );
        }
      } finally {
        if (sequence === sequenceRef.current) {
          ddlControllerRef.current = null;
          setDdlLoading(false);
        }
      }
    },
    [collectionPath, detail, loadErrorMessage, timeoutErrorMessage],
  );

  useEffect(
    () => () => {
      sequenceRef.current += 1;
      controllerRef.current?.abort();
      ddlControllerRef.current?.controller.abort();
    },
    [],
  );

  const requestVersion = useCallback(() => sequenceRef.current, []);

  return {
    selectedName,
    detail,
    setDetail,
    loading,
    ddlLoading,
    error,
    ddlError,
    load,
    loadDdl,
    cancel,
    clear,
    requestVersion,
  };
}
