"""Unit tests for the Docker RPM availability gate (no live network)."""

import gzip
import hashlib
import json
from pathlib import Path
from urllib.error import URLError

import pytest
from compression import zstd

import scripts.check_docker_rpm_availability as dra

_VERSIONS = {
    "docker_ce": "98.7.6",
    "containerd_io": "97.6.5",
    "docker_buildx_plugin": "96.5.4",
}
_PACKAGES = (
    ("docker-ce", "docker_ce", 3),
    ("docker-ce-cli", "docker_ce", 1),
    ("containerd.io", "containerd_io", 0),
    ("docker-buildx-plugin", "docker_buildx_plugin", 0),
)


def _write_manifest(tmp_path: Path, versions: dict[str, str]) -> Path:
    manifest = {
        "tools": {tool: {"version": version} for tool, version in versions.items()}
    }
    path = tmp_path / "tool-versions.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def _primary_xml(
    *,
    omit: set[tuple[str, str]] | None = None,
    epochs: dict[tuple[str, str], int] | None = None,
    omit_epoch: set[tuple[str, str]] | None = None,
    hrefs: dict[tuple[str, str], str] | None = None,
    release: str = "1.fc44",
    extra_releases: dict[tuple[str, str], str] | None = None,
    extra_hrefs: dict[tuple[str, str], str] | None = None,
) -> bytes:
    omit = omit or set()
    epochs = epochs or {}
    omit_epoch = omit_epoch or set()
    hrefs = hrefs or {}
    extra_releases = extra_releases or {}
    extra_hrefs = extra_hrefs or {}
    entries = []
    for arch in dra.SUPPORTED_ARCHES:
        for name, tool, default_epoch in _PACKAGES:
            key = (name, arch)
            if key in omit:
                continue
            version = _VERSIONS[tool]
            epoch = epochs.get(key, default_epoch)
            epoch_attr = "" if key in omit_epoch else f' epoch="{epoch}"'
            href = hrefs.get(key, f"Packages/{name}-{version}-{release}.{arch}.rpm")
            entries.append(
                f'  <package type="rpm">\n'
                f"    <name>{name}</name>\n"
                f"    <arch>{arch}</arch>\n"
                f'    <version{epoch_attr} ver="{version}" rel="{release}"/>\n'
                f'    <location href="{href}"/>\n'
                f"  </package>"
            )
            if key not in extra_releases:
                continue
            extra = extra_releases[key]
            extra_href = extra_hrefs.get(
                key, f"Packages/{name}-{version}-{extra}.{arch}.rpm"
            )
            entries.append(
                f'  <package type="rpm">\n'
                f"    <name>{name}</name>\n"
                f"    <arch>{arch}</arch>\n"
                f'    <version{epoch_attr} ver="{version}" rel="{extra}"/>\n'
                f'    <location href="{extra_href}"/>\n'
                f"  </package>"
            )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xmlns="http://linux.duke.edu/metadata/common">\n'
        + "\n".join(entries)
        + "\n</metadata>\n"
    ).encode()


def _repomd(primary: bytes, href: str = "repodata/primary.xml.zst") -> bytes:
    digest = hashlib.sha256(primary).hexdigest()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<repomd xmlns="http://linux.duke.edu/metadata/repo">\n'
        '  <data type="primary">\n'
        f'    <checksum type="sha256">{digest}</checksum>\n'
        f'    <location href="{href}"/>\n'
        "  </data>\n"
        "</repomd>\n"
    ).encode()


class _FakeRepo:
    def __init__(
        self,
        primaries: dict[str, bytes],
        *,
        hrefs: dict[str, str] | None = None,
        head_error: str | None = None,
        head_failures: set[str] | None = None,
    ) -> None:
        self.primaries = primaries
        self.repomd = {
            arch: _repomd(primary, (hrefs or {}).get(arch, "repodata/primary.xml.zst"))
            for arch, primary in primaries.items()
        }
        self.head_error = head_error
        self.head_failures = head_failures or set()
        self.fetches: list[str] = []
        self.heads: list[str] = []

    def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes:
        self.fetches.append(url)
        for arch in dra.SUPPORTED_ARCHES:
            if f"/{arch}/stable/" in url:
                if url.endswith("repomd.xml"):
                    return self.repomd[arch]
                return self.primaries[arch]
        raise AssertionError(f"unexpected URL: {url}")

    def head(self, url: str, timeout: float) -> None:
        self.heads.append(url)
        if self.head_error and self.head_error in url:
            raise dra.DockerRepoError(f"HEAD {url} returned 404")
        if any(fragment in url for fragment in self.head_failures):
            raise dra.DockerRepoError(f"HEAD {url} returned 404")


