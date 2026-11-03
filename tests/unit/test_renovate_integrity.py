import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
import tomllib

from scripts.verify_provenance import _render_url, verify_provenance

_CHECKSUM_GROUPS = {
    "hadolint/hadolint": ("hadolint release artifacts", 3),
    "golangci/golangci-lint": ("golangci-lint release artifacts", 3),
    "astral-sh/uv": ("uv release artifacts and Python pins", 2),
    "google-antigravity/antigravity-cli": ("antigravity cli release artifacts", 3),
    "lima-vm/lima": ("limactl release artifacts", 3),
    "kubernetes-sigs/kind": ("kind release artifacts", 3),
    "kubernetes-sigs/krew": ("krew release artifacts", 3),
    "kubernetes/minikube": ("minikube release artifacts", 3),
    "ast-grep/ast-grep": ("ast-grep release artifacts", 3),
    "acli": ("acli release artifacts", 3),
}


def _config_object(config: str, marker: str) -> str:
    marker_index = config.index(marker)
    start = config.rfind("{", 0, marker_index)
    assert start >= 0

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(config)):
        character = config[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return config[start : index + 1]
    raise AssertionError(f"unclosed config object containing {marker!r}")


def _single_quoted_value(config_object: str, key: str) -> str:
    match = re.search(
        rf'"{re.escape(key)}"\s*:\s*(?:\[\s*)?\'((?:\\.|[^\'\\])*)\'',
        config_object,
        re.DOTALL,
    )
    assert match, f"missing single-quoted {key} value"
    escaped = match.group(1).replace('"', r"\"")
    return json.loads(f'"{escaped}"')


def _object_value(config: str, key: str) -> str:
    key_index = config.index('"' + key + '":')
    start = config.index("{", key_index)
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(config)):
        character = config[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == chr(92):
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in (chr(34), chr(39)):
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return config[start : index + 1]
    raise AssertionError(f"unclosed object value for {key!r}")


def _double_quoted_array_value(config_object: str, key: str) -> str:
    key_index = config_object.index('"' + key + '"')
    array_index = config_object.index("[", key_index)
    value_start = config_object.index('"', array_index)
    value, _ = json.JSONDecoder().raw_decode(config_object[value_start:])
    assert isinstance(value, str), f"{key} must contain a JSON string"
    return value


def _acli_formula_artifact(formula: str, arch: str) -> dict[str, str]:
    lines = formula.splitlines()
    url_index = next(
        index
        for index, line in enumerate(lines)
        if "/linux/" in line and f"linux_{arch}.tar.gz" in line
    )
    url_match = re.search(r"/linux/([^/]+)/", lines[url_index])
    digest_match = re.search(r'sha256\s+"([0-9a-f]{64})"', lines[url_index + 1])
    assert url_match, f"missing {arch} formula release version"
    assert digest_match, f"missing {arch} formula SHA-256 line after URL"
    return {"version": url_match.group(1), "digest": digest_match.group(1)}


def _fake_release_digest(
    fixture: dict,
    current_value: str,
    current_digest: str,
    new_value: str,
) -> str:
    current = fixture["current"]
    candidate = fixture["candidate"]
    assert current["tag_name"] == current_value
    assert candidate["tag_name"] == new_value

    current_asset = next(
        asset
        for asset in current["assets"]
        if hashlib.sha256(asset["content"].encode()).hexdigest() == current_digest
    )
    old_version = current["tag_name"].removeprefix("v")
    new_version = candidate["tag_name"].removeprefix("v")
    candidate_name = current_asset["name"].replace(old_version, new_version)
    candidate_asset = next(
        asset for asset in candidate["assets"] if asset["name"] == candidate_name
    )
    return hashlib.sha256(candidate_asset["content"].encode()).hexdigest()


def _simulate_renovate_artifact_update(
    manager: str, manifest_text: str, fixture: dict
) -> tuple[str, list[tuple[str, str, str]]]:
    match_string = _single_quoted_value(manager, "matchStrings")
    template = _single_quoted_value(manager, "autoReplaceStringTemplate")
    assert '"datasourceTemplate": "{{{datasource}}}"' in manager
    python_pattern = re.sub(r"\(\?<([A-Za-z][A-Za-z0-9_]*)>", r"(?P<\1>", match_string)
    pattern = re.compile(python_pattern)
    matches = list(pattern.finditer(manifest_text))
    assert len(matches) == 2

    candidate_tag = fixture["candidate"]["tag_name"]
    updates: list[tuple[str, str, str]] = []
    for match in reversed(matches):
        dep_name = match.group("depName")
        package_name = match.group("packageName")
        current_value = match.group("currentValue")
        current_digest = match.group("currentDigest")
        new_digest = _fake_release_digest(
            fixture, current_value, current_digest, candidate_tag
        )
        replacement = template
        for key, value in {
            "newValue": candidate_tag,
            "newDigest": new_digest,
            "depName": dep_name,
            "packageName": package_name,
            "datasource": match.group("datasource"),
        }.items():
            replacement = replacement.replace(f"{{{{{{{key}}}}}}}", value)
        manifest_text = (
            manifest_text[: match.start()] + replacement + manifest_text[match.end() :]
        )
        updates.append((dep_name, candidate_tag, new_digest))
    return manifest_text, list(reversed(updates))


def _shell_function(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}$", script)
    assert match, f"missing shell function {name!r} in provision-system.sh"
    return match.group(0)


_DOCKER_RPM_TOOLS = ("docker_ce", "containerd_io", "docker_buildx_plugin")
_DOCKER_RPM_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
)
# Docker's RPM epochs, mirrored from the production pin specs.
_DOCKER_RPM_EPOCHS = {
    "docker-ce": 3,
    "docker-ce-cli": 1,
    "containerd.io": 0,
    "docker-buildx-plugin": 0,
}
_DOCKER_KEY_ID = "c52feb6b621e9f35"  # pragma: allowlist secret - public key ID
_DOCKER_PACKAGE_TOOL = {
    "docker-ce": "docker_ce",
    "docker-ce-cli": "docker_ce",
    "containerd.io": "containerd_io",
    "docker-buildx-plugin": "docker_buildx_plugin",
}
# Version DNF is simulated as selecting; deliberately unrelated to the manifest.
_DNF_SELECTED_VERSION = "99.1.1"


