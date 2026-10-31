import { useEffect, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { readWorkspace, writeWorkspace, type WorkspaceField } from "@/lib/workspace-state";

function isSearchString(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("?") && value.length <= 2000;
}

/**
 * URL の検索パラメータで絞り込み・並べ替え・ページを持つ画面の作業状態を、
 * サイドナビなどでパラメータなしの URL へ戻ったときにも復元する（workspace-state.md）。
 * 再読込は URL がそのまま残るため、ここでは同じタブ内のページ往復だけを補う。
 */
export function RememberedSearchParams({
  field,
  children,
}: {
  field: WorkspaceField;
  children: ReactNode;
}) {
  const location = useLocation();
  const saved = location.search ? null : readWorkspace(field, "", isSearchString);

  useEffect(() => {
    if (location.search) writeWorkspace(field, location.search);
  }, [field, location.search]);

  if (saved) {
    return <Navigate to={{ pathname: location.pathname, search: saved, hash: location.hash }} replace />;
  }
  return children;
}