class _RetryRepo(_FakeRepo):
    def __init__(
        self,
        primaries: dict[str, bytes],
        *,
        primary_status: int = 404,
        primary_failures: dict[str, int] | None = None,
        checksum_failures: dict[str, int] | None = None,
    ) -> None:
        super().__init__(primaries)
        self.primary_status = primary_status
        self.primary_failures = primary_failures or {}
        self.checksum_failures = checksum_failures or {}
        self.repomd_fetches = 0

    def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes:
        if url.endswith("repomd.xml"):
            self.repomd_fetches += 1
            return super().fetch(url, max_bytes, timeout)
        arch = next(arch for arch in dra.SUPPORTED_ARCHES if f"/{arch}/stable/" in url)
        if self.primary_failures.get(arch, 0):
            self.primary_failures[arch] -= 1
            self.fetches.append(url)
            raise dra.DockerRepoError(
                f"could not fetch {url}: HTTP {self.primary_status}",
                status=self.primary_status,
            )
        if self.checksum_failures.get(arch, 0):
            self.checksum_failures[arch] -= 1
            return super().fetch(url, max_bytes, timeout) + b"stale"
        return super().fetch(url, max_bytes, timeout)


def _repo(
    *,
    primary: bytes | None = None,
    primaries: dict[str, bytes] | None = None,
    **kwargs,
) -> _FakeRepo:
    if primaries is None:
        payload = primary if primary is not None else zstd.compress(_primary_xml())
        primaries = dict.fromkeys(dra.SUPPORTED_ARCHES, payload)
    return _FakeRepo(primaries, **kwargs)


def _write_template(tmp_path: Path, releases: dict[str, str]) -> Path:
    images = []
    for arch, release in releases.items():
        images.append(
            "  - location: "
            f'"https://download.fedoraproject.org/pub/fedora/linux/releases/'
            f'{release}/Cloud/{arch}/images/Fedora-Cloud-Base-Generic.qcow2"\n'
            f'    arch: "{arch}"'
        )
    path = tmp_path / "devbox.yaml"
    path.write_text("images:\n" + "\n".join(images) + "\n")
    return path


def _check(repo: _FakeRepo, tmp_path: Path, versions: dict[str, str] | None = None):
    return dra.check_docker_rpm_availability(
        manifest_path=_write_manifest(tmp_path, versions or _VERSIONS),
        fetch=repo.fetch,
        head=repo.head,
    )


@pytest.mark.unit
def test_checks_all_pinned_packages_for_both_arches(tmp_path: Path):
    repo = _repo()

    errors = _check(repo, tmp_path)

    assert errors == []
    assert {url.split("/")[-1] for url in repo.heads} == {
        f"{name}-{_VERSIONS[tool]}-1.fc44.{arch}.rpm"
        for name, tool, _epoch in _PACKAGES
        for arch in dra.SUPPORTED_ARCHES
    }
    assert {url.split("/")[6] for url in repo.heads} == set(dra.SUPPORTED_ARCHES)
    release = dra.detect_fedora_release()
    assert all(f"fedora/{release}/" in url for url in repo.heads + repo.fetches)


@pytest.mark.unit
def test_uses_release_from_both_template_image_urls(tmp_path: Path):
    template = _write_template(tmp_path, {"x86_64": "47", "aarch64": "47"})
    repo = _repo()

    errors = dra.check_docker_rpm_availability(
        manifest_path=_write_manifest(tmp_path, _VERSIONS),
        devbox_template=template,
        fetch=repo.fetch,
        head=repo.head,
    )

    assert errors == []
    assert all("fedora/47/" in url for url in repo.heads + repo.fetches)


