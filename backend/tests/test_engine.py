"""Focused tests for deterministic transformation and migration safety rules."""
from __future__ import annotations

import copy

from app.services import engine as E


def test_stable_serialization_and_hash_are_deterministic():
    left = {"z": [1, True, None], "a": {"b": "value", "a": 2}}
    right = {"a": {"a": 2, "b": "value"}, "z": [1, True, None]}

    assert E.stable(left) == E.stable(right)
    assert E.hash_str(E.stable(left)) == E.hash_str(E.stable(right))
    assert E.source_hash([left]) == E.source_hash([right])


def test_reference_mapping_passes_validation():
    result = E.validate_plan(E.REFERENCE_MAPPING)

    assert result["ok"] is True
    assert not [issue for issue in result["issues"] if issue["level"] == "error"]


def test_unknown_rule_and_missing_required_mapping_are_rejected():
    invalid = [
        {"target": "account_id", "source": "cust_id", "rule": "execute_python", "params": {}},
    ]

    result = E.validate_plan(invalid)
    codes = {issue["code"] for issue in result["issues"]}

    assert result["ok"] is False
    assert "unsupported_rule" in codes
    assert "required_unmapped" in codes


def test_dry_run_is_deterministic_and_count_invariant_holds(sample_records):
    first = E.dry_run(E.REFERENCE_MAPPING, sample_records)
    second = E.dry_run(E.REFERENCE_MAPPING, copy.deepcopy(sample_records))

    assert "error" not in first
    assert first["hash"] == second["hash"]
    assert first["plan_hash"] == second["plan_hash"]
    assert first["source_hash"] == second["source_hash"]
    counts = first["counts"]
    assert counts["source"] == len(sample_records)
    assert counts["source"] == counts["accepted"] + counts["rejected"]
    assert len(first["accepted"]) == counts["accepted"]
    assert len(first["rejected"]) == counts["rejected"]


def test_duplicate_source_keys_are_quarantined(sample_records):
    # Keep two otherwise-valid records but force the second to reuse the first ID.
    records = copy.deepcopy(sample_records[:2])
    records[1]["cust_id"] = records[0]["cust_id"]

    result = E.dry_run(E.REFERENCE_MAPPING, records)

    assert "error" not in result
    assert result["counts"]["source"] == 2
    assert result["counts"]["accepted"] + result["counts"]["rejected"] == 2
    assert any(
        error["code"] == "duplicate_key"
        for row in result["rejected"]
        for error in row["errors"]
    )


def test_dry_run_rejects_oversized_input(sample_records):
    records = sample_records * (E.MAX_RECORDS // len(sample_records) + 1)

    result = E.dry_run(E.REFERENCE_MAPPING, records)

    assert result["error"] == "too_many_records"


def test_existing_conflicting_key_is_never_overwritten():
    accepted = [{
        "row": 1,
        "key": "CUST-1001",
        "record": {"account_id": "CUST-1001", "balance_minor": 100},
        "hash": "new-hash",
    }]
    original = {"record": {"account_id": "CUST-1001", "balance_minor": 200}, "hash": "old-hash", "run_ref": "seed", "seq": 1}
    target = {"records": {"CUST-1001": original}, "seq": 1}

    result = E.apply_batch(target, accepted, "run-test")

    assert result["outcome"]["inserted"] == []
    assert result["outcome"]["conflicts"][0]["key"] == "CUST-1001"
    assert result["target"]["records"]["CUST-1001"] == original


def test_rollback_only_removes_rows_owned_by_that_run():
    target = {"records": {
        "OWNED": {"record": {"account_id": "OWNED"}, "hash": "1", "run_ref": "run-1", "seq": 2},
        "SEED": {"record": {"account_id": "SEED"}, "hash": "2", "run_ref": "seed", "seq": 1},
    }, "seq": 2}

    result = E.rollback_run(target, "run-1")

    assert result["removed"] == ["OWNED"]
    assert set(result["target"]["records"]) == {"SEED"}
