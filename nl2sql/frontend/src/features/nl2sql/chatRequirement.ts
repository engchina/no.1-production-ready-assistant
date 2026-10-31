// チャットの回答前の確認（#1442）の決まりごと。
//
// チャットで質問を送ると、SQL を作る前に要件の検査（回答前の確認の session の作成）を自動で行う。確認が要らない
// （SELECT・FROM が明確で、足りない・あいまいな情報が無い）ときは聞かずにそのまま SQL を作り、確認が要るときは
// 会話の中で 1 問ずつ聞く。全部に答えたら、まとめ（再検査・SQL の骨組み）を利用者が確かめてから SQL を作る。
// 送った質問と session の ID は作業状態に残し、再読込・画面の移動の後も同じ確認を続ける。
//
// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import type { QuerySessionCreateRequest } from "./ontology/types";
import type { Nl2SqlEngine } from "./types";

/** チャットの 1 回の送信の内容。再送信は同じ内容（同じ job ID）で送り直す。 */
export interface ChatJobRequest {
  question: string;
  profile_id: string;
  engine: Nl2SqlEngine;
  previous_job_id: string | null;
  /** 送信の前に画面が決める job ID（#900）。 */
  client_job_id: string;
  /** チャットの回答前の確認で反映した session（#1442）。確認せずに作るときは無い。 */
  requirement_session_id?: string;
  /** 回答前の確認を「確認せずに SQL を生成」で飛ばした（#1839。会話に残すだけで生成は変えない）。 */
  requirement_skipped?: boolean;
  /** 質問に添付するファイル（`uploadChatAttachment` の `attachment_id`。#2517）。 */
  attachment_ids?: string[];
}

/** 送った質問の要件の検査・確認（作業状態に残す。生成のジョブを投入するまで）。 */
export interface ChatRequirementCheck {
  request: ChatJobRequest;
  /** 送った時刻（要件の検査の経過時間の起点）。 */
  sentAtMs: number;
  /** 作った回答前の確認の session（作成の応答の前は空）。 */
  sessionId: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const ENGINES: readonly Nl2SqlEngine[] = ["select_ai", "select_ai_agent", "enterprise_ai_direct"];

/** 作業状態から戻した値を検査する（古い形・壊れた値は捨てる）。 */
export function parseChatRequirementCheck(value: unknown): ChatRequirementCheck | null {
  if (!isRecord(value) || !isRecord(value.request)) return null;
  const request = value.request;
  if (
    typeof request.question !== "string" ||
    !request.question.trim() ||
    typeof request.profile_id !== "string" ||
    typeof request.client_job_id !== "string" ||
    !request.client_job_id ||
    !ENGINES.includes(request.engine as Nl2SqlEngine) ||
    !(request.previous_job_id === null || typeof request.previous_job_id === "string")
  ) {
    return null;
  }
  if (typeof value.sentAtMs !== "number" || typeof value.sessionId !== "string") return null;
  return {
    request: {
      question: request.question,
      profile_id: request.profile_id,
      engine: request.engine as Nl2SqlEngine,
      previous_job_id: request.previous_job_id,
      client_job_id: request.client_job_id,
      // 質問に添付したファイル（#2517）。確認の後に SQL の生成へ渡す。
      ...(Array.isArray(request.attachment_ids) &&
      request.attachment_ids.every((item) => typeof item === "string")
        ? { attachment_ids: request.attachment_ids as string[] }
        : {}),
    },
    sentAtMs: value.sentAtMs,
    sessionId: value.sessionId,
  };
}

/** 作業状態に残す文字列（JSON。確認が無ければ空）。 */
export function encodeChatRequirementCheck(check: ChatRequirementCheck | null): string {
  return check ? JSON.stringify(check) : "";
}

/** 作業状態の文字列から戻す（空・壊れた値は確認なし）。 */
export function decodeChatRequirementCheck(raw: string): ChatRequirementCheck | null {
  if (!raw) return null;
  try {
    return parseChatRequirementCheck(JSON.parse(raw));
  } catch {
    return null;
  }
}

/** 要件の検査の session を作る要求（チャットの業務プロファイル・会話の続き）。 */
export function chatRequirementSessionRequest(request: ChatJobRequest): QuerySessionCreateRequest {
  return {
    question: request.question,
    profile_id: request.profile_id,
    engine: request.engine,
    clarification_mode: "guided",
    chat: true,
    previous_job_id: request.previous_job_id,
    attachment_ids: request.attachment_ids,
  };
}

/**
 * 同じ送信（job ID）の検査は同じ session にする（再読込で作り直しても二重に作らない。作成が終わっていれば
 * サーバーは作った session を返す）。
 */
export function chatRequirementIdempotencyKey(clientJobId: string): string {
  return `nl2sql:chat-requirement:${clientJobId}`;
}