def _docker_rpm_block(script: str) -> str:
    """Return the production Docker RPM policy, key pin, and install block.

    The legacy all-true branch reads pins through ``docker_legacy_pin_version``
    rather than the generic ``manifest_version`` so the manifest validator keeps
    treating Docker records with ``consumers.lima: false`` as non-consumers.
    """

    packages = re.search(r"(?m)^docker_rpm_packages=\(.*\)$", script)
    assert packages, "missing Docker RPM package list in provision-system.sh"
    key = re.search(r"(?m)^DOCKER_RPM_SIGNING_KEY_ID=\S+$", script)
    assert key, "missing Docker RPM signing key pin in provision-system.sh"
    helpers = "\n\n".join(
        _shell_function(script, name)
        for name in (
            "docker_rpms_installed",
            "docker_rpm_signing_key_id",
            "verify_docker_rpm_origin",
            "docker_rpm_policy",
            "docker_legacy_pin_version",
        )
    )
    block = re.search(
        r'(?ms)^docker_rpm_mode="\$\(docker_rpm_policy\)" \|\| exit 1\n'
        r".*?^verify_docker_rpm_origin$",
        script,
    )
    assert block, "missing Docker RPM install policy in provision-system.sh"
    return (
        packages.group(0)
        + "\n"
        + key.group(0)
        + "\n\n"
        + helpers
        + "\n\n"
        + block.group(0)
    )


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def _host_rpm_arch() -> str:
    return subprocess.run(
        ["uname", "-m"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _manifest_docker_versions(repo_root: Path) -> dict[str, str]:
    """Return the checked-in manifest versions used by the legacy pin mode."""

    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    return {tool: manifest["tools"][tool]["version"] for tool in _DOCKER_RPM_TOOLS}


def _policy_modes(mode: object) -> dict[str, object]:
    return dict.fromkeys(_DOCKER_RPM_TOOLS, mode)


def _docker_manifest(
    tmp_path: Path,
    repo_root: Path,
    modes: dict[str, object],
    *,
    versions: dict[str, str] | None = None,
    omit_versions: frozenset[str] = frozenset(),
) -> Path:
    """Write a live-manifest stand-in with the requested consumer flags.

    ``modes`` values are booleans, or ``"missing"``/``"invalid"`` to simulate an
    absent or non-boolean ``consumers.lima`` flag. ``versions`` overrides the
    checked-in versions; ``omit_versions`` drops a version record entirely.
    """

    base_versions = _manifest_docker_versions(repo_root)
    base_versions.update(versions or {})
    tools = {}
    for tool in _DOCKER_RPM_TOOLS:
        mode = modes[tool]
        consumers: dict[str, object] = {}
        if mode == "missing":
            pass
        elif mode == "invalid":
            consumers["lima"] = "false"
        else:
            consumers["lima"] = mode
        record: dict[str, object] = {
            "integrity": "version-only",
            "consumers": consumers,
        }
        if tool not in omit_versions:
            record["version"] = base_versions[tool]
        tools[tool] = record
    path = tmp_path / "live-tool-versions.json"
    path.write_text(json.dumps({"tools": tools}, indent=2) + "\n")
    return path


def _expected_pin_specs(versions: dict[str, str]) -> list[str]:
    return [
        f"docker-ce-3:{versions['docker_ce']}",
        f"docker-ce-cli-1:{versions['docker_ce']}",
        f"containerd.io-{versions['containerd_io']}",
        f"docker-buildx-plugin-0:{versions['docker_buildx_plugin']}",
    ]


def _installed_pin_lines(
    versions: dict[str, str], arch: str | None = None
) -> list[str]:
    arch = arch or _host_rpm_arch()
    return [
        f"{name} {arch} {_DOCKER_KEY_ID} {_DOCKER_RPM_EPOCHS[name]} "
        f"{versions[_DOCKER_PACKAGE_TOOL[name]]}"
        for name in _DOCKER_RPM_PACKAGES
    ]


def _fake_docker_rpm_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "installed-rpms"
    log = tmp_path / "dnf.log"
    # State lines: "<name> <arch> <key-id> <epoch> <version>".
    _write_executable(
        fake_bin / "rpm",
        """#!/usr/bin/env bash
set -euo pipefail
package="${!#}"
[[ -f "$RPM_STATE" ]] || exit 1
line="$(grep -m1 -- "^${package} " "$RPM_STATE" || true)"
[[ -n "$line" ]] || exit 1
read -r name arch key epoch version <<<"$line"
if [[ "$*" != *--queryformat* ]]; then
  printf '%s %s\\n' "$package" "$version"
  exit 0
fi
format="$3"
case "$format" in
  *RSAHEADER*)
    printf 'RSA/SHA256, Tue 20 Jan 2026 03:22:11 AM EST, Key ID %s\\n' "$key"
    printf '(none)\\n(none)\\n(none)\\n'
    if [[ -n "${RPM_EXTRA_KEY_ID:-}" ]]; then
      printf 'DSA/SHA256, Tue 20 Jan 2026 03:22:11 AM EST, Key ID %s\\n' \
        "$RPM_EXTRA_KEY_ID"
    fi
    ;;
  *EPOCHNUM*) printf '%s %s %s\\n' "$name" "$epoch" "$version" ;;
  *ARCH*) printf '%s\\n' "$arch" ;;
  *) echo "unexpected rpm query format: $format" >&2; exit 64 ;;
esac
""",
    )
    # The extracted production block clears the dnf cache; intercept that
    # exact command so the harness never touches the host filesystem.
    _write_executable(
        fake_bin / "rm",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" != "-rf /var/cache/dnf" ]]; then
  echo "unexpected rm invocation: $*" >&2
  exit 64
fi
printf 'rm %s\\n' "$*" >>"$DNF_LOG"
""",
    )
    _write_executable(
        fake_bin / "dnf",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$DNF_LOG"
if [[ "$1 $2" == "clean all" ]]; then exit 0; fi
[[ "$1" == install ]] || { echo "unexpected dnf invocation: $*" >&2; exit 64; }
if [[ "${DNF_INSTALL_FAILS:-}" == 1 ]]; then
  echo 'simulated dnf install failure' >&2
  exit 1
fi
if [[ "${DNF_INSTALLS_NOTHING:-}" == 1 ]]; then exit 0; fi
: >"$RPM_STATE"
for argument in "$@"; do
  case "$argument" in
    install|-y|--allow-downgrade|--enablerepo=*|--setopt=*) continue ;;
    docker-ce-3:*)
      name=docker-ce; epoch=3; version="${argument#docker-ce-3:}" ;;
    docker-ce-cli-1:*)
      name=docker-ce-cli; epoch=1; version="${argument#docker-ce-cli-1:}" ;;
    containerd.io-[0-9]*)
      name=containerd.io; epoch=0; version="${argument#containerd.io-}" ;;
    docker-buildx-plugin-0:*)
      name=docker-buildx-plugin; epoch=0
      version="${argument#docker-buildx-plugin-0:}" ;;
    docker-ce) name=docker-ce; epoch=3; version="$DNF_SELECTED_VERSION" ;;
    docker-ce-cli) name=docker-ce-cli; epoch=1; version="$DNF_SELECTED_VERSION" ;;
    containerd.io) name=containerd.io; epoch=0; version="$DNF_SELECTED_VERSION" ;;
    docker-buildx-plugin)
      name=docker-buildx-plugin; epoch=0; version="$DNF_SELECTED_VERSION" ;;
    *) continue ;;
  esac
  printf '%s %s %s %s %s\\n' "$name" "$RPM_ARCH" \
    "${DNF_SIGNING_KEY:-c52feb6b621e9f35}" "$epoch" "$version" >>"$RPM_STATE"
done
""",
    )
    return fake_bin, state, log


def _install_docker_rpms(
    state: Path,
    *,
    arch: str | None = None,
    key: str = _DOCKER_KEY_ID,
    version: str = _DNF_SELECTED_VERSION,
    versions: dict[str, str] | None = None,
    packages: tuple[str, ...] = _DOCKER_RPM_PACKAGES,
) -> None:
    """Install fake RPMs; ``versions`` is keyed by manifest tool name."""

    arch = arch or _host_rpm_arch()
    tool_versions = versions or {}
    per_package = {
        name: tool_versions.get(_DOCKER_PACKAGE_TOOL[name], version)
        for name in _DOCKER_RPM_PACKAGES
    }
    state.write_text(
        "".join(
            f"{name} {arch} {key} {_DOCKER_RPM_EPOCHS[name]} {per_package[name]}\n"
            for name in packages
        )
    )


def _run_docker_rpm_block(
    repo_root: Path,
    fake_bin: Path,
    state: Path,
    log: Path,
    manifest: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = (repo_root / "lima" / "provision-system.sh").read_text()
    harness = (
        "set -euo pipefail\n"
        "declare -A VERSION_ONLY_DIAGNOSTICS=()\n" + _docker_rpm_block(script) + "\n"
    )
    return subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "RPM_STATE": str(state),
            "DNF_LOG": str(log),
            "RPM_ARCH": _host_rpm_arch(),
            "MANIFEST": str(manifest),
            "DNF_SELECTED_VERSION": _DNF_SELECTED_VERSION,
            **(env or {}),
        },
        timeout=15,
    )


@pytest.mark.unit
def test_artifact_regex_matches_manifest_and_noop_replacement_is_stable(
    repo_root: Path,
):
    config = (repo_root / ".github" / "renovate.json5").read_text()
    manager = _config_object(
        config,
        '"description": "Update each checksum-managed architecture artifact '
        'and digest"',
    )
    match_string = _single_quoted_value(manager, "matchStrings")
    template = _single_quoted_value(manager, "autoReplaceStringTemplate")
    pattern = re.compile(
        re.sub(r"\(\?<([A-Za-z][A-Za-z0-9_]*)>", r"(?P<\1>", match_string)
    )
    manifest_text = (repo_root / "lima" / "tool-versions.json").read_text()
    matches = list(pattern.finditer(manifest_text))

    assert len(matches) == 20
    assert {match.group("packageName") for match in matches} == set(_CHECKSUM_GROUPS)
    assert {
        datasource: sum(match.group("datasource") == datasource for match in matches)
        for datasource in {match.group("datasource") for match in matches}
    } == {
        "github-release-attachments": 18,
        "custom.acli-amd64": 1,
        "custom.acli-arm64": 1,
    }

    rewritten = manifest_text
    for match in reversed(matches):
        replacement = template
        for template_key, match_key in (
            ("newValue", "currentValue"),
            ("newDigest", "currentDigest"),
            ("depName", "depName"),
            ("packageName", "packageName"),
            ("datasource", "datasource"),
        ):
            replacement = replacement.replace(
                "{{{" + template_key + "}}}",
                match.group(match_key),
            )
        rewritten = rewritten[: match.start()] + replacement + rewritten[match.end() :]

    assert rewritten == manifest_text


@pytest.mark.unit
def test_release_attachment_regex_updates_both_architectures_and_verifies(
    repo_root: Path, tmp_path: Path
):
    config = (repo_root / ".github" / "renovate.json5").read_text()
    manager = _config_object(
        config,
        '"description": "Update each checksum-managed architecture artifact '
        'and digest"',
    )
    assert "(?<datasource>" in manager
    assert '"datasourceTemplate": "{{{datasource}}}"' in manager
    assert "currentValue" in manager
    assert "currentDigest" in manager
    assert "newValue" in manager
    assert "newDigest" in manager
    assert '"packageName": $v.depName' in config

    fixture = json.loads(
        (
            repo_root
            / "tests"
            / "fixtures"
            / "renovate"
            / "github-release-attachments.json"
        ).read_text()
    )
    current_tag = fixture["current"]["tag_name"]
    old_digests = {
        asset["name"].rsplit("-", 1)[-1].split(".", 1)[0]: hashlib.sha256(
            asset["content"].encode()
        ).hexdigest()
        for asset in fixture["current"]["assets"]
    }
    current_digest_by_arch = {
        "amd64": hashlib.sha256(
            fixture["current"]["assets"][0]["content"].encode()
        ).hexdigest(),
        "arm64": hashlib.sha256(
            fixture["current"]["assets"][1]["content"].encode()
        ).hexdigest(),
    }
    assert current_tag == "v1.0.0"
    assert len(old_digests) == 2

    spec = {
        "version": current_tag.removeprefix("v"),
        "datasource": "github-releases",
        "depName": "example/node-like",
        "integrity": "sha256",
        "artifacts": {
            "amd64": {
                "version": current_tag,
                "sha256": current_digest_by_arch["amd64"],
                "depName": "example/node-like-amd64",
                "packageName": "example/node-like",
                "datasource": "github-release-attachments",
            },
            "arm64": {
                "version": current_tag,
                "sha256": current_digest_by_arch["arm64"],
                "depName": "example/node-like-arm64",
                "packageName": "example/node-like",
                "datasource": "github-release-attachments",
            },
        },
        "provenance": {
            "url_templates": {
                "artifacts.amd64.sha256": (
                    "https://example.test/download/"
                    "{artifacts.amd64.version}/node-{artifacts.amd64.version}-linux-x64.tar.xz"
                ),
                "artifacts.arm64.sha256": (
                    "https://example.test/download/"
                    "{artifacts.arm64.version}/node-{artifacts.arm64.version}-linux-arm64.tar.xz"
                ),
            }
        },
    }
    manifest_text = json.dumps({"tools": {"node_like": spec}}, indent=2) + "\n"

    updated_text, updates = _simulate_renovate_artifact_update(
        manager, manifest_text, fixture
    )
    assert [item[0] for item in updates] == [
        "example/node-like-amd64",
        "example/node-like-arm64",
    ]
    assert {item[1] for item in updates} == {"v1.1.0"}

    updated_manifest = json.loads(updated_text)
    updated_spec = updated_manifest["tools"]["node_like"]
    updated_spec["version"] = fixture["candidate"]["tag_name"].removeprefix("v")
    for arch, asset in zip(
        ("amd64", "arm64"), fixture["candidate"]["assets"], strict=True
    ):
        expected_digest = hashlib.sha256(asset["content"].encode()).hexdigest()
        assert updated_spec["artifacts"][arch]["version"] == "v1.1.0"
        assert updated_spec["artifacts"][arch]["sha256"] == expected_digest
        assert (
            updated_spec["artifacts"][arch]["datasource"]
            == "github-release-attachments"
        )

    manifest = tmp_path / "tool-versions.json"
    manifest.write_text(json.dumps(updated_manifest, indent=2) + "\n")
    expected_by_arch = {
        arch: hashlib.sha256(asset["content"].encode()).hexdigest()
        for arch, asset in zip(
            ("amd64", "arm64"), fixture["candidate"]["assets"], strict=True
        )
    }
    fetch_by_url = {
        _render_url(template, updated_spec): expected_by_arch[field.split(".")[1]]
        for field, template in updated_spec["provenance"]["url_templates"].items()
    }
    assert (
        verify_provenance(
            manifest,
            fetch=lambda url: fetch_by_url[url],
        )
        == []
    )


@pytest.mark.unit
def test_each_checksum_tool_groups_alias_with_both_architecture_dependencies(
    repo_root: Path,
):
    config = (repo_root / ".github" / "renovate.json5").read_text()
    for package_name, (group_name, minimum_group_size) in _CHECKSUM_GROUPS.items():
        rule = _config_object(config, f'"groupName": "{group_name}"')
        assert f'"{package_name}"' in rule
        assert f'"minimumGroupSize": {minimum_group_size}' in rule
    assert "verify_provenance.py --update" not in config
    assert "commitMessageSuffix" not in config
    assert "postUpgradeTasks" not in config


@pytest.mark.unit
def test_uv_renovate_group_includes_the_source_python_pins(repo_root: Path):
    """A uv bump must be tested against the Python pins it has to support."""

    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    config = (repo_root / ".github" / "renovate.json5").read_text()
    rule = _config_object(config, '"groupName": "uv release artifacts and Python pins"')

    assert manifest["tools"]["uv"]["consumers"]["ci"] is True
    assert rule.count('"groupName"') == 1
    assert '"astral-sh/uv"' in rule
    assert '"python"' in rule
    # pep621 extracts requires-python from each pyproject as packageName
    # "python", so both the root and The Source pins join the uv group. The
    # minimum group size of two relies on these exact pins updating together.
    pins = [
        tomllib.loads((repo_root / pyproject).read_text())["project"]["requires-python"]
        for pyproject in ("pyproject.toml", "lima/the-source/pyproject.toml")
    ]
    assert len(set(pins)) == 1
    for pin in pins:
        assert re.fullmatch(r"==\d+\.\d+\.\d+", pin)
    # Two Python pins can group without a uv release also being available.
    assert '"minimumGroupSize": 2' in rule


@pytest.mark.unit
def test_go_version_only_policy_keeps_timestamped_source(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    go = manifest["tools"]["go"]
    config = (repo_root / ".github" / "renovate.json5").read_text()

    assert go["datasource"] == "golang-version"
    assert go["integrity"] == "version-only"
    assert "artifacts" not in go
    assert "provenance" not in go
    assert '"minimumReleaseAge": "10 days"' in config
    assert "custom.golang" not in config
    version_group = _config_object(config, '"groupName": "devbox tool versions"')
    assert '"golang"' in version_group


@pytest.mark.unit
def test_ruby_renovate_pin_waits_for_fedora_44_rpm(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    ruby = manifest["tools"]["ruby"]
    config = (repo_root / ".github" / "renovate.json5").read_text()
    version_group = _config_object(config, '"groupName": "devbox tool versions"')
    ruby_rule = _config_object(
        config,
        '"description": "Wait for Fedora 44\'s Ruby RPM before '
        'bumping the manifest pin"',
    )
    match_package_names = re.search(
        r'"matchPackageNames"\s*:\s*\[(?P<packages>.*?)\]',
        ruby_rule,
        re.DOTALL,
    )

    assert ruby["depName"] == "ruby"
    assert match_package_names
    assert '"ruby"' in match_package_names.group("packages")
    assert '"enabled": false' in ruby_rule
    assert '"ruby"' in version_group


@pytest.mark.unit
def test_docker_rpm_renovate_updates_are_held_while_versions_float(repo_root: Path):
    """Docker RPM versions come from DNF, so Renovate must not bump the pins.

    The manifest entries stay so existing VMs can be described and the
    dependency metadata remains valid, but no automated bump can make a
    reviewed-version tuple a prerequisite for provisioning.
    """

    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    docker_ce = manifest["tools"]["docker_ce"]
    containerd_io = manifest["tools"]["containerd_io"]
    buildx = manifest["tools"]["docker_buildx_plugin"]
    config = (repo_root / ".github" / "renovate.json5").read_text()
    version_group = _config_object(config, '"groupName": "devbox tool versions"')
    hold_rule = _config_object(
        config,
        '"description": "Hold legacy Docker RPM pins for pre-existing VMs; '
        'versions float at VM creation"',
    )

    assert docker_ce["datasource"] == "github-releases"
    assert docker_ce["depName"] == "moby/moby"
    assert re.fullmatch(r"\d+\.\d+\.\d+", docker_ce["version"])
    assert containerd_io["datasource"] == "github-releases"
    assert containerd_io["depName"] == "containerd/containerd"
    assert re.fullmatch(r"\d+\.\d+\.\d+", containerd_io["version"])
    assert buildx["datasource"] == "github-releases"
    assert buildx["depName"] == "docker/buildx"
    assert re.fullmatch(r"\d+\.\d+\.\d+", buildx["version"])

    for package in ("moby/moby", "containerd/containerd", "docker/buildx"):
        assert f'"{package}"' in hold_rule
        assert f'"{package}"' not in version_group
    assert '"enabled": false' in hold_rule
    # The old tag normalization only existed to drive the exact-version pin.
    assert "Normalize Docker Engine release tags" not in config
    assert "^docker-v" not in config


@pytest.mark.unit
def test_rust_renovate_pin_uses_full_semver(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    rust = manifest["tools"]["rust"]
    config = (repo_root / ".github" / "renovate.json5").read_text()
    jsonata_manager = _config_object(config, '"customType": "jsonata"')

    assert rust["datasource"] == "rust-version"
    assert rust["versioning"] == "semver"
    assert re.fullmatch(r"\d+\.\d+\.\d+", rust["version"])
    assert '"versioning": $v.versioning' in jsonata_manager
    assert (
        '"versioningTemplate": "{{#if versioning}}{{{versioning}}}'
        '{{else}}semver-coerced{{/if}}"'
    ) in jsonata_manager


@pytest.mark.unit
def test_floating_policy_installs_unversioned_names_only_when_incomplete(
    repo_root: Path, tmp_path: Path
):
    """A fresh VM lets DNF pick the latest signed set; no version is passed.

    This uses the checked-in manifest, whose three Docker tools currently
    declare no Lima consumer, so the real live file selects floating packages.
    """

    manifest = repo_root / "lima" / "tool-versions.json"
    tools = json.loads(manifest.read_text())["tools"]
    assert all(tools[tool]["consumers"]["lima"] is False for tool in _DOCKER_RPM_TOOLS)
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    installed = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert installed.returncode == 0, installed.stderr
    install_command = log.read_text()
    install_line = next(
        line for line in install_command.splitlines() if line.startswith("install ")
    )
    assert install_line == (
        "install -y --enablerepo=docker-ce-stable "
        "--setopt=install_weak_deps=False --setopt=tsflags=noscripts "
        + " ".join(_DOCKER_RPM_PACKAGES)
    )
    assert "--allow-downgrade" not in install_line
    assert "rm -rf /var/cache/dnf" in install_command
    # The unversioned names leave the choice to DNF: no epoch/version token.
    requested = install_line.split("--setopt=tsflags=noscripts ", 1)[1].split()
    assert requested == list(_DOCKER_RPM_PACKAGES)
    for argument in requested:
        assert ":" not in argument
        assert not any(character.isdigit() for character in argument)
    installed_state = state.read_text()
    for name in _DOCKER_RPM_PACKAGES:
        assert f"{name} {_host_rpm_arch()} {_DOCKER_KEY_ID} " in installed_state


@pytest.mark.unit
def test_floating_complete_set_is_not_refreshed_on_later_boots(
    repo_root: Path, tmp_path: Path
):
    """A complete installed set is trusted as-is; no DNF transaction runs."""

    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state, version="1.2.3")

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode == 0, result.stderr
    assert not log.exists() or log.read_text() == ""
    assert state.read_text().splitlines() == [
        f"{name} {_host_rpm_arch()} {_DOCKER_KEY_ID} {_DOCKER_RPM_EPOCHS[name]} 1.2.3"
        for name in _DOCKER_RPM_PACKAGES
    ]


@pytest.mark.unit
def test_floating_partial_set_installs_all_unversioned_names(
    repo_root: Path, tmp_path: Path
):
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state, packages=("docker-ce",))

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode == 0, result.stderr
    install_line = next(
        line for line in log.read_text().splitlines() if line.startswith("install ")
    )
    assert install_line.endswith(" ".join(_DOCKER_RPM_PACKAGES))
    assert "--allow-downgrade" not in install_line
    assert len(state.read_text().splitlines()) == len(_DOCKER_RPM_PACKAGES)


@pytest.mark.unit
def test_legacy_policy_downgrades_newer_set_to_live_manifest_pins(
    repo_root: Path, tmp_path: Path
):
    """An all-true manifest restores pre-#412 exact-EVR convergence."""

    versions = _manifest_docker_versions(repo_root)
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(True))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state, versions=dict.fromkeys(_DOCKER_RPM_TOOLS, "99.0.0"))

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode == 0, result.stderr
    install_line = next(
        line for line in log.read_text().splitlines() if line.startswith("install ")
    )
    assert install_line.startswith("install -y --allow-downgrade ")
    requested = install_line.split()
    assert requested == [
        "install",
        "-y",
        "--allow-downgrade",
        "--enablerepo=docker-ce-stable",
        "--setopt=install_weak_deps=False",
        "--setopt=tsflags=noscripts",
        *_expected_pin_specs(versions),
    ]
    # The pinned branch wins over the floating branch.
    assert "docker-ce" not in requested
    assert "rm -rf /var/cache/dnf" in log.read_text()
    assert state.read_text().splitlines() == _installed_pin_lines(versions)
    # Docker legacy pins are read outside the generic version-only consumer
    # path, so no manifest-integrity diagnostic is emitted.
    assert "uses version-only integrity" not in result.stderr


