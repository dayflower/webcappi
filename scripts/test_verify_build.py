"""Exercise OCI platform and executable validation using tiny synthetic images."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "verify_build", Path(__file__).with_name("verify-build.py")
)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class BuildVerificationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.artifacts = Path(self.directory.name)

    def fixture(self, arches=("amd64", "arm64"), mismatch=False, wrong_platform=False):
        blobs = {}

        def blob(data):
            digest = hashlib.sha256(data).hexdigest()
            blobs[f"blobs/sha256/{digest}"] = data
            return {"digest": f"sha256:{digest}"}

        def json_blob(data):
            return blob(json.dumps(data).encode())

        manifests = []
        for arch, machine in VERIFY.MACHINES.items():
            binary = b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", machine)
            destination = self.artifacts / "binaries" / f"linux_{arch}" / "webcappi"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(binary)
            if arch not in arches:
                continue
            if mismatch:
                binary += b"different image executable"
            layer = io.BytesIO()
            with tarfile.open(fileobj=layer, mode="w:gz") as contents:
                entry = tarfile.TarInfo(VERIFY.IMAGE_BINARY)
                entry.size = len(binary)
                contents.addfile(entry, io.BytesIO(binary))
            manifest = json_blob(
                {
                    "config": json_blob({"os": "linux", "architecture": arch}),
                    "layers": [blob(layer.getvalue())],
                }
            )
            manifest["platform"] = {
                "os": "linux",
                "architecture": "wrong" if wrong_platform else arch,
            }
            manifests.append(manifest)
        # An attestation is not an additional runtime platform.
        manifests.append(
            {"annotations": {"vnd.docker.reference.type": "attestation-manifest"}}
        )
        nested = json_blob({"manifests": manifests})
        blobs["index.json"] = json.dumps({"manifests": [nested]}).encode()
        with tarfile.open(self.artifacts / "webcappi.oci.tar", "w") as archive:
            for path, data in blobs.items():
                entry = tarfile.TarInfo(path)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def test_nested_index_and_attestation(self):
        self.fixture()
        report = VERIFY.verify(self.artifacts)
        self.assertEqual(set(report), {"amd64", "arm64"})
        self.assertEqual(report["arm64"]["platform"], "linux/arm64")

    def test_missing_runtime_platform(self):
        self.fixture(arches=("amd64",))
        with self.assertRaisesRegex(ValueError, "Missing runtime platforms"):
            VERIFY.verify(self.artifacts)

    def test_descriptor_config_mismatch(self):
        self.fixture(wrong_platform=True)
        with self.assertRaisesRegex(ValueError, "descriptor/config platform mismatch"):
            VERIFY.verify(self.artifacts)

    def test_different_image_binary(self):
        self.fixture(mismatch=True)
        with self.assertRaisesRegex(ValueError, "binaries differ"):
            VERIFY.verify(self.artifacts)

    def test_incorrect_elf_architecture(self):
        self.fixture()
        amd64 = self.artifacts / "binaries/linux_amd64/webcappi"
        arm64 = self.artifacts / "binaries/linux_arm64/webcappi"
        arm64.write_bytes(amd64.read_bytes())
        with self.assertRaisesRegex(ValueError, "Expected a Linux arm64 ELF64"):
            VERIFY.verify(self.artifacts)


if __name__ == "__main__":
    unittest.main()
