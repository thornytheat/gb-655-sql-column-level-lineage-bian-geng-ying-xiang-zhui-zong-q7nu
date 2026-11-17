"""M8 执行交接的 HTTP 面：参数校验、状态码、撤回后拒绝领取、页面可读。"""

from __future__ import annotations

import pytest

from app import store
from app.modules.m8_handoff import service as handoff_service


def _new_handoff(client) -> dict:
    return client.post("/api/handoffs", json={}).json()


def test_create_is_idempotent_over_http(client):
    first = _new_handoff(client)
    second = _new_handoff(client)
    assert first["reused"] is False
    assert second["reused"] is True
    assert first["handoff_id"] == second["handoff_id"]


def test_unknown_create_param_is_rejected(client):
    response = client.post("/api/handoffs", json={"run_id": "x", "bogus": 1})
    assert response.status_code == 400
    assert response.json()["error"] == "BAD_INPUT"


def test_unknown_claim_param_is_rejected(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    task = detail["tasks"][0]
    response = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/claim",
        json={"expected_revision": detail["revision"], "extra": True})
    assert response.status_code == 400


def test_claim_without_revision_is_bad_input(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    task = detail["tasks"][0]
    response = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/claim",
        json={})
    assert response.status_code == 400


def test_stale_claim_returns_handoff_stale(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    task = detail["tasks"][0]
    response = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/claim",
        json={"expected_revision": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "HANDOFF_STALE"


def test_pending_task_claim_after_recall_returns_recalled(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    recall = client.post(f"/api/handoffs/{handoff['handoff_id']}/recall",
                         json={"expected_revision": detail["revision"]})
    assert recall.status_code == 200
    task = detail["tasks"][0]
    response = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/claim",
        json={"expected_revision": recall.json()["revision"]})
    assert response.status_code == 409
    assert response.json()["error"] == "HANDOFF_RECALLED"


def test_receipt_rejects_bad_outcome(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    task = detail["tasks"][0]
    claim = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/claim",
        json={"expected_revision": detail["revision"]}).json()
    response = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/receipt",
        json={"credential": claim["credential"], "outcome": "maybe"})
    assert response.status_code == 400


def test_receipt_requires_credential(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    task = detail["tasks"][0]
    response = client.post(
        f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/receipt",
        json={"outcome": "executed"})
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["conflict"]["code"] == "CREDENTIAL_INVALID"


def test_missing_handoff_is_404(client):
    assert client.get("/api/handoffs/ho-missing").status_code == 404
    assert client.get("/api/handoffs/ho-missing/todos").status_code == 404


def test_handoff_page_lists_groups_and_structure(client):
    page = client.get("/handoff")
    assert page.status_code == 200
    assert "执行交接" in page.text
    assert "下游实际结构" in page.text
    assert "批次回读" in page.text


def test_manifest_endpoint_is_stable(client):
    handoff = _new_handoff(client)
    first = client.get(f"/api/handoffs/{handoff['handoff_id']}/manifest.json").json()
    second = client.get(f"/api/handoffs/{handoff['handoff_id']}/manifest.json").json()
    assert first == second
    assert first["manifest_sha256"] == handoff["manifest_sha256"]
    assert len(first["manifest"]["tasks"]) == handoff_service.get_detail(
        handoff["handoff_id"])["counts"]["total"]


def test_todos_endpoint_empty_after_clean_claim(client):
    handoff = _new_handoff(client)
    detail = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    task = detail["tasks"][0]
    client.post(f"/api/handoffs/{handoff['handoff_id']}/tasks/{task['task_id']}/claim",
                json={"expected_revision": detail["revision"]})
    todos = client.get(f"/api/handoffs/{handoff['handoff_id']}/todos").json()
    assert todos["open"] == 0


def test_list_endpoint_reports_progress(client):
    handoff = _new_handoff(client)
    rows = client.get("/api/handoffs").json()["handoffs"]
    assert rows[0]["handoff_id"] == handoff["handoff_id"]
    assert rows[0]["counts"]["groups"]["pending"] == rows[0]["counts"]["total"]


def test_full_rollback_flow_over_http(client):
    handoff = _new_handoff(client)
    hid = handoff["handoff_id"]
    detail = client.get(f"/api/handoffs/{hid}").json()
    task = detail["tasks"][0]

    claim = client.post(f"/api/handoffs/{hid}/tasks/{task['task_id']}/claim",
                        json={"expected_revision": detail["revision"]}).json()
    receipt = client.post(f"/api/handoffs/{hid}/tasks/{task['task_id']}/receipt",
                          json={"credential": claim["credential"],
                                "outcome": "executed"}).json()
    assert receipt["status"] == "executed"

    recall = client.post(f"/api/handoffs/{hid}/recall",
                         json={"expected_revision": receipt["revision"]}).json()
    assert recall["rollback"]["complete"] is False
    assert task["task_id"] in recall["rollback"]["executed_without_compensation"]

    comp = client.post(f"/api/handoffs/{hid}/tasks/{task['task_id']}/claim",
                       json={"expected_revision": recall["revision"],
                             "purpose": "compensate"}).json()
    done = client.post(f"/api/handoffs/{hid}/tasks/{task['task_id']}/receipt",
                       json={"credential": comp["credential"], "outcome": "done"}).json()
    assert done["status"] == "completed"
    assert done["rollback"]["complete"] is True
    final = client.get(f"/api/handoffs/{hid}").json()
    assert final["status"] == "completed"
    assert final["counts"]["by_status"]["completed"] == 1
    assert len(final["conflicts"]) == 0


def test_changing_lineage_does_not_move_handoff(client, run):
    handoff = _new_handoff(client)
    detail_before = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    # 重跑同一批次不产生新 run，也不改任务结构
    client.post("/api/runs", json={})
    detail_after = client.get(f"/api/handoffs/{handoff['handoff_id']}").json()
    assert detail_after["counts"] == detail_before["counts"]
    assert detail_after["manifest_sha256"] == detail_before["manifest_sha256"]
    assert store.count_runs() == 1
