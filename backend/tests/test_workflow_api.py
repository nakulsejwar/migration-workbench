"""API-level safety tests using a temporary SQLite database and baseline plan."""
from __future__ import annotations

from app.services import engine as E


def _create_reference_plan(client, headers):
    response = client.post(
        "/api/plans",
        headers=headers,
        json={
            "summary": "Test plan based on the supported reference mapping",
            "mappings": E.REFERENCE_MAPPING,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _prepare_approved_plan(client, headers):
    plan = _create_reference_plan(client, headers)
    version = plan["version"]

    dry = client.post(f"/api/plans/{version}/dry-run", headers=headers)
    assert dry.status_code == 200, dry.text
    dry_data = dry.json()
    counts = dry_data["counts"]
    assert counts["source"] == counts["accepted"] + counts["rejected"]
    assert dry_data["verified"] is True

    approval = client.post(
        f"/api/plans/{version}/approve",
        headers=headers,
        json={
            "comment": "Automated test acknowledges the documented migration risks",
            "acknowledge_risks": True,
            "acknowledge_open_questions": True,
        },
    )
    assert approval.status_code == 200, approval.text
    return version, dry_data


def test_health_and_ready_endpoints(client):
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["database"] is True


def test_mutating_endpoint_requires_demo_identity(client):
    response = client.post("/api/plans/propose", json={"mode": "baseline"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "identity_required"


def test_execution_is_blocked_without_approval(client, demo_headers):
    plan = _create_reference_plan(client, demo_headers)

    response = client.post(
        "/api/runs",
        headers=demo_headers,
        json={"plan_version": plan["version"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_required"


def test_dry_run_does_not_change_target_and_is_repeatable(client, demo_headers):
    plan = _create_reference_plan(client, demo_headers)
    version = plan["version"]
    before = client.get("/api/target")

    first = client.post(f"/api/plans/{version}/dry-run", headers=demo_headers)
    second = client.post(f"/api/plans/{version}/dry-run", headers=demo_headers)
    after = client.get("/api/target")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["fingerprint"] == second.json()["fingerprint"]
    assert before.status_code == 200 and after.status_code == 200
    assert before.json() == after.json()


def test_failed_run_can_retry_then_reconcile_and_rollback(client, demo_headers):
    version, dry = _prepare_approved_plan(client, demo_headers)

    failed = client.post(
        "/api/runs",
        headers=demo_headers,
        json={"plan_version": version, "simulate_failure_after": 2},
    )
    assert failed.status_code == 201, failed.text
    failed_run = failed.json()
    assert failed_run["status"] == "failed"
    assert failed_run["checkpoint"] >= 0

    retried = client.post(
        f"/api/runs/{failed_run['id']}/retry",
        headers=demo_headers,
        json={"simulate_failure_after": None},
    )
    assert retried.status_code == 200, retried.text
    completed = retried.json()
    assert completed["status"] == "completed"

    # Retrying a completed run must not insert duplicate rows.
    inserted_before = completed["inserted_total"]
    retried_again = client.post(
        f"/api/runs/{failed_run['id']}/retry",
        headers=demo_headers,
        json={},
    )
    assert retried_again.status_code == 200, retried_again.text
    assert retried_again.json()["status"] == "completed"
    assert retried_again.json()["inserted_total"] == inserted_before

    reconciliation = client.post(
        f"/api/runs/{failed_run['id']}/reconciliation",
        headers=demo_headers,
    )
    assert reconciliation.status_code == 201, reconciliation.text
    assert reconciliation.json()["passed"] is True

    rollback = client.post(
        f"/api/runs/{failed_run['id']}/rollback",
        headers=demo_headers,
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["status"] == "rolled_back"

    reconciliation_after_rollback = client.post(
        f"/api/runs/{failed_run['id']}/reconciliation",
        headers=demo_headers,
    )
    assert reconciliation_after_rollback.status_code == 201, reconciliation_after_rollback.text
    assert reconciliation_after_rollback.json()["passed"] is True


def test_second_run_for_same_approved_plan_is_rejected(client, demo_headers):
    version, _ = _prepare_approved_plan(client, demo_headers)
    first = client.post("/api/runs", headers=demo_headers, json={"plan_version": version})
    assert first.status_code == 201, first.text

    second = client.post("/api/runs", headers=demo_headers, json={"plan_version": version})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "run_exists"
