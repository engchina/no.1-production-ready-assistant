"""ロギング設定のテスト。"""

import logging

from app.logging_config import _NOISY_LOGGERS, _ServiceStatusAccessFilter, configure_logging


def test_configure_logging_sets_root_level() -> None:
    """ルートロガーのレベルが指定どおりに設定される。"""
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    # 後続テストへ影響しないよう既定へ戻す。
    configure_logging("INFO")


def test_configure_logging_quiets_noisy_loggers() -> None:
    """無害な noisy logger を抑制する。"""
    # 事前に WARNING に下げても configure_logging 後は ERROR に引き上げられる。
    logging.getLogger("pdfminer.pdffont").setLevel(logging.WARNING)

    configure_logging("INFO")

    for name, level in _NOISY_LOGGERS.items():
        logger = logging.getLogger(name)
        assert logger.level == level
        assert not logger.isEnabledFor(logging.INFO)
    # FontBBox 警告(WARNING)は実際に無効化される。
    assert not logging.getLogger("pdfminer.pdffont").isEnabledFor(logging.WARNING)
    # httpx は health check の INFO だけ落とし、警告は残す。
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_configure_logging_filters_service_status_access_logs() -> None:
    """サービス status poll の access log だけを落とす。"""
    configure_logging("INFO")
    access_logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(item, _ServiceStatusAccessFilter) for item in access_logger.filters)
    access_filter = next(
        item for item in access_logger.filters if isinstance(item, _ServiceStatusAccessFilter)
    )
    status_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1:1 - "GET /api/services/parser-mineru/status HTTP/1.1" 200 OK',
        (),
        None,
    )
    document_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1:1 - "GET /api/documents/doc-1 HTTP/1.1" 200 OK',
        (),
        None,
    )

    assert not access_filter.filter(status_record)
    assert access_filter.filter(document_record)


def test_service_status_failures_are_never_suppressed() -> None:
    """成功 poll だけを落とし、認証失敗・サービス障害・未知の形式は残す。"""
    access_filter = _ServiceStatusAccessFilter()
    for status in (200, 204, 401, 403, 404, 429, 500, 503):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("client", "GET", "/api/services/parser/status?probe=1", "1.1", status),
            None,
        )
        assert access_filter.filter(record) == (status >= 300)
    malformed = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '"GET /api/services/parser/status HTTP/1.1" unknown',
        (),
        None,
    )
    assert access_filter.filter(malformed)
