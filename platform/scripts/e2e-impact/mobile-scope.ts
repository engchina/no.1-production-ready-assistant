/**
 * PR の e2e で、375px（mobile）の project は画面幅に依存するテストだけを実行する（#1582）。
 *
 * 3 製品の e2e は desktop と mobile（375px のタッチ端末）の 2 project で全テストを実行していたが、API の mock・
 * 保存・状態遷移を確かめるテストの多くは画面幅に依存せず、PR の runner の時間の約半分が同じ確認の 2 回目だった。
 *
 * - PR の CI（ci.yml の smoke と E2E impact）は `E2E_MOBILE_SCOPE=responsive` を付ける。mobile の project は
 *   `@responsive` の tag のテストと、名前の末尾の括弧で狭い幅を前提にするテスト（`(mobile)` / `(375px)` など。
 *   RAG・NL2SQL は viewport-project.ts が desktop で skip するので、ここで mobile に残す）だけを実行する。
 *   desktop は今までどおり全件。
 * - nightly（e2e-nightly.yml）とローカルは env を付けない（`all`）。両方の project で全件を実行し、mobile の退行は
 *   nightly で拾う。
 *
 * tag の付け方（`test("…", { tag: "@responsive" }, async …)`、describe にも付けられる）:
 *
 * - レイアウト（`boundingBox` の位置・幅・並び・縦積み）、ナビ（ドロワー）、横スクロール・はみ出し・折り返し、
 *   タッチ端末の高さ（`expectedControlHeight`・pointer: coarse）・タップ、`isMobile` / project 名 /
 *   `page.viewportSize()` で確かめる内容が変わるテストに付ける。spec 全体がそうなら、spec の全テスト
 *   （先頭の describe）に付ける。
 * - mobile だけで実行するテスト（`test.skip(testInfo.project.name !== "mobile…")`・`test.skip(!isMobile)`）には
 *   必ず付ける（付けないと PR ではどの project でも実行されない）。
 * - 付けなくてよいもの: テストの中で `page.setViewportSize` で幅を決めるもの（desktop の project で 375px も
 *   確かめる）、desktop だけで実行するもの、ナビを開くための分岐だけのもの、末尾の汎用の横あふれの確認
 *   （`expectNoHorizontalOverflow` など）だけのもの。迷ったら付ける。
 *
 * playwright.config.ts が `mobileProjectScope()` を mobile の project に展開する。記録の fixture
 * （fixture.ts）は `inPullRequestScope()` で、PR で実行するテストかを記録し、e2e_impact.py が PR の shard の
 * 時間の見積もりに使う。このファイルの変更は e2e の影響分析で全件の実行になる。
 */

export const RESPONSIVE_TAG = "@responsive";

/** 名前の括弧の中で mobile か 480px 以下の幅を前提にするテスト（viewport-project.ts の判定を含む広さ）。 */
export const MOBILE_TITLE = /[(（][^()（）]*(?:\bmobile(?:-\d+)?\b|(?<![\d.])(?:[1-9]\d?|[1-3]\d\d|4[0-7]\d|480)px)[^()（）]*[)）]/;

const RESPONSIVE = new RegExp(`${RESPONSIVE_TAG}\\b`);

export type MobileScope = "all" | "responsive";

export function mobileScope(value: string | undefined = process.env.E2E_MOBILE_SCOPE): MobileScope {
  if (!value || value === "all") return "all";
  if (value === "responsive") return "responsive";
  throw new Error(`E2E_MOBILE_SCOPE は all か responsive です（${value}）`);
}

/** mobile の project に足す `grep`（`all` なら何も足さない）。 */
export function mobileProjectScope(value?: string): { grep?: RegExp[] } {
  return mobileScope(value) === "responsive" ? { grep: [RESPONSIVE, MOBILE_TITLE] } : {};
}

/** PR の CI（`E2E_MOBILE_SCOPE=responsive`）でも実行するテストか。desktop などの project は常に true。 */
export function inPullRequestScope(projectName: string, titlePath: readonly string[], tags: readonly string[]): boolean {
  if (!projectName.startsWith("mobile")) return true;
  return tags.includes(RESPONSIVE_TAG) || titlePath.some((title) => MOBILE_TITLE.test(title));
}
