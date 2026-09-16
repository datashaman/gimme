import json
import stat
import subprocess
from pathlib import Path

import pytest

import gimme.secrets as secrets_module
from gimme.secrets import SecretError, protected_secret_file, resolve_secret_references


def test_secret_references_are_resolved_without_returning_document(
    tmp_path: Path, monkeypatch
) -> None:
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("encrypted")
    monkeypatch.setattr(secrets_module.shutil, "which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr(
        secrets_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps({"preview": {"TOKEN": "value"}}), ""
        ),
    )

    assert resolve_secret_references(encrypted, {"API_TOKEN": "preview/TOKEN"}) == {
        "API_TOKEN": "value"
    }


def test_missing_secret_reports_reference_not_document(tmp_path: Path, monkeypatch) -> None:
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("encrypted")
    monkeypatch.setattr(secrets_module.shutil, "which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr(
        secrets_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "{}", ""),
    )
    with pytest.raises(SecretError, match="preview/TOKEN"):
        resolve_secret_references(encrypted, {"API_TOKEN": "preview/TOKEN"})


def test_protected_secret_file_is_owner_only_and_removed() -> None:
    with protected_secret_file({"API_TOKEN": "value"}) as path:
        assert path is not None
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {"API_TOKEN": "value"}
        remembered = path
    assert not remembered.exists()
