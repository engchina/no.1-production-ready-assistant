/**
 * e2e の `test` / `expect`（#885）。spec は `@playwright/test` ではなくここから import する。
 * nightly で spec ごとの実行範囲を記録する fixture（platform/scripts/e2e-impact/fixture.ts）を足している。
 * 記録は `E2E_IMPACT_DIR` があるときだけで、それ以外は `@playwright/test` と同じ。
 */
import { test as base } from "@playwright/test";

import { withImpactCoverage } from "../../../../platform/scripts/e2e-impact/fixture";

export * from "@playwright/test";
export const test = withImpactCoverage(base);
