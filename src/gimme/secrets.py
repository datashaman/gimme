from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SecretError(RuntimeError):
    pass


def _lookup(document: object, reference: str) -> str:
    value = document
    for part in reference.split("/"):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(reference)
        value = value[part]
    if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
        raise SecretError(f"secret reference is not a single-line string: {reference}")
    return value


def resolve_secret_references(path: Path, references: dict[str, str]) -> dict[str, str]:
    if not references:
        return {}
    executable = shutil.which("sops")
    if executable is None:
        raise SecretError("SOPS is required to resolve deployment secret references")
    if not path.is_file() or path.is_symlink():
        raise SecretError("encrypted secret document is missing or unsafe")
    result = subprocess.run(  # nosec B603
        [executable, "--decrypt", "--output-type", "json", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
        env={
            name: os.environ[name]
            for name in ("PATH", "SOPS_AGE_KEY", "SOPS_AGE_KEY_FILE")
            if name in os.environ
        },
    )
    if result.returncode != 0:
        raise SecretError("SOPS could not decrypt the secret document")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SecretError("SOPS returned an invalid JSON secret document") from exc
    missing: list[str] = []
    resolved: dict[str, str] = {}
    for key, reference in references.items():
        try:
            resolved[key] = _lookup(document, reference)
        except KeyError:
            missing.append(reference)
    if missing:
        raise SecretError("missing secret references: " + ", ".join(sorted(missing)))
    return resolved


@contextmanager
def protected_secret_file(values: dict[str, str]) -> Iterator[Path | None]:
    if not values:
        yield None
        return
    descriptor, temporary = tempfile.mkstemp(prefix="gimme-secrets-", suffix=".json")
    path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(values, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)