@pytest.mark.unit
def test_rejects_inconsistent_template_releases(tmp_path: Path):
    template = _write_template(tmp_path, {"x86_64": "47", "aarch64": "48"})

    with pytest.raises(dra.DockerRepoError, match="inconsistent"):
        dra.detect_fedora_release(template)


@pytest.mark.unit
def test_rejects_template_missing_an_architecture(tmp_path: Path):
    template = _write_template(tmp_path, {"x86_64": "47"})

    with pytest.raises(dra.DockerRepoError, match="missing: aarch64"):
        dra.detect_fedora_release(template)


@pytest.mark.unit
def test_rejects_unreadable_template(tmp_path: Path):
    with pytest.raises(dra.DockerRepoError, match="unable to read"):
        dra.detect_fedora_release(tmp_path / "missing.yaml")


@pytest.mark.unit
def test_accepts_any_package_release(tmp_path: Path):
    primary = zstd.compress(_primary_xml(release="77.fc44"))
    repo = _repo(primary=primary)

    assert _check(repo, tmp_path) == []


@pytest.mark.unit
def test_accepts_packages_without_an_epoch_attribute(tmp_path: Path):
    no_epoch = {("containerd.io", arch) for arch in dra.SUPPORTED_ARCHES}
    no_epoch |= {("docker-buildx-plugin", arch) for arch in dra.SUPPORTED_ARCHES}
    primary = zstd.compress(_primary_xml(omit_epoch=no_epoch))
    repo = _repo(primary=primary)

    assert _check(repo, tmp_path) == []


@pytest.mark.unit
def test_reports_missing_package_for_one_architecture(tmp_path: Path):
    primary = zstd.compress(_primary_xml(omit={("containerd.io", "aarch64")}))
    repo = _repo(primary=primary)

    errors = _check(repo, tmp_path)

    assert len(errors) == 1
    assert "aarch64" in errors[0]
    assert "containerd.io" in errors[0]
    assert "97.6.5" in errors[0]


@pytest.mark.unit
def test_reports_wrong_epoch_as_missing(tmp_path: Path):
    primary = zstd.compress(_primary_xml(epochs={("docker-ce", "x86_64"): 2}))
    repo = _repo(primary=primary)

    errors = _check(repo, tmp_path)

    assert len(errors) == 1
    assert "x86_64" in errors[0]
    assert "docker-ce 3:98.7.6" in errors[0]


@pytest.mark.unit
def test_reports_head_failure_as_unavailable(tmp_path: Path):
    repo = _repo(head_error="docker-ce-cli-98.7.6-1.fc44.aarch64.rpm")

    errors = _check(repo, tmp_path)

    assert len(errors) == 1
    assert "docker-ce-cli 1:98.7.6" in errors[0]
    assert "listed but unavailable" in errors[0]


@pytest.mark.unit
def test_uses_an_available_alternate_release(tmp_path: Path):
    key = ("docker-ce-cli", "aarch64")
    listed = "docker-ce-cli-98.7.6-1.fc44.aarch64.rpm"
    alternate = "docker-ce-cli-98.7.6-2.fc44.aarch64.rpm"
    primary = zstd.compress(_primary_xml(extra_releases={key: "2.fc44"}))
    repo = _repo(primary=primary, head_failures={listed})

    errors = _check(repo, tmp_path)

    assert errors == []
    attempted = [
        url.rsplit("/", 1)[-1]
        for url in repo.heads
        if "docker-ce-cli-" in url and url.endswith(".aarch64.rpm")
    ]
    assert attempted == [listed, alternate]


@pytest.mark.unit
def test_reports_unavailable_when_no_release_artifact_responds(tmp_path: Path):
    key = ("docker-ce-cli", "aarch64")
    listed = "docker-ce-cli-98.7.6-1.fc44.aarch64.rpm"
    alternate = "docker-ce-cli-98.7.6-2.fc44.aarch64.rpm"
    primary = zstd.compress(_primary_xml(extra_releases={key: "2.fc44"}))
    repo = _repo(primary=primary, head_failures={listed, alternate})

    errors = _check(repo, tmp_path)

    assert len(errors) == 1
    assert "listed but unavailable in 2 release(s)" in errors[0]
    assert "1.fc44" in errors[0]
    assert "2.fc44" in errors[0]


