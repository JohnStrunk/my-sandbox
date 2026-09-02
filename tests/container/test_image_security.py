import pytest

from tests.conftest import run_in_devbox


@pytest.mark.container
def test_newuidmap_file_capabilities(devbox_image: str):
    res = run_in_devbox(
        devbox_image, ["/usr/sbin/getcap", "/usr/bin/newuidmap"], user="root"
    )
    assert res.returncode == 0
    assert "cap_setuid" in res.stdout


@pytest.mark.container
def test_newgidmap_file_capabilities(devbox_image: str):
    res = run_in_devbox(
        devbox_image, ["/usr/sbin/getcap", "/usr/bin/newgidmap"], user="root"
    )
    assert res.returncode == 0
    assert "cap_setgid" in res.stdout


@pytest.mark.container
def test_newuidmap_setuid_stripped(devbox_image: str):
    res = run_in_devbox(
        devbox_image, ["stat", "-c", "%A", "/usr/bin/newuidmap"], user="sandbox"
    )
    assert res.returncode == 0
    # Permissions should not have setuid bit 's' or 'S'
    perms = res.stdout.strip()
    assert "s" not in perms.lower()


@pytest.mark.container
def test_sandbox_sudo_nopasswd(devbox_image: str):
    res = run_in_devbox(devbox_image, ["sudo", "id", "-u"], user="sandbox")
    assert res.returncode == 0
    assert res.stdout.strip() == "0"


@pytest.mark.container
def test_containers_storage_conf(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["cat", "/sandbox/.config/containers/storage.conf"],
        user="sandbox",
    )
    assert res.returncode == 0
    content = res.stdout
    assert 'driver = "overlay"' in content
    assert 'mount_program = "/usr/bin/fuse-overlayfs"' in content
    assert 'mountopt = "nodev,fsync=0"' in content


@pytest.mark.container
def test_containers_containers_conf(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["cat", "/sandbox/.config/containers/containers.conf"],
        user="sandbox",
    )
    assert res.returncode == 0
    content = res.stdout
    assert 'cgroups = "disabled"' in content
    assert 'volumes = ["/proc:/proc"]' in content
    assert 'utsns = "host"' in content
    assert 'netns = "pasta"' in content
    assert 'network_backend = "netavark"' in content
    assert 'default_rootless_network_cmd = "pasta"' in content


@pytest.mark.container
def test_docker_host_environment(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["bash", "-c", "printf '%s\\n' \"$DOCKER_HOST\""],
        user="sandbox",
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "unix:///sandbox/.docker/run/docker.sock"


@pytest.mark.container
def test_docker_socket_directory_owned_by_sandbox(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["stat", "-c", "%U:%G", "/sandbox/.docker/run"],
        user="sandbox",
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "sandbox:sandbox"


@pytest.mark.container
def test_devbox_docker_api_check_reports_no_socket_by_default(devbox_image: str):
    # A fresh, one-off `podman run` (as used by these container tests) never
    # runs devbox-entry.sh's background service, so the socket legitimately
    # doesn't exist yet; this just checks the *diagnostic*, not the service.
    res = run_in_devbox(
        devbox_image,
        ["devbox-docker-api-check"],
        user="sandbox",
    )
    assert res.returncode == 1
    assert "Docker API unavailable" in res.stdout + res.stderr


@pytest.mark.container
def test_buildah_env_variables(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["bash", "-c", "echo BUILDAH=$BUILDAH_ISOLATION"],
        user="sandbox",
    )
    assert res.returncode == 0
    assert "BUILDAH=chroot" in res.stdout


@pytest.mark.container
def test_sandbox_local_bin_on_path(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        ["bash", "-c", "printf '%s\\n' \"$PATH\""],
        user="sandbox",
    )
    assert res.returncode == 0
    assert "/sandbox/.local/bin" in res.stdout.strip().split(":")


@pytest.mark.container
def test_redhat_ca_certificates_installed(devbox_image: str):
    res = run_in_devbox(
        devbox_image,
        [
            "openssl",
            "verify",
            "-CAfile",
            "/etc/ssl/certs/ca-certificates.crt",
            "/usr/local/share/ca-certificates/redhat/redhat-rhcsv2-ca.crt",
        ],
        user="sandbox",
    )
    assert res.returncode == 0
    assert "OK" in res.stdout


@pytest.mark.container
def test_ca_environment_variables(devbox_image: str):
    cmd = (
        "echo SSL=$SSL_CERT_FILE REQUESTS=$REQUESTS_CA_BUNDLE NODE=$NODE_EXTRA_CA_CERTS"
    )
    res = run_in_devbox(
        devbox_image,
        ["bash", "-c", cmd],
        user="sandbox",
    )
    assert res.returncode == 0
    assert "SSL=/etc/ssl/certs/ca-certificates.crt" in res.stdout
    assert "REQUESTS=/etc/ssl/certs/ca-certificates.crt" in res.stdout
    assert "NODE=/etc/ssl/certs/ca-certificates.crt" in res.stdout
