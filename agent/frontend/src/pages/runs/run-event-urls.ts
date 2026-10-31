// Run のイベントの購読（SSE）の URL。配信の基点（`/agent/` など。#1316）を付ける。
import { APP_BASE_PATH, appPath } from "../../lib/base-path";

/** SSE（EventSource）の URL。同じ origin の path（`/<base>api/runs/{id}/events?follow=true`）。 */
export function runEventStreamUrl(runId: string, base: string = APP_BASE_PATH): string {
  return appPath(`/api/runs/${encodeURIComponent(runId)}/events?follow=true`, base);
}
