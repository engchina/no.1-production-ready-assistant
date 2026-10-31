/**
 * e2e の `test` / `expect`（#885）。spec は `@playwright/test` ではなくここから import する。
 * nightly で spec ごとの実行範囲を記録する fixture（platform/scripts/e2e-impact/fixture.ts）を足している。
 * 記録は `E2E_IMPACT_DIR` があるときだけで、それ以外は `@playwright/test` と同じ。
 * 名前の末尾で画面幅を決めるテスト（`(desktop)` / `(mobile)` / `(375px)` など）は、合わない project で
 * skip する（platform/scripts/e2e-impact/viewport-project.ts。#1488）。
 * 処理の経過の API（`.../trace`。#3226 の案 A）は、mock の span の表（`job-progress.ts`）から答える route を
 * context ごとに入れる（spec の mock が記録の応答を作るときに span の表へ書く）。
 */
import { test as base } from "@playwright/test";

import { withImpactCoverage } from "../../../../../platform/scripts/e2e-impact/fixture";
import { withViewportProject } from "../../../../../platform/scripts/e2e-impact/viewport-project";
import { isTraceRequest, resetMockTraces, routeMockTraces } from "./job-progress";

export * from "@playwright/test";
export const test = withViewportProject(withImpactCoverage(base)).extend<{ mockTraces: void }>({
  mockTraces: [
    async ({ context, page }, use) => {
      resetMockTraces();
      await routeMockTraces(context);
      // spec の広い pattern の mock（`history**`・`**/api/**` など）が処理の経過の API に答えないよう、page の route は
      // 処理の経過の要求を context の mock の span の表へ回す（page の route は context の route より先に当たる）。
      const route = page.route.bind(page);
      page.route = ((url, handler, options) =>
        route(
          url,
          (request, ...rest) => (isTraceRequest(request) ? request.fallback() : handler(request, ...rest)),
          options,
        )) as typeof page.route;
      await use();
    },
    { auto: true },
  ],
});