@pytest.mark.unit
def test_legacy_policy_matching_pins_skip_dnf(repo_root: Path, tmp_path: Path):
    versions = _manifest_docker_versions(repo_root)
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(True))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state, versions=versions)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode == 0, result.stderr
    assert not log.exists() or log.read_text() == ""
    assert state.read_text().splitlines() == _installed_pin_lines(versions)


@pytest.mark.unit
def test_legacy_policy_normalizes_a_leading_v(repo_root: Path, tmp_path: Path):
    """Legacy pins may carry upstream's ``v`` prefix; install specs do not."""

    versions = _manifest_docker_versions(repo_root)
    prefixed = {tool: f"v{versions[tool]}" for tool in _DOCKER_RPM_TOOLS}
    manifest = _docker_manifest(
        tmp_path, repo_root, _policy_modes(True), versions=prefixed
    )
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode == 0, result.stderr
    install_line = next(
        line for line in log.read_text().splitlines() if line.startswith("install ")
    )
    assert install_line.split()[-4:] == _expected_pin_specs(versions)
    assert state.read_text().splitlines() == _installed_pin_lines(versions)


@pytest.mark.unit
def test_legacy_policy_rejects_an_invalid_pin(repo_root: Path, tmp_path: Path):
    manifest = _docker_manifest(
        tmp_path,
        repo_root,
        _policy_modes(True),
        versions={"docker_ce": "not-a-version"},
    )
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert (
        "invalid legacy Docker pin 'not-a-version' for manifest tool 'docker_ce'"
        in result.stderr
    )
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_legacy_policy_rejects_a_missing_pin(repo_root: Path, tmp_path: Path):
    manifest = _docker_manifest(
        tmp_path,
        repo_root,
        _policy_modes(True),
        omit_versions=frozenset({"docker_ce"}),
    )
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert "legacy Docker pin for 'docker_ce' is missing from the live" in result.stderr
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_docker_legacy_pin_helper_is_separate_from_generic_consumers(
    repo_root: Path,
):
    """The Docker rollback pin reader must not look like a Lima consumer.

    ``scripts/validate_tool_versions.py`` treats ``manifest_version <tool>`` as
    a declared Lima consumer read. Docker records stay ``consumers.lima: false``
    in the current manifest, so the legacy branch uses a dedicated helper.
    """

    script = (repo_root / "lima" / "provision-system.sh").read_text()
    block = _docker_rpm_block(script)

    assert "docker_legacy_pin_version()" in block
    for tool in _DOCKER_RPM_TOOLS:
        assert f"docker_legacy_pin_version {tool}" in block
        assert f"manifest_version {tool}" not in block
    helper = _shell_function(script, "docker_legacy_pin_version")
    assert 'version="${version#v}"' in helper
    assert "invalid legacy Docker pin" in helper
    assert "is missing from the live" in helper


