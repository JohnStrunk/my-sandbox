import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.verify_provenance import _render_url, verify_provenance

_CHECKSUM_GROUPS = {
    "hadolint/hadolint": "hadolint release artifacts",
    "astral-sh/uv": "uv release artifacts",
    "google-antigravity/antigravity-cli": "antigravity cli release artifacts",
    "lima-vm/lima": "limactl release artifacts",
    "kubernetes-sigs/kind": "kind release artifacts",
    "kubernetes/minikube": "minikube release artifacts",
    "ast-grep/ast-grep": "ast-grep release artifacts",
    "acli": "acli release artifacts",
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

    assert len(matches) == 16
    assert {match.group("packageName") for match in matches} == set(_CHECKSUM_GROUPS)
    assert {
        datasource: sum(match.group("datasource") == datasource for match in matches)
        for datasource in {match.group("datasource") for match in matches}
    } == {
        "github-release-attachments": 14,
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
    for package_name, group_name in _CHECKSUM_GROUPS.items():
        rule = _config_object(config, f'"groupName": "{group_name}"')
        assert f'"{package_name}"' in rule
        assert '"minimumGroupSize": 3' in rule
    assert "verify_provenance.py --update" not in config
    assert "commitMessageSuffix" not in config
    assert "postUpgradeTasks" not in config


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
def test_docker_ce_renovate_pin_normalizes_moby_release_tags(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    docker_ce = manifest["tools"]["docker_ce"]
    containerd_io = manifest["tools"]["containerd_io"]
    config = (repo_root / ".github" / "renovate.json5").read_text()
    version_group = _config_object(config, '"groupName": "devbox tool versions"')
    extract_rule = _config_object(
        config,
        '"description": "Normalize Docker Engine release tags for the manifest pin"',
    )
    containerd_rule = _config_object(
        config,
        '"description": "Wait for Docker\'s Fedora repo before bumping containerd.io"',
    )

    assert docker_ce["datasource"] == "github-releases"
    assert docker_ce["depName"] == "moby/moby"
    assert re.fullmatch(r"\d+\.\d+\.\d+", docker_ce["version"])
    assert containerd_io["datasource"] == "github-releases"
    assert containerd_io["depName"] == "containerd/containerd"
    assert re.fullmatch(r"\d+\.\d+\.\d+", containerd_io["version"])
    assert '"moby/moby"' in version_group
    assert '"containerd/containerd"' in containerd_rule
    assert '"enabled": false' in containerd_rule
    assert (
        r'"extractVersion": "^docker-v(?<version>\\d+\\.\\d+\\.\\d+)$"' in extract_rule
    )
    parsed_tag = re.fullmatch(r"docker-v(?P<version>\d+\.\d+\.\d+)", "docker-v29.8.2")
    assert parsed_tag
    assert parsed_tag.group("version") == "29.8.2"
    assert (
        re.fullmatch(r"docker-v(?P<version>\d+\.\d+\.\d+)", "docker-v29.9.0-rc.1")
        is None
    )


@pytest.mark.unit
def test_docker_engine_and_containerd_rpm_pair_requires_review(repo_root: Path):
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    tools = manifest["tools"]
    pinned_pair = (tools["docker_ce"]["version"], tools["containerd_io"]["version"])
    reviewed_pairs = {("29.8.2", "2.3.6")}

    assert pinned_pair in reviewed_pairs, (
        "Review Docker Fedora 44 stable RPM metadata for x86_64 and aarch64, "
        "then update docker_ce, containerd_io, and this approved version pair "
        f"together; got {pinned_pair!r}"
    )


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
    assert "timestamp-optional" not in config.replace(acli_rule, "")
    group_rule = _config_object(config, '"groupName": "acli release artifacts"')
    assert '"acli"' in group_rule
    assert '"versioning": "loose"' in group_rule
