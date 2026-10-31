import type { RunState } from "@/lib/api";

/**
 * 判断の返却値が、この利用者が押した判断になっているか（#1119）。
 *
 * ほかの操作者が先に判断した・実行が先に終わった承認は、backend が状態を変えずに 200 で返す。返った承認が
 * 押した判断と違う、または判断した利用者が自分でなければ、成功と案内しない（最新の内容の確認を促す）。
 */
export function isOwnDecision(
  run: RunState,
  approvalId: string,
  approved: boolean,
  loginUserId: string | undefined
): boolean {
  const decided = run.approvals.find((item) => item.id === approvalId);
  if (!decided || decided.status !== (approved ? "approved" : "rejected")) return false;
  return !loginUserId || !decided.decided_by || decided.decided_by === loginUserId;
}
