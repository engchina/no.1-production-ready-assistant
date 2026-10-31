import { MessagesSquare } from "lucide-react";

import { ButtonLink, type ButtonLinkComponent } from "../ui/button-link";

/*
 * 実行の詳細の「会話を開く」（3 製品共通。#2343。UX 契約 run-history.md「詳細の見出しの操作」）。
 *
 * チャットの往復の実行は、元の会話をチャットで開く。チャットでない実行（RAG 検索・NL2SQL の SQL 生成・MCP・
 * 品質評価）は開く元の会話が無いので出さない（RAG 検索・SQL 生成は `OpenRunInputAction`。#3309）。出すかどうか（会話を見られる人か・チャットの権限があるか）
 * は製品が決め、出すときは `href` を渡す（`href` が無ければ何も描かない）。再実行は実行の詳細に置かない。
 */

export interface OpenConversationActionLabels {
  /** ボタンの文言（既定「会話を開く」）。 */
  label: string;
  /** 読み上げ名（既定「この実行の会話をチャットで開く」）。 */
  ariaLabel: string;
}

export const DEFAULT_OPEN_CONVERSATION_LABELS: OpenConversationActionLabels = {
  label: "会話を開く",
  ariaLabel: "この実行の会話をチャットで開く",
};

export interface OpenConversationActionProps {
  /** 元の会話をチャットで開く URL。無ければ描かない（チャットでない実行・会話を見られない人）。 */
  href: string | null | undefined;
  /** 画面の中の移動に使うリンク（react-router の `Link`）。 */
  linkComponent?: ButtonLinkComponent;
  labels?: Partial<OpenConversationActionLabels>;
  testId?: string;
}

/** 実行の詳細の概要の見出しの右に置く「会話を開く」（`RunSummary` の `actions`）。 */
export function OpenConversationAction({ href, linkComponent, labels, testId }: OpenConversationActionProps) {
  if (!href) return null;
  const resolved = { ...DEFAULT_OPEN_CONVERSATION_LABELS, ...labels };
  return (
    <ButtonLink to={href} linkComponent={linkComponent} icon={MessagesSquare} aria-label={resolved.ariaLabel} testId={testId}>
      {resolved.label}
    </ButtonLink>
  );
}