@pytest.mark.unit
def test_legacy_policy_fails_when_pins_do_not_converge(repo_root: Path, tmp_path: Path):
    versions = _manifest_docker_versions(repo_root)
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(True))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state, versions=dict.fromkeys(_DOCKER_RPM_TOOLS, "99.0.0"))

    result = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        manifest,
        env={"DNF_INSTALLS_NOTHING": "1"},
    )

    assert result.returncode != 0
    assert "do not match the live" in result.stderr
    assert "manifest pins" in result.stderr
    assert versions["docker_ce"] in " ".join(
        line for line in log.read_text().splitlines() if line.startswith("install ")
    )


@pytest.mark.unit
def test_mixed_consumer_flags_fail_closed(repo_root: Path, tmp_path: Path):
    modes = _policy_modes(False) | {"containerd_io": True}
    manifest = _docker_manifest(tmp_path, repo_root, modes)
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert "mixed consumers.lima flags" in result.stderr
    assert "refusing to mix pinned and floating installs" in result.stderr
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_missing_consumer_flag_fails_closed(repo_root: Path, tmp_path: Path):
    modes = _policy_modes(False) | {"docker_ce": "missing"}
    manifest = _docker_manifest(tmp_path, repo_root, modes)
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert "has no consumers.lima flag" in result.stderr
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_invalid_consumer_flag_fails_closed(repo_root: Path, tmp_path: Path):
    modes = _policy_modes(False) | {"docker_buildx_plugin": "invalid"}
    manifest = _docker_manifest(tmp_path, repo_root, modes)
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert (
        "invalid consumers.lima 'invalid' for manifest tool "
        "'docker_buildx_plugin'" in result.stderr
    )
    assert "expected true (pre-#412) or false (floating)" in result.stderr
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_floating_install_failure_keeps_installed_state(
    repo_root: Path, tmp_path: Path
):
    """A failed DNF transaction stops provisioning without mutating the set."""

    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    # Start from a partial set so the floating branch attempts a transaction.
    _install_docker_rpms(state, packages=("docker-ce",))
    installed_before = state.read_text()

    result = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        manifest,
        env={"DNF_INSTALL_FAILS": "1"},
    )

    assert result.returncode != 0
    assert "simulated dnf install failure" in result.stderr
    assert state.read_text() == installed_before
    assert "rm -rf /var/cache/dnf" not in log.read_text()


