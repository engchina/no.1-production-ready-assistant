import { createContext, type ReactNode, useContext } from "react";

import type { FieldWidthScope } from "./control-size";

const FieldWidthScopeContext = createContext<FieldWidthScope>("viewport");

/**
 * 中の欄（`TextField`・`SearchField`・`SelectField`・`FilterSelect`・`SearchableSelectField`・`SearchableMultiSelect`・
 * `SecretField`・`TextareaField`）の幅の段を、画面ではなくいちばん近い `@container` の幅（28rem）で切り替える（#1933）。
 * `ListToolbar` が絞り込みを包む。製品で使うのは、container query で並べ方を決める自前の区画の中だけ。
 */
export function FieldWidthScopeProvider({ scope, children }: { scope: FieldWidthScope; children: ReactNode }) {
  return <FieldWidthScopeContext.Provider value={scope}>{children}</FieldWidthScopeContext.Provider>;
}

/** 欄の幅の段を切り替える基準（既定 viewport）。 */
export function useFieldWidthScope(): FieldWidthScope {
  return useContext(FieldWidthScopeContext);
}
