# Feedback Root Cause Lens — Design QA

- Result: `passed`
- Reference: `/root/.codex/generated_images/019f2450-ec59-7002-92a6-6bed46e262cf/exec-399dfde9-9084-4b4d-abd1-a2eb212cfa58.png`
- Desktop implementation: `/u01/workspace/no.1-production-ready-rag/frontend/test-results/feedback-高密度一覧を検索・数値ページングし、三つの詳細タブから原因を追える-desktop/feedback-root-cause-desktop.png`
- Mobile implementation: `/u01/workspace/no.1-production-ready-rag/frontend/test-results/feedback-375pxではカード一覧と全画面詳細になり、URLから状態を復元できる-mobile/feedback-root-cause-mobile.png`
- Side-by-side comparison: `/tmp/feedback-qa-comparison.png`

## Visual checks

- 1440px: compact analysis summary, dense sticky-header table, numeric pagination, and right-side detail drawer match the selected Root Cause Lens hierarchy.
- Table columns have fixed readable widths; rating labels and row actions no longer wrap vertically.
- Drawer uses the selected three-tab structure and preserves a clear content/evidence/execution information hierarchy.
- 375px: desktop table is replaced by compact cards, the detail dialog fills the viewport, and no horizontal page overflow is present.
- Existing navigation, semantic colors, Noto Sans JP typography, shared Button components, and Lucide icons are preserved.

## Interaction checks

- URL restoration covers filters, search, sort, page, page size, and selected feedback.
- Search debounces by 300ms; filter and page-size changes reset to page 1.
- Escape closes the dialog and restores focus to the originating detail button.
- Loading/error retry, empty state, legacy no-content fallback, citation links, and conversation links are implemented.

---

# CitationCard design QA

## Evidence

- Source screenshot: `/root/.codex/attachments/86af182d-4d88-4ce3-8c49-64f47a280fc5/codex-clipboard-e5dc212d-9863-4260-926e-38c0d9c7f3ca.png`
- Desktop implementation: `frontend/test-results/citation-variant-badge-引用カードに-variant-chunk-set-バッジが出る-desktop/citation-card-desktop.png`
- Mobile implementation: `frontend/test-results/citation-variant-badge-引用カードに-variant-chunk-set-バッジが出る-mobile/citation-card-mobile.png`
- Full-page captures: each Playwright result directory's `citation-page-*.png`
- Side-by-side comparison: `/tmp/citation-score-layout-qa.png`

## Review

| Check | Result | Evidence |
|---|---|---|
| Container density | Passed | RAG search and KB search lists no longer add a second border, background, or large padding around cards. |
| Desktop hierarchy | Passed | Identity, three-line body, and metadata occupy the left column while the compact 176px score rail stays independently aligned at right; body text no longer waits below the score rail. |
| Score semantics | Passed | Retrieval `0.048` is shown as an unscaled three-decimal value with no meter; Rerank `0.869` uses a fixed 0–1 meter and renders at approximately 86.9%. |
| Score edge cases | Passed | Missing/non-finite Rerank shows only `Rerank 未実行`; negative and above-one values clamp the meter without changing the displayed value. |
| Mobile layout | Passed | At 375px content, score rail, and footer stack in that order, controls remain at least 44px high, and the page has no horizontal overflow. |
| Body preview | Passed | Citation text uses 14px comfortable leading and a computed three-line clamp. |
| Metadata | Passed | Page, content kind, recipe, and other metadata share one wrapping chip row. |
| Actions and feedback | Passed | A divider anchors the footer; preview/deep-link remain left and feedback remains right. Mobile controls measure at least 44px high. |
| Existing behavior | Passed | Preview, deep link, feedback controls, bounded scrolling, and all four consumers remain covered by desktop/mobile Playwright. |

final result: passed

---

# Retrieval candidate columns design QA

## Evidence

- Source visual truth: `/root/.codex/attachments/21605ad8-a19b-40f7-8de1-958c42a531af/codex-clipboard-21f2f9ae-2b55-4d4c-9668-fdd614a6decf.png`
- Desktop implementation: `frontend/test-results/citation-variant-badge-引用カードに-variant-chunk-set-バッジが出る-desktop/candidate-preview-desktop.png`
- Mobile implementation: `frontend/test-results/citation-variant-badge-引用カードに-variant-chunk-set-バッジが出る-mobile/candidate-preview-mobile.png`
- Focused comparison: `/tmp/candidate-file-column-qa.png`
- Viewports: 1440px desktop and 375px mobile; diagnostics open, candidate collapsed.

## Findings

- No actionable P0/P1/P2 findings. The filename and candidate preview now have independent desktop headers and grid columns; disclosure affordance and score-column alignment remain unchanged.
- The chunk ID remains internal data only, while the candidate column shows the beginning of the original text with single-line ellipsis.
- The mobile layout keeps the preview readable without horizontal page overflow. No image assets are involved.

## Patches made

- Reused the existing filename i18n key and candidate row grid; no API, component, or dependency changes.
- Added desktop/mobile regression coverage for the independent filename column, visible preview, hidden ID, disclosure, and keyboard behavior.

final result: passed
