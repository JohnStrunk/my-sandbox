"""Unit tests for the Fedora container preflight and its CI wiring."""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/ci-workflow.yaml")
CONTAINERFILE = Path("lima/fedora-preflight.Containerfile")
PACKAGE_SCRIPT = Path("lima/fedora-packages.sh")
CONTAINER_SCRIPT = Path("scripts/fedora-preflight-container.sh")
RUNTIME_SCRIPT = Path("scripts/fedora-preflight-runtime.sh")
LOCAL_SCRIPT = Path("scripts/fedora-preflight.sh")
DOCKERIGNORE = Path(".dockerignore")


def _workflow(repo_root: Path) -> dict:
    return yaml.safe_load((repo_root / WORKFLOW).read_text())


def _package_array(text: str, name: str) -> str:
    marker = f"{name}=(\n"
    start = text.index(marker) + len(marker)
    return text[start : text.index("\n)", start)]


@pytest.mark.unit
def test_ci_has_a_required_kvm_free_container_preflight_job(repo_root: Path):
    jobs = _workflow(repo_root)["jobs"]
    job = jobs["container-preflight"]

    assert job["name"] == "Fedora Container Preflight"
    assert job["runs-on"] == "ubuntu-latest"
    job_text = yaml.safe_dump(job).lower()
    for vm_only in ("limactl", "vm_preflight", "run-vm-ci", "/dev/kvm", "qemu"):
        assert vm_only not in job_text

    build = next(
        step
        for step in job["steps"]
        if "docker/build-push-action" in step.get("uses", "")
    )
    assert build["with"]["context"] == "."
    assert build["with"]["file"] == str(CONTAINERFILE)
    assert build["with"]["load"] is True
    assert build["with"]["cache-from"] == "type=gha"
    assert build["with"]["cache-to"] == "type=gha,mode=max"

    run = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Run Fedora 44 container preflight"
    )
    assert "--no-build" in run
    assert "my-sandbox-fedora-preflight:ci" in run

    assert "container-preflight" in jobs["ci-success"]["needs"]


@pytest.mark.unit
def test_vm_and_recursive_skip_summaries_are_distinct_and_honest(repo_root: Path):
    jobs = _workflow(repo_root)["jobs"]
    vm_text = " ".join(yaml.safe_dump(jobs["vm"], width=10000).split())
    recursive_text = " ".join(yaml.safe_dump(jobs["recursive-vm"], width=10000).split())

    assert "VM tier skipped (KVM unavailable)" in vm_text
    assert "No Lima VM was created, provisioned, or readiness-checked." in vm_text
    assert "does not replace this VM tier" in vm_text
    assert "VM Tests" != jobs["container-preflight"]["name"]

    assert "Recursive VM tier skipped (host nested KVM unavailable)" in recursive_text
    assert (
        "No Lima-in-Lima VM was created, provisioned, or readiness-checked."
        in recursive_text
    )
    assert "does not replace this recursive tier" in recursive_text


@pytest.mark.unit
def test_containerfile_pins_fedora_44_and_uses_the_runtime_entrypoint(
    repo_root: Path,
):
    containerfile = (repo_root / CONTAINERFILE).read_text()
    template = yaml.safe_load((repo_root / "lima/devbox.yaml").read_text())

    base = re.search(
        r"^FROM fedora:(?P<release>\d+)@sha256:(?P<digest>[0-9a-f]{64})$",
        containerfile,
        re.MULTILINE,
    )
    assert base, "Fedora base image must be pinned by release and index digest"
    assert base.group("release") == "44"
    assert any("/releases/44/" in image["location"] for image in template["images"])
    assert 'ENTRYPOINT ["/opt/devbox/fedora-preflight-runtime.sh"]' in containerfile
    assert str(RUNTIME_SCRIPT) in containerfile
    assert containerfile.index("RUN chmod 0755") < containerfile.index("ENTRYPOINT")


