/**
 * e2e の `test` / `expect`（#885）。spec は `@playwright/test` ではなくここから import する。
 * nightly で spec ごとの実行範囲を記録する fixture（platform/scripts/e2e-impact/fixture.ts）を足している。
 * 記録は `E2E_IMPACT_DIR` があるときだけで、それ以外は `@playwright/test` と同じ。
 * 名前の末尾で画面幅を決めるテスト（`(desktop)` / `(mobile)` / `(375px)` など）は、合わない project で
 * skip する（platform/scripts/e2e-impact/viewport-project.ts。#1488）。
 */
import { test as base } from "@playwright/test";

import { withImpactCoverage } from "../../../../platform/scripts/e2e-impact/fixture";
import { withViewportProject } from "../../../../platform/scripts/e2e-impact/viewport-project";

export * from "@playwright/test";
export const test = withViewportProject(withImpactCoverage(base));
