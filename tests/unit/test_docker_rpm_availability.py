"""Unit tests for the Docker RPM availability gate (no live network)."""

import gzip
import hashlib
from pathlib import Path
from urllib.error import URLError

import pytest
from compression import zstd

import scripts.check_docker_rpm_availability as dra

_VERSION = "29.9.2"
_RELEASE = "1.fc44"


def _package_entry(
    name: str,
    arch: str,
    *,
    version: str = _VERSION,
    release: str = _RELEASE,
    epoch: int | None = 0,
    href: str | None = None,
) -> str:
    href = href or f"Packages/{name}-{version}-{release}.{arch}.rpm"
    epoch_attr = "" if epoch is None else f' epoch="{epoch}"'
    return (
        '<package type="rpm">\n'
        f"  <name>{name}</name>\n"
        f"  <arch>{arch}</arch>\n"
        f'  <version{epoch_attr} ver="{version}" rel="{release}"/>\n'
        f'  <location href="{href}"/>\n'
        "</package>"
    )


def _entries(
    arch: str,
    *,
    omit: set[str] | frozenset[str] = frozenset(),
    version: str = _VERSION,
    release: str = _RELEASE,
    epoch: int | None = 0,
    versions: dict[str, str] | None = None,
    releases: dict[str, str] | None = None,
    hrefs: dict[str, str] | None = None,
) -> list[str]:
    """Return primary metadata entries for the four required packages."""

    versions = versions or {}
    releases = releases or {}
    hrefs = hrefs or {}
    return [
        _package_entry(
            name,
            arch,
            version=versions.get(name, version),
            release=releases.get(name, release),
            epoch=epoch,
            href=hrefs.get(name),
        )
        for name in dra.DOCKER_RPM_PACKAGES
        if name not in omit
    ]


def _primary_xml(*entry_groups: list[str]) -> bytes:
    entries = [entry for group in entry_groups for entry in group]
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
        head_failures: set[str] | None = None,
    ) -> None:
        self.primaries = primaries
        self.repomd = {
            arch: _repomd(primary, (hrefs or {}).get(arch, "repodata/primary.xml.zst"))
            for arch, primary in primaries.items()
        }
        self.fetches: list[str] = []
        self.head_failures = head_failures or set()
        self.heads: list[tuple[str, float]] = []

    def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes:
        self.fetches.append(url)
        for arch in dra.SUPPORTED_ARCHES:
            if f"/{arch}/stable/" in url:
                if url.endswith("repomd.xml"):
                    return self.repomd[arch]
                return self.primaries[arch]
        raise AssertionError(f"unexpected URL: {url}")

    def head(self, url: str, timeout: float) -> None:
        """Synthetic HEAD checker: never touches the network."""

        self.heads.append((url, timeout))
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
    x86_64: list[str] | None = None,
    aarch64: list[str] | None = None,
    **kwargs,
) -> _FakeRepo:
    primaries = {
        "x86_64": zstd.compress(
            _primary_xml(x86_64 if x86_64 is not None else _entries("x86_64"))
        ),
        "aarch64": zstd.compress(
            _primary_xml(aarch64 if aarch64 is not None else _entries("aarch64"))
        ),
    }
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


def _check(repo: _FakeRepo) -> tuple[list[str], list[str]]:
    return dra.check_docker_rpm_availability(fetch=repo.fetch, head=repo.head)


def _head_names(repo: _FakeRepo) -> list[str]:
    return [url.rsplit("/", 1)[-1] for url, _timeout in repo.heads]


@pytest.fixture(autouse=True)
def _no_live_http(monkeypatch):
    """Fail fast if a test forgets to inject synthetic fetch/HEAD callables."""

    def unexpected(*_args, **_kwargs):
        raise AssertionError("unit tests must not perform live HTTP requests")

    monkeypatch.setattr(dra._OPENER, "open", unexpected)


@pytest.mark.unit
def test_checks_every_required_package_for_both_arches():
    repo = _repo()

    errors, report = _check(repo)

    assert dra.DOCKER_RPM_PACKAGES == (
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
    )
    assert errors == []
    assert report == [
        f"{arch}: {name}: 1 release(s), newest 0:{_VERSION}-{_RELEASE}"
        for arch in dra.SUPPORTED_ARCHES
        for name in dra.DOCKER_RPM_PACKAGES
    ]
    release = dra.detect_fedora_release()
    assert {url.split("/")[6] for url in repo.fetches} == set(dra.SUPPORTED_ARCHES)
    assert all(f"fedora/{release}/" in url for url in repo.fetches)


