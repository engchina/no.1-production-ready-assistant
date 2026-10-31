/**
 * 3 製品・ほかのタブへログイン状態の変化を知らせる（#1565）。
 *
 * ログインの Cookie は 3 製品で共有する（1 台に配備した `/rag/`・`/nl2sql/`・`/agent/` は同じ origin）。
 * ある製品でログアウトしたら、開いているほかの製品・タブもログイン画面へ移し、ログインし直したら
 * 利用者を確かめ直す。`BroadcastChannel` は同じ origin にだけ届く。使えない環境では何もしない
 * （そのときも、次の API の 401 でログイン画面へ移る）。
 */
export const AUTH_BROADCAST_CHANNEL = "production-ready-auth";

export type AuthBroadcastType = "login" | "logout";

export interface AuthBroadcastMessage {
  type: AuthBroadcastType;
}

/** ログイン状態の通知の channel を開く。`BroadcastChannel` が無い・開けない環境では null。 */
export function openAuthBroadcastChannel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    return new BroadcastChannel(AUTH_BROADCAST_CHANNEL);
  } catch {
    return null;
  }
}

/** 受けたメッセージの種類。知らない形なら null（ほかの送り手のメッセージを誤って扱わない）。 */
export function authBroadcastType(data: unknown): AuthBroadcastType | null {
  if (!data || typeof data !== "object") return null;
  const type = (data as { type?: unknown }).type;
  return type === "login" || type === "logout" ? type : null;
}

/** 通知を送る。送れなくても（閉じた channel など）認証の操作は妨げない。 */
export function postAuthBroadcast(channel: BroadcastChannel | null, type: AuthBroadcastType): void {
  if (!channel) return;
  try {
    channel.postMessage({ type } satisfies AuthBroadcastMessage);
  } catch {
    /* 通知できなくても、ほかのタブは次の API の 401 でログイン画面へ移る */
  }
}
