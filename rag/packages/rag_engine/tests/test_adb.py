"""adb の挙動を保護するテスト。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from rag_engine.adapters.oracle.connection import connect_adb_thin, load_adb_settings


class AdbTests(unittest.TestCase):
    def test_connect_passes_only_credentials_and_connect_settings(self):
        for dsn in ("localhost:1521/FREEPDB1", "tcp://localhost:1521/FREEPDB1", "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)))"):
            with self.subTest(dsn=dsn):
                settings = load_adb_settings(
                    environ={
                        "ADB_DSN": dsn,
                        "ADB_DB_USER": "admin",
                        "ADB_DB_PASSWORD": "test-only-password",
                        "ADB_CONNECT_TIMEOUT_SECONDS": "4",
                        "ADB_CONNECT_RETRY_COUNT": "2",
                        "ADB_CONNECT_RETRY_DELAY_SECONDS": "1",
                        "ADB_POOL_MAX": "0",  # 直接 connect の経路を検証する (#887)
                    }
                )
                driver = Mock()
                result = connect_adb_thin(settings, oracledb_module=driver)
                driver.ConnectParams.assert_called_once_with(
                    tcp_connect_timeout=4.0,
                    retry_count=2,
                    retry_delay=1,
                )
                params = driver.ConnectParams.return_value
                params.parse_connect_string.assert_called_once_with(dsn)
                params.set.assert_called_once_with(
                    tcp_connect_timeout=4.0,
                    retry_count=2,
                    retry_delay=1,
                )
                driver.connect.assert_called_once_with(
                    user="admin",
                    password="test-only-password",
                    params=params,
                )
                self.assertIs(result, driver.connect.return_value)

    def test_load_adb_settings_resolves_defaults(self):
        settings = load_adb_settings(
            environ={
                "ADB_DSN": "localhost:1521/FREEPDB1",
                "ADB_DB_PASSWORD": "db-password",
            }
        )

        self.assertEqual(settings.dsn, "localhost:1521/FREEPDB1")
        self.assertEqual(settings.db_user, "admin")
        self.assertEqual(settings.tcp_connect_timeout, 10.0)
        self.assertEqual(settings.connect_retry_count, 0)
        self.assertEqual(settings.connect_retry_delay, 0)

    def test_load_adb_settings_requires_dsn_and_db_password(self):
        with self.assertRaisesRegex(RuntimeError, "ADB_DSN"):
            load_adb_settings(environ={"ADB_DB_PASSWORD": "db-password"})

        with self.assertRaisesRegex(RuntimeError, "ADB_DB_PASSWORD"):
            load_adb_settings(environ={"ADB_DSN": "localhost:1521/FREEPDB1"})

    def test_connect_adb_thin_sets_dsn_and_retry_parameters(self):
        calls = {}

        class FakeConnectParams:
            def __init__(self, **kwargs):
                calls["params_init"] = kwargs

            def parse_connect_string(self, value):
                calls["parse_connect_string"] = value

            def set(self, **kwargs):
                calls["params_set"] = kwargs

        fake_oracledb = SimpleNamespace(
            ConnectParams=FakeConnectParams,
            connect=lambda **kwargs: calls.setdefault("connect", kwargs) or object(),
            defaults=SimpleNamespace(fetch_lobs=True),
        )
        settings = load_adb_settings(
            environ={
                "ADB_DB_USER": "admin",
                "ADB_DB_PASSWORD": "db-password",
                "ADB_DSN": "localhost:1521/FREEPDB1",
                "ADB_CONNECT_TIMEOUT_SECONDS": "7.5",
                "ADB_CONNECT_RETRY_COUNT": "2",
                "ADB_CONNECT_RETRY_DELAY_SECONDS": "1",
                "ADB_POOL_MAX": "0",
            }
        )
        connection = connect_adb_thin(settings, oracledb_module=fake_oracledb)

        self.assertIsNotNone(connection)
        self.assertEqual(calls["connect"]["user"], "admin")
        self.assertEqual(calls["connect"]["password"], "db-password")
        self.assertNotIn("dsn", calls["connect"])
        self.assertIsInstance(calls["connect"]["params"], FakeConnectParams)
        self.assertEqual(calls["params_init"], {"tcp_connect_timeout": 7.5, "retry_count": 2, "retry_delay": 1})
        self.assertEqual(calls["parse_connect_string"], "localhost:1521/FREEPDB1")
        self.assertEqual(calls["params_set"]["tcp_connect_timeout"], 7.5)
        self.assertEqual(calls["params_set"]["retry_count"], 2)
        self.assertEqual(calls["params_set"]["retry_delay"], 1)
        self.assertFalse(fake_oracledb.defaults.fetch_lobs)  # CLOB を str で受け取り LOB の往復を無くす (#846)

    def test_connect_adb_thin_acquires_from_one_pool_per_settings(self):
        # 既定（ADB_POOL_MAX=4）では設定ごとに 1 つのプールを作り、以後は acquire で使い回す (#887)。
        calls = {"create_pool": [], "acquire": 0}

        class FakePoolParams:
            def __init__(self, **kwargs):
                calls["params_init"] = kwargs

            def parse_connect_string(self, value):
                calls["parse_connect_string"] = value

            def set(self, **kwargs):
                pass

        class FakePool:
            def acquire(self):
                calls["acquire"] += 1
                return object()

        def create_pool(**kwargs):
            calls["create_pool"].append(kwargs)
            return FakePool()

        fake_oracledb = SimpleNamespace(
            PoolParams=FakePoolParams,
            ConnectParams=None,
            create_pool=create_pool,
            connect=lambda **kwargs: self.fail("pool があるときは connect しない"),
            defaults=SimpleNamespace(fetch_lobs=True),
        )
        environ = {"ADB_DSN": "localhost:1521/freepdb1", "ADB_DB_PASSWORD": "db-password"}
        settings = load_adb_settings(environ=environ)
        self.assertEqual(settings.pool_max, 4)
        connect_adb_thin(settings, oracledb_module=fake_oracledb)
        connect_adb_thin(settings, oracledb_module=fake_oracledb)
        self.assertEqual(len(calls["create_pool"]), 1)
        self.assertEqual(calls["acquire"], 2)
        self.assertEqual((calls["create_pool"][0]["min"], calls["create_pool"][0]["max"], calls["create_pool"][0]["increment"]), (0, 4, 1))
        self.assertEqual(calls["create_pool"][0]["user"], "admin")
        self.assertEqual(calls["parse_connect_string"], "localhost:1521/freepdb1")
        self.assertFalse(fake_oracledb.defaults.fetch_lobs)
        # 別の設定（pool_max が違う）は別のプール
        other = load_adb_settings(environ={**environ, "ADB_POOL_MAX": "2"})
        connect_adb_thin(other, oracledb_module=fake_oracledb)
        self.assertEqual(len(calls["create_pool"]), 2)


if __name__ == "__main__":
    unittest.main()
