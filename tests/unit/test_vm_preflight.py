from pathlib import Path

import pytest

from scripts.vm_preflight import check_vm_capabilities


def _mock_kvm_device(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(Path, "is_char_device", lambda self: self == path)
    monkeypatch.setattr("scripts.vm_preflight.os.access", lambda *_args: True)


@pytest.mark.unit
def test_vm_preflight_accepts_read_write_kvm_device(tmp_path: Path, monkeypatch):
    device = tmp_path / "kvm"
    _mock_kvm_device(monkeypatch, device)

    result = check_vm_capabilities(kvm_device=device)

    assert result.available
    assert not result.reasons


@pytest.mark.unit
def test_vm_preflight_rejects_inaccessible_kvm_device(tmp_path: Path, monkeypatch):
    device = tmp_path / "kvm"
    monkeypatch.setattr(Path, "is_char_device", lambda self: False)
    monkeypatch.setattr("scripts.vm_preflight.os.access", lambda *_args: False)

    result = check_vm_capabilities(kvm_device=device)

    assert not result.available
    assert "is missing or not accessible to this user" in result.reasons[0]


@pytest.mark.unit
def test_recursive_preflight_requires_enabled_host_setting(tmp_path: Path, monkeypatch):
    device = tmp_path / "kvm"
    _mock_kvm_device(monkeypatch, device)
    nested = tmp_path / "nested"
    nested.write_text("N\n")

    result = check_vm_capabilities(
        require_nested=True,
        kvm_device=device,
        nested_parameters=(nested,),
    )

    assert not result.available
    assert "nested KVM is disabled" in result.reasons[0]


@pytest.mark.unit
@pytest.mark.parametrize("value", ["Y", "1"])
def test_recursive_preflight_accepts_enabled_host_setting(
    tmp_path: Path, monkeypatch, value: str
):
    device = tmp_path / "kvm"
    _mock_kvm_device(monkeypatch, device)
    nested = tmp_path / "nested"
    nested.write_text(f"{value}\n")

    result = check_vm_capabilities(
        require_nested=True,
        kvm_device=device,
        nested_parameters=(nested,),
    )

    assert result.available
