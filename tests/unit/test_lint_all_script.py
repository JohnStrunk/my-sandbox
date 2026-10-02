import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _git_env(home: Path) -> dict[str, str]:
    env = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(home),
        }
    )
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(repo),
    )


def _fake_pre_commit(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "pre-commit-args"
    executable = fake_bin / "pre-commit"
    executable.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\0\' "$PWD" "$@" > "$LINT_CAPTURE"\n'
    )
    executable.chmod(0o755)
    return fake_bin, capture


def _lint_env(fake_bin: Path, capture: Path) -> dict[str, str]:
    env = _git_env(capture.parent)
    env.update(
        {
            "LINT_CAPTURE": str(capture),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
    )
    return env


@pytest.mark.unit
def test_lint_all_script_passes_existing_nonignored_files_nul_safely(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign-worktree"))
    repo = tmp_path / "repo"
    repo.mkdir()
    github_dir = repo / ".github"
    github_dir.mkdir()
    script = github_dir / "lint-all.sh"
    shutil.copyfile(repo_root / ".github" / "lint-all.sh", script)
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        env=_git_env(repo),
    )
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / ".gitignore").write_text(
        "ignored-untracked.txt\nignored-tracked.txt\nignored-dir/\n"
        ".next/\n.venv/\nnode_modules/\nout/\n"
    )
    global_ignore = tmp_path / "global-ignore"
    global_ignore.write_text("untracked.txt\n")
    global_git_config = tmp_path / "global-gitconfig"
    global_git_config.write_text(f"[core]\n\texcludesFile = {global_ignore}\n")
    tracked_paths = [
        ".gitignore",
        ".github/lint-all.sh",
        "tracked.txt",
        "tracked with spaces.txt",
        "tracked\nnewline.txt",
        "-tracked-leading-dash.txt",
        "ignored-tracked.txt",
        "linked-parent/tracked-outside.txt",
        "deleted.txt",
        "target.txt",
        ".next/tracked.txt",
        ".venv/tracked.txt",
        "node_modules/tracked.txt",
        "out/tracked.txt",
    ]
    for name in tracked_paths:
        if name in {".gitignore", ".github/lint-all.sh"}:
            continue
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"contents of {name}")
    (repo / "tracked-link").symlink_to("target.txt")
    _git(repo, "add", "-f", "--", *tracked_paths, "tracked-link")
    _git(repo, "commit", "--quiet", "-m", "initial tracked files")

    (repo / "deleted.txt").unlink()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "tracked-outside.txt"
    outside_file.write_text("outside the worktree")
    shutil.rmtree(repo / "linked-parent")
    (repo / "linked-parent").symlink_to(outside_dir, target_is_directory=True)
    (repo / "untracked-link").symlink_to("target.txt")
    for name in (
        "untracked.txt",
        "untracked with spaces.txt",
        "untracked\nnewline.txt",
        "-untracked-leading-dash.txt",
        "ignored-untracked.txt",
        "ignored-dir/untracked.txt",
    ):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"contents of {name}")

    fake_bin, capture = _fake_pre_commit(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    git_wrapper = fake_bin / "git"
    git_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == ls-files ]]; then\n'
        '  "$REAL_GIT" "$@"\n'
        "  status=$?\n"
        "  printf '.git/test-data\\0'\n"
        '  exit "$status"\n'
        "fi\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    git_wrapper.chmod(0o755)
    git_metadata_file = repo / ".git" / "test-data"
    git_metadata_file.write_text("not a worktree file")
    env = _lint_env(fake_bin, capture)
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    env["GIT_DIR"] = str(tmp_path / "foreign.git")
    env["GIT_WORK_TREE"] = str(tmp_path / "foreign-worktree")
    env["GIT_INDEX_FILE"] = str(tmp_path / "foreign-index")
    env["GIT_CONFIG_GLOBAL"] = str(global_git_config)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "core.excludesFile"
    env["GIT_CONFIG_VALUE_0"] = str(global_ignore)
    env["REAL_GIT"] = real_git
    result = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    captured = capture.read_bytes().split(b"\0")
    assert captured[-1] == b""
    args = [os.fsdecode(value) for value in captured[:-1]]
    assert args[0] == str(repo), "pre-commit must run from TOP_DIR"
    assert args[1:3] == ["run", "--files"]
    assert outside_file.read_text() == "outside the worktree"
    assert set(args[3:]) == {
        "./.gitignore",
        "./.github/lint-all.sh",
        "./tracked.txt",
        "./tracked with spaces.txt",
        "./tracked\nnewline.txt",
        "./-tracked-leading-dash.txt",
        "./ignored-tracked.txt",
        "./target.txt",
        "./untracked.txt",
        "./untracked with spaces.txt",
        "./untracked\nnewline.txt",
        "./-untracked-leading-dash.txt",
    }


@pytest.mark.unit
def test_lint_all_script_does_not_run_pre_commit_for_an_empty_file_list(
    repo_root: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        env=_git_env(repo),
    )
    (repo / ".git" / "info" / "exclude").write_text(".github/\n")
    github_dir = repo / ".github"
    github_dir.mkdir()
    script = github_dir / "lint-all.sh"
    shutil.copyfile(repo_root / ".github" / "lint-all.sh", script)
    fake_bin, capture = _fake_pre_commit(tmp_path)

    result = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_lint_env(fake_bin, capture),
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert not capture.exists(), "xargs must not run pre-commit without file paths"
