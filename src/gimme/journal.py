from __future__ import annotations

import fcntl
import json
import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CorrelationId = str
Phase = Literal["plan", "apply", "outcome"]
Status = Literal["started", "succeeded", "failed", "rejected", "stale"]


class OperationEvent(BaseModel):
    """One deliberately small, secret-safe control-plane journal record."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^event_[a-f0-9]{32}$")
    correlation_id: str = Field(pattern=r"^corr_[a-f0-9]{32}$")
    plan_correlation_id: str | None = Field(
        default=None, pattern=r"^corr_[a-f0-9]{32}$"
    )
    timestamp: datetime
    operation: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    phase: Phase
    status: Status
    subjects: dict[str, str] = Field(default_factory=dict)
    plan_id: str | None = Field(default=None, pattern=r"^plan_[a-f0-9]{20}$")
    error_code: Literal["stale_plan", "policy_rejected", "operation_failed"] | None = None

    @field_validator("subjects")
    @classmethod
    def safe_subjects(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"name", "source", "destination"}
        safe_name = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
        if not set(value).issubset(allowed) or any(
            safe_name.fullmatch(item) is None for item in value.values()
        ):
            raise ValueError("journal subjects must be bounded registered names")
        return value

    @model_validator(mode="after")
    def coherent_lifecycle(self) -> "OperationEvent":
        allowed = {
            "plan": {"succeeded", "failed", "rejected"},
            "apply": {"started"},
            "outcome": {"succeeded", "failed", "rejected", "stale"},
        }
        expected_error = {
            "started": None,
            "succeeded": None,
            "failed": "operation_failed",
            "rejected": "policy_rejected",
            "stale": "stale_plan",
        }
        if self.status not in allowed[self.phase]:
            raise ValueError("journal phase and status are inconsistent")
        if self.error_code != expected_error[self.status]:
            raise ValueError("journal status and error_code are inconsistent")
        return self


class OperationJournal:
    """Append-only JSONL journal stored beside, but separate from, desired state."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.path = self.root / "operations.jsonl"
        self.lock_path = self.root / ".gimme-journal.lock"

    @staticmethod
    def correlation_id() -> CorrelationId:
        return f"corr_{uuid4().hex}"

    def append(
        self,
        *,
        correlation_id: CorrelationId,
        operation: str,
        phase: Phase,
        status: Status,
        subjects: dict[str, str],
        plan_id: str | None = None,
        plan_correlation_id: str | None = None,
        error_code: str | None = None,
    ) -> OperationEvent:
        event = OperationEvent(
            event_id=f"event_{uuid4().hex}",
            correlation_id=correlation_id,
            plan_correlation_id=plan_correlation_id,
            timestamp=datetime.now(UTC),
            operation=operation,
            phase=phase,
            status=status,
            subjects=subjects,
            plan_id=plan_id,
            error_code=error_code,
        )
        payload = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                remaining = memoryview((payload + "\n").encode())
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written == 0:
                        raise OSError("operation journal append made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        return event

    def list(
        self,
        *,
        limit: int = 50,
        operation: str | None = None,
        subject: str | None = None,
        correlation_id: str | None = None,
    ) -> list[OperationEvent]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if (
            correlation_id is not None
            and re.fullmatch(r"corr_[a-f0-9]{32}", correlation_id) is None
        ):
            raise ValueError("invalid correlation_id")
        if not self.path.exists():
            return []
        selected: deque[OperationEvent] = deque(maxlen=limit)
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            with self.path.open() as stream:
                for line_number, line in enumerate(stream, start=1):
                    event = self._event(line, line_number)
                    if operation is not None and event.operation != operation:
                        continue
                    if subject is not None and subject not in event.subjects.values():
                        continue
                    if correlation_id is not None and event.correlation_id != correlation_id:
                        continue
                    selected.append(event)
        return list(reversed(selected))

    def plan_correlation(self, plan_id: str | None, operation: str) -> str | None:
        if plan_id is None or not self.path.exists():
            return None
        found = None
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            with self.path.open() as stream:
                for line_number, line in enumerate(stream, start=1):
                    event = self._event(line, line_number)
                    if (
                        event.operation == operation
                        and event.phase == "plan"
                        and event.status == "succeeded"
                        and event.plan_id == plan_id
                    ):
                        found = event.correlation_id
        return found

    @staticmethod
    def _event(line: str, line_number: int) -> OperationEvent:
        try:
            return OperationEvent.model_validate_json(line)
        except ValueError as exc:
            raise RuntimeError(
                f"operation journal contains an invalid record at line {line_number}"
            ) from exc