@pytest.mark.unit
def test_dockerignore_excludes_unrelated_build_context(repo_root: Path):
    patterns = [
        line.strip()
        for line in (repo_root / DOCKERIGNORE).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    containerfile = (repo_root / CONTAINERFILE).read_text()

    assert patterns[0] == "*"
    assert patterns.index("!lima/") < patterns.index("lima/*")
    assert patterns.index("lima/*") < patterns.index("!lima/fedora-packages.sh")
    assert patterns.index("!scripts/") < patterns.index("scripts/*")
    assert patterns.index("scripts/*") < patterns.index(
        "!scripts/fedora-preflight-container.sh"
    )
    copy_sources = set(re.findall(r"^COPY\s+(\S+)", containerfile, re.MULTILINE))
    assert copy_sources == {
        str(PACKAGE_SCRIPT),
        "lima/tool-versions.json",
        str(CONTAINER_SCRIPT),
        str(RUNTIME_SCRIPT),
    }
    for source in copy_sources:
        assert f"!{source}" in patterns
    assert ".git" not in patterns
    assert ".worktrees" not in patterns


@pytest.mark.unit
def test_runtime_entrypoint_refreshes_metadata_and_checks_live_pins(
    repo_root: Path,
):
    shared = (repo_root / PACKAGE_SCRIPT).read_text()
    runtime = (repo_root / RUNTIME_SCRIPT).read_text()

    assert "dnf -q makecache --refresh" in shared
    assert "dnf -q repoquery --available" in shared
    assert "devbox_refresh_dnf_metadata" in runtime
    assert "devbox_verify_dnf_package_resolution" in runtime
    assert runtime.index("devbox_refresh_dnf_metadata") < runtime.index(
        "devbox_verify_dnf_package_resolution"
    )
    assert 'devbox_verify_manifest_pinned_ruby_available "$MANIFEST"' in runtime
    assert "require_ruby_package_available" in shared
    assert "updates-archive.gpgcheck=1" in shared
    assert 'RUBY_VERSION="${RUBY_VERSION#v}"' in runtime
    for check in (
        "HADOLINT_VERSION",
        "UV_VERSION",
        "KIND_VERSION",
        "RUBY_VERSION",
    ):
        assert check in runtime


@pytest.mark.unit
def test_package_resolution_reports_repoquery_failure(repo_root: Path, tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dnf = fake_bin / "dnf"
    fake_dnf.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"repoquery"* ]]; then\n'
        '  printf "fake dnf: repository metadata is unavailable\\n" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    fake_dnf.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f'source "{repo_root / PACKAGE_SCRIPT}"\n'
        "devbox_verify_dnf_package_resolution jq\n"
    )

    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "cannot query Fedora repositories for jq" in result.stderr
    assert "fake dnf: repository metadata is unavailable" in result.stderr
    assert "check repository configuration/connectivity" in result.stderr


@pytest.mark.unit
def test_package_resolution_reports_successful_query_without_match(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dnf = fake_bin / "dnf"
    fake_dnf.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"repoquery"* ]]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    fake_dnf.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f'source "{repo_root / PACKAGE_SCRIPT}"\n'
        "devbox_verify_dnf_package_resolution jq\n"
    )

    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert (
        "Fedora package is unavailable in the configured repositories: jq"
        in result.stderr
    )
    assert "cannot query Fedora repositories" not in result.stderr


@pytest.mark.unit
def test_ruby_resolution_check_fails_for_an_unavailable_exact_pin(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dnf_log = tmp_path / "dnf.log"
    fake_dnf = fake_bin / "dnf"
    fake_dnf.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >>"$DNF_LOG"\n'
        'if [[ "$*" == *"repoquery"* && "$*" == *"ruby-4.0.7"* ]]; then\n'
        '  printf "ruby-4.0.9\\n"\n'
        "fi\n"
        'if [[ "$*" == *"repoquery"* && "$*" == *" jq"* ]]; then\n'
        '  printf "jq\\n"\n'
        "fi\n"
        "exit 0\n"
    )
    fake_dnf.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f'source "{repo_root / PACKAGE_SCRIPT}"\n'
        "manifest_version() { printf '%s\\n' 4.0.7; }\n"
        "devbox_refresh_dnf_metadata\n"
        "devbox_verify_dnf_package_resolution jq\n"
        "devbox_verify_manifest_pinned_ruby_available /dev/null\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "DNF_LOG": str(dnf_log),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "ruby-4.0.7 is not available" in result.stderr
    logged = dnf_log.read_text()
    assert "makecache --refresh" in logged
    assert "repoquery" in logged and "ruby-4.0.7" in logged


