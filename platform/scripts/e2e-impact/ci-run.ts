/**
 * 3 製品の e2e の retries と worker の数（#1593）。各製品の playwright.config.ts が使う。
 *
 * - retries: PR の CI は 1、main の push と nightly（e2e-nightly.yml）は 2、ローカルは 0。
 *   ci.yml は workflow の env の `E2E_RETRIES`（PR は 1、push は 2）で渡す。e2e-nightly.yml は付けないので 2。
 *   PR では flaky のテストを 3 回実行していた（retries 2）。1 回の retry で通らないものは PR を赤にして直し、
 *   遅い環境でだけ出る flaky は nightly の retries 2 と「N flaky」の報告で拾う（#175）。
 * - workers: CI（GitHub の ubuntu-24.04。4 vCPU）は `CI_WORKERS`。手元は製品の値（引数）。
 *   2 と 3 の実測（2026-10-13、e2e-nightly の手動の実行。#1593 の PR #1621）: 3 は Playwright の時間の合計が
 *   NL2SQL 96.0 → 89.9 分・RAG 29.2 → 28.4 分・Agent 19.8 → 18.2 分と少ししか減らず、NL2SQL で retries 2 でも
 *   通らない 4 件（locator.setChecked の時間切れなど）と flaky 2 件が出た（2 は 0 件）。そのため 2 にしている。
 *   値を変えるときは、同じ方式（`gh workflow run e2e-nightly.yml -f workers=<N>`）で測り直す。
 *
 * `E2E_RETRIES` / `E2E_WORKERS` で上書きできる（worker の数の測定に使う）。
 */

/** CI の worker の数（4 vCPU の runner。#1489・#1593）。 */
export const CI_WORKERS = 2;

type Env = Record<string, string | undefined>;

function count(name: string, value: string | undefined): number | undefined {
  if (value === undefined || value === "") return undefined;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`${name} は 0 以上の整数です（${value}）`);
  }
  return parsed;
}

/** retries（CI は `E2E_RETRIES`、無ければ 2。ローカルは 0）。 */
export function ciRetries(env: Env = process.env): number {
  if (!env.CI) return count("E2E_RETRIES", env.E2E_RETRIES) ?? 0;
  return count("E2E_RETRIES", env.E2E_RETRIES) ?? 2;
}

/** worker の数（CI は `CI_WORKERS`、ローカルは `local`。どちらも `E2E_WORKERS` で上書きできる）。 */
export function ciWorkers(local: number, env: Env = process.env): number {
  const override = count("E2E_WORKERS", env.E2E_WORKERS);
  if (override !== undefined && override > 0) return override;
  return env.CI ? CI_WORKERS : local;
}