@pytest.mark.unit
def test_unreadable_manifest_fails_closed(repo_root: Path, tmp_path: Path):
    """The live manifest is the policy source; an unreadable one never installs."""

    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        tmp_path / "missing-manifest.json",
    )

    assert result.returncode != 0
    assert "cannot read the Docker RPM consumer policy" in result.stderr
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_docker_rpm_origin_requires_the_pinned_signing_key(
    repo_root: Path, tmp_path: Path
):
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state, key="deadbeefdeadbeef")

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert "signed by key deadbeefdeadbeef" in result.stderr
    assert "pinned Docker release key" in result.stderr
    assert not log.exists() or log.read_text() == ""


@pytest.mark.unit
def test_docker_rpm_signing_key_reads_a_single_match(repo_root: Path, tmp_path: Path):
    """rpm reports several signature tags; the first key ID decides trust."""

    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    _install_docker_rpms(state)

    accepted = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        manifest,
        env={"RPM_EXTRA_KEY_ID": "deadbeefdeadbeef"},
    )
    assert accepted.returncode == 0, accepted.stderr
    assert not log.exists() or log.read_text() == ""

    _install_docker_rpms(state, key="deadbeefdeadbeef")
    rejected = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        manifest,
        env={"RPM_EXTRA_KEY_ID": _DOCKER_KEY_ID},
    )
    assert rejected.returncode != 0
    assert "signed by key deadbeefdeadbeef" in rejected.stderr