@pytest.mark.unit
def test_reports_independent_architecture_specific_newest_versions():
    """Architectures are checked separately; differing versions are reported."""

    repo = _repo(
        x86_64=_entries("x86_64", versions={"docker-ce": "29.9.2"}),
        aarch64=_entries("aarch64", versions={"docker-ce": "29.8.1"}),
    )

    errors, report = _check(repo)

    assert errors == []
    assert f"x86_64: docker-ce: 1 release(s), newest 0:29.9.2-{_RELEASE}" in report
    assert f"aarch64: docker-ce: 1 release(s), newest 0:29.8.1-{_RELEASE}" in report
    # Architecture-specific versions are checked independently, not compared.
    assert f"docker-ce-29.9.2-{_RELEASE}.x86_64.rpm" in _head_names(repo)
    assert f"docker-ce-29.8.1-{_RELEASE}.aarch64.rpm" in _head_names(repo)
    assert len(repo.heads) == len(dra.DOCKER_RPM_PACKAGES) * 2


@pytest.mark.unit
def test_reports_missing_package_for_one_architecture_only():
    repo = _repo(aarch64=_entries("aarch64", omit={"containerd.io"}))

    errors, report = _check(repo)

    assert errors == [
        f"aarch64: containerd.io is absent from Docker Fedora "
        f"{dra.detect_fedora_release()} stable repodata"
    ]
    # The other architecture and the remaining aarch64 packages are still
    # checked, reported, and HEADed.
    assert len(report) == len(dra.DOCKER_RPM_PACKAGES) * 2 - 1
    assert not any("aarch64: containerd.io" in line for line in report)
    assert f"containerd.io-{_VERSION}-{_RELEASE}.aarch64.rpm" not in _head_names(repo)
    assert len(repo.heads) == len(dra.DOCKER_RPM_PACKAGES) * 2 - 1


@pytest.mark.unit
def test_reports_every_missing_package_for_both_architectures():
    # The repository still lists other Docker packages, so metadata is valid
    # but none of the four required packages is present for either arch.
    repo = _repo(
        x86_64=[_package_entry("docker-compose-plugin", "x86_64")],
        aarch64=[_package_entry("docker-compose-plugin", "aarch64")],
    )

    errors, report = _check(repo)

    assert report == []
    assert repo.heads == []
    assert len(errors) == len(dra.DOCKER_RPM_PACKAGES) * 2
    for arch in dra.SUPPORTED_ARCHES:
        for name in dra.DOCKER_RPM_PACKAGES:
            assert any(
                error.startswith(f"{arch}: {name} is absent") for error in errors
            )


@pytest.mark.unit
def test_requires_each_package_built_for_the_repository_architecture():
    """A package listed only for the other architecture does not satisfy it."""

    repo = _repo(
        x86_64=[
            _package_entry("docker-ce", "aarch64"),
            *(_entries("x86_64", omit={"docker-ce"})),
        ]
    )

    errors, _report = _check(repo)

    assert errors == [
        f"x86_64: docker-ce is absent from Docker Fedora "
        f"{dra.detect_fedora_release()} stable repodata"
    ]
    assert repo.heads
    assert f"docker-ce-{_VERSION}-{_RELEASE}.x86_64.rpm" not in _head_names(repo)
    assert len(repo.heads) == len(dra.DOCKER_RPM_PACKAGES) * 2 - 1


@pytest.mark.unit
def test_reports_the_newest_epoch_then_version():
    repo = _repo(
        x86_64=[
            _package_entry("docker-ce", "x86_64", epoch=1, version="99.0.0"),
            _package_entry("docker-ce", "x86_64", epoch=3, version="1.0.0"),
            *_entries("x86_64", omit={"docker-ce"}),
        ],
        aarch64=[
            _package_entry("docker-ce", "aarch64", version="29.9.2"),
            _package_entry("docker-ce", "aarch64", version="29.10.0"),
            *_entries("aarch64", omit={"docker-ce"}),
        ],
    )

    errors, report = _check(repo)

    assert errors == []
    assert f"x86_64: docker-ce: 2 release(s), newest 3:1.0.0-{_RELEASE}" in report
    assert f"aarch64: docker-ce: 2 release(s), newest 0:29.10.0-{_RELEASE}" in report
    assert f"docker-ce-1.0.0-{_RELEASE}.x86_64.rpm" in _head_names(repo)
    assert f"docker-ce-29.10.0-{_RELEASE}.aarch64.rpm" in _head_names(repo)
    assert f"docker-ce-99.0.0-{_RELEASE}.x86_64.rpm" not in _head_names(repo)
    assert f"docker-ce-29.9.2-{_RELEASE}.aarch64.rpm" not in _head_names(repo)


