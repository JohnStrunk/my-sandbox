import os
import shutil
from pathlib import Path

import pytest

from tests.conftest import (
    copy_repository_for_vm,
    initialize_local_test_repository,
    run_in_process_group,
)


@pytest.mark.cold_bootstrap
def test_pre_commit_hooks_bootstrap_from_empty_cache(repo_root: Path, tmp_path: Path):
    pre_commit = shutil.which("pre-commit")
    if not pre_commit:
        pytest.skip("pre-commit is not installed")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    copy_repository_for_vm(repo_root, checkout)
    initialize_local_test_repository(checkout)

    cache = tmp_path / "empty-pre-commit-cache"
    cache.mkdir()
    env = {
        name: os.environ[name]
        for name in (
            "PATH",
            "HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "CI",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
        )
        if name in os.environ
    }
    env["PRE_COMMIT_HOME"] = str(cache)
    result = run_in_process_group(
        [pre_commit, "run", "--all-files"],
        timeout=600,
        env=env,
        cwd=checkout,
    )

    assert result.returncode == 0, (
        "Pre-commit hooks could not initialize from an empty cache.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
