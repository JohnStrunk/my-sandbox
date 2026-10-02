from pathlib import Path

import pytest


@pytest.mark.unit
def test_ci_lima_installer_requires_and_uses_sha256_artifact_record(
    repo_root: Path,
):
    script = (repo_root / "scripts" / "install-lima-ci.sh").read_text()

    assert ".tools.limactl.integrity" in script
    assert '[[ "$integrity" != sha256 ]]' in script
    assert ".tools.limactl.artifacts.$checksum_arch.version" in script
    assert ".tools.limactl.artifacts.$checksum_arch.sha256" in script
    assert '"${artifact_version#v}" != "$version"' in script
    assert "releases/download/${artifact_version}/${archive}" in script
    assert "sha256sum --check --status" in script
