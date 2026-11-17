"""M8 执行交接：逐下游任务、凭据绑定、回执状态机、撤回与补偿、并发与恢复。"""

from __future__ import annotations

import copy

import pytest

from app import errors, store
from app.modules.m8_handoff import service as handoff_service
from app.modules.m8_handoff import spec as handoff_spec


# ---------- 夹具 ----------

@pytest.fixture()
def handoff(client):
    created = handoff_service.create()
    return created["handoff"]


def _detail(handoff: dict) -> dict:
    return handoff_service.get_detail(handoff["handoff_id"])


def _task(detail: dict, consumer: str) -> dict:
    return next(item for item in detail["tasks"] if item["consumer"] == consumer)


def _claim(handoff_id: str, task_id: str, revision: int, purpose: str = "execute",
           ttl: int | None = None, key: str | None = None) -> dict:
    return handoff_service.claim(handoff_id, task_id, revision, purpose=purpose,
                                 ttl_seconds=ttl, idempotency_key=key)


def _receipt(handoff_id: str, task_id: str, credential: dict, outcome: str,
             key: str | None = None) -> dict:
    return handoff_service.receipt(handoff_id, task_id, credential, outcome,
                                   idempotency_key=key)


# ---------- 任务生成 ----------

def test_tasks_follow_real_batch_impact(handoff, run):
    detail = _detail(handoff)
    impacted = [row["name"] for row in run["impact"]["impact"]["rows"]]
    assert [task["consumer"] for task in detail["tasks"]] == sorted(impacted)
    assert detail["counts"]["total"] == len(impacted)


def test_task_manifest_binds_structure_digest(handoff):
    detail = _detail(handoff)
    for task in detail["tasks"]:
        payload = {"task_id": task["task_id"], "consumer": task["consumer"],
                   "kind": task["kind"], "structure": task["structure"]}
        assert handoff_spec.digest(payload) == task["manifest_sha256"]


def test_downstream_structure_reads_batch_resolution(handoff, run):
    detail = _detail(handoff)
    by_consumer = {item["consumer"]: item for item in detail["downstream"]}
    for consumer, item in by_consumer.items():
        expected = [row for row in run["resolution"]["resolved"]
                    if row["consumer"] == consumer]
        resolved = [ref for ref in item["structure"]["refs"] if ref["resolved"]]
        assert len(resolved) == len(expected)
        assert {ref["ref"] for ref in resolved} == {row["ref"] for row in expected}


def test_creating_twice_reuses_same_handoff(client, handoff):
    again = handoff_service.create()
    assert again["reused"] is True
    assert again["handoff"]["handoff_id"] == handoff["handoff_id"]
    assert len(handoff_service.list_all()) == 1


def test_task_ids_are_stable_across_regeneration(handoff, run):
    from datetime import datetime

    rebuilt = handoff_service._build_handoff(
        run, datetime.fromisoformat(handoff["created_at"]))
    assert [task["task_id"] for task in rebuilt["tasks"]] == \
        [task["task_id"] for task in handoff["tasks"]]
    assert rebuilt["manifest_sha256"] == handoff["manifest_sha256"]


# ---------- 领取 ----------

