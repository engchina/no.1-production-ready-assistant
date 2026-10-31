import { SquarePen } from "lucide-react";

import { ButtonLink, type ButtonLinkComponent } from "../ui/button-link";

/*
 * 実行の詳細の「〜で開く」（RAG 検索・NL2SQL の SQL 生成。#3309。UX 契約 run-history.md「詳細」）。
 *
 * 会話の無い 1 回ごとの実行を、元の画面に、その実行の質問と条件を入れた状態で開く（LangSmith の「Open in
 * Playground」と同じ考え方）。開いた画面は入力欄と条件を埋めるだけで、実行は利用者が押す（自動で実行しない）。
 * チャットの往復は `OpenConversationAction`（「会話を開く」）を使い、この部品と並べない。
 * 出すかどうか（実行した本人か・元の画面の権限があるか）は製品が決め、出すときは `href` を渡す
 * （`href` が無ければ何も描かない）。文言は画面の名前を含むので製品が渡す。
 */

export interface OpenRunInputActionLabels {
  /** ボタンの文言（例「RAG 検索で開く」「SQL 生成で開く」）。 */
  label: string;
  /** 読み上げ名（例「この実行の質問と条件を RAG 検索の画面に入れて開く」）。 */
  ariaLabel: string;
}

export interface OpenRunInputActionProps {
  /** 元の画面を、質問と条件を入れて開く URL。無ければ描かない（本人の実行でない・画面の権限が無い）。 */
  href: string | null | undefined;
  /** 画面の中の移動に使うリンク（react-router の `Link`）。 */
  linkComponent?: ButtonLinkComponent;
  labels: OpenRunInputActionLabels;
  testId?: string;
}

/** 実行の詳細の概要の見出しの右に置く「〜で開く」（`RunSummary` の `actions`）。 */
export function OpenRunInputAction({ href, linkComponent, labels, testId }: OpenRunInputActionProps) {
  if (!href) return null;
  return (
    <ButtonLink to={href} linkComponent={linkComponent} icon={SquarePen} aria-label={labels.ariaLabel} testId={testId}>
      {labels.label}
    </ButtonLink>
  );
}