@pytest.mark.unit
def test_docker_rpm_install_rejected_when_dnf_selects_an_untrusted_key(
    repo_root: Path, tmp_path: Path
):
    """Origin is re-verified after install, not assumed from the repo URL."""

    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        manifest,
        env={"DNF_SIGNING_KEY": "deadbeefdeadbeef"},
    )

    assert result.returncode != 0
    assert "signed by key deadbeefdeadbeef" in result.stderr


@pytest.mark.unit
def test_docker_rpm_origin_rejects_an_architecture_mismatch(
    repo_root: Path, tmp_path: Path
):
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)
    foreign_arch = "aarch64" if _host_rpm_arch() != "aarch64" else "x86_64"
    _install_docker_rpms(state, arch=foreign_arch)

    result = _run_docker_rpm_block(repo_root, fake_bin, state, log, manifest)

    assert result.returncode != 0
    assert f"is {foreign_arch}, expected {_host_rpm_arch()}" in result.stderr


@pytest.mark.unit
def test_floating_install_fails_when_the_full_set_is_still_missing(
    repo_root: Path, tmp_path: Path
):
    manifest = _docker_manifest(tmp_path, repo_root, _policy_modes(False))
    fake_bin, state, log = _fake_docker_rpm_tools(tmp_path)

    result = _run_docker_rpm_block(
        repo_root,
        fake_bin,
        state,
        log,
        manifest,
        env={"DNF_INSTALLS_NOTHING": "1"},
    )

    assert result.returncode != 0
    assert "required Docker package docker-ce is not installed" in result.stderr


