/**
 * チャットで送信したメッセージの楽観的な表示（optimistic UI。#907）の状態。
 *
 * 送信した瞬間に、サーバーの応答（会話・メッセージ・ジョブ・Run の作成）を待たずに会話の欄の末尾へ出す。
 * サーバーの応答で ID・時刻が決まったら、製品はこの仮のメッセージを外して確定したメッセージに置き換える
 * （二重に出さない）。失敗・停止のときも外さずに残し、利用者の入力を失わない。
 *
 * 製品ごとに API の形（SSE・ジョブのポーリング・Run）が違うため、送信と置き換えは製品が持ち、
 * ここは 3 製品で同じ「仮のメッセージ」の形だけを持つ。
 */

/** 仮のメッセージの状態。確定したら製品が仮のメッセージを外すので、`sent` は持たない。 */
export type OptimisticChatStatus = "sending" | "failed" | "stopped";

export interface OptimisticChatMessage {
  /** 画面の中だけの ID（React の key・data 属性）。サーバーの ID ではない。 */
  localId: string;
  /** 送った本文（再送信でそのまま送り直す）。 */
  content: string;
  status: OptimisticChatStatus;
  /** 送信した時刻（ms）。回答の作成中の経過時間の起点にする。 */
  sentAtMs: number;
}

let sequence = 0;

/**
 * 送信した本文から仮のメッセージを作る。`crypto.randomUUID` は安全でない文脈（http の IP 直打ち）で
 * 使えないため、時刻と連番で画面の中だけで一意な ID を作る。
 */
export function createOptimisticChatMessage(
  content: string,
  now: number = Date.now()
): OptimisticChatMessage {
  sequence += 1;
  return {
    localId: `local-${now.toString(36)}-${sequence.toString(36)}`,
    content,
    status: "sending",
    sentAtMs: now,
  };
}

/** 状態を変える（同じ仮のメッセージを、失敗・停止・再送信で使い回す）。 */
export function withOptimisticChatStatus(
  message: OptimisticChatMessage,
  status: OptimisticChatStatus,
  now: number = Date.now()
): OptimisticChatMessage {
  // 再送信は経過時間を 0 から数え直す。
  return status === "sending"
    ? { ...message, status, sentAtMs: now }
    : { ...message, status };
}
