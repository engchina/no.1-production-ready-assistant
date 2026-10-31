"""OCI を呼ばず、配布する stack 入力を Terraform CLI で検証する。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_stack_contract import _schema_groups, _schema_variable, _terraform_variable

ROOT = Path(__file__).resolve().parents[3]


class StackInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        cls.directory = tempfile.TemporaryDirectory(prefix="adb-workload-", dir=scratch)
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cwd = Path(cls.directory.name)
        source = (ROOT / "terraform/stack/variables.tf").read_text()
        # 実際の変数定義だけを隔離し、provider・credential・resource を使わない。
        (cls.cwd / "main.tf").write_text(
            "\n".join(
                _terraform_variable(source, name) for name in ("adb_workload", "region")
            )
            + '\noutput "workload" { value = var.adb_workload }\noutput "region" { value = var.region }\n'
        )
        cls.terraform = os.environ.get("TERRAFORM_BIN", "terraform")
        initialized = cls.run_cli("init", "-backend=false", "-input=false", "-no-color")
        if initialized.returncode != 0:
            raise AssertionError(initialized.stdout)

    @classmethod
    def run_cli(cls, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [cls.terraform, *args],
            cwd=cls.cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )

    def plan(
        self, workload: str | None = None, *, region: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        args = ["plan", "-input=false", "-lock=false", "-no-color"]
        if workload is not None:
            args.append(f"-var=adb_workload={workload}")
        if region is not None:
            args.append(f"-var=region={region}")
        return self.run_cli(*args)

    def test_form_excludes_apex_service(self) -> None:
        schema = (ROOT / "terraform/stack/schema.yaml").read_text()
        block = _schema_variable(schema, "adb_workload")
        self.assertEqual(
            re.findall(r'^      - "([^"]+)"$', block, re.MULTILINE),
            ["OLTP", "AJD", "LH"],
        )
        self.assertIn('default: "LH"', block)

    def test_default_remains_lakehouse(self) -> None:
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('workload = "LH"', result.stdout)
        self.assertRegex(result.stdout, r'region\s*= "ap-osaka-1"')

    def test_supported_workloads(self) -> None:
        for workload in ("OLTP", "AJD", "LH"):
            with self.subTest(workload=workload):
                result = self.plan(workload)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'workload = "{workload}"', result.stdout)

    def test_apex_is_rejected_before_resource_creation(self) -> None:
        result = self.plan("APEX")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Invalid value for variable", result.stdout)
        self.assertIn("SQL*Net", result.stdout)

    def test_unknown_workload_remains_rejected(self) -> None:
        result = self.plan("INVALID")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Invalid value for variable", result.stdout)

    def test_supported_regions(self) -> None:
        for region in ("ap-osaka-1", "ap-tokyo-1", "us-chicago-1"):
            with self.subTest(region=region):
                result = self.plan(region=region)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'region   = "{region}"', result.stdout)

    def test_unknown_region_is_rejected(self) -> None:
        result = self.plan(region="unsupported-region")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Invalid value for variable", result.stdout)

    def test_form_has_an_image_for_each_supported_region(self) -> None:
        schema = (ROOT / "terraform/stack/schema.yaml").read_text()
        for name in ("instance_image_source_id", "bastion_image_source_id"):
            with self.subTest(field=name):
                block = _schema_variable(schema, name)
                regions = re.findall(
                    r'^      - "ocid1\.image\.oc1\.([a-z0-9-]+)\.', block, re.MULTILINE
                )
                self.assertEqual(regions, ["ap-osaka-1", "ap-tokyo-1", "us-chicago-1"])

    def test_compute_and_bastion_networks_are_separate(self) -> None:
        schema = (ROOT / "terraform/stack/schema.yaml").read_text()
        groups = _schema_groups(schema)
        self.assertEqual(
            groups["主 Compute のネットワークと SSH"],
            (
                "true",
                [
                    "availability_domain",
                    "vcn_ai_vcn_id",
                    "subnet_ai_subnet_id",
                    "ssh_authorized_keys",
                ],
            ),
        )
        self.assertEqual(
            groups["bastion のネットワークと接続"],
            (
                "bastion_enabled",
                [
                    "bastion_compartment_ocid",
                    "bastion_availability_domain",
                    "bastion_vcn_id",
                    "bastion_subnet_id",
                    "bastion_ssh_authorized_keys",
                    "bastion_vnc_password",
                ],
            ),
        )


if __name__ == "__main__":
    unittest.main()