# RPM-like ordering pairs: (older, newer). Each spec may override epoch,
# version, and release; unspecified fields use the shared defaults.
_ORDERING_CASES = [
    ({"version": "29.9.2"}, {"version": "30.0.0~rc1"}),
    ({"version": "30.0.0~rc1"}, {"version": "30.0.0"}),
    ({"version": "30.0.0~rc1"}, {"version": "30.0.0a"}),
    ({"version": "1.a"}, {"version": "1.2"}),
    ({"version": "1.9"}, {"version": "1.10"}),
    (
        {"version": "30.0.0", "release": "1.fc44"},
        {"version": "30.0.0", "release": "2.fc44"},
    ),
    ({"epoch": 0, "version": "99.0.0"}, {"epoch": 1, "version": "1.0.0"}),
]
_ORDERING_IDS = [
    "prerelease-30-beats-29",
    "final-beats-own-prerelease",
    "tilde-beats-alpha",
    "alpha-below-numeric",
    "numeric-integer-compare",
    "release-order",
    "epoch-before-version",
]


def _repo_package(spec: dict[str, object]) -> "dra.RepoPackage":
    return dra.RepoPackage(
        name="docker-ce",
        arch="x86_64",
        epoch=int(spec.get("epoch", 0)),
        version=str(spec["version"]),
        release=str(spec.get("release", _RELEASE)),
        href="Packages/unused.rpm",
    )


def _spec_evr(spec: dict[str, object]) -> str:
    return f"{spec.get('epoch', 0)}:{spec['version']}-{spec.get('release', _RELEASE)}"


def _spec_href(spec: dict[str, object], arch: str = "x86_64") -> str:
    return f"docker-ce-{spec['version']}-{spec.get('release', _RELEASE)}.{arch}.rpm"


@pytest.mark.unit
@pytest.mark.parametrize(("older", "newer"), _ORDERING_CASES, ids=_ORDERING_IDS)
def test_version_key_orders_rpm_like(
    older: dict[str, object], newer: dict[str, object]
):
    older_package = _repo_package(older)
    newer_package = _repo_package(newer)

    assert dra._version_key(older_package) < dra._version_key(newer_package)
    assert dra._newest_package([older_package, newer_package]) == newer_package
    assert dra._newest_package([newer_package, older_package]) == newer_package


@pytest.mark.unit
@pytest.mark.parametrize(("older", "newer"), _ORDERING_CASES, ids=_ORDERING_IDS)
def test_selects_the_rpm_newest_artifact_for_report_and_head(
    older: dict[str, object], newer: dict[str, object]
):
    repo = _repo(
        x86_64=[
            _package_entry("docker-ce", "x86_64", **older),
            _package_entry("docker-ce", "x86_64", **newer),
            *_entries("x86_64", omit={"docker-ce"}),
        ]
    )

    errors, report = _check(repo)

    assert errors == []
    assert f"x86_64: docker-ce: 2 release(s), newest {_spec_evr(newer)}" in report
    newest_href = _spec_href(newer)
    older_href = _spec_href(older)
    assert newest_href in _head_names(repo)
    assert older_href not in _head_names(repo)
    assert _head_names(repo).count(newest_href) == 1


@pytest.mark.unit
def test_accepts_any_package_release():
    repo = _repo(
        x86_64=_entries("x86_64", release="77.fc44"),
        aarch64=_entries("aarch64", release="77.fc44"),
    )

    errors, report = _check(repo)

    assert errors == []
    assert all(line.endswith("newest 0:29.9.2-77.fc44") for line in report)


@pytest.mark.unit
def test_accepts_packages_without_an_epoch_attribute():
    repo = _repo(
        x86_64=_entries("x86_64", epoch=None),
        aarch64=_entries("aarch64", epoch=None),
    )

    errors, report = _check(repo)

    assert errors == []
    assert all(line.endswith(f"newest 0:{_VERSION}-{_RELEASE}") for line in report)


