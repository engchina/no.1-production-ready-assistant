import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet } from "@/lib/api";
import { useAuth } from "@/features/security/AuthProvider";
import { MENU_PERMISSIONS } from "@/features/security/menu-permissions";

import {
  SELECT_AI_CREDENTIAL_READINESS_KEY,
  SELECT_AI_CREDENTIAL_READINESS_URL,
  isSelectAiCredentialMissing,
  isSelectAiCredentialMissingError,
} from "./selectAiCredentialReadiness";
import type { SelectAiCredentialReadinessData } from "./types";

/**
 * Select AI の Credential の事前条件（#1683 / #1688）。画面のフォーカスで取り直し、別のタブで
 * Credential を作って戻ったときに案内を消す。読めないときは `missing=false`（止めない。backend の
 * 409 が守る）。
 */
export function useSelectAiCredentialReadiness({ enabled = true }: { enabled?: boolean } = {}) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: SELECT_AI_CREDENTIAL_READINESS_KEY,
    queryFn: ({ signal }) =>
      apiGet<SelectAiCredentialReadinessData>(SELECT_AI_CREDENTIAL_READINESS_URL, { signal }),
    enabled,
    staleTime: 30_000,
    retry: false,
    refetchOnWindowFocus: true,
  });
  // 「再確認」を押したときだけボタンを loading にする（フォーカスでの取り直しでは回さない。buttons.md §8）。
  const [rechecking, setRechecking] = useState(false);
  const recheck = async () => {
    setRechecking(true);
    try {
      await query.refetch();
    } finally {
      setRechecking(false);
    }
  };
  return {
    missing: enabled && isSelectAiCredentialMissing(query.data),
    credentialName: query.data?.credential_name ?? "",
    canOpenSettings: auth.hasPermission(MENU_PERMISSIONS.settingsSelectAiCredential),
    rechecking,
    recheck,
    /** 操作の開始が 409 `SELECT_AI_CREDENTIAL_MISSING` を返したら、状態を取り直して案内を出す。 */
    refreshAfterError: (error: unknown) => {
      if (isSelectAiCredentialMissingError(error)) {
        void queryClient.invalidateQueries({ queryKey: SELECT_AI_CREDENTIAL_READINESS_KEY });
      }
    },
  };
}