@pytest.mark.unit
def test_rejects_an_untrusted_alternate_release_location(tmp_path: Path):
    key = ("docker-ce-cli", "aarch64")
    primary = zstd.compress(
        _primary_xml(
            extra_releases={key: "2.fc44"},
            extra_hrefs={key: "https://evil.example/docker-ce-cli.rpm"},
        )
    )
    # The first candidate must fail so the untrusted alternate is attempted.
    repo = _repo(primary=primary, head_failures={"-1.fc44.aarch64.rpm"})

    with pytest.raises(dra.DockerRepoError, match="not an allowed"):
        _check(repo, tmp_path)


@pytest.mark.unit
def test_rejects_primary_checksum_mismatch(tmp_path: Path):
    primary = zstd.compress(_primary_xml())
    repo = _repo(primaries=dict.fromkeys(dra.SUPPORTED_ARCHES, primary))
    repo.primaries = dict.fromkeys(dra.SUPPORTED_ARCHES, primary + b"tampered")

    with pytest.raises(dra.DockerRepoError, match="SHA-256 mismatch"):
        _check(repo, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 410])
def test_retries_primary_404_with_a_fresh_repomd_once(tmp_path: Path, status: int):
    primary = zstd.compress(_primary_xml())
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        primary_status=status,
        primary_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 1),
    )

    assert _check(repo, tmp_path) == []
    assert repo.repomd_fetches == 4


@pytest.mark.unit
def test_retries_a_primary_checksum_mismatch_once(tmp_path: Path):
    primary = zstd.compress(_primary_xml())
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        checksum_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 1),
    )

    assert _check(repo, tmp_path) == []
    assert repo.repomd_fetches == 4


@pytest.mark.unit
def test_does_not_retry_non_retryable_primary_errors(tmp_path: Path):
    primary = zstd.compress(_primary_xml())
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        primary_status=500,
        primary_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 1),
    )

    with pytest.raises(dra.DockerRepoError, match="HTTP 500"):
        _check(repo, tmp_path)
    assert repo.repomd_fetches == 1


@pytest.mark.unit
def test_does_not_retry_a_second_404(tmp_path: Path):
    primary = zstd.compress(_primary_xml())
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        primary_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 2),
    )

    with pytest.raises(dra.DockerRepoError, match="HTTP 404"):
        _check(repo, tmp_path)
    assert repo.repomd_fetches == 2


@pytest.mark.unit
def test_rejects_untrusted_primary_location(tmp_path: Path):
    repo = _repo(
        hrefs=dict.fromkeys(
            dra.SUPPORTED_ARCHES, "https://evil.example/primary.xml.zst"
        )
    )

    with pytest.raises(dra.DockerRepoError, match="not an allowed"):
        _check(repo, tmp_path)


@pytest.mark.unit
def test_rejects_untrusted_package_location(tmp_path: Path):
    primary = zstd.compress(
        _primary_xml(
            hrefs={("docker-ce", "x86_64"): "https://evil.example/docker-ce.rpm"}
        )
    )
    repo = _repo(primary=primary)

    with pytest.raises(dra.DockerRepoError, match="not an allowed"):
        _check(repo, tmp_path)