@pytest.mark.unit
def test_heads_each_newest_artifact_once_with_the_requested_timeout():
    repo = _repo()

    errors, _report = dra.check_docker_rpm_availability(
        fetch=repo.fetch, head=repo.head, timeout=7.5
    )

    assert errors == []
    assert len(repo.heads) == len(dra.DOCKER_RPM_PACKAGES) * 2
    assert len({url for url, _timeout in repo.heads}) == len(repo.heads)
    assert {timeout for _url, timeout in repo.heads} == {7.5}


@pytest.mark.unit
def test_heads_only_the_newest_release_for_each_package():
    repo = _repo(
        aarch64=[
            *_entries("aarch64", releases={"docker-ce": "9.fc44"}),
            _package_entry("docker-ce", "aarch64", release=_RELEASE),
        ]
    )

    errors, report = _check(repo)

    assert errors == []
    assert "aarch64: docker-ce: 2 release(s), newest 0:29.9.2-9.fc44" in report
    newest_href = "docker-ce-29.9.2-9.fc44.aarch64.rpm"
    older_href = f"docker-ce-29.9.2-{_RELEASE}.aarch64.rpm"
    assert _head_names(repo).count(newest_href) == 1
    assert older_href not in _head_names(repo)
    assert len(repo.heads) == len(dra.DOCKER_RPM_PACKAGES) * 2


@pytest.mark.unit
def test_accepts_an_unreachable_older_release_when_the_newest_responds():
    repo = _repo(
        x86_64=[
            *_entries("x86_64", releases={"docker-ce": "9.fc44"}),
            _package_entry("docker-ce", "x86_64", release=_RELEASE),
        ],
        head_failures={f"docker-ce-29.9.2-{_RELEASE}.x86_64.rpm"},
    )

    errors, report = _check(repo)

    assert errors == []
    assert "x86_64: docker-ce: 2 release(s), newest 0:29.9.2-9.fc44" in report


@pytest.mark.unit
def test_reports_an_unreachable_newest_artifact():
    failed_href = f"docker-ce-cli-{_VERSION}-{_RELEASE}.aarch64.rpm"
    repo = _repo(head_failures={failed_href})

    errors, report = _check(repo)

    assert len(errors) == 1
    error = errors[0]
    assert error.startswith(f"aarch64: docker-ce-cli newest 0:{_VERSION}-{_RELEASE}")
    assert "artifact is unavailable" in error
    assert "HEAD " in error and "returned 404" in error
    assert not any(line.startswith("aarch64: docker-ce-cli:") for line in report)
    assert len(report) == len(dra.DOCKER_RPM_PACKAGES) * 2 - 1
    # One unreachable artifact does not stop the remaining checks.
    assert len(repo.heads) == len(dra.DOCKER_RPM_PACKAGES) * 2
    assert _head_names(repo).count(failed_href) == 1


@pytest.mark.unit
def test_rejects_an_untrusted_older_release_before_any_head():
    repo = _repo(
        x86_64=[
            *_entries("x86_64", releases={"docker-ce": "9.fc44"}),
            _package_entry(
                "docker-ce",
                "x86_64",
                release=_RELEASE,
                href="https://evil.example/docker-ce.rpm",
            ),
        ]
    )

    with pytest.raises(dra.DockerRepoError, match="not an allowed"):
        dra.check_docker_rpm_availability(
            releasever="44", fetch=repo.fetch, head=repo.head
        )
    assert repo.heads == [], "every listed location is validated before any HEAD"


@pytest.mark.unit
def test_rejects_unsupported_architecture():
    with pytest.raises(dra.DockerRepoError, match="unsupported architecture"):
        dra.check_docker_rpm_availability(
            releasever="44", arches=("riscv64",), fetch=_repo().fetch
        )


