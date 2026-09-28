"""Unit tests for the lima/devbox.yaml VM template and its scripts."""

import re
from pathlib import Path

import pytest
import yaml

LIMA_DIR = "lima"

EXPECTED_MOUNT_LOCATIONS = {
    "~/.agents",
    "~/.config/acli",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.config/gws",
    "~/.config/opencode",
    "~/.local/share/opencode",
    "~/kb",
    "~/src",
}

EXPECTED_SHARED_RELS = sorted(
    location.removeprefix("~/") for location in EXPECTED_MOUNT_LOCATIONS
)

_SCRIPTS = (
    "provision-system.sh",
    "provision-user.sh",
    "probe-readiness.sh",
)


def _template(repo_root: Path) -> dict:
    return yaml.safe_load((repo_root / LIMA_DIR / "devbox.yaml").read_text())


def _script(repo_root: Path, name: str) -> str:
    return (repo_root / LIMA_DIR / name).read_text()


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _shared_rels_from_loop(text: str) -> list[str]:
    """Extract the entries of a `for rel in ...` loop body."""

    match = re.search(r"^for rel in \\\n(.*?)^\s*do$", text, re.DOTALL | re.MULTILINE)
    assert match, "no 'for rel in' loop found"
    entries = []
    for line in match.group(1).splitlines():
        entry = line.strip().rstrip("\\").strip()
        if entry:
            entries.append(entry)
    return entries


@pytest.mark.unit
def test_template_uses_qemu_with_nested_virtualization(repo_root: Path):
    config = _template(repo_root)

    assert config["vmType"] == "qemu"
    assert config["nestedVirtualization"] is True
    # The default cpuType ("host" on Linux/KVM) exposes VMX/SVM to the
    # guest; overriding it would break nested virtualization.
    assert "cpuType" not in config


@pytest.mark.unit
def test_template_minimum_lima_version(repo_root: Path):
    config = _template(repo_root)

    version = config["minimumLimaVersion"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    # 2.1.3 is the floor for the CVE-2026-53657 guest-agent fix.
    assert _version_tuple(version) >= (2, 1, 3)


@pytest.mark.unit
def test_template_resources(repo_root: Path):
    config = _template(repo_root)

    assert 6 <= config["cpus"] <= 8
    assert config["memory"] == "16GiB"
    assert config["disk"] == "100GiB"


@pytest.mark.unit
def test_template_images_are_digest_pinned_fedora(repo_root: Path):
    config = _template(repo_root)

    images = config["images"]
    assert {image["arch"] for image in images} == {"x86_64", "aarch64"}
    for image in images:
        assert image["location"].startswith("https://")
        assert "fedora" in image["location"].lower()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"])


@pytest.mark.unit
def test_template_mounts_are_the_expected_same_path_set(repo_root: Path):
    config = _template(repo_root)

    mounts = config["mounts"]
    locations = {mount["location"] for mount in mounts}
    assert locations == EXPECTED_MOUNT_LOCATIONS
    for mount in mounts:
        assert mount["writable"] is True
        # Same-path mounts: never override the guest mount point, and
        # never mount all of $HOME.
        assert "mountPoint" not in mount
        assert mount["location"] != "~"
    # OpenCode's volatile state stays VM-local.
    assert "~/.local/state/opencode" not in locations


@pytest.mark.unit
def test_template_disables_containerd(repo_root: Path):
    config = _template(repo_root)

    assert config["containerd"] == {"system": False, "user": False}


@pytest.mark.unit
def test_provision_and_probe_scripts_are_referenced_and_valid(
    repo_root: Path,
):
    config = _template(repo_root)

    assert [(entry["mode"], entry["file"]) for entry in config["provision"]] == [
        ("system", "provision-system.sh"),
        ("user", "provision-user.sh"),
    ]
    probes = config["probes"]
    assert len(probes) == 1
    assert probes[0]["mode"] == "readiness"
    assert "file" not in probes[0]
    assert probes[0]["script"] == _script(repo_root, "probe-readiness.sh")

    for name in _SCRIPTS:
        path = repo_root / LIMA_DIR / name
        assert path.is_file(), name
        # Lima requires provision and probe scripts to start with '#!'.
        assert path.read_text().startswith("#!"), name


@pytest.mark.unit
def test_params_are_consumed_by_the_user_script(repo_root: Path):
    config = _template(repo_root)

    assert config["param"] == {"GitUserName": "", "GitUserEmail": ""}
    user_script = _script(repo_root, "provision-user.sh")
    # Lima rejects params that no script references ($PARAM_<Key>).
    assert "PARAM_GitUserName" in user_script
    assert "PARAM_GitUserEmail" in user_script


@pytest.mark.unit
def test_scripts_contain_no_unexpected_go_template_expressions(
    repo_root: Path,
):
    # Lima renders provision/probe scripts as Go templates. Only the
    # system script may use a template variable, and only {{.User}}.
    for name in _SCRIPTS:
        found = re.findall(r"{{.*?}}", _script(repo_root, name))
        if name == "provision-system.sh":
            assert set(found) == {"{{.User}}"}
        else:
            assert found == []


@pytest.mark.unit
def test_user_script_links_exactly_the_mounted_paths(repo_root: Path):
    user_script = _script(repo_root, "provision-user.sh")

    assert _shared_rels_from_loop(user_script) == EXPECTED_SHARED_RELS


@pytest.mark.unit
def test_probe_checks_exactly_the_mounted_paths(repo_root: Path):
    probe = _script(repo_root, "probe-readiness.sh")

    assert _shared_rels_from_loop(probe) == EXPECTED_SHARED_RELS


@pytest.mark.unit
def test_system_script_adds_user_to_kvm_group(repo_root: Path):
    system_script = _script(repo_root, "provision-system.sh")

    assert 'DEVBOX_USER="{{.User}}"' in system_script
    assert 'usermod --append --groups kvm "$DEVBOX_USER"' in system_script
