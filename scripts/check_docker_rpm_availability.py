#!/usr/bin/env python3
"""Check pinned Docker RPM availability in Docker's Fedora stable repository.

The gate reads the Docker pins from ``lima/tool-versions.json`` and verifies
that Docker's Fedora stable repodata lists each exact RPM epoch and version
for both supported architectures. The Fedora release is derived from the
x86_64 and aarch64 cloud-image URLs in ``lima/devbox.yaml``, which must agree.
Any RPM release is accepted. Every release listed for a pinned version is then
tried with a bounded HTTP HEAD request until one artifact responds, so an
in-flight republish of the newest release does not fail the gate.

Scope: this proves repository metadata and artifact availability only. It does
not verify RPM signatures, resolve dependencies, or exercise the packages at
runtime. DNF verifies repository metadata and package signatures with the pinned
Docker key, and the provisioned x86_64 VM tier exercises runtime compatibility.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from compression import zstd

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "lima" / "tool-versions.json"
DEFAULT_DEVBOX_TEMPLATE = REPO_ROOT / "lima" / "devbox.yaml"
SUPPORTED_ARCHES = ("x86_64", "aarch64")

_DOCKER_REPO_HOST = "download.docker.com"
_DOCKER_REPO_PATH_PREFIX = "/linux/fedora/"
_MAX_REPOMD_BYTES = 1 << 20
_MAX_PRIMARY_BYTES = 8 << 20
_MAX_PRIMARY_XML_BYTES = 64 << 20
_MAX_HREF_DECODE_ROUNDS = 3
_HTTP_TIMEOUT_SECONDS = 30.0
_USER_AGENT = "devbox-docker-rpm-availability"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_VERSION_PATTERN = re.compile(r"^[0-9][A-Za-z0-9._+~-]*$")
_FEDORA_IMAGE_PATTERN = re.compile(
    r"https://download\.fedoraproject\.org/pub/fedora/linux/releases/"
    r"(?P<release>\d+)/Cloud/(?P<arch>x86_64|aarch64)/"
)

# RPM epoch contracts enforced by lima/provision-system.sh. The manifest
# supplies the versions, so this gate never carries a tool version.
_PACKAGE_SPECS = (
    ("docker_ce", "docker-ce", 3),
    ("docker_ce", "docker-ce-cli", 1),
    ("containerd_io", "containerd.io", 0),
    ("docker_buildx_plugin", "docker-buildx-plugin", 0),
)

_FETCH_ERRORS = (URLError, TimeoutError, http.client.HTTPException, OSError)
Fetcher = Callable[[str, int, float], bytes]
HeadChecker = Callable[[str, float], None]


class DockerRepoError(RuntimeError):
    """Raised when repository metadata or a repository request is unusable."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ExpectedPackage:
    name: str
    epoch: int
    version: str


@dataclass(frozen=True)
class PrimaryDescriptor:
    href: str
    sha256: str


@dataclass(frozen=True)
class RepoPackage:
    name: str
    arch: str
    epoch: int
    version: str
    release: str
    href: str


