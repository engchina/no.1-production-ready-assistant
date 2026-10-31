"""再利用可能な文書登録と質問応答の pipeline。"""

from .pipelines import IngestionPipeline, QueryPipeline

__all__ = ["IngestionPipeline", "QueryPipeline"]
