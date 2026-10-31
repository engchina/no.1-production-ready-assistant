export type VisibleSelectionKey = string | number;

export interface VisibleSelectionOptions {
  preserveSelected?: boolean;
}

export function selectedVisibleKey<T, K extends VisibleSelectionKey>(
  items: readonly T[],
  selectedKey: K | null | undefined,
  getKey: (item: T) => K,
  options: VisibleSelectionOptions = {}
) {
  const preserveSelected = options.preserveSelected ?? true;
  const keys = items.map(getKey);
  if (preserveSelected && selectedKey != null && keys.includes(selectedKey)) return selectedKey;
  return keys[0] ?? null;
}

export function selectedVisibleStringKey<T>(
  items: readonly T[],
  selectedKey: string,
  getKey: (item: T) => string,
  options: VisibleSelectionOptions = {}
) {
  return selectedVisibleKey(items, selectedKey, getKey, options) ?? "";
}
