/**
 * 判定の校正（#3578）のデータ取得と更新。記入は自分の判定だけ、全員の判定・裁定・校正は管理の権限。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type AgreementFilter,
  type CalibrationLabelInput,
  type LabelSetCreateBody,
} from "./api";

const ROOT = "evaluation-calibration";

export const calibrationKeys = {
  all: [ROOT] as const,
  status: [ROOT, "status"] as const,
  sources: [ROOT, "sources"] as const,
  labelSets: [ROOT, "label-sets"] as const,
  labelSet: (id: string) => [ROOT, "label-set", id] as const,
  rows: (id: string) => [ROOT, "rows", id] as const,
  agreement: (id: string) => [ROOT, "agreement", id] as const,
  synthetic: (id: string) => [ROOT, "synthetic", id] as const,
  calibrations: (id: string) => [ROOT, "calibrations", id] as const,
};

/** 今の評価器の観点ごとの校正の状態（品質評価の結果の「校正済み」）。取れなければ出さない。 */
export function useJudgeCalibrationStatus() {
  return useQuery({
    queryKey: calibrationKeys.status,
    queryFn: api.getJudgeCalibrationStatus,
    staleTime: 60_000,
    retry: false,
  });
}

export function useCalibrationSources(enabled: boolean) {
  return useQuery({
    queryKey: calibrationKeys.sources,
    queryFn: api.listCalibrationSources,
    enabled,
  });
}

export function useLabelSets() {
  return useQuery({
    queryKey: calibrationKeys.labelSets,
    queryFn: api.listLabelSets,
  });
}

export function useLabelSet(id: string | null) {
  return useQuery({
    queryKey: calibrationKeys.labelSet(id ?? ""),
    queryFn: () => api.getLabelSet(id ?? ""),
    enabled: Boolean(id),
  });
}

export function useAnnotationRows(
  id: string,
  params: { aspect?: string; status: "all" | "unlabeled"; offset: number },
) {
  return useQuery({
    queryKey: [...calibrationKeys.rows(id), params],
    queryFn: () => api.listAnnotationRows(id, { ...params, limit: 1 }),
    placeholderData: (previous) => previous,
  });
}

export function useCalibrationAgreement(
  id: string,
  params: {
    aspect?: string;
    status: AgreementFilter;
    offset: number;
    limit: number;
  },
  enabled: boolean,
) {
  return useQuery({
    queryKey: [...calibrationKeys.agreement(id), params],
    queryFn: () => api.getCalibrationAgreement(id, params),
    enabled,
    placeholderData: (previous) => previous,
  });
}

export function useSyntheticItems(id: string, enabled: boolean) {
  return useQuery({
    queryKey: calibrationKeys.synthetic(id),
    queryFn: () => api.listSyntheticItems(id),
    enabled,
  });
}

export function useCalibrations(id: string, enabled: boolean) {
  return useQuery({
    queryKey: calibrationKeys.calibrations(id),
    queryFn: () => api.listCalibrations(id),
    enabled,
  });
}

function useInvalidateSet() {
  const qc = useQueryClient();
  return (id: string) => {
    void qc.invalidateQueries({ queryKey: calibrationKeys.labelSet(id) });
    void qc.invalidateQueries({ queryKey: calibrationKeys.labelSets });
    void qc.invalidateQueries({ queryKey: calibrationKeys.rows(id) });
    void qc.invalidateQueries({ queryKey: calibrationKeys.agreement(id) });
  };
}

export function useCreateLabelSet() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: LabelSetCreateBody) => api.createLabelSet(body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: calibrationKeys.labelSets }),
  });
}

export function useImportLabelSet() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.importLabelSet(file),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: calibrationKeys.labelSets }),
  });
}

/**
 * 記入の集合を消す。削除で変わる一覧と校正の状態だけを取り直す（#3587）。消した集合の詳細・行は、
 * 詳細を閉じる前に取り直すと 404 になるので invalidate しない（集合の ID は使い回されない）。
 */
export function useDeleteLabelSet() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteLabelSet(id),
    onSuccess: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: calibrationKeys.labelSets }),
        qc.invalidateQueries({ queryKey: calibrationKeys.status }),
      ]),
  });
}

export function useSaveCalibrationLabel(id: string) {
  const invalidate = useInvalidateSet();
  return useMutation({
    mutationFn: (body: CalibrationLabelInput) =>
      api.saveCalibrationLabel(id, body),
    onSuccess: () => invalidate(id),
  });
}

export function useSaveCalibrationAdjudication(id: string) {
  const invalidate = useInvalidateSet();
  return useMutation({
    mutationFn: (body: CalibrationLabelInput) =>
      api.saveCalibrationAdjudication(id, body),
    onSuccess: () => invalidate(id),
  });
}

export function useSetSyntheticExcluded(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { itemId: string; excluded: boolean }) =>
      api.setSyntheticExcluded(id, input.itemId, input.excluded),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: calibrationKeys.synthetic(id) }),
  });
}

export function useStartCalibration(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (repeats: number) => api.startCalibration(id, repeats),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: calibrationKeys.calibrations(id) }),
  });
}

/** 校正の job が終わったら、結果・状態・一覧を読み直す。 */
export function useRefreshCalibrationResults() {
  const qc = useQueryClient();
  return (id: string) => {
    void qc.invalidateQueries({ queryKey: calibrationKeys.calibrations(id) });
    void qc.invalidateQueries({ queryKey: calibrationKeys.status });
    void qc.invalidateQueries({ queryKey: calibrationKeys.labelSets });
    void qc.invalidateQueries({ queryKey: calibrationKeys.labelSet(id) });
  };
}
