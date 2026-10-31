import type { ReactNode } from "react";
import { ArrowLeft } from "lucide-react";

import {
  Button,
  EmptyState,
  FixedSplitPane,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";

/** Agent の分割ペインの比率を保存する localStorage key の前置き（製品ごとに分ける）。 */
export const AGENT_SPLIT_STORAGE_PREFIX = "production-ready-agent.fixedSplitPane";

/** 一覧 + 詳細（共有 ListDetailLayout）の一覧の幅と開閉を保存する localStorage key の前置き（#2283）。 */
export const AGENT_LIST_DETAIL_STORAGE_PREFIX = "production-ready-agent.listDetail";

/** URL の `?id=` が一覧に無いときの説明。黙って別の対象に置き換えず、一覧へ戻る導線を出す。 */
export function MissingEditorTarget({ id, onBack }: { id: string; onBack: () => void }) {
  return (
    <EmptyState
      title={t("editor.notFoundTitle")}
      hint={t("editor.notFoundHint", { id })}
      action={
        <Button variant="secondary" icon={ArrowLeft} onClick={onBack}>
          {t("common.backToList")}
        </Button>
      }
    />
  );
}

/**
 * B 型（マスタ詳細の閲覧）の一覧 + 詳細。共有の FixedSplitPane に Agent の保存 key と文言を渡す。
 * xl 未満は縦積み（共有部品の既定）。
 */
export function AgentSplitPane({
  splitId,
  left,
  right,
}: {
  splitId: string;
  left: ReactNode;
  right: ReactNode;
}) {
  return (
    <FixedSplitPane
      splitId={splitId}
      storagePrefix={AGENT_SPLIT_STORAGE_PREFIX}
      labels={{
        separator: t("split.separator"),
        hint: t("split.hint"),
        equal: t("split.equal"),
        leftWide: t("split.leftWide"),
        rightWide: t("split.rightWide"),
      }}
      left={left}
      right={right}
    />
  );
}