@pytest.mark.unit
def test_acli_uses_scoped_timestamp_exception(repo_root: Path):
    """Check both base and candidate states; CI verifies live artifact bytes.

    The formula fixture is a snapshot for the initial Renovate transition.
    When its version matches the manifest, compare both architecture digests
    with that snapshot. The live check-only provenance verifier remains the
    integrity gate for other versions and future updates.
    """
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    acli = manifest["tools"]["acli"]

    assert acli["datasource"] == "custom.acli"
    assert acli["depName"] == "acli"
    assert acli["versioning"] == "loose"
    assert acli["integrity"] == "sha256"
    assert set(acli["artifacts"]) == {"amd64", "arm64"}
    assert {artifact["packageName"] for artifact in acli["artifacts"].values()} == {
        "acli"
    }

    config = (repo_root / ".github" / "renovate.json5").read_text()
    formula_url = (
        "https://raw.githubusercontent.com/atlassian/homebrew-acli/main/Formula/acli.rb"
    )
    assert formula_url in config
    assert "ryan-pip/acli-versions" not in config
    custom_datasources = _object_value(config, "customDatasources")
    version_source = _object_value(custom_datasources, "acli")
    assert formula_url in version_source
    assert '"format": "plain"' in version_source
    version_transform = _double_quoted_array_value(version_source, "transformTemplates")
    assert '$contains(version, "version ")' in version_transform
    assert "$replace(version" in version_transform

    formula = (
        repo_root / "tests" / "fixtures" / "renovate" / "atlassian-acli-formula.rb"
    ).read_text()
    version_match = re.search(r'^\s*version\s+"([^"]+)"', formula, re.MULTILINE)
    assert version_match
    assert version_match.group(1) == "1.3.39-stable"
    assert {
        arch: artifact["version"] for arch, artifact in acli["artifacts"].items()
    } == dict.fromkeys(("amd64", "arm64"), acli["version"])
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        for artifact in acli["artifacts"].values()
    )
    assert {artifact["depName"] for artifact in acli["artifacts"].values()} == {
        "acli-amd64",
        "acli-arm64",
    }

    for arch in ("amd64", "arm64"):
        artifact = acli["artifacts"][arch]
        formula_artifact = _acli_formula_artifact(formula, arch)
        datasource = f"custom.acli-{arch}"
        assert formula_artifact["version"] == version_match.group(1)
        if artifact["version"] == formula_artifact["version"]:
            assert artifact["sha256"] == formula_artifact["digest"]
        assert artifact["depName"] == f"acli-{arch}"
        assert artifact["datasource"] == datasource
        assert artifact["packageName"] == "acli"

        source_config = _object_value(config, f"acli-{arch}")
        assert formula_url in source_config
        assert '"format": "plain"' in source_config
        transform = _double_quoted_array_value(source_config, "transformTemplates")
        assert "$$.releases#$i" in transform
        assert f"linux_{arch}.tar.gz" in transform
        assert "$i + 1" in transform
        assert '"digest"' in transform

        provenance_field = f"artifacts.{arch}.sha256"
        provenance_template = acli["provenance"]["url_templates"][provenance_field]
        assert f"{{artifacts.{arch}.version}}" in provenance_template
        provenance_url = _render_url(provenance_template, acli)
        assert provenance_url == (
            f"https://acli.atlassian.com/linux/{acli['version']}/"
            f"acli_{acli['version']}_linux_{arch}.tar.gz"
        )

    assert '"minimumReleaseAge": "10 days"' in config
    acli_rule = _config_object(
        config,
        '"description": "Allow acli\'s official formula feed, which has no '
        'release timestamp"',
    )
    assert '"matchPackageNames"' in acli_rule
    assert '"acli"' in acli_rule
    assert '"minimumReleaseAgeBehaviour": "timestamp-optional"' in acli_rule
    coderabbit_rule = _config_object(
        config,
        '"description": "Allow CodeRabbit\'s official version feed, which has no '
        'release timestamp"',
    )
    assert "timestamp-optional" not in config.replace(acli_rule, "").replace(
        coderabbit_rule, ""
    )
    group_rule = _config_object(config, '"groupName": "acli release artifacts"')
    assert '"acli"' in group_rule
    assert '"versioning": "loose"' in group_rule