@pytest.mark.unit
def test_ruby_resolution_check_accepts_an_available_exact_pin(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dnf = fake_bin / "dnf"
    fake_dnf.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"ruby-4.0.7"* ]]; then\n'
        '  printf "ruby-4.0.7\\n"\n'
        "fi\n"
        'if [[ "$*" == *" jq"* ]]; then\n'
        '  printf "jq\\n"\n'
        "fi\n"
        "exit 0\n"
    )
    fake_dnf.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f'source "{repo_root / PACKAGE_SCRIPT}"\n'
        "manifest_version() { printf '%s\\n' 4.0.7; }\n"
        "devbox_refresh_dnf_metadata\n"
        "devbox_verify_dnf_package_resolution jq\n"
        "devbox_verify_manifest_pinned_ruby_available /dev/null\n"
    )

    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_preflight_reuses_shared_package_and_ruby_pin_logic(repo_root: Path):
    shared = (repo_root / PACKAGE_SCRIPT).read_text()
    system = (repo_root / "lima/provision-system.sh").read_text()
    container = (repo_root / CONTAINER_SCRIPT).read_text()

    assert 'copy_repo_file lima/fedora-packages.sh "$fedora_packages_snapshot"' in (
        system
    )
    assert 'source "$fedora_packages_snapshot"' in system
    assert "devbox_install_fedora_packages vm" in system
    assert 'devbox_install_manifest_pinned_ruby "$MANIFEST"' in system

    assert "source /opt/devbox/fedora-packages.sh" in container
    assert "devbox_install_fedora_packages preflight" in container
    assert 'devbox_install_manifest_pinned_ruby "$MANIFEST"' in container
    assert "--allow-downgrade" in shared
    assert "manifest_version ruby" in shared

    common = _package_array(shared, "DEVBOX_FEDORA_PACKAGES")
    vm_only = _package_array(shared, "DEVBOX_FEDORA_VM_PACKAGES")
    assert "rubypick" in common
    assert "fedora-repos-archive" in common
    assert "qemu-kvm" not in common
    assert "libvirt-daemon-kvm" not in common
    for package in ("qemu-kvm", "qemu-img", "edk2-ovmf", "libvirt-daemon-kvm"):
        assert package in vm_only


@pytest.mark.unit
def test_c_cpp_toolchain_packages_remain_vm_only(repo_root: Path):
    shared = (repo_root / PACKAGE_SCRIPT).read_text()
    common = _package_array(shared, "DEVBOX_FEDORA_PACKAGES")
    vm_only = _package_array(shared, "DEVBOX_FEDORA_VM_PACKAGES")

    def exact_package(name: str) -> str:
        return rf"^\s*{re.escape(name)}(?:\s+#.*)?\s*$"

    # The container preflight keeps the existing base build packages.
    for package in ("gcc", "make", "glibc-devel"):
        assert re.search(exact_package(package), common, re.MULTILINE), package
        assert not re.search(exact_package(package), vm_only, re.MULTILINE), package

    # The C/C++/CMake/debugger additions stay out of the shared preflight set.
    for package in ("gcc-c++", "cmake", "gdb", "ninja-build", "pkgconf-pkg-config"):
        assert re.search(exact_package(package), vm_only, re.MULTILINE), package
        assert not re.search(exact_package(package), common, re.MULTILINE), package


