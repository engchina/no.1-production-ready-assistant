// Run のイベントの購読（SSE。#215 / #814。旧 AgentRuntimePages.tsx から分けた。#818）。
// push は SSE、取消・再開・承認などの操作は REST（#2260）。
import { useCallback, useEffect, useState } from "react";
import { type RunState } from "@/lib/api";
import { securityApi } from "@/lib/security-api";
import { useValuesChanged } from "@/lib/render-sync";
import { runEventStreamUrl } from "./run-event-urls";

/** Run の詳細の操作（`ObjectActionBar`）。押した操作だけを loading にする（messaging.md §3.7）。 */
export type RunCommandKind = "cancel" | "resume" | "approve" | "reject";

export type SseStreamStatus = "idle" | "open" | "failed";

export interface RunEventSourceState {
  status: SseStreamStatus;
  reconnect: () => void;
}

/** 接続できなかった理由がログインの失効かを確かめる（401 なら共通の認証イベントでログイン画面へ移る）。 */
function verifySession(): void {
  void securityApi.me().catch(() => undefined);
}

const RUN_EVENT_TYPES = [
  "run.status_changed",
  "step.started",
  "tool.approval_required",
  "approval.decided",
  "artifact.created",
  "tool.completed",
  "tool.failed",
  "tool.guardrail_warning",
  "run.completed",
  "run.cancelled",
  "runtime.dispatch_claimed",
  "runtime.submitted",
  "runtime.failed",
];

/**
 * 非終端の Run のイベントを SSE（Cookie セッション）で購読する。
 * EventSource は status code を読めないため、失敗したら自動の再接続をやめ、ログインの失効かを確かめて
 * 利用者に停止を示す（#215）。
 */
export function useRunEventSource(
  run: RunState | undefined,
  enabled: boolean,
  onRuntimeEvent: () => void
): RunEventSourceState {
  const runId = run?.id;
  const active = enabled && Boolean(runId) && Boolean(run) && !isRunTerminal(run?.status ?? "completed");
  const [status, setStatus] = useState<SseStreamStatus>("idle");
  const [generation, setGeneration] = useState(0);

  // 購読の条件が変わったレンダーで状態を初期化する（effect で setState しない）。
  if (useValuesChanged([active, runId ?? null, generation])) {
    setStatus(active ? "open" : "idle");
  }

  useEffect(() => {
    if (!active || !runId) return;
    const source = new EventSource(runEventStreamUrl(runId), {
      withCredentials: true,
    });
    const refresh = () => {
      onRuntimeEvent();
    };
    RUN_EVENT_TYPES.forEach((type) => source.addEventListener(type, refresh));
    source.onerror = () => {
      // 終端した Run の stream の終了も error になる。状態は一覧の再取得で分かるので、再接続だけやめる。
      source.close();
      setStatus("failed");
      verifySession();
      onRuntimeEvent();
    };
    return () => source.close();
  }, [active, runId, onRuntimeEvent, generation]);

  const reconnect = useCallback(() => setGeneration((current) => current + 1), []);
  return { status, reconnect };
}

function isRunTerminal(status: RunState["status"]): boolean {
  return ["completed", "failed", "cancelled"].includes(status);
}