@pytest.mark.unit
def test_resolves_normal_docker_relative_hrefs():
    base_url = dra._repository_base_url("44", "x86_64")
    prefix = "https://download.docker.com/linux/fedora/44/x86_64/stable/"

    assert (
        dra._resolve_repo_url(base_url, "repodata/repomd.xml")
        == prefix + "repodata/repomd.xml"
    )
    assert (
        dra._resolve_repo_url(base_url, "Packages/docker-ce-98.7.6-1.fc44.x86_64.rpm")
        == prefix + "Packages/docker-ce-98.7.6-1.fc44.x86_64.rpm"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "href",
    [
        "/linux/fedora/44/aarch64/stable/Packages/docker-ce-98.7.6.rpm",
        "https://download.docker.com/linux/fedora/44/aarch64/stable/"
        "Packages/docker-ce-98.7.6.rpm",
        "/linux/fedora/43/x86_64/stable/Packages/docker-ce-98.7.6.rpm",
        "https://download.docker.com/linux/fedora/43/x86_64/stable/"
        "Packages/docker-ce-98.7.6.rpm",
    ],
)
def test_rejects_package_href_from_another_arch_or_release(tmp_path: Path, href: str):
    primary = zstd.compress(_primary_xml(hrefs={("docker-ce", "x86_64"): href}))
    repo = _repo(primary=primary)

    with pytest.raises(
        dra.DockerRepoError, match="outside the Docker Fedora repository subtree"
    ):
        dra.check_docker_rpm_availability(
            manifest_path=_write_manifest(tmp_path, _VERSIONS),
            releasever="44",
            fetch=repo.fetch,
            head=repo.head,
        )
    assert repo.heads == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "href",
    [
        "../aarch64/stable/Packages/docker-ce-98.7.6.rpm",
        "../../43/x86_64/stable/Packages/docker-ce-98.7.6.rpm",
        "Packages/../../aarch64/stable/Packages/docker-ce-98.7.6.rpm",
        "%2e%2e/%2e%2e/43/x86_64/stable/Packages/docker-ce-98.7.6.rpm",
        "%2E%2E/%2E%2E/aarch64/stable/Packages/docker-ce-98.7.6.rpm",
        "../%2e%2e/43/x86_64/stable/Packages/docker-ce-98.7.6.rpm",
        "%252e%252e/aarch64/stable/Packages/docker-ce-98.7.6.rpm",
        "..%2faarch64%2fstable%2fPackages%2fdocker-ce-98.7.6.rpm",
        "https://download.docker.com/linux/fedora/44/x86_64/stable/"
        "%2e%2e/%2e%2e/aarch64/stable/Packages/docker-ce-98.7.6.rpm",
    ],
)
def test_rejects_traversal_in_package_href(tmp_path: Path, href: str):
    primary = zstd.compress(_primary_xml(hrefs={("docker-ce", "x86_64"): href}))
    repo = _repo(primary=primary)

    with pytest.raises(dra.DockerRepoError, match="Docker repository"):
        dra.check_docker_rpm_availability(
            manifest_path=_write_manifest(tmp_path, _VERSIONS),
            releasever="44",
            fetch=repo.fetch,
            head=repo.head,
        )
    assert repo.heads == []


@pytest.mark.unit
def test_rejects_repository_redirects():
    with pytest.raises(dra.DockerRepoError, match="redirected"):
        dra._RejectRedirects().redirect_request(
            None, None, 302, "Found", {}, "https://evil.example/x"
        )


@pytest.mark.unit
def test_rejects_primary_href_from_another_arch(tmp_path: Path):
    cross_arch = "/linux/fedora/44/aarch64/stable/repodata/primary.xml.zst"
    repo = _repo(hrefs=dict.fromkeys(dra.SUPPORTED_ARCHES, cross_arch))

    with pytest.raises(
        dra.DockerRepoError, match="outside the Docker Fedora repository subtree"
    ):
        dra.check_docker_rpm_availability(
            manifest_path=_write_manifest(tmp_path, _VERSIONS),
            releasever="44",
            fetch=repo.fetch,
            head=repo.head,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://download.docker.com/linux/fedora/44/x86_64/stable/repodata/x",
        "https://evil.example/linux/fedora/44/x86_64/stable/repodata/x",
        "https://download.docker.com/linux/ubuntu/44/x86_64/stable/repodata/x",
        "https://download.docker.com:8443/linux/fedora/44/x86_64/stable/x",
        "https://user@download.docker.com/linux/fedora/44/x86_64/stable/x",
        "https://download.docker.com/linux/fedora/44/x86_64/stable/x?y=1",
    ],
)
def test_rejects_urls_outside_the_docker_fedora_repository(url: str):
    with pytest.raises(dra.DockerRepoError, match="not an allowed"):
        dra._require_allowed_repo_url(url)


@pytest.mark.unit
def test_rejects_malformed_primary_xml(tmp_path: Path):
    repo = _repo(primary=zstd.compress(b"<metadata>"))

    with pytest.raises(dra.DockerRepoError, match="could not parse"):
        _check(repo, tmp_path)


