"""Doclingの任意依存をロードした後に利用する進捗付きPDF pipeline。"""

from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

from rag_engine.parsing.progress import ACTIVE_PAGE_PROGRESS, ProgressOutputQueue


class ProgressPdfPipeline(StandardPdfPipeline):
    """全文変換を保ったまま最終ページ出力を観測する。

    docling-slim 2.123.0の内部API `_create_run_ctx` に依存する。
    Docling更新時は実PDFの進捗・出力互換性も検証する。
    """

    def _create_run_ctx(self):
        """実行専用queueだけを包み、共有モデルとproducer側は変更しない。"""
        context = super()._create_run_ctx()
        progress = ACTIVE_PAGE_PROGRESS.get()
        if progress is not None:
            context.output_queue = ProgressOutputQueue(context.output_queue, progress)
        return context