def test_claim_requires_current_revision(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    with pytest.raises(errors.HandoffStale):
        _claim(handoff["handoff_id"], task["task_id"], detail["revision"] - 1)


def test_claim_issues_credential_bound_to_manifest(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    credential = claimed["credential"]
    assert claimed["status"] == "claimed"
    assert credential["manifest_sha256"] == task["manifest_sha256"]
    assert credential["purpose"] == "execute"
    assert credential["token"].startswith("hg-")


def test_second_claim_is_conflict(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    first = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    with pytest.raises(errors.HandoffConflict) as caught:
        _claim(handoff["handoff_id"], task["task_id"], first["revision"])
    assert caught.value.detail["reason"] == "ALREADY_CLAIMED"


def test_claim_with_idempotency_key_is_replayable(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    first = _claim(handoff["handoff_id"], task["task_id"], detail["revision"],
                   key="claim-1")
    replay = _claim(handoff["handoff_id"], task["task_id"], 999, key="claim-1")
    assert replay["reused"] is True
    assert replay["credential"]["credential_id"] == \
        first["credential"]["credential_id"]
    assert replay["revision"] == first["revision"]


def test_expired_credential_can_be_reissued(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    first = _claim(handoff["handoff_id"], task["task_id"], detail["revision"], ttl=1)
    # 把当前凭据的到期时间提到过去，模拟凭据过期
    stored = store.get_handoff(handoff["handoff_id"])
    for item in stored["tasks"]:
        if item["task_id"] == task["task_id"]:
            item["credential"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    store.cas_handoff(stored["handoff_id"], stored["revision"], stored)
    renewed = _claim(handoff["handoff_id"], task["task_id"], first["revision"])
    assert renewed["status"] == "claimed"
    assert renewed["credential"]["credential_id"] != \
        first["credential"]["credential_id"]


# ---------- 回执 ----------

def test_execute_receipt_requires_matching_credential(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    bad = dict(claimed["credential"], token="hg-forged")
    result = _receipt(handoff["handoff_id"], task["task_id"], bad, "executed")
    assert result["accepted"] is False
    assert result["conflict"]["code"] == "CREDENTIAL_INVALID"
    assert _task(_detail(handoff), task["consumer"])["status"] == "claimed"


def test_execute_receipt_marks_task_executed(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    result = _receipt(handoff["handoff_id"], task["task_id"],
                      claimed["credential"], "executed")
    assert result["accepted"] is True
    assert result["status"] == "executed"
    assert _task(_detail(handoff), task["consumer"])["status"] == "executed"


def test_not_executed_receipt_returns_task_to_pending(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    result = _receipt(handoff["handoff_id"], task["task_id"],
                      claimed["credential"], "not_executed")
    assert result["status"] == "pending"


def test_expired_credential_receipt_is_conflict_and_does_not_settle(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    stored = store.get_handoff(handoff["handoff_id"])
    for item in stored["tasks"]:
        if item["task_id"] == task["task_id"]:
            item["credential"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    store.cas_handoff(stored["handoff_id"], stored["revision"], stored)

    result = _receipt(handoff["handoff_id"], task["task_id"],
                      claimed["credential"], "executed")
    assert result["accepted"] is False
    assert result["conflict"]["code"] == "CREDENTIAL_EXPIRED"
    updated = _task(_detail(handoff), task["consumer"])
    assert updated["status"] == "claimed"
    assert updated["receipts"] == []


def test_receipt_with_idempotency_key_is_not_double_counted(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    first = _receipt(handoff["handoff_id"], task["task_id"],
                     claimed["credential"], "executed", key="rc-1")
    replay = _receipt(handoff["handoff_id"], task["task_id"],
                      claimed["credential"], "executed", key="rc-1")
    assert replay["reused"] is True
    assert replay["status"] == first["status"]
    stored = _task(_detail(handoff), task["consumer"])
    assert len(stored["receipts"]) == 1


def test_receipt_after_settlement_is_recorded_as_conflict(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    _receipt(handoff["handoff_id"], task["task_id"], claimed["credential"], "executed")
    again = _receipt(handoff["handoff_id"], task["task_id"],
                     claimed["credential"], "not_executed")
    assert again["accepted"] is False
    assert again["conflict"]["code"] == "RECEIPT_STATE"
    assert _task(_detail(handoff), task["consumer"])["status"] == "executed"


# ---------- 撤回 ----------

def test_recall_cancels_pending_and_keeps_claimed_exposed(handoff):
    detail = _detail(handoff)
    first, second = detail["tasks"][0], detail["tasks"][1]
    claimed = _claim(handoff["handoff_id"], first["task_id"], detail["revision"])
    recalled = handoff_service.recall(handoff["handoff_id"], claimed["revision"])

    statuses = {task["task_id"]: task["status"] for task in _detail(handoff)["tasks"]}
    assert statuses[first["task_id"]] == "claimed"
    assert statuses[second["task_id"]] == "cancelled"
    assert recalled["rollback"]["required"] is True
    assert recalled["rollback"]["complete"] is False
    assert first["task_id"] in recalled["rollback"]["outstanding"]


def test_recall_blocks_new_claims(handoff):
    detail = _detail(handoff)
    handoff_service.recall(handoff["handoff_id"], detail["revision"])
    task = detail["tasks"][0]
    with pytest.raises(errors.HandoffClosed):
        _claim(handoff["handoff_id"], task["task_id"],
               _detail(handoff)["revision"])


def test_executed_task_generates_compensation_after_recall(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    _receipt(handoff["handoff_id"], task["task_id"], claimed["credential"], "executed")

    recalled = handoff_service.recall(handoff["handoff_id"],
                                      _detail(handoff)["revision"])
    updated = _task(_detail(handoff), task["consumer"])
    assert updated["status"] == "compensation_pending"
    assert updated["compensation"]["status"] == "pending"
    assert task["task_id"] in recalled["rollback"]["executed_without_compensation"]
    assert recalled["rollback"]["complete"] is False


def test_compensation_receipt_completes_rollback(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    _receipt(handoff["handoff_id"], task["task_id"], claimed["credential"], "executed")
    handoff_service.recall(handoff["handoff_id"], _detail(handoff)["revision"])

    comp_claim = _claim(handoff["handoff_id"], task["task_id"],
                        _detail(handoff)["revision"], purpose="compensate")
    assert comp_claim["status"] == "compensation_claimed"
    assert comp_claim["credential"]["purpose"] == "compensate"

    result = _receipt(handoff["handoff_id"], task["task_id"],
                      comp_claim["credential"], "done")
    assert result["status"] == "completed"
    rollback = result["rollback"]
    assert rollback["complete"] is True
    assert rollback["settled"] == rollback["total"]
    assert handoff_service.get_detail(handoff["handoff_id"])["status"] == "completed"


def test_in_flight_receipt_after_recall_forks_correctly(handoff):
    detail = _detail(handoff)
    first, second = detail["tasks"][0], detail["tasks"][1]
    claimed_first = _claim(handoff["handoff_id"], first["task_id"], detail["revision"])
    claimed_second = _claim(handoff["handoff_id"], second["task_id"],
                            claimed_first["revision"])
    handoff_service.recall(handoff["handoff_id"], claimed_second["revision"])

    executed = _receipt(handoff["handoff_id"], first["task_id"],
                        claimed_first["credential"], "executed")
    assert executed["status"] == "compensation_pending"
    skipped = _receipt(handoff["handoff_id"], second["task_id"],
                       claimed_second["credential"], "not_executed")
    assert skipped["status"] == "cancelled"


def test_recall_is_idempotent_with_key(handoff):
    detail = _detail(handoff)
    first = handoff_service.recall(handoff["handoff_id"], detail["revision"],
                                   idempotency_key="recall-1")
    replay = handoff_service.recall(handoff["handoff_id"], 999,
                                    idempotency_key="recall-1")
    assert replay["reused"] is True
    assert replay["revision"] == first["revision"]
    with pytest.raises(errors.HandoffConflict):
        handoff_service.recall(handoff["handoff_id"], first["revision"])


def test_recall_revision_is_optimistic(handoff):
    detail = _detail(handoff)
    with pytest.raises(errors.HandoffStale):
        handoff_service.recall(handoff["handoff_id"], detail["revision"] + 5)


# ---------- 并发 ----------

def test_claim_and_recall_race_keeps_one_winner(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    revision = detail["revision"]

    # 模拟并发：两个写者都读到同一版本，先撤回成功，后到的领取看到旧版本。
    handoff_service.recall(handoff["handoff_id"], revision)
    with pytest.raises((errors.HandoffStale, errors.HandoffClosed)):
        _claim(handoff["handoff_id"], task["task_id"], revision)


def test_two_claims_on_same_revision_only_first_wins(handoff):
    from app.modules.m8_handoff import service as svc

    detail = _detail(handoff)
    task = detail["tasks"][0]
    revision = detail["revision"]
    original = svc._mutate
    seen: list[int] = []

    def racing_mutate(handoff_id, op, key, task_id, mutate, **kwargs):
        result = original(handoff_id, op, key, task_id, mutate, **kwargs)
        seen.append(result[0]["revision"])
        return result

    # 直接并发写：先领取一次，再用旧版本号领取必须被拒。
    first = _claim(handoff["handoff_id"], task["task_id"], revision)
    with pytest.raises(errors.HandoffStale):
        _claim(handoff["handoff_id"], task["task_id"], revision)
    assert first["status"] == "claimed"


# ---------- 写账中断与恢复 ----------

def test_interrupted_write_is_completed_on_recovery(handoff, monkeypatch):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    revision = detail["revision"]

    real_cas = store.cas_handoff

    def crashed_cas(handoff_id, expected_revision, payload):
        # 日志已写、改库前进程挂掉：既不落库，也不记完成。
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "cas_handoff", crashed_cas)
    with pytest.raises(RuntimeError):
        _claim(handoff["handoff_id"], task["task_id"], revision)
    monkeypatch.setattr(store, "cas_handoff", real_cas)

    pending = store.open_journals(handoff["handoff_id"])
    assert pending, "中断应留下待办日志"
    outcomes = handoff_service.recover(handoff["handoff_id"])
    assert {item["outcome"] for item in outcomes} == {"recovered"}
    recovered = _task(_detail(handoff), task["consumer"])
    assert recovered["status"] == "claimed"
    assert store.open_journals(handoff["handoff_id"]) == []


def test_recovery_after_landed_write_does_not_double_apply(handoff, monkeypatch):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    revision = detail["revision"]

    real_finish = store.journal_finish
    calls = {"n": 0}

    def flaky_finish(journal_id, status, finished_at, note=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # 改库成功，但记账完成前中断
        return real_finish(journal_id, status, finished_at, note)

    monkeypatch.setattr(store, "journal_finish", flaky_finish)
    claimed = _claim(handoff["handoff_id"], task["task_id"], revision)
    monkeypatch.setattr(store, "journal_finish", real_finish)

    outcomes = handoff_service.recover(handoff["handoff_id"])
    assert {item["outcome"] for item in outcomes} == {"applied"}
    stored = _task(_detail(handoff), task["consumer"])
    assert stored["status"] == "claimed"
    assert stored["credential"]["credential_id"] == \
        claimed["credential"]["credential_id"]
    assert len(stored["receipts"]) == 0


def test_recovery_is_repeatable(handoff):
    assert handoff_service.recover(handoff["handoff_id"]) == []
    assert handoff_service.recover(handoff["handoff_id"]) == []


# ---------- 凭据绑定清单内容 ----------

def test_credential_manifest_mismatch_is_conflict(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    tampered = dict(claimed["credential"], manifest_sha256="0" * 64)
    result = _receipt(handoff["handoff_id"], task["task_id"], tampered, "executed")
    assert result["accepted"] is False
    assert result["conflict"]["code"] == "CREDENTIAL_MANIFEST_MISMATCH"


def test_superseded_credential_receipt_is_conflict(handoff):
    detail = _detail(handoff)
    task = detail["tasks"][0]
    first = _claim(handoff["handoff_id"], task["task_id"], detail["revision"], ttl=1)
    stored = store.get_handoff(handoff["handoff_id"])
    for item in stored["tasks"]:
        if item["task_id"] == task["task_id"]:
            item["credential"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    store.cas_handoff(stored["handoff_id"], stored["revision"], stored)
    renewed = _claim(handoff["handoff_id"], task["task_id"], first["revision"])

    result = _receipt(handoff["handoff_id"], task["task_id"],
                      first["credential"], "executed")
    assert result["accepted"] is False
    assert result["conflict"]["code"] == "CREDENTIAL_SUPERSEDED"
    # 新凭据仍可正常回执
    ok = _receipt(handoff["handoff_id"], task["task_id"],
                  renewed["credential"], "executed")
    assert ok["accepted"] is True


# ---------- 只回读、不改血缘 ----------

def test_handoff_does_not_change_run_or_lineage(client, handoff, run):
    before = copy.deepcopy(run)
    detail = _detail(handoff)
    task = detail["tasks"][0]
    claimed = _claim(handoff["handoff_id"], task["task_id"], detail["revision"])
    _receipt(handoff["handoff_id"], task["task_id"], claimed["credential"], "executed")
    after = store.run_by_id(run["run_id"])
    assert after["diff"] == before["diff"]
    assert after["impact"] == before["impact"]
    assert after["resolution"] == before["resolution"]
    assert store.count_runs() == 1
