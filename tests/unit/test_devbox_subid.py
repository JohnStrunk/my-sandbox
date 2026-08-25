import subprocess

import pytest


def eval_subid_ranges(uid: int, max_uid: int) -> list[str]:
    script = f"""
    subid_ranges() {{
      local id="$1" max="$2"
      if [ "$id" -gt 1 ]; then
        echo "sandbox:1:$((id - 1))"
      fi
      if [ "$max" -gt "$id" ]; then
        echo "sandbox:$((id + 1)):$((max - id))"
      fi
    }}
    subid_ranges {uid} {max_uid}
    """
    res = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]


@pytest.mark.unit
def test_subid_ranges_standard_user():
    ranges = eval_subid_ranges(1000, 65535)
    assert ranges == [
        "sandbox:1:999",
        "sandbox:1001:64535",
    ]


@pytest.mark.unit
def test_subid_ranges_boundary_uid_one():
    ranges = eval_subid_ranges(1, 65535)
    assert ranges == [
        "sandbox:2:65534",
    ]


@pytest.mark.unit
def test_subid_ranges_boundary_uid_max():
    ranges = eval_subid_ranges(65535, 65535)
    assert ranges == [
        "sandbox:1:65534",
    ]


@pytest.mark.unit
def test_subid_ranges_custom_allocation():
    ranges = eval_subid_ranges(500, 1000)
    assert ranges == [
        "sandbox:1:499",
        "sandbox:501:500",
    ]
