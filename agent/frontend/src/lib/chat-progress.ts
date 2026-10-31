/**
 * チャットの回答の処理の段階（3 製品共通の `ChatProgress`。#1145 / #1147）の、Agent の段階の定義（#1359）。
 *
 * 処理の経過は Run の trace（span の木。#3226）の投影で、span は共通の span の表にあり（案 A）、Run の trace の API
 * （`GET /api/runs/{id}/trace`。3 製品共通のイベントの形）で届く。共通の `useTraceProgressSnapshot`
 * （`useRunTraceProgress`）/ `chatProgressStepsFromEvents` が木の順・入れ子にする。ここは工程の span の種類（`kind`）→ i18n の名前だけを持つ: モデルの呼び出しがツールを
 * 求めたら進め方の検討（`plan`）、最終の回答なら回答の作成（`respond`）、ツールの呼び出し（`tool`）、承認の待ち
 * （`approval`）、回答前の確認の待ち（`confirmation`。#1839）。種類の無い span（呼び出し中のモデル・MCP の呼び出し
 * など）は共通の規則の名前で出す。
 */

import { chatProgressLabelState, type ChatProgressStep, type ChatProgressStepDefinitions } from "@production-ready/ui";

import { t } from "./i18n";

/** 送った質問（Run の作成の応答の前。#907）の段階。送信が遅いとき、回答の作成ではなく送信で待っていると分かる。 */
export function chatSubmitProgressSteps(sentAtMs: number): ChatProgressStep[] {
  return [
    { id: "submit", label: t("chat.progress.submit.running"), status: "running", startedAt: new Date(sentAtMs).toISOString() },
  ];
}

/** ツールの表示名（MCP の `title`。backend が params `title` に入れる）。無ければ空。 */
function toolTitle(params: Readonly<Record<string, string | number | boolean>>): string {
  return typeof params.title === "string" ? params.title.trim() : "";
}

/**
 * 段階の定義（実行中「〜しています」・完了「〜しました」・失敗「〜できませんでした」・未実行は名詞。NL2SQL と同じ。
 * #1145）。i18n の key は静的に書く（辞書の検査が key を見つけられるように）。
 */
export const AGENT_CHAT_PROGRESS_STEPS: ChatProgressStepDefinitions = {
  plan: {
    label: (status) =>
      ({
        running: t("chat.progress.plan.running"),
        done: t("chat.progress.plan.done"),
        failed: t("chat.progress.plan.failed"),
        idle: t("chat.progress.plan.idle"),
      })[chatProgressLabelState(status)],
  },
  tool: {
    // 表示名（MCP の `title`）があれば表示名で出し、内部名は段の詳細に残す。無ければ一般の処理名を出す。
    label: (status, params) => {
      const title = toolTitle(params);
      if (title) {
        return {
          running: t("chat.progress.toolTitle.running", { title }),
          done: t("chat.progress.toolTitle.done", { title }),
          failed: t("chat.progress.toolTitle.failed", { title }),
          idle: t("chat.progress.toolTitle.idle", { title }),
        }[chatProgressLabelState(status)];
      }
      return {
        running: t("chat.progress.tool.running"),
        done: t("chat.progress.tool.done"),
        failed: t("chat.progress.tool.failed"),
        idle: t("chat.progress.tool.idle"),
      }[chatProgressLabelState(status)];
    },
    // 却下したツールは実行しない（スキップ）。理由を補足に出す。
    detail: (status, params) => (status === "skipped" && params.rejected === true ? t("chat.progress.toolRejected") : undefined),
  },
  approval: {
    label: (status) =>
      ({
        running: t("chat.progress.approval.running"),
        done: t("chat.progress.approval.done"),
        failed: t("chat.progress.approval.failed"),
        idle: t("chat.progress.approval.idle"),
      })[chatProgressLabelState(status)],
    // 承認を待ったツールの名前を補足に出す（待ちの span は待ちが終わったときに記録する。#3226）。
    detail: (_status, params) => (typeof params.tools === "string" && params.tools ? params.tools : undefined),
  },
  // Agent 自身の回答前の確認（`ask_user_confirmation`。#1839）で、利用者の答えを待つ段階。
  confirmation: {
    label: (status) =>
      ({
        running: t("chat.progress.confirmation.running"),
        done: t("chat.progress.confirmation.done"),
        failed: t("chat.progress.confirmation.failed"),
        idle: t("chat.progress.confirmation.idle"),
      })[chatProgressLabelState(status)],
  },
  respond: {
    label: (status) =>
      ({
        running: t("chat.progress.respond.running"),
        done: t("chat.progress.respond.done"),
        failed: t("chat.progress.respond.failed"),
        idle: t("chat.progress.respond.idle"),
      })[chatProgressLabelState(status)],
  },
};