@pytest.mark.unit
def test_coderabbit_cli_uses_its_official_version_feed(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    coderabbit = manifest["tools"]["coderabbit_cli"]

    assert coderabbit["datasource"] == "custom.coderabbit-cli"
    assert coderabbit["depName"] == "coderabbit-cli"
    assert coderabbit["integrity"] == "version-only"
    assert re.fullmatch(r"\d+\.\d+\.\d+", coderabbit["version"])

    config = (repo_root / ".github" / "renovate.json5").read_text()
    custom_datasources = _object_value(config, "customDatasources")
    source = _object_value(custom_datasources, "coderabbit-cli")
    assert "https://cli.coderabbit.ai/releases/latest/VERSION" in source
    assert '"format": "plain"' in source
    transform = _double_quoted_array_value(source, "transformTemplates")
    assert "$trim(version)" in transform
    assert "releases" in transform

    version_group = _config_object(config, '"groupName": "devbox tool versions"')
    assert '"coderabbit-cli"' in version_group
    timestamp_rule = _config_object(
        config,
        '"description": "Allow CodeRabbit\'s official version feed, which has no '
        'release timestamp"',
    )
    assert '"coderabbit-cli"' in timestamp_rule
    assert '"minimumReleaseAgeBehaviour": "timestamp-optional"' in timestamp_rule