@pytest.mark.unit
def test_uses_release_from_both_template_image_urls(tmp_path: Path):
    template = _write_template(tmp_path, {"x86_64": "47", "aarch64": "47"})
    repo = _repo()

    errors, _report = dra.check_docker_rpm_availability(
        devbox_template=template, fetch=repo.fetch, head=repo.head
    )

    assert errors == []
    assert all("fedora/47/" in url for url in repo.fetches)
    assert all("fedora/47/" in url for url, _timeout in repo.heads)


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
def test_rejects_primary_checksum_mismatch():
    repo = _repo()
    repo.primaries = {
        arch: primary + b"tampered" for arch, primary in repo.primaries.items()
    }

    with pytest.raises(dra.DockerRepoError, match="SHA-256 mismatch"):
        _check(repo)


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 410])
def test_retries_primary_404_with_a_fresh_repomd_once(status: int):
    primary = zstd.compress(_primary_xml(_entries("x86_64"), _entries("aarch64")))
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        primary_status=status,
        primary_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 1),
    )

    errors, _report = _check(repo)

    assert errors == []
    assert repo.repomd_fetches == 4


@pytest.mark.unit
def test_retries_a_primary_checksum_mismatch_once():
    primary = zstd.compress(_primary_xml(_entries("x86_64"), _entries("aarch64")))
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        checksum_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 1),
    )

    errors, _report = _check(repo)

    assert errors == []
    assert repo.repomd_fetches == 4


@pytest.mark.unit
def test_does_not_retry_non_retryable_primary_errors():
    primary = zstd.compress(_primary_xml(_entries("x86_64"), _entries("aarch64")))
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        primary_status=500,
        primary_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 1),
    )

    with pytest.raises(dra.DockerRepoError, match="HTTP 500"):
        _check(repo)
    assert repo.repomd_fetches == 1


@pytest.mark.unit
def test_does_not_retry_a_second_404():
    primary = zstd.compress(_primary_xml(_entries("x86_64"), _entries("aarch64")))
    repo = _RetryRepo(
        dict.fromkeys(dra.SUPPORTED_ARCHES, primary),
        primary_failures=dict.fromkeys(dra.SUPPORTED_ARCHES, 2),
    )

    with pytest.raises(dra.DockerRepoError, match="HTTP 404"):
        _check(repo)
    assert repo.repomd_fetches == 2


@pytest.mark.unit
def test_rejects_untrusted_primary_location():
    repo = _repo()
    repo.repomd = {
        arch: _repomd(repo.primaries[arch], "https://evil.example/primary.xml.zst")
        for arch in dra.SUPPORTED_ARCHES
    }

    with pytest.raises(dra.DockerRepoError, match="not an allowed"):
        _check(repo)


@pytest.mark.unit
@pytest.mark.parametrize(
    "href",
    [
        # Foreign host and non-HTTPS scheme.
        "https://evil.example/docker-ce.rpm",
        "http://download.docker.com/linux/fedora/44/x86_64/stable/"
        "Packages/docker-ce.rpm",
        # Absolute locations in another architecture or Fedora release.
        "/linux/fedora/44/aarch64/stable/Packages/docker-ce.rpm",
        "https://download.docker.com/linux/fedora/43/x86_64/stable/"
        "Packages/docker-ce.rpm",
        # Traversal, including percent-encoded separators and dot segments.
        "../aarch64/stable/Packages/docker-ce.rpm",
        "../../43/x86_64/stable/Packages/docker-ce.rpm",
        "%2e%2e/%2e%2e/aarch64/stable/Packages/docker-ce.rpm",
        "..%2faarch64%2fstable%2fPackages%2fdocker-ce.rpm",
        # Query, fragment, and unsafe path characters.
        "Packages/docker-ce.rpm?download=1",
        "Packages/docker-ce.rpm#fragment",
        "Packages\\docker-ce.rpm",
    ],
)
def test_rejects_a_package_location_outside_the_trusted_subtree(href: str):
    """Package locations must stay in the signed repository subtree.

    The gate reports repository metadata availability, so it must not accept a
    listing whose artifact URL escapes the Docker Fedora release/architecture
    subtree it just read. Bad locations are rejected before any HEAD request.
    """

    repo = _repo(
        x86_64=_entries(
            "x86_64",
            hrefs={"docker-ce": href},
        )
    )

    with pytest.raises(dra.DockerRepoError, match="Docker"):
        dra.check_docker_rpm_availability(
            releasever="44", fetch=repo.fetch, head=repo.head
        )
    assert repo.heads == [], "an untrusted href must be rejected before any HEAD"


@pytest.mark.unit
def test_rejects_repository_redirects():
    with pytest.raises(dra.DockerRepoError, match="redirected"):
        dra._RejectRedirects().redirect_request(
            None, None, 302, "Found", {}, "https://evil.example/x"
        )


