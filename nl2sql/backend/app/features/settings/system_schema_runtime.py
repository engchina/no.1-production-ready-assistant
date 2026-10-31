"""Schema epoch を process-local cache / persistence readiness へ反映する。"""

from __future__ import annotations

import logging
import threading

from app.features.nl2sql.ontology_router import ontology_runtime
from app.features.nl2sql.service import nl2sql_service

from .system_schema import system_schema_manager

logger = logging.getLogger(__name__)
_epoch_lock = threading.Lock()
_observed_epoch: int | None = None


def reset_system_schema_runtime(*, schema_epoch: int | None = None) -> None:
    """現在 process の NL2SQL/Ontology cache を破棄し、永続化を再確認する。"""

    global _observed_epoch
    if nl2sql_service.uses_incremental_store:
        nl2sql_service.reset_after_system_schema_change()
        ontology_runtime.reset_after_system_schema_change()
        nl2sql_service.recover_persistence()
    if schema_epoch is not None:
        with _epoch_lock:
            _observed_epoch = schema_epoch


def observe_system_schema_epoch() -> bool:
    """readiness 時に別 replica の schema change を一度だけ反映する。"""

    global _observed_epoch
    current = system_schema_manager.current_epoch()
    if current is None:
        return False
    with _epoch_lock:
        previous = _observed_epoch
        _observed_epoch = current
    if previous is None or previous == current:
        return False
    reset_system_schema_runtime(schema_epoch=current)
    logger.info(
        "nl2sql_system_schema_epoch_observed",
        extra={"previous_schema_epoch": previous, "schema_epoch": current},
    )
    return True


def reset_observed_system_schema_epoch() -> None:
    """test isolation 用。"""

    global _observed_epoch
    with _epoch_lock:
        _observed_epoch = None
