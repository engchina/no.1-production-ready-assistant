/**
 * nightly（e2e-nightly.yml）だけで実行する spec（#1595）。
 *
 * 主要な画面の axe（a11y）の確認のように、PR の差分と結び付きにくく、画面をまとめて確かめる spec は PR の e2e
 * （ci.yml の smoke と E2E impact）で実行しない。nightly の全件の実行とローカルの実行には含める。
 *
 * - 名前は `*.nightly.spec.ts` にする（規則はファイル名だけ。e2e_impact.py の `NIGHTLY_ONLY_SPEC` と同じ）。
 * - ci.yml は workflow の env で `E2E_NIGHTLY_ONLY=skip` を付ける。各製品の playwright.config.ts が
 *   `nightlyOnlyIgnore()` を `testIgnore` に足し、PR の全件（spec を指定しない shard）でも実行しない。
 *   project の `testIgnore` は config の `testIgnore` を上書きするので、project で `testIgnore` を書くときは
 *   そこにも足す。
 * - e2e_impact.py は spec の一覧から除き、PR で選ばない（spec だけの shard を作らない）。
 * - e2e-nightly.yml は env を付けないので全件に含まれる。ローカルで実行するときは spec のパスか tag（`-g @a11y`）で選ぶ。
 */

export const NIGHTLY_ONLY_SPECS = "**/*.nightly.spec.ts";

export type NightlyOnlyMode = "run" | "skip";

export function nightlyOnlyMode(value: string | undefined = process.env.E2E_NIGHTLY_ONLY): NightlyOnlyMode {
  if (!value || value === "run") return "run";
  if (value === "skip") return "skip";
  throw new Error(`E2E_NIGHTLY_ONLY は run か skip です（${value}）`);
}

/** `testIgnore` に足す glob（`skip` なら nightly だけの spec、`run` なら無し）。 */
export function nightlyOnlyIgnore(value?: string): string[] {
  return nightlyOnlyMode(value) === "skip" ? [NIGHTLY_ONLY_SPECS] : [];
}