@pytest.mark.unit
def test_container_preflight_avoids_vm_only_actions(repo_root: Path):
    container = (repo_root / CONTAINER_SCRIPT).read_text()
    runtime = (repo_root / RUNTIME_SCRIPT).read_text()
    containerfile = (repo_root / CONTAINERFILE).read_text()

    for source in (container, runtime):
        for command in ("systemctl", "limactl", "/dev/kvm", "mount -", "run-vm-ci"):
            assert command not in source
    assert "FROM fedora:44@" in containerfile
    assert str(CONTAINER_SCRIPT) in containerfile
    assert str(PACKAGE_SCRIPT) in containerfile
    assert "/opt/devbox/fedora-preflight-container.sh" in containerfile


@pytest.mark.unit
def test_vm_provisioner_verifies_the_shared_helper_digest_before_sourcing(
    repo_root: Path,
):
    helper = (repo_root / PACKAGE_SCRIPT).read_bytes()
    system = (repo_root / "lima/provision-system.sh").read_text()
    digest = hashlib.sha256(helper).hexdigest()

    assert f"FEDORA_PACKAGES_SHA256={digest}" in system
    verify = system.index('sha256sum "$fedora_packages_snapshot"')
    source = system.index('source "$fedora_packages_snapshot"')
    assert verify < source
    assert "devbox --recreate" in system


@pytest.mark.unit
def test_vm_installs_system_packages_before_the_pinned_ruby(repo_root: Path):
    system = (repo_root / "lima/provision-system.sh").read_text()

    packages = system.index("devbox_install_fedora_packages vm")
    ruby = system.index('devbox_install_manifest_pinned_ruby "$MANIFEST"')
    assert packages < ruby


@pytest.mark.unit
def test_local_entry_point_builds_and_runs_the_preflight(
    repo_root: Path, tmp_path: Path
):
    log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$DOCKER_LOG"\nexit 0\n'
    )
    fake_docker.chmod(0o755)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "DOCKER": str(fake_docker),
        "DOCKER_LOG": str(log),
    }

    built = subprocess.run(
        ["bash", str(repo_root / LOCAL_SCRIPT)],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert built.returncode == 0, built.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("buildx build --file ")
    assert str(repo_root / CONTAINERFILE) in calls[0]
    assert "--tag my-sandbox-fedora-preflight:local --load" in calls[0]
    assert calls[0].endswith(f" {repo_root}")
    assert calls[1] == "run --rm my-sandbox-fedora-preflight:local"

    log.write_text("")
    rerun = subprocess.run(
        [
            "bash",
            str(repo_root / LOCAL_SCRIPT),
            "--no-build",
            "--image",
            "prebuilt:test",
        ],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert rerun.returncode == 0, rerun.stderr
    assert log.read_text().splitlines() == ["run --rm prebuilt:test"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "script",
    [PACKAGE_SCRIPT, CONTAINER_SCRIPT, RUNTIME_SCRIPT, LOCAL_SCRIPT],
    ids=["packages", "container", "runtime", "local"],
)
def test_preflight_scripts_are_valid_bash(repo_root: Path, script: Path):
    result = subprocess.run(
        ["bash", "-n", str(repo_root / script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_checklists_require_a_fresh_l2_for_vm_affecting_changes(repo_root: Path):
    agents = " ".join((repo_root / "AGENTS.md").read_text().split())
    lima_readme = " ".join((repo_root / "lima/README.md").read_text().split())
    readme = " ".join((repo_root / "README.md").read_text().split())

    for text in (agents, lima_readme):
        assert (
            'limactl shell "$name" -- echo hello' in text
            or "limactl shell <instance> -- echo hello" in text
        )
        assert "does not satisfy" in text or "not enough" in text

    assert "fresh L2 from the active checkout" in agents
    assert "Provision a fresh L2 from the active checkout" in lima_readme
    assert "wait for provisioning and readiness" in lima_readme

    assert "./scripts/fedora-preflight.sh" in readme
    assert "GitHub Actions BuildKit cache" in readme
    assert "FEDORA_PREFLIGHT_IMAGE" in readme
    assert "FEDORA_PREFLIGHT_IMAGE" in lima_readme
    assert "does not" in readme
    assert "Fedora Container Preflight" in agents
    assert "lima/fedora-packages.sh" in lima_readme
    assert "devbox --recreate" in lima_readme
