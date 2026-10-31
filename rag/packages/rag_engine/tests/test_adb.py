"""adb の挙動を保護するテスト。"""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from rag_engine.adapters.oracle.connection import connect_adb_thin, download_adb_wallet, load_adb_settings, validate_mtls_wallet

ADB_OCID = "ocid1.autonomousdatabase.oc1.ap-osaka-1.example"


class AdbTests(unittest.TestCase):
    def test_local_easy_connect_does_not_require_or_pass_wallet_settings(self):
        for dsn in ("localhost:1521/FREEPDB1", "tcp://localhost:1521/FREEPDB1"):
            with self.subTest(dsn=dsn):
                settings = load_adb_settings(
                    environ={
                        "ADB_TNS_ALIAS": dsn,
                        "ADB_DB_USER": "admin",
                        "ADB_DB_PASSWORD": "test-only-password",
                        "ADB_CONNECT_TIMEOUT_SECONDS": "4",
                        "ADB_CONNECT_RETRY_COUNT": "2",
                        "ADB_CONNECT_RETRY_DELAY_SECONDS": "1",
                        "ADB_POOL_MAX": "0",  # 直接 connect の経路を検証する (#887)
                        "ADB_WALLET_DIR": "/nonexistent/local-db-wallet",
                        "ADB_WALLET_PASSWORD": "unused-wallet-password",
                    }
                )
                self.assertFalse(settings.uses_wallet)
                self.assertEqual(settings.ocid, "")
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

    def test_local_settings_without_cloud_credentials_and_wallet_download_guard(self):
        settings = load_adb_settings(
            environ={
                "ADB_TNS_ALIAS": "localhost:1521/FREEPDB1",
                "ADB_DB_PASSWORD": "test-only-password",
            }
        )
        self.assertEqual(settings.wallet_password, "")
        with self.assertRaisesRegex(RuntimeError, "ADB_OCID and ADB_WALLET_PASSWORD"):
            download_adb_wallet(settings, oci_module=Mock())
        with self.assertRaisesRegex(RuntimeError, "ADB_DB_PASSWORD"):
            load_adb_settings(environ={"ADB_TNS_ALIAS": "localhost:1521/FREEPDB1"})

    def test_tcps_and_descriptors_keep_wallet_requirements(self):
        for dsn in ("tcps://adb.example.test:1522/service", "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)))"):
            with self.subTest(dsn=dsn), self.assertRaisesRegex(RuntimeError, "ADB_OCID"):
                load_adb_settings(environ={"ADB_TNS_ALIAS": dsn, "ADB_DB_PASSWORD": "test"})

    def test_load_adb_settings_resolves_defaults_and_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            settings = load_adb_settings(
                env_path,
                {
                    "ADB_OCID": ADB_OCID,
                    "ADB_WALLET_PASSWORD": "wallet-password",
                    "ADB_DB_PASSWORD": "db-password",
                    "OCI_CONFIG_FILE": "/home/opc/.oci/config",
                    "OCI_PROFILE": "DEFAULT",
                },
            )

            self.assertEqual(settings.db_name, "AIRAGADB")
            self.assertEqual(settings.tns_alias, "AIRAGADB_medium")
            self.assertEqual(settings.wallet_dir, Path(tmp) / ".wallets" / "airagadb")
            self.assertEqual(settings.db_user, "admin")
            self.assertEqual(settings.oci_region, "ap-osaka-1")
            self.assertEqual(settings.tcp_connect_timeout, 10.0)
            self.assertEqual(settings.connect_retry_count, 0)
            self.assertEqual(settings.connect_retry_delay, 0)

    def test_load_adb_settings_requires_ocid_and_db_password(self):
        with self.assertRaisesRegex(RuntimeError, "ADB_OCID"):
            load_adb_settings(environ={"ADB_DB_PASSWORD": "db-password"})

        with self.assertRaisesRegex(RuntimeError, "ADB_WALLET_PASSWORD"):
            load_adb_settings(environ={"ADB_OCID": ADB_OCID, "ADB_DB_PASSWORD": "db-password"})

        with self.assertRaisesRegex(RuntimeError, "ADB_DB_PASSWORD"):
            load_adb_settings(environ={"ADB_OCID": ADB_OCID, "ADB_WALLET_PASSWORD": "wallet-password"})

    def test_download_wallet_uses_oci_client_and_extracts_stream(self):
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w") as archive:
            archive.writestr("tnsnames.ora", "AIRAGADB_medium = descriptor")
            archive.writestr("ewallet.pem", "pem")

        class FakeStream:
            closed = False

            def iter_content(self, chunk_size):
                data = zip_bytes.getvalue()
                yield data[:10]
                yield data[10:]

            def close(self):
                self.closed = True

        class FakeDetails:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        calls = {}
        stream = FakeStream()

        class FakeDatabaseClient:
            def __init__(self, config):
                calls["config"] = config

            def generate_autonomous_database_wallet(self, **kwargs):
                calls["wallet_request"] = kwargs
                return SimpleNamespace(data=stream)

        fake_oci = SimpleNamespace(
            config=SimpleNamespace(from_file=lambda **kwargs: {"region": "us-chicago-1", **kwargs}),
            database=SimpleNamespace(
                DatabaseClient=FakeDatabaseClient,
                models=SimpleNamespace(GenerateAutonomousDatabaseWalletDetails=FakeDetails),
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_adb_settings(
                Path(tmp) / ".env",
                {
                    "ADB_OCID": ADB_OCID,
                    "ADB_WALLET_DIR": "wallet",
                    "ADB_WALLET_PASSWORD": "wallet-password",
                    "ADB_DB_PASSWORD": "db-password",
                    "OCI_CONFIG_FILE": "/home/opc/.oci/config",
                    "OCI_PROFILE": "DEFAULT",
                },
            )
            wallet_zip = download_adb_wallet(settings, oci_module=fake_oci)

            self.assertTrue(wallet_zip.exists())
            self.assertEqual((settings.wallet_dir / "tnsnames.ora").read_text(), "AIRAGADB_medium = descriptor")
            self.assertEqual((settings.wallet_dir / "ewallet.pem").read_text(), "pem")

        self.assertEqual(calls["config"]["region"], "ap-osaka-1")
        self.assertEqual(calls["wallet_request"]["autonomous_database_id"], ADB_OCID)
        details = calls["wallet_request"]["generate_autonomous_database_wallet_details"]
        self.assertEqual(details.kwargs["password"], "wallet-password")
        self.assertEqual(details.kwargs["generate_type"], "SINGLE")
        self.assertFalse(details.kwargs["is_regional"])
        self.assertTrue(stream.closed)

    def test_connect_adb_thin_passes_wallet_parameters(self):
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
        with tempfile.TemporaryDirectory() as tmp:
            wallet_dir = Path(tmp) / "wallet"
            wallet_dir.mkdir()
            (wallet_dir / "tnsnames.ora").write_text("AIRAGADB_medium = descriptor", encoding="utf-8")
            (wallet_dir / "ewallet.pem").write_text("pem", encoding="utf-8")
            settings = load_adb_settings(
                Path(tmp) / ".env",
                {
                    "ADB_OCID": ADB_OCID,
                    "ADB_WALLET_DIR": "wallet",
                    "ADB_WALLET_PASSWORD": "wallet-password",
                    "ADB_DB_USER": "admin",
                    "ADB_DB_PASSWORD": "db-password",
                    "ADB_TNS_ALIAS": "AIRAGADB_medium",
                    "ADB_CONNECT_TIMEOUT_SECONDS": "7.5",
                    "ADB_CONNECT_RETRY_COUNT": "2",
                    "ADB_CONNECT_RETRY_DELAY_SECONDS": "1",
                    "ADB_POOL_MAX": "0",
                },
            )
            connection = connect_adb_thin(settings, oracledb_module=fake_oracledb)

        self.assertIsNotNone(connection)
        self.assertEqual(calls["connect"]["user"], "admin")
        self.assertEqual(calls["connect"]["password"], "db-password")
        self.assertNotIn("dsn", calls["connect"])
        self.assertIsInstance(calls["connect"]["params"], FakeConnectParams)
        self.assertEqual(calls["params_init"]["config_dir"], str(settings.wallet_dir))
        self.assertEqual(calls["params_init"]["wallet_location"], str(settings.wallet_dir))
        self.assertEqual(calls["params_init"]["wallet_password"], "wallet-password")
        self.assertEqual(calls["params_init"]["tcp_connect_timeout"], 7.5)
        self.assertEqual(calls["params_init"]["retry_count"], 2)
        self.assertEqual(calls["params_init"]["retry_delay"], 1)
        self.assertEqual(calls["parse_connect_string"], "AIRAGADB_medium")
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
        environ = {"ADB_TNS_ALIAS": "localhost:1521/freepdb1", "ADB_DB_PASSWORD": "db-password"}
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

    def test_connect_adb_thin_requires_mtls_wallet_files(self):
        fake_oracledb = SimpleNamespace(connect=lambda **kwargs: object())
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_adb_settings(
                Path(tmp) / ".env",
                {
                    "ADB_OCID": ADB_OCID,
                    "ADB_WALLET_DIR": "wallet",
                    "ADB_WALLET_PASSWORD": "wallet-password",
                    "ADB_DB_PASSWORD": "db-password",
                },
            )

            with self.assertRaisesRegex(RuntimeError, "ADB mTLS wallet is not ready"):
                connect_adb_thin(settings, oracledb_module=fake_oracledb)

    def test_validate_mtls_wallet_accepts_required_thin_wallet_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet_dir = Path(tmp) / "wallet"
            wallet_dir.mkdir()
            (wallet_dir / "tnsnames.ora").write_text("AIRAGADB_medium = descriptor", encoding="utf-8")
            (wallet_dir / "ewallet.pem").write_text("pem", encoding="utf-8")
            settings = load_adb_settings(
                Path(tmp) / ".env",
                {
                    "ADB_OCID": ADB_OCID,
                    "ADB_WALLET_DIR": "wallet",
                    "ADB_WALLET_PASSWORD": "wallet-password",
                    "ADB_DB_PASSWORD": "db-password",
                },
            )

            validate_mtls_wallet(settings)


if __name__ == "__main__":
    unittest.main()
