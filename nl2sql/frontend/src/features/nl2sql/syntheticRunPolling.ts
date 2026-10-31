interface RunStatus {
  run_id: string;
  status: string;
}

interface PollingQuery {
  state: {
    data: RunStatus | RunStatus[] | undefined;
    dataUpdateCount: number;
    errorUpdateCount: number;
    error: unknown;
  };
}

const ACTIVE_STATUSES = new Set(["pending", "running", "verifying"]);
const RECHECK_DELAYS = [5_000, 15_000, 30_000];

/** 一覧と詳細で共用。再描画ではなく取得結果の更新だけを数え、再確認を3回で止める。 */
export function createSyntheticRunPollingInterval() {
  const observations = new WeakMap<PollingQuery, {
    dataCount: number;
    errorCount: number;
    failures: number;
    unknownKey: string;
    unknownChecks: number;
  }>();

  return (query: PollingQuery): number | false => {
    const state = query.state;
    let observation = observations.get(query);
    if (!observation) {
      observation = { dataCount: -1, errorCount: 0, failures: 0, unknownKey: "", unknownChecks: 0 };
      observations.set(query, observation);
    }
    const runs = Array.isArray(state.data) ? state.data : state.data ? [state.data] : [];
    const active = runs.some((run) => ACTIVE_STATUSES.has(run.status));
    const unknownKey = active ? "" : JSON.stringify(runs.filter((run) => run.status === "unknown").map((run) => run.run_id).sort());

    if (state.dataUpdateCount !== observation.dataCount) {
      observation.dataCount = state.dataUpdateCount;
      observation.failures = 0;
      observation.unknownChecks = unknownKey === observation.unknownKey ? observation.unknownChecks + 1 : 1;
      observation.unknownKey = unknownKey;
    }
    if (state.errorUpdateCount !== observation.errorCount) {
      observation.failures += 1;
      observation.errorCount = state.errorUpdateCount;
    }
    if (state.error) {
      const status = (state.error as { status?: number }).status;
      // 認証・認可・存在エラーは定期再送しない。手動更新/復帰時に再検証できる。
      if (status && status >= 400 && status < 500 && status !== 408 && status !== 429) return false;
      return RECHECK_DELAYS[observation.failures - 1] ?? false;
    }
    if (active) return 2_000;
    if (runs.some((run) => run.status === "unknown")) return RECHECK_DELAYS[observation.unknownChecks - 1] ?? false;
    return false;
  };
}

// Query 単位の WeakMap により、ユーザー/DB/詳細 ID の再確認回数を隔離する。
export const syntheticRunPollingInterval = createSyntheticRunPollingInterval();