@pytest.mark.unit
def test_rejects_primary_href_from_another_arch():
    cross_arch = "/linux/fedora/44/aarch64/stable/repodata/primary.xml.zst"
    repo = _repo(hrefs=dict.fromkeys(dra.SUPPORTED_ARCHES, cross_arch))

    with pytest.raises(
        dra.DockerRepoError, match="outside the Docker Fedora repository subtree"
    ):
        dra.check_docker_rpm_availability(
            releasever="44", fetch=repo.fetch, head=repo.head
        )
    assert repo.heads == []


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
def test_rejects_malformed_primary_xml():
    repo = _repo()
    bad = zstd.compress(b"<metadata>")
    repo.primaries = dict.fromkeys(dra.SUPPORTED_ARCHES, bad)
    repo.repomd = {arch: _repomd(bad) for arch in dra.SUPPORTED_ARCHES}

    with pytest.raises(dra.DockerRepoError, match="could not parse"):
        _check(repo)


@pytest.mark.unit
def test_rejects_dtd_in_primary_metadata():
    malformed = b'<?xml version="1.0"?><!DOCTYPE metadata [<!ENTITY x "y">]><metadata/>'
    repo = _repo()
    payload = zstd.compress(malformed)
    repo.primaries = dict.fromkeys(dra.SUPPORTED_ARCHES, payload)
    repo.repomd = {arch: _repomd(payload) for arch in dra.SUPPORTED_ARCHES}

    with pytest.raises(dra.DockerRepoError, match="DTD or entity"):
        _check(repo)


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
def test_accepts_a_gzip_primary():
    primary = gzip.compress(_primary_xml(_entries("x86_64"), _entries("aarch64")))
    repo = _FakeRepo(dict.fromkeys(dra.SUPPORTED_ARCHES, primary))
    repo.repomd = {
        arch: _repomd(primary, "repodata/primary.xml.gz")
        for arch in dra.SUPPORTED_ARCHES
    }

    errors, _report = _check(repo)

    assert errors == []


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
@pytest.mark.parametrize("releasever", ["", "0", "abc", "1000"])
def test_rejects_invalid_fedora_release(releasever: str):
    with pytest.raises(dra.DockerRepoError, match="Fedora release"):
        dra.check_docker_rpm_availability(releasever=releasever)


@pytest.mark.unit
def test_main_reports_newest_versions_for_both_arches(monkeypatch, capsys):
    report = [
        f"x86_64: docker-ce: 3 release(s), newest 3:{_VERSION}-{_RELEASE}",
        f"aarch64: docker-ce: 2 release(s), newest 0:29.8.1-{_RELEASE}",
    ]
    monkeypatch.setattr(dra, "detect_fedora_release", lambda _path: "44")
    monkeypatch.setattr(
        dra, "check_docker_rpm_availability", lambda **_kwargs: ([], report)
    )

    assert dra.main([]) == 0
    output = capsys.readouterr().out
    for line in report:
        assert line in output
    assert "lists every required package for Fedora 44" in output
    assert "newest-artifact HEAD checks succeeded." in output
    assert "pinned package" not in output


@pytest.mark.unit
def test_main_reports_the_report_before_exiting_one(monkeypatch, capsys):
    report = [f"x86_64: docker-ce: 1 release(s), newest 0:{_VERSION}-{_RELEASE}"]
    errors = ["aarch64: containerd.io is absent from Docker Fedora 44 stable repodata"]
    monkeypatch.setattr(dra, "detect_fedora_release", lambda _path: "44")
    monkeypatch.setattr(
        dra, "check_docker_rpm_availability", lambda **_kwargs: (errors, report)
    )

    assert dra.main([]) == 1
    captured = capsys.readouterr()
    assert report[0] in captured.out
    assert f"ERROR: {errors[0]}" in captured.err


@pytest.mark.unit
def test_main_exits_two_on_repository_error(monkeypatch, capsys):
    def fail(**_kwargs):
        raise dra.DockerRepoError("could not fetch repomd.xml")

    monkeypatch.setattr(dra, "detect_fedora_release", lambda _path: "44")
    monkeypatch.setattr(dra, "check_docker_rpm_availability", fail)

    assert dra.main([]) == 2
    assert "ERROR: could not fetch repomd.xml" in capsys.readouterr().err
