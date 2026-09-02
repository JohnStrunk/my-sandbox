import subprocess
from pathlib import Path

import pytest

from tests.conftest import run_bash_script

ALPINE_IMAGE = "docker.io/library/alpine:3.22"
TESTCONTAINERS_VERSION = "12.1.0"

HTTP_SMOKE_SCRIPT = r"""
const { GenericContainer, Wait } = require("testcontainers");
const http = require("node:http");

function request(host, port) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host, port, path: "/" }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          reject(new Error(`unexpected HTTP status: ${response.statusCode}`));
          return;
        }
        resolve(body);
      });
    });
    request.on("error", reject);
    request.setTimeout(10000, () => {
      request.destroy(new Error("HTTP request timed out"));
    });
  });
}

async function main() {
  let container;
  try {
    container = await new GenericContainer(process.env.ALPINE_IMAGE)
      .withCommand([
        "sh",
        "-c",
        "while true; do printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 20\\r\\n" +
        "Connection: close\\r\\n\\r\\nnested-docker-api-ok' | nc -l -p 8080; done",
      ])
      .withExposedPorts(8080)
      .withWaitStrategy(Wait.forHttp("/", 8080).withStartupTimeout(120000))
      .start();

    const body = await request(
      container.getHost(),
      container.getMappedPort(8080),
    );
    if (body !== "nested-docker-api-ok") {
      throw new Error(`unexpected HTTP response: ${body}`);
    }
    console.log("published-port-ok");
  } finally {
    if (container) {
      await container.stop();
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""

NETWORK_SMOKE_SCRIPT = r"""
const { GenericContainer, Network } = require("testcontainers");

