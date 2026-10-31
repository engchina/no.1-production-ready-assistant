"""bastion の独立した配置先・接続設定と作成時の必須条件。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_stack_contract import (
    ORACLE_LINUX_IMAGES,
    _schema_variable,
    _terraform_variable,
)

ROOT = Path(__file__).resolve().parents[3]


class BastionFormTests(unittest.TestCase):
    def test_dedicated_inputs_are_required_only_when_visible(self) -> None:
        schema = (ROOT / "terraform/stack/schema.yaml").read_text()
        for name in (
            "shape",
            "image_source_id",
            "ssh_authorized_keys",
            "subnet_id",
            "compartment_ocid",
            "availability_domain",
            "vcn_id",
        ):
            with self.subTest(field=name):
                block = _schema_variable(schema, f"bastion_{name}")
                self.assertIn("visible: bastion_enabled", block)
                self.assertIn("required: true", block)

    def test_bastion_has_its_own_defaults(self) -> None:
        schema = (ROOT / "terraform/stack/schema.yaml").read_text()
        self.assertIn(
            'default: "VM.Standard.E5.Flex"', _schema_variable(schema, "bastion_shape")
        )
        self.assertIn(
            f'default: "{ORACLE_LINUX_IMAGES["ap-osaka-1"]}"',
            _schema_variable(schema, "bastion_image_source_id"),
        )

    def test_resource_does_not_reference_main_compute_inputs(self) -> None:
        source = (ROOT / "terraform/stack/bastion.tf").read_text()
        for name in (
            "instance_",
            "ssh_authorized_keys",
            "subnet_ai_subnet_id",
            "availability_domain",
            "compartment_ocid",
            "vcn_ai_vcn_id",
        ):
            with self.subTest(reference=name):
                self.assertNotIn(f"var.{name}", source)


class BastionPlanTests(unittest.TestCase):
    MAIN: ClassVar[dict[str, str]] = {
        "instance_shape": "VM.Standard.E4.Flex",
        "instance_image_source_id": "synthetic-main-image",
        "ssh_authorized_keys": "synthetic-main-public-key",
        "compartment_ocid": "synthetic-main-compartment",
        "availability_domain": "synthetic-main-ad",
        "subnet_ai_subnet_id": "synthetic-main-subnet",
    }
    VALID: ClassVar[dict[str, object]] = {
        "bastion_enabled": True,
        "bastion_compartment_ocid": "synthetic-bastion-compartment",
        "bastion_availability_domain": "synthetic-bastion-ad",
        "bastion_vcn_id": "synthetic-bastion-vcn",
        "bastion_subnet_id": "synthetic-bastion-subnet",
        "bastion_ssh_authorized_keys": "synthetic-public-key-A\nsynthetic-public-key-B\n",
        "bastion_vnc_password": "Test1234",
    }
    EXPECTED: ClassVar[dict[str, str]] = {
        "shape": "VM.Standard.E5.Flex",
        "image": ORACLE_LINUX_IMAGES["ap-osaka-1"],
        "ssh_keys": str(VALID["bastion_ssh_authorized_keys"]),
        "compartment": str(VALID["bastion_compartment_ocid"]),
        "availability_domain": str(VALID["bastion_availability_domain"]),
        "subnet": str(VALID["bastion_subnet_id"]),
    }

    @classmethod
    def setUpClass(cls) -> None:
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        cls.directory = tempfile.TemporaryDirectory(prefix="bastion-plan-", dir=scratch)
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cwd = Path(cls.directory.name)
        variables = (ROOT / "terraform/stack/variables.tf").read_text()
        source = (ROOT / "terraform/stack/bastion.tf").read_text()
        names = (
            *cls.MAIN,
            *re.findall(r'^variable "(bastion_\w+)"', variables, re.MULTILINE),
        )
        lifecycle = re.search(r"(?ms)^  lifecycle \{.*?^  \}", source)
        if lifecycle is None:
            raise AssertionError("bastion の precondition がない")
        fields = {}
        for key, attribute in {
            "shape": "shape",
            "image": "source_id",
            "ssh_keys": '"ssh_authorized_keys"',
            "compartment": "compartment_id",
            "availability_domain": "availability_domain",
            "subnet": "subnet_id",
        }.items():
            match = re.search(
                rf"(?m)^\s*{re.escape(attribute)}\s*= (var\.\w+)\s*$", source
            )
            if match is None:
                raise AssertionError(f"bastion の入力参照がない: {attribute}")
            fields[key] = match.group(1)
        # 実変数・実 resource の入力式・実 precondition を使う。OCI resource を作らず plan だけで確かめる。
        (cls.cwd / "main.tf").write_text(
            "\n".join(_terraform_variable(variables, name) for name in names)
            + '\nresource "terraform_data" "bastion" {\n  count = var.bastion_enabled ? 1 : 0\n  input = {\n'
            + "\n".join(f"    {key} = {value}" for key, value in fields.items())
            + "\n  }\n"
            + lifecycle.group(0)
            + "\n}\n"
        )
        cls.terraform = os.environ.get("TERRAFORM_BIN", "terraform")
        result = cls.run_cli("init", "-backend=false", "-input=false", "-no-color")
        if result.returncode != 0:
            raise AssertionError(result.stdout)

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

    def plan(self, values: dict[str, object]) -> subprocess.CompletedProcess[str]:
        (self.cwd / "inputs.tfvars.json").write_text(json.dumps(self.MAIN | values))
        return self.run_cli(
            "plan",
            "-input=false",
            "-lock=false",
            "-no-color",
            "-var-file=inputs.tfvars.json",
            "-out=plan.bin",
        )

    def planned_inputs(self, values: dict[str, object]) -> dict[str, str]:
        result = self.plan(values)
        self.assertEqual(result.returncode, 0, result.stdout)
        shown = self.run_cli("show", "-json", "plan.bin")
        self.assertEqual(shown.returncode, 0, shown.stdout)
        return json.loads(shown.stdout)["planned_values"]["root_module"]["resources"][
            0
        ]["values"]["input"]

    def test_own_defaults_are_not_inherited(self) -> None:
        self.assertEqual(self.planned_inputs(self.VALID), self.EXPECTED)

    def test_changing_main_compute_does_not_change_bastion(self) -> None:
        changed = {key: f"changed-{value}" for key, value in self.MAIN.items()}
        changed["instance_shape"] = "VM.Standard.E5.Flex"
        self.assertEqual(self.planned_inputs(self.VALID | changed), self.EXPECTED)

    def test_dedicated_values_reach_resource(self) -> None:
        for variable, key, value in (
            ("bastion_shape", "shape", "VM.Standard.E4.Flex"),
            ("bastion_image_source_id", "image", "synthetic-bastion-image"),
            ("bastion_ssh_authorized_keys", "ssh_keys", "synthetic-other-public-key"),
            ("bastion_subnet_id", "subnet", "synthetic-other-subnet"),
            ("bastion_compartment_ocid", "compartment", "synthetic-other-compartment"),
            (
                "bastion_availability_domain",
                "availability_domain",
                "synthetic-other-ad",
            ),
        ):
            with self.subTest(variable=variable):
                self.assertEqual(
                    self.planned_inputs(self.VALID | {variable: value}),
                    self.EXPECTED | {key: value},
                )

    def test_enabled_bastion_rejects_missing_or_whitespace_inputs(self) -> None:
        for name in (
            "compartment_ocid",
            "availability_domain",
            "subnet_id",
            "ssh_authorized_keys",
            "image_source_id",
        ):
            for blank in ("", " \t\n"):
                with self.subTest(field=name, blank=repr(blank)):
                    variable = f"bastion_{name}"
                    result = self.plan(self.VALID | {variable: blank})
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("Resource precondition failed", result.stdout)
                    self.assertIn(variable, result.stdout)

    def test_disabled_bastion_does_not_require_connection_inputs(self) -> None:
        result = self.plan({"bastion_enabled": False})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("No changes", result.stdout)

    def test_empty_or_unsupported_shape_is_rejected(self) -> None:
        for shape in ("", " ", "UNSUPPORTED"):
            with self.subTest(shape=shape):
                result = self.plan(self.VALID | {"bastion_shape": shape})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Invalid value for variable", result.stdout)


if __name__ == "__main__":
    unittest.main()
