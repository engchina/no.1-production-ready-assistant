export interface TraceRecordingSettingsData { enabled: boolean; updated_at?: string | null }
export interface TraceRecordingSettingsApi {
  get: (options?: { signal?: AbortSignal }) => Promise<TraceRecordingSettingsData>;
  update: (payload: { enabled: boolean }) => Promise<TraceRecordingSettingsData>;
}
export const TRACE_RECORDING_SETTINGS_QUERY_KEY = ["settings", "trace-recording"] as const;