class _RejectRedirects(HTTPRedirectHandler):
    """Keep reads pinned to the validated Docker repository host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DockerRepoError(
            f"Docker repository URL redirected (HTTP {code}) to {newurl}; "
            "refusing to follow"
        )


_OPENER = build_opener(_RejectRedirects())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _child_text(element: ET.Element, name: str) -> str | None:
    child = _child(element, name)
    return child.text.strip() if child is not None and child.text else None


def _parse_xml(data: bytes, label: str) -> ET.Element:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise DockerRepoError(f"{label} contains a DTD or entity declaration")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DockerRepoError(f"could not parse {label}: {exc}") from exc


def _require_allowed_repo_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DockerRepoError(f"invalid Docker repository URL: {url}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _DOCKER_REPO_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(_DOCKER_REPO_PATH_PREFIX)
    ):
        raise DockerRepoError(f"{url} is not an allowed Docker Fedora repository URL")


def _decoded_repo_path(path: str, url: str) -> str:
    """Return a fully percent-decoded path, rejecting repeated encoding."""

    decoded = path
    for _ in range(_MAX_HREF_DECODE_ROUNDS):
        candidate = unquote(decoded)
        if candidate == decoded:
            return decoded
        decoded = candidate
    raise DockerRepoError(f"{url} has an over-encoded Docker repository path")


def _require_safe_repo_path(path: str, url: str) -> None:
    """Reject paths whose canonical form could leave the repository subtree."""

    decoded = _decoded_repo_path(path, url)
    if "\\" in decoded or "\x00" in decoded:
        raise DockerRepoError(f"{url} has an unsafe Docker repository path")
    if ".." in decoded.split("/"):
        raise DockerRepoError(f"{url} has a non-canonical Docker repository path")


def _require_repo_subtree(base_url: str, resolved: str) -> None:
    """Require a resolved URL to stay in the exact base release/arch subtree."""

    base_path = urlsplit(base_url).path
    resolved_path = urlsplit(resolved).path
    canonical_path = _decoded_repo_path(resolved_path, resolved)
    if (
        not base_path.endswith("/")
        or not resolved_path.startswith(base_path)
        or not canonical_path.startswith(base_path)
    ):
        raise DockerRepoError(
            f"{resolved} is outside the Docker Fedora repository subtree {base_path}"
        )


def _resolve_repo_url(base_url: str, href: str) -> str:
    if not href:
        raise DockerRepoError("Docker repository metadata has an empty URL")
    try:
        href_path = urlsplit(href).path
        resolved = urljoin(base_url, href)
    except ValueError as exc:
        raise DockerRepoError(f"invalid Docker repository URL: {href}") from exc
    _require_safe_repo_path(href_path, href)
    _require_allowed_repo_url(resolved)
    _require_repo_subtree(base_url, resolved)
    return resolved


def _repository_base_url(releasever: str, arch: str) -> str:
    url = (
        f"https://{_DOCKER_REPO_HOST}{_DOCKER_REPO_PATH_PREFIX}"
        f"{releasever}/{arch}/stable/"
    )
    _require_allowed_repo_url(url)
    return url


def _read_limited(response, max_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise DockerRepoError(
            f"response Content-Length {declared} exceeds the {max_bytes}-byte limit"
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(1 << 16, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DockerRepoError(f"response exceeds the {max_bytes}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_bytes(url: str, max_bytes: int, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            _require_allowed_repo_url(response.geturl())
            return _read_limited(response, max_bytes)
    except DockerRepoError:
        raise
    except _FETCH_ERRORS as exc:
        status = getattr(exc, "code", None)
        raise DockerRepoError(
            f"could not fetch {url}: {exc}",
            status=status if isinstance(status, int) else None,
        ) from exc


def _head_ok(url: str, timeout: float) -> None:
    request = Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            _require_allowed_repo_url(response.geturl())
            status = getattr(response, "status", None)
            if status is not None and not 200 <= status < 300:
                raise DockerRepoError(
                    f"HEAD {url} returned unexpected HTTP status {status}"
                )
    except DockerRepoError:
        raise
    except _FETCH_ERRORS as exc:
        raise DockerRepoError(f"could not confirm {url}: {exc}") from exc


def _decompress_primary(data: bytes, href: str, max_bytes: int) -> bytes:
    suffix = Path(urlsplit(href).path).suffix.lower()
    if suffix in (".zst", ".zstd"):
        decompressor = zstd.ZstdDecompressor()
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = decompressor.decompress(
                    data, max_length=min(1 << 16, max_bytes - total + 1)
                )
            except zstd.ZstdError as exc:
                raise DockerRepoError(
                    f"could not decompress primary metadata {href}: {exc}"
                ) from exc
            data = b""
            total += len(chunk)
            if total > max_bytes:
                raise DockerRepoError(
                    f"decompressed primary metadata exceeds the {max_bytes}-byte limit"
                )
            chunks.append(chunk)
            if decompressor.eof:
                break
            if decompressor.needs_input or not chunk:
                raise DockerRepoError(f"truncated primary metadata {href}")
        return b"".join(chunks)
    if suffix == ".gz":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                raw = stream.read(max_bytes + 1)
        except OSError as exc:
            raise DockerRepoError(
                f"could not decompress primary metadata {href}: {exc}"
            ) from exc
        if len(raw) > max_bytes:
            raise DockerRepoError(
                f"decompressed primary metadata exceeds the {max_bytes}-byte limit"
            )
        return raw
    if suffix == ".xml":
        if len(data) > max_bytes:
            raise DockerRepoError(
                f"primary metadata exceeds the {max_bytes}-byte limit"
            )
        return data
    raise DockerRepoError(f"unsupported primary metadata compression for {href}")


def _parse_repomd(data: bytes) -> PrimaryDescriptor:
    root = _parse_xml(data, "repomd.xml")
    if _local_name(root.tag) != "repomd":
        raise DockerRepoError("repomd.xml has an unexpected root element")
    for entry in root:
        if _local_name(entry.tag) != "data" or entry.get("type") != "primary":
            continue
        checksum = _child(entry, "checksum")
        location = _child(entry, "location")
        if checksum is None or location is None:
            raise DockerRepoError("repomd.xml primary entry is incomplete")
        digest = (checksum.text or "").strip().lower()
        if checksum.get("type") != "sha256" or not _SHA256_PATTERN.fullmatch(digest):
            raise DockerRepoError(
                "repomd.xml primary checksum must be a lowercase SHA-256 digest"
            )
        href = location.get("href")
        if not href:
            raise DockerRepoError("repomd.xml primary entry has no location")
        return PrimaryDescriptor(href=href, sha256=digest)
    raise DockerRepoError("repomd.xml does not declare primary metadata")


def _parse_primary(data: bytes) -> tuple[RepoPackage, ...]:
    root = _parse_xml(data, "primary metadata")
    if _local_name(root.tag) != "metadata":
        raise DockerRepoError("primary metadata has an unexpected root element")
    packages: list[RepoPackage] = []
    for entry in root:
        if _local_name(entry.tag) != "package":
            continue
        name = _child_text(entry, "name")
        arch = _child_text(entry, "arch")
        version = _child(entry, "version")
        location = _child(entry, "location")
        if (
            not name
            or not arch
            or version is None
            or location is None
            or not version.get("ver")
            or not location.get("href")
        ):
            raise DockerRepoError(
                "primary metadata contains an incomplete package entry"
            )
        epoch_text = version.get("epoch", "0")
        if not epoch_text.isdigit():
            raise DockerRepoError(
                f"primary metadata package {name} has a non-numeric epoch"
            )
        packages.append(
            RepoPackage(
                name=name,
                arch=arch,
                epoch=int(epoch_text),
                version=version.get("ver", ""),
                release=version.get("rel", ""),
                href=location.get("href", ""),
            )
        )
    if not packages:
        raise DockerRepoError("primary metadata contains no packages")
    return tuple(packages)


def load_expected_packages(
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[ExpectedPackage, ...]:
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DockerRepoError(f"unable to load {manifest_path}: {exc}") from exc
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, dict):
        raise DockerRepoError(f"{manifest_path} must contain a 'tools' object")
    expected: list[ExpectedPackage] = []
    for tool, name, epoch in _PACKAGE_SPECS:
        spec = tools.get(tool)
        version = spec.get("version") if isinstance(spec, dict) else None
        if not isinstance(version, str) or not version:
            raise DockerRepoError(f"{manifest_path}: tool '{tool}' has no version")
        normalized = version.removeprefix("v")
        if not _VERSION_PATTERN.fullmatch(normalized):
            raise DockerRepoError(
                f"{manifest_path}: tool '{tool}' version is not a safe token"
            )
        expected.append(ExpectedPackage(name=name, epoch=epoch, version=normalized))
    return tuple(expected)


def _validate_releasever(releasever: str) -> None:
    if not releasever.isdigit() or not 1 <= int(releasever) <= 999:
        raise DockerRepoError(
            f"Fedora release must be a small positive integer, not {releasever!r}"
        )


def detect_fedora_release(
    template_path: Path = DEFAULT_DEVBOX_TEMPLATE,
) -> str:
    """Return the single Fedora release pinned by both cloud-image URLs."""

    try:
        template = template_path.read_text()
    except OSError as exc:
        raise DockerRepoError(f"unable to read {template_path}: {exc}") from exc
    releases_by_arch: dict[str, set[str]] = {}
    for match in _FEDORA_IMAGE_PATTERN.finditer(template):
        releases_by_arch.setdefault(match["arch"], set()).add(match["release"])
    missing = set(SUPPORTED_ARCHES) - set(releases_by_arch)
    if missing:
        raise DockerRepoError(
            f"{template_path} must pin Fedora cloud images for both "
            f"architectures; missing: {', '.join(sorted(missing))}"
        )
    releases = set().union(*releases_by_arch.values())
    if len(releases) != 1:
        raise DockerRepoError(
            f"{template_path} has inconsistent Fedora release pins: "
            + ", ".join(sorted(releases))
        )
    release = releases.pop()
    _validate_releasever(release)
    return release


def _matching_packages(
    packages: Iterable[RepoPackage],
    expected: ExpectedPackage,
    arch: str,
) -> list[RepoPackage]:
    return [
        package
        for package in packages
        if package.name == expected.name
        and package.arch == arch
        and package.epoch == expected.epoch
        and package.version == expected.version
    ]


def _read_primary_metadata(
    base_url: str,
    fetch: Fetcher,
    timeout: float,
) -> tuple[bytes, PrimaryDescriptor]:
    """Load repomd and its primary, retrying once after a republish race."""

    repomd_url = _resolve_repo_url(base_url, "repodata/repomd.xml")
    for attempt in (1, 2):
        descriptor = _parse_repomd(fetch(repomd_url, _MAX_REPOMD_BYTES, timeout))
        primary_url = _resolve_repo_url(base_url, descriptor.href)
        try:
            primary_data = fetch(primary_url, _MAX_PRIMARY_BYTES, timeout)
        except DockerRepoError as exc:
            if attempt == 1 and exc.status in (404, 410):
                continue
            raise
        digest = hashlib.sha256(primary_data).hexdigest()
        if digest == descriptor.sha256:
            return primary_data, descriptor
        if attempt == 2:
            raise DockerRepoError(
                f"primary metadata SHA-256 mismatch for {primary_url}: "
                f"got {digest}, expected {descriptor.sha256}"
            )
    raise AssertionError("unreachable")  # pragma: no cover


def _available_package_error(
    base_url: str,
    matches: list[RepoPackage],
    head: HeadChecker,
    timeout: float,
) -> str | None:
    """Return failure detail unless at least one listed artifact responds.

    Candidates are tried in repodata order and the first success ends the
    check. Each candidate's location is validated by
    :func:`_resolve_repo_url` immediately before its HEAD request, so an
    untrusted href aborts the check instead of being reported as unavailable.
    """

    failures: list[str] = []
    for package in matches:
        package_url = _resolve_repo_url(base_url, package.href)
        try:
            head(package_url, timeout)
        except DockerRepoError as exc:
            failures.append(f"{package.release}: {exc}")
        else:
            return None
    return "; ".join(failures)


def check_docker_rpm_availability(
    manifest_path: Path = DEFAULT_MANIFEST,
    releasever: str | None = None,
    devbox_template: Path = DEFAULT_DEVBOX_TEMPLATE,
    arches: tuple[str, ...] = SUPPORTED_ARCHES,
    fetch: Fetcher = _fetch_bytes,
    head: HeadChecker = _head_ok,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> list[str]:
    """Return availability errors; raise when repository metadata is unusable."""

    if releasever is None:
        releasever = detect_fedora_release(devbox_template)
    else:
        _validate_releasever(releasever)
    expected_packages = load_expected_packages(manifest_path)
    errors: list[str] = []
    for arch in arches:
        if arch not in SUPPORTED_ARCHES:
            raise DockerRepoError(f"unsupported architecture: {arch}")
        base_url = _repository_base_url(releasever, arch)
        primary_data, descriptor = _read_primary_metadata(base_url, fetch, timeout)
        primary = _decompress_primary(
            primary_data, descriptor.href, _MAX_PRIMARY_XML_BYTES
        )
        packages = _parse_primary(primary)
        for expected in expected_packages:
            matches = _matching_packages(packages, expected, arch)
            if not matches:
                errors.append(
                    f"{arch}: {expected.name} {expected.epoch}:"
                    f"{expected.version} (any release) is absent from Docker "
                    f"Fedora {releasever} stable repodata"
                )
                continue
            detail = _available_package_error(base_url, matches, head, timeout)
            if detail is not None:
                errors.append(
                    f"{arch}: {expected.name} {expected.epoch}:"
                    f"{expected.version} is listed but unavailable in "
                    f"{len(matches)} release(s): {detail}"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check that pinned Docker RPMs are available for both supported "
            "architectures in Docker's Fedora stable repository."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to tool-versions.json (defaults to the repo manifest).",
    )
    parser.add_argument(
        "--devbox-template",
        type=Path,
        default=DEFAULT_DEVBOX_TEMPLATE,
        help=(
            "Lima template whose x86_64/aarch64 image URLs select the Fedora "
            "release (defaults to lima/devbox.yaml)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        releasever = detect_fedora_release(args.devbox_template)
        errors = check_docker_rpm_availability(
            manifest_path=args.manifest,
            releasever=releasever,
        )
    except DockerRepoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "Docker RPM metadata lists every pinned package for "
        f"Fedora {releasever} {', '.join(SUPPORTED_ARCHES)}; "
        "artifact HEAD checks succeeded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
