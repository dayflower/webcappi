#!/usr/bin/env python3
"""Install checksummed Linux Helm/Kubernetes tooling into an isolated directory."""

import argparse
import hashlib
import io
from pathlib import Path
import platform
import subprocess
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
HELM_VERSION = "4.3.0"
KIND_VERSION = "0.33.0"
KUBERNETES_VERSION = "1.36.4"
KUBECONFORM_VERSION = "0.7.0"
NODE_IMAGE = (
    "kindest/node:v1.36.4@sha256:"
    "099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed"
)
SCHEMA_REVISION = "f7d855e49c34b039c9ef2196444910275e31e09e"
APPARMOR_VERSION = "4.0.1really4.0.1-0ubuntu0.24.04.9"
CHECKSUMS = {
    "apparmor": {
        "amd64": "59af23f557d666039708ec176aa056d2fd85125dabd452accf6d86f5c608fb74",
        "arm64": "ed3c59de9e0c2e04976552cca187fc435c88892153df92e6c7bbb513cd470e5c",
    },
    "kind": {
        "amd64": "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d",
        "arm64": "20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2",
    },
    "helm": {
        "amd64": "86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb",
        "arm64": "31c5794dd55c66a51e6b7d2e2ac7a114ae8b1de41ff1d9ba51748ac973b06a08",
    },
    "kubectl": {
        "amd64": "8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a",
        "arm64": "0ecf44450ee6063bf19dd166a103ee6df4a9034455c2abce626e6eea657d73fb",
    },
    "kubeconform": {
        "amd64": "c31518ddd122663b3f3aa874cfe8178cb0988de944f29c74a0b9260920d115d3",
        "arm64": "cc907ccf9e3c34523f0f32b69745265e0a6908ca85b92f41931d4537860eb83c",
    },
}
SCHEMAS = {
    "configmap-v1.json": "e0eaddebd677c08aa092b2da2264d86ac4fc34eed112b9fac2945b3f00c1e9b1",
    "service-v1.json": "8bf019854daed511e7c174896a898173fa65d88ec5937c687a37303d4cc9351b",
    "deployment-apps-v1.json": "3725782fb01e3f27d8be2da565e2d653d7b78bf6debe5440804cea993c87b8f9",
}


def fetch(url, checksum):
    with urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != checksum:
        raise RuntimeError(f"checksum mismatch: {url}")
    return data


def write(path, data, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o755 if executable else 0o644)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "bin/chart-tools")
    args = parser.parse_args()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine()
    )
    if platform.system() != "Linux" or not arch:
        parser.error("supported tool hosts: Linux amd64/arm64")
    sources = {
        "helm": (
            f"https://get.helm.sh/helm-v{HELM_VERSION}-linux-{arch}.tar.gz",
            f"linux-{arch}/helm",
        ),
        "kind": (
            f"https://github.com/kubernetes-sigs/kind/releases/download/v{KIND_VERSION}/kind-linux-{arch}",
            None,
        ),
        "kubectl": (
            f"https://dl.k8s.io/release/v{KUBERNETES_VERSION}/bin/linux/{arch}/kubectl",
            None,
        ),
        "kubeconform": (
            f"https://github.com/yannh/kubeconform/releases/download/v{KUBECONFORM_VERSION}/kubeconform-linux-{arch}.tar.gz",
            "kubeconform",
        ),
    }
    for name, (url, member) in sources.items():
        archive_path = args.destination / f"{name}.download"
        checksum = CHECKSUMS[name][arch]
        if (
            archive_path.exists()
            and hashlib.sha256(archive_path.read_bytes()).hexdigest() == checksum
        ):
            data = archive_path.read_bytes()
        else:
            data = fetch(url, checksum)
            write(archive_path, data)
        if member:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                entry = archive.getmember(member)
                if not entry.isfile():
                    raise RuntimeError(f"not a regular file: {member}")
                binary = archive.extractfile(entry).read()
        else:
            binary = data
        write(args.destination / name, binary, executable=True)
    # Extract the parser only: no package installation, service or host policy changes.
    archive_path = args.destination / "apparmor.download"
    checksum = CHECKSUMS["apparmor"][arch]
    if (
        not archive_path.exists()
        or hashlib.sha256(archive_path.read_bytes()).hexdigest() != checksum
    ):
        mirror = (
            "archive.ubuntu.com/ubuntu"
            if arch == "amd64"
            else "ports.ubuntu.com/ubuntu-ports"
        )
        write(
            archive_path,
            fetch(
                f"https://{mirror}/pool/main/a/apparmor/apparmor_{APPARMOR_VERSION}_{arch}.deb",
                checksum,
            ),
        )
    data = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(archive_path)],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        write(
            args.destination / "apparmor_parser",
            archive.extractfile("./sbin/apparmor_parser").read(),
            executable=True,
        )
        write(
            args.destination / "apparmor/abi/4.0",
            archive.extractfile("./etc/apparmor.d/abi/4.0").read(),
        )
    for name, checksum in SCHEMAS.items():
        target = args.destination / "schemas" / name
        if (
            target.exists()
            and hashlib.sha256(target.read_bytes()).hexdigest() == checksum
        ):
            continue
        url = f"https://raw.githubusercontent.com/yannh/kubernetes-json-schema/{SCHEMA_REVISION}/v{KUBERNETES_VERSION}-standalone-strict/{name}"
        write(target, fetch(url, checksum))
    print(f"Installed pinned chart tools in {args.destination}")


if __name__ == "__main__":
    main()
