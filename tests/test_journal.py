import json

import pytest

from gimme.journal import OperationJournal


def test_journal_is_append_only_private_and_filterable(tmp_path) -> None:
    journal = OperationJournal(tmp_path)
    correlation_id = journal.correlation_id()
    journal.append(
        correlation_id=correlation_id,
        operation="deployment",
        phase="apply",
        status="started",
        subjects={"name": "example-local"},
        plan_id="plan_" + "a" * 20,
    )
    journal.append(
        correlation_id=correlation_id,
        operation="deployment",
        phase="outcome",
        status="succeeded",
        subjects={"name": "example-local"},
        plan_id="plan_" + "a" * 20,
    )

    events = journal.list(correlation_id=correlation_id)

    assert [event.phase for event in events] == ["outcome", "apply"]
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert len(journal.path.read_text().splitlines()) == 2


def test_journal_rejects_unbounded_subject_data(tmp_path) -> None:
    journal = OperationJournal(tmp_path)

    with pytest.raises(ValueError, match="bounded registered names"):
        journal.append(
            correlation_id=journal.correlation_id(),
            operation="deployment",
            phase="apply",
            status="started",
            subjects={"name": "SECRET=value"},
        )

    assert not journal.path.exists()


def test_journal_fails_closed_on_invalid_record(tmp_path) -> None:
    journal = OperationJournal(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    journal.path.write_text(json.dumps({"unexpected": "record"}) + "\n")

    with pytest.raises(RuntimeError, match="invalid record at line 1"):
        journal.list()