@pytest.mark.unit
def test_rejects_dtd_in_primary_metadata(tmp_path: Path):
    malformed = b'<?xml version="1.0"?><!DOCTYPE metadata [<!ENTITY x "y">]><metadata/>'
    repo = _repo(primary=zstd.compress(malformed))

    with pytest.raises(dra.DockerRepoError, match="DTD or entity"):
        _check(repo, tmp_path)


@pytest.mark.unit
def test_rejects_oversized_decompressed_primary():
    compressed = zstd.compress(b"x" * 1000)

    with pytest.raises(dra.DockerRepoError, match="exceeds"):
        dra._decompress_primary(compressed, "primary.xml.zst", 999)


@pytest.mark.unit
def test_rejects_unsupported_primary_compression():
    with pytest.raises(dra.DockerRepoError, match="unsupported"):
        dra._decompress_primary(b"data", "primary.xml.lz4", 100)


@pytest.mark.unit
def test_accepts_a_gzip_primary(tmp_path: Path):
    primary = gzip.compress(_primary_xml())
    repo = _repo(primaries=dict.fromkeys(dra.SUPPORTED_ARCHES, primary))
    repo.repomd = {
        arch: _repomd(primary, "repodata/primary.xml.gz")
        for arch in dra.SUPPORTED_ARCHES
    }

    assert _check(repo, tmp_path) == []


@pytest.mark.unit
def test_network_error_is_reported_as_repository_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise URLError("name resolution failed")

    monkeypatch.setattr(dra._OPENER, "open", fail)

    with pytest.raises(dra.DockerRepoError, match="could not fetch"):
        dra._fetch_bytes(
            "https://download.docker.com/linux/fedora/44/x86_64/stable/x",
            1024,
            1,
        )


@pytest.mark.unit
def test_manifest_versions_drive_the_check(tmp_path: Path):
    arbitrary = {
        "docker_ce": "v98.7.6",
        "containerd_io": "97.6.5",
        "docker_buildx_plugin": "96.5.4",
    }
    expected = dra.load_expected_packages(_write_manifest(tmp_path, arbitrary))

    assert [package.name for package in expected] == [
        name for name, _tool, _epoch in _PACKAGES
    ]
    assert [package.version for package in expected] == [
        "98.7.6",
        "98.7.6",
        "97.6.5",
        "96.5.4",
    ]


@pytest.mark.unit
def test_rejects_manifest_without_a_docker_version(tmp_path: Path):
    path = _write_manifest(tmp_path, {"docker_ce": "98.7.6"})

    with pytest.raises(dra.DockerRepoError, match="containerd_io"):
        dra.load_expected_packages(path)


@pytest.mark.unit
@pytest.mark.parametrize("releasever", ["", "0", "abc", "1000"])
def test_rejects_invalid_fedora_release(releasever: str):
    with pytest.raises(dra.DockerRepoError, match="Fedora release"):
        dra.check_docker_rpm_availability(releasever=releasever)


@pytest.mark.unit
def test_main_reports_success(monkeypatch, capsys):
    monkeypatch.setattr(dra, "detect_fedora_release", lambda _path: "44")
    monkeypatch.setattr(dra, "check_docker_rpm_availability", lambda **_kwargs: [])

    assert dra.main([]) == 0
    assert "Fedora 44" in capsys.readouterr().out


@pytest.mark.unit
def test_main_exits_one_when_a_package_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(dra, "detect_fedora_release", lambda _path: "44")
    monkeypatch.setattr(
        dra,
        "check_docker_rpm_availability",
        lambda **_kwargs: ["x86_64: containerd.io 0:97.6.5 is absent"],
    )

    assert dra.main([]) == 1
    assert "ERROR: x86_64: containerd.io 0:97.6.5 is absent" in capsys.readouterr().err


@pytest.mark.unit
def test_main_exits_two_on_repository_error(monkeypatch, capsys):
    def fail(**_kwargs):
        raise dra.DockerRepoError("could not fetch repomd.xml")

    monkeypatch.setattr(dra, "detect_fedora_release", lambda _path: "44")
    monkeypatch.setattr(dra, "check_docker_rpm_availability", fail)

    assert dra.main([]) == 2
    assert "ERROR: could not fetch repomd.xml" in capsys.readouterr().err