async function main() {
  let network;
  let client;
  let server;
  try {
    network = await new Network().start();
    server = await new GenericContainer(process.env.ALPINE_IMAGE)
      .withCommand(["sh", "-c", "sleep 60"])
      .withNetwork(network)
      .withNetworkAliases("api-server")
      .start();
    client = await new GenericContainer(process.env.ALPINE_IMAGE)
      .withCommand(["sh", "-c", "sleep 60"])
      .withNetwork(network)
      .withNetworkAliases("api-client")
      .start();

    const result = await client.exec(["getent", "hosts", "api-server"]);
    if (result.exitCode !== 0) {
      throw new Error(`network alias lookup failed: ${result.output}`);
    }
    console.log("network-alias-ok");
  } finally {
    if (client) {
      await client.stop();
    }
    if (server) {
      await server.stop();
    }
    if (network) {
      await network.stop();
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def _prepare_testcontainers_project(test_dir: Path, script: str, name: str) -> None:
    (test_dir / "package.json").write_text("{}\n")
    (test_dir / name).write_text(script)


def _nested_runtime_capability_failure(
    result: subprocess.CompletedProcess[str],
) -> bool:
    output = (result.stdout + result.stderr).lower()
    return any(
        detail in output
        for detail in (
            "newuidmap",
            "newgidmap",
            "cannot set up namespace",
            "user namespaces are not supported",
        )
    )


def _bridge_preflight_unavailable(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stdout.lower()
    return "docker api default network: pasta" in output


def _install_testcontainers(
    devbox_path: Path, test_dir: Path, env: dict[str, str]
) -> None:
    result = run_bash_script(
        devbox_path,
        [
            "npm",
            "install",
            "--no-fund",
            "--no-audit",
            "--no-package-lock",
            f"testcontainers@{TESTCONTAINERS_VERSION}",
        ],
        env=env,
        cwd=test_dir,
        timeout=300,
    )
    assert result.returncode == 0, (
        "Failed to install Testcontainers:\n"
        f"Stdout: {result.stdout}\nStderr: {result.stderr}"
    )


def _run_node_smoke(
    devbox_path: Path,
    test_dir: Path,
    script_name: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return run_bash_script(
        devbox_path,
        [
            "env",
            "TESTCONTAINERS_RYUK_DISABLED=true",
            "TESTCONTAINERS_HOST_OVERRIDE=127.0.0.1",
            f"ALPINE_IMAGE={ALPINE_IMAGE}",
            "node",
            script_name,
        ],
        env=env,
        cwd=test_dir,
        timeout=300,
    )


def _remove_devbox(devbox_path: Path, test_dir: Path, env: dict[str, str]) -> None:
    result = run_bash_script(
        devbox_path, ["--remove"], env=env, cwd=test_dir, timeout=60
    )
    if result.returncode != 0:
        subprocess.run(
            ["podman", "rm", "-f", f"devbox-{test_dir.name}"],
            env=env,
            capture_output=True,
            check=False,
        )


def _assert_devbox_is_unprivileged(test_dir: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        [
            "podman",
            "inspect",
            "--format",
            "{{.HostConfig.Privileged}}",
            f"devbox-{test_dir.name}",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


@pytest.fixture
def nested_podman_available(
    devbox_path: Path, isolated_env: dict[str, str], tmp_path: Path
):
    probe_dir = tmp_path / "docker_api_probe"
    probe_dir.mkdir()
    try:
        probe = run_bash_script(
            devbox_path,
            ["podman", "run", "--rm", ALPINE_IMAGE, "true"],
            env=isolated_env,
            cwd=probe_dir,
            timeout=300,
        )
        if probe.returncode != 0:
            if _nested_runtime_capability_failure(probe):
                pytest.skip(
                    "Nested Podman is not supported in this host/container "
                    "environment:\n"
                    f"{probe.stdout}\n{probe.stderr}"
                )
            pytest.fail(
                "Nested Podman probe failed for an unexpected reason:\n"
                f"{probe.stdout}\n{probe.stderr}"
            )
        yield
    finally:
        _remove_devbox(devbox_path, probe_dir, isolated_env)


@pytest.mark.integration
def test_docker_api_published_port(
    devbox_path: Path,
    devbox_image: str,
    nested_podman_available,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    del devbox_image, nested_podman_available
    test_dir = tmp_path / "docker_api_port"
    test_dir.mkdir()
    _prepare_testcontainers_project(test_dir, HTTP_SMOKE_SCRIPT, "http-smoke.cjs")

    try:
        check = run_bash_script(
            devbox_path,
            ["devbox-docker-api-check"],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert check.returncode == 0, (
            f"Docker API preflight failed:\n{check.stdout}\n{check.stderr}"
        )
        assert "Docker API ready" in check.stdout

        rootless = run_bash_script(
            devbox_path,
            ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert rootless.returncode == 0, rootless.stderr
        assert rootless.stdout.strip().splitlines()[-1] == "true"

        ping = run_bash_script(
            devbox_path,
            [
                "curl",
                "--fail",
                "--silent",
                "--unix-socket",
                "/sandbox/.docker/run/docker.sock",
                "http://localhost/_ping",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert ping.returncode == 0, ping.stderr
        assert ping.stdout.strip().splitlines()[-1] == "OK"
        _assert_devbox_is_unprivileged(test_dir, isolated_env)

        _install_testcontainers(devbox_path, test_dir, isolated_env)
        result = _run_node_smoke(devbox_path, test_dir, "http-smoke.cjs", isolated_env)
        assert result.returncode == 0, (
            f"Published-port smoke test failed:\n{result.stdout}\n{result.stderr}"
        )
        assert "published-port-ok" in result.stdout
        assert "Could not find a working container runtime strategy" not in (
            result.stdout + result.stderr
        )
        assert "Read-only file system" not in result.stdout + result.stderr
    finally:
        _remove_devbox(devbox_path, test_dir, isolated_env)


@pytest.mark.integration
def test_docker_api_user_defined_network_aliases(
    devbox_path: Path,
    devbox_image: str,
    nested_podman_available,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    del devbox_image, nested_podman_available
    test_dir = tmp_path / "docker_api_network"
    test_dir.mkdir()
    _prepare_testcontainers_project(test_dir, NETWORK_SMOKE_SCRIPT, "network-smoke.cjs")

    try:
        check = run_bash_script(
            devbox_path,
            ["devbox-docker-api-check", "--require-user-networks"],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        if check.returncode == 2 and _bridge_preflight_unavailable(check):
            pytest.skip(
                "Nested bridge-network preflight is unavailable:\n"
                f"{check.stdout}\n{check.stderr}"
            )
        assert check.returncode == 0, (
            f"Docker API network preflight failed:\n{check.stdout}\n{check.stderr}"
        )

        _install_testcontainers(devbox_path, test_dir, isolated_env)
        result = _run_node_smoke(
            devbox_path, test_dir, "network-smoke.cjs", isolated_env
        )
        assert result.returncode == 0, (
            f"Network alias smoke test failed:\n{result.stdout}\n{result.stderr}"
        )
        assert "network-alias-ok" in result.stdout
        assert "Read-only file system" not in result.stdout + result.stderr
    finally:
        _remove_devbox(devbox_path, test_dir, isolated_env)
