// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import { draftKey, isWorkspaceOwner } from "../../lib/workspace-drafts";
import { API_TIMEOUT_MS } from "../../lib/requestPolicy";

import type { JobStatus } from "./types";

export const ACTIVE_JOB_ID_KEY = "nl2sql.activeJobId";
export const ACTIVE_JOB_STARTED_AT_KEY = "nl2sql.activeJobStartedAt";

// longRunningJob の HTTP timeout と同じ寿命。これを超えた snapshot は復元せず破棄する
// (失効 job への無限ポーリング再開を防ぐ)。
export const ACTIVE_JOB_SNAPSHOT_TTL_MS = API_TIMEOUT_MS.longRunningJob;

export interface ActiveJobStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface ActiveJobSnapshot {
  jobId: string;
  startedAtMs: number;
}

/** sessionStorage を渡す。workspace prefix に含め、logout 時に草稿と一緒に消去する。 */
export function scopedActiveJobStorage(storage: ActiveJobStorage, owner: string, context: string): ActiveJobStorage {
  const keyFor = (key: string) => draftKey(owner, context, "/query", key);
  return {
    getItem: (key) => isWorkspaceOwner(storage, owner) ? storage.getItem(keyFor(key)) : null,
    setItem: (key, value) => {
      // logout 後に完了した非同期リクエストが旧ユーザーの情報を復活させない。
      if (!isWorkspaceOwner(storage, owner)) throw new Error("Workspace owner changed");
      storage.setItem(keyFor(key), value);
    },
    removeItem: (key) => storage.removeItem(keyFor(key)),
  };
}

/** 旧共有記録は所有タブが不明。案内だけに使い、元のタブの記録も変更しない。 */
export function hasLegacyActiveJobSnapshot(storage: ActiveJobStorage, nowMs: number): boolean {
  if (!storage.getItem(ACTIVE_JOB_ID_KEY)) return false;
  const startedAt = Number(storage.getItem(ACTIVE_JOB_STARTED_AT_KEY));
  return !Number.isFinite(startedAt) || startedAt <= 0 || nowMs - startedAt <= ACTIVE_JOB_SNAPSHOT_TTL_MS;
}

export function isJobInFlight(status: JobStatus | null | undefined): boolean {
  return status === "pending" || status === "running";
}

export function isJobTerminal(status: JobStatus | null | undefined): boolean {
  return status === "done" || status === "error";
}

export function readActiveJobSnapshot(
  storage: ActiveJobStorage,
  nowMs: number
): ActiveJobSnapshot | null {
  const jobId = storage.getItem(ACTIVE_JOB_ID_KEY);
  if (!jobId) return null;
  const storedStartedAt = Number(storage.getItem(ACTIVE_JOB_STARTED_AT_KEY));
  const startedAtValid = Number.isFinite(storedStartedAt) && storedStartedAt > 0;
  if (startedAtValid && nowMs - storedStartedAt > ACTIVE_JOB_SNAPSHOT_TTL_MS) {
    clearActiveJobSnapshot(storage, jobId);
    return null;
  }
  if (!startedAtValid) {
    // 次回 mount から TTL が効くように現在時刻で補完して書き戻す。
    storage.setItem(ACTIVE_JOB_STARTED_AT_KEY, String(nowMs));
  }
  return {
    jobId,
    startedAtMs: startedAtValid ? storedStartedAt : nowMs,
  };
}

export function persistActiveJobSnapshot(
  storage: ActiveJobStorage,
  jobId: string,
  startedAtMs: number
) {
  storage.setItem(ACTIVE_JOB_ID_KEY, jobId);
  storage.setItem(ACTIVE_JOB_STARTED_AT_KEY, String(startedAtMs));
}

export function clearActiveJobSnapshot(storage: ActiveJobStorage, expectedJobId: string | null) {
  // 旧 job の遅延 cleanup が、このタブで新しく開始した job を削除しない。
  if (!expectedJobId || storage.getItem(ACTIVE_JOB_ID_KEY) !== expectedJobId) return;
  storage.removeItem(ACTIVE_JOB_ID_KEY);
  storage.removeItem(ACTIVE_JOB_STARTED_AT_KEY);
}
