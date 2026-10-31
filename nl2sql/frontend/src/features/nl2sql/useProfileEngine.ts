import { useCallback } from "react";

import { useWorkspaceState } from "@/components/WorkspaceState";
import { resolveProfileEngine } from "./profileEngineOptions";
import type { Nl2SqlEngine } from "./types";

/**
 * SQL 生成・チャットの生成の方式（#1524）。選んだ方式は業務プロファイルごとに作業状態に残し、選んでいない
 * 業務プロファイルは、その既定の生成方式（`default_engine`）を初期の選択にする。
 */
export function useProfileEngine(
  profileId: string,
  profileDefault: Nl2SqlEngine | undefined
): [Nl2SqlEngine, (engine: Nl2SqlEngine) => void] {
  // 作業状態は既定値と同じ型の値だけを戻すので、文字列で持つ（空は未選択）。
  const [choice, setChoice] = useWorkspaceState(`engine:${profileId}`, "");
  const setEngine = useCallback((engine: Nl2SqlEngine) => setChoice(engine), [setChoice]);
  return [resolveProfileEngine(choice, profileDefault), setEngine];
}
