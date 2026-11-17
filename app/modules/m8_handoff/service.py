"""执行交接状态机与持久化编排。

一次交接单对应一个真实批次，逐条下游任务由批次里的影响面展开。状态推进只有
下面几条边，其余一律拒绝：

    待领 pending ──领取──▶ 在途 claimed ──回执=已执行──▶ 已执行 executed
        │                      │
        │撤回收回              │回执=未执行
        ▼                      ▼
      已取消 cancelled        待领 pending（可重新领取，须带新版本）

撤回：
    待领 → 已取消
    在途 → 留在途，等待回执；回执到达后再分叉（已执行 → 待补偿，未执行 → 已取消）
    已执行 → 待补偿 compensation_pending
    待补偿 ──领取补偿──▶ 补偿在途 ──补偿回执=done──▶ 完成 completed

只有「撤回后每个任务要么已取消、要么补偿回执已确认」才算回退完成；已执行项在
补偿确认前一律计入未结算，避免整单撤销虚报回退。

写账先记日志再比较交换 revision；中断后恢复时按日志把未落地的意图补完，
重试凭幂等键取回同一结果，不重复派发也不重复记账。
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timedelta

from app import config, errors, store
from app.modules.m8_handoff import spec

EXECUTE = "execute"
COMPENSATE = "compensate"

TASK_STATUSES = (
    "pending", "claimed", "executed", "cancelled",
    "compensation_pending", "compensation_claimed", "completed",
)

GROUPS = {
    "pending": ("pending",),
    "in_flight": ("claimed", "compensation_claimed"),
    "pending_compensation": ("compensation_pending",),
    "completed": ("executed", "cancelled", "completed"),
}

EXECUTE_OUTCOMES = ("executed", "not_executed")
COMPENSATE_OUTCOMES = ("done", "failed")

MAX_IDEMPOTENCY_KEYS = 200
CAS_RETRIES = 3


# ---------- 小工具 ----------

def _now() -> datetime:
    return config.now_utc()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


def _load_or_404(handoff_id: str) -> dict:
    handoff = store.get_handoff(handoff_id)
    if handoff is None:
        raise errors.NotFound(f"没有这张交接单：{handoff_id}",
                             detail={"handoff_id": handoff_id})
    return handoff


def _task_of(handoff: dict, task_id: str) -> dict:
    for task in handoff.get("tasks") or []:
        if task["task_id"] == task_id:
            return task
    raise errors.NotFound(f"交接单里没有这个任务：{task_id}",
                         detail={"handoff_id": handoff["handoff_id"],
                                 "task_id": task_id})


def _stale(expected, current: int) -> errors.HandoffStale:
    return errors.HandoffStale(
        f"交接版本 {expected} 已落后于当前版本 {current}",
        detail={"expected_revision": expected, "current_revision": current,
                "reason": "HANDOFF_STALE"},
    )


def _check_revision(handoff: dict, expected) -> int:
    if expected is None:
        raise errors.BadInput("必须带上 expected_revision",
                             detail={"field": "expected_revision"})
    try:
        value = int(expected)
    except (TypeError, ValueError):
        raise errors.BadInput("expected_revision 必须是整数",
                             detail={"value": repr(expected)})
    if value != int(handoff["revision"]):
        raise _stale(value, int(handoff["revision"]))
    return value


def _idem_key(value) -> str:
    return str(value or "").strip()[:120]


def _idem_entry(handoff: dict, key: str, op: str,
                task_id: str | None) -> dict | None:
    if not key:
        return None
    entry = (handoff.get("idempotency") or {}).get(key)
    if entry is None:
        return None
    if entry.get("op") != op or entry.get("task_id") != task_id:
        raise errors.HandoffConflict(
            "幂等键已用于别的动作",
            detail={"idempotency_key": key, "op": op, "task_id": task_id,
                    "reason": "IDEMPOTENCY_CONFLICT"},
        )
    return entry


def _remember(handoff: dict, key: str, op: str, task_id: str | None,
              result: dict) -> None:
    if not key:
        return
    bucket = dict(handoff.get("idempotency") or {})
    bucket[key] = {"op": op, "task_id": task_id,
                   "result": copy.deepcopy(result)}
    while len(bucket) > MAX_IDEMPOTENCY_KEYS:
        bucket.pop(next(iter(bucket)))
    handoff["idempotency"] = bucket


def _record_conflict(handoff: dict, task_id: str | None, code: str,
                     detail: str, credential, key: str) -> dict:
    item = {
        "conflict_id": _new_id("cf-"),
        "task_id": task_id,
        "code": code,
        "detail": detail,
        "credential_id": (credential.get("credential_id")
                          if isinstance(credential, dict) else None),
        "idempotency_key": key,
        "recorded_at": _iso(_now()),
    }
    handoff.setdefault("conflicts", []).append(item)
    return item


def _refresh_status(handoff: dict) -> None:
    state = rollback_state(handoff)
    if not state["required"]:
        handoff["status"] = "active"
    elif state["complete"]:
        handoff["status"] = "completed"
    else:
        handoff["status"] = "recalled"


def _bump(handoff: dict, moment: datetime) -> None:
    handoff["revision"] = int(handoff["revision"]) + 1
    handoff["updated_at"] = _iso(moment)


# ---------- 状态视图 ----------

def rollback_state(handoff: dict) -> dict:
    recalled = handoff.get("recall") is not None
    tasks = handoff.get("tasks") or []
    outstanding = [task["task_id"] for task in tasks
                   if task["status"] not in ("cancelled", "completed")]
    return {
        "required": recalled,
        "complete": bool(recalled) and not outstanding,
        "total": len(tasks),
        "settled": len(tasks) - len(outstanding),
        "outstanding": outstanding,
        "executed_without_compensation": [
            task["task_id"] for task in tasks
            if task["status"] in ("executed", "compensation_pending",
                                  "compensation_claimed")
        ],
    }


def _counts(handoff: dict) -> dict:
    counts = {name: 0 for name in TASK_STATUSES}
    for task in handoff.get("tasks") or []:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    groups = {name: sum(counts.get(status, 0) for status in statuses)
              for name, statuses in GROUPS.items()}
    return {"by_status": counts, "groups": groups, "total": sum(counts.values())}


def _public_credential(credential: dict | None) -> dict | None:
    if not credential:
        return None
    return {key: value for key, value in credential.items() if key != "token"}


def _public_compensation(comp: dict | None) -> dict | None:
    if not comp:
        return None
    out = {key: value for key, value in comp.items() if key not in
           ("credential", "credential_history")}
    out["credential"] = _public_credential(comp.get("credential"))
    out["receipts"] = copy.deepcopy(comp.get("receipts") or [])
    return out


def _public_task(task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "consumer": task["consumer"],
        "kind": task["kind"],
        "status": task["status"],
        "manifest_sha256": task["manifest_sha256"],
        "structure": copy.deepcopy(task.get("structure") or {}),
        "credential": _public_credential(task.get("credential")),
        "receipts": copy.deepcopy(task.get("receipts") or []),
        "recall_pending_receipt": bool(task.get("recall_pending_receipt")),
        "compensation": _public_compensation(task.get("compensation")),
    }


def detail(handoff: dict) -> dict:
    return {
        "handoff_id": handoff["handoff_id"],
        "run_id": handoff["run_id"],
        "status": handoff["status"],
        "revision": handoff["revision"],
        "created_at": handoff["created_at"],
        "updated_at": handoff["updated_at"],
        "manifest_sha256": handoff["manifest_sha256"],
        "version_label_old": handoff.get("version_label_old"),
        "version_label_new": handoff.get("version_label_new"),
        "counts": _counts(handoff),
        "groups": {
            name: [_public_task(task) for task in handoff["tasks"]
                   if task["status"] in statuses]
            for name, statuses in GROUPS.items()
        },
        "tasks": [_public_task(task) for task in handoff["tasks"]],
        "recall": copy.deepcopy(handoff.get("recall")),
        "rollback": rollback_state(handoff),
        "conflicts": copy.deepcopy(handoff.get("conflicts") or []),
        "downstream": [
            {
                "consumer": task["consumer"],
                "kind": task["kind"],
                "structure": copy.deepcopy(task.get("structure") or {}),
            }
            for task in handoff["tasks"]
        ],
    }


# ---------- 创建 ----------

def _build_handoff(run: dict, moment: datetime) -> dict:
    handoff_id = spec.handoff_id_for(run["run_id"])
    rows = ((run.get("impact") or {}).get("impact") or {}).get("rows") or []
    seen: set[str] = set()
    ordered: list[dict] = []
    for row in sorted(rows, key=lambda item: item["name"]):
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        ordered.append(row)

    tasks: list[dict] = []
    for row in ordered:
        built = spec.task_spec(run, handoff_id, row)
        built.update({
            "status": "pending",
            "created_at": _iso(moment),
            "updated_at": _iso(moment),
            "credential": None,
            "receipts": [],
            "recall_pending_receipt": False,
            "compensation": None,
        })
        tasks.append(built)

    manifest = spec.build_manifest(run, tasks)
    return {
        "handoff_id": handoff_id,
        "run_id": run["run_id"],
        "batch_key": run.get("batch_key"),
        "input_sha256": run.get("input_sha256"),
        "ruleset": run.get("ruleset"),
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
        "effective_from": run.get("effective_from"),
        "effective_to": run.get("effective_to"),
        "created_at": _iso(moment),
        "updated_at": _iso(moment),
        "revision": 1,
        "status": "active",
        "manifest": manifest,
        "manifest_sha256": spec.digest(manifest),
        "tasks": tasks,
        "recall": None,
        "conflicts": [],
        "idempotency": {},
    }


def create(run_id: str | None = None) -> dict:
    recover()
    run = store.run_by_id(run_id) if run_id else store.latest_run()
    if run is None:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次",
                             detail={"run_id": run_id})
    handoff = _build_handoff(run, _now())
    inserted = store.insert_handoff(handoff)
    if inserted:
        return {"handoff": store.get_handoff(handoff["handoff_id"]) or handoff,
                "reused": False}
    existing = store.find_handoff_by_run(run["run_id"])
    if existing is None:
        raise errors.Conflict("交接单创建发生竞争，请重试",
                             detail={"run_id": run["run_id"]})
    return {"handoff": existing, "reused": True}


def ensure_for_run(run: dict, moment: datetime | None = None) -> dict:
    """幂等取用：同一批次永远只有一张交接单。"""
    existing = store.find_handoff_by_run(run["run_id"])
    if existing is not None:
        return existing
    handoff = _build_handoff(run, moment or _now())
    store.insert_handoff(handoff)
    return store.get_handoff(handoff["handoff_id"]) or handoff


def list_all(limit: int = 30) -> list[dict]:
    recover()
    return [detail(handoff) for handoff in store.list_handoffs(limit)]


def get_detail(handoff_id: str) -> dict:
    recover(handoff_id)
    return detail(_load_or_404(handoff_id))


# ---------- 写账：日志 + 比较交换 ----------

def _commit(handoff_id: str, before: dict, after: dict, op: str,
            journal_key: str, moment: datetime) -> bool:
    """返回 True 表示已落地；False 表示版本被别人抢先。"""
    store.journal_begin(journal_key, handoff_id, op, before, after, _iso(moment))
    landed = store.cas_handoff(handoff_id, int(before["revision"]), after)
    store.journal_finish(journal_key, "applied" if landed else "aborted",
                         _iso(moment), None if landed else "revision changed")
    return landed


def _journal_key(handoff_id: str, op: str, task_id: str | None) -> str:
    """每次尝试一行日志：恢复只能依赖行内的 before / after 比对，
    所以同一次逻辑动作重试出的多行日志要各自独立，不能互相覆盖。"""
    seed = "|".join([handoff_id, op, task_id or "", _new_id("attempt")])
    return "hj-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _mutate(handoff_id: str, op: str, key: str, task_id: str | None, mutate,
            expected_revision=None, allow_retry: bool = False) -> tuple[dict, bool]:
    """统一入口：幂等键优先 → 版本校验 → 变更 → 写账。

    返回 (交接单, 是否命中幂等)。幂等命中时不校验版本：重试方拿到的可能正是
    自己第一次已经推进过的版本，这不应算旧版本冲突。
    """
    for _ in range(CAS_RETRIES):
        before = _load_or_404(handoff_id)
        entry = _idem_entry(before, key, op, task_id)
        if entry is not None:
            return before, True
        if expected_revision is not None:
            _check_revision(before, expected_revision)
        after = mutate(copy.deepcopy(before))
        journal_key = _journal_key(handoff_id, op, task_id)
        if _commit(handoff_id, before, after, op, journal_key, _now()):
            return after, False
        if expected_revision is not None or not allow_retry:
            current = store.get_handoff(handoff_id) or before
            raise _stale(expected_revision, int(current["revision"]))
    current = _load_or_404(handoff_id)
    if _idem_entry(current, key, op, task_id) is not None:
        return current, True
    raise _stale(expected_revision, int(current["revision"]))


# ---------- 领取 ----------

def _ttl_seconds(value) -> int:
    if value is None:
        return config.HANDOFF_CREDENTIAL_TTL
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        raise errors.BadInput("ttl_seconds 必须是整数", detail={"value": repr(value)})
    if not (config.HANDOFF_CREDENTIAL_TTL_MIN <= ttl
            <= config.HANDOFF_CREDENTIAL_TTL_MAX):
        raise errors.BadInput(
            "ttl_seconds 超出范围",
            detail={"min": config.HANDOFF_CREDENTIAL_TTL_MIN,
                    "max": config.HANDOFF_CREDENTIAL_TTL_MAX, "value": ttl},
        )
    return ttl


def _issue_credential(handoff: dict, task: dict, purpose: str, ttl: int,
                      moment: datetime, key: str) -> dict:
    manifest_sha = (task["compensation"]["manifest_sha256"]
                    if purpose == COMPENSATE else task["manifest_sha256"])
    token_seed = "|".join([
        handoff["handoff_id"], task["task_id"], purpose, str(manifest_sha),
        str(handoff["revision"]), key or _new_id("nonce"),
    ])
    return {
        "credential_id": _new_id("cr-"),
        "token": "hg-" + hashlib.sha256(token_seed.encode("utf-8")).hexdigest()[:32],
        "handoff_id": handoff["handoff_id"],
        "task_id": task["task_id"],
        "purpose": purpose,
        "manifest_sha256": manifest_sha,
        "task_manifest_sha256": task["manifest_sha256"],
        "issued_revision": handoff["revision"],
        "issued_at": _iso(moment),
        "expires_at": _iso(moment + timedelta(seconds=ttl)),
        "idempotency_key": key,
    }


def _credential_expired(credential: dict | None, moment: datetime) -> bool:
    if not credential or not credential.get("expires_at"):
        return False
    return _parse(credential["expires_at"]) <= moment


def _claim_result(task: dict, credential: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "consumer": task["consumer"],
        "status": task["status"],
        "credential": credential,
    }


def claim(handoff_id: str, task_id: str, expected_revision,
          purpose: str | None = None, ttl_seconds=None,
          idempotency_key=None) -> dict:
    key = _idem_key(idempotency_key)
    ttl = _ttl_seconds(ttl_seconds)
    seen = {"purpose": None}

    def mutate(handoff: dict) -> dict:
        # 幂等命中在 _mutate 里已经返回，走到这里的一定是新的一次领取。
        if expected_revision is None:
            raise errors.BadInput("必须带上 expected_revision",
                                 detail={"field": "expected_revision"})
        task = _task_of(handoff, task_id)
        wanted = purpose or (COMPENSATE if task["status"] == "compensation_pending"
                             else EXECUTE)
        seen["purpose"] = wanted
        if wanted not in (EXECUTE, COMPENSATE):
            raise errors.BadInput("purpose 只能是 execute 或 compensate",
                                 detail={"value": purpose})
        moment = _now()

        if wanted == EXECUTE:
            renewing = (task["status"] == "claimed"
                        and _credential_expired(task.get("credential"), moment))
            if task["status"] == "claimed" and not renewing:
                raise errors.HandoffConflict(
                    "任务已被领取",
                    detail={"task_id": task_id,
                            "credential_id": task["credential"]["credential_id"],
                            "reason": "ALREADY_CLAIMED"})
            # 旧凭据过期：只续签同一任务的凭据，不算重新派发；
            # 即使整单已撤回也允许，否则在途项永远等不到回执。
            if handoff.get("recall") is not None and not renewing:
                raise errors.HandoffClosed(
                    "整单已撤回，不再派发新的执行任务",
                    detail={"handoff_id": handoff_id, "task_id": task_id,
                            "reason": "HANDOFF_RECALLED",
                            "status": task["status"]},
                )
            if task["status"] not in ("pending", "claimed"):
                raise errors.HandoffConflict(
                    "任务状态不允许领取",
                    detail={"task_id": task_id, "status": task["status"],
                            "reason": "TASK_STATE"})
        else:
            if task["status"] == "compensation_claimed":
                if not _credential_expired(
                        (task.get("compensation") or {}).get("credential"), moment):
                    raise errors.HandoffConflict(
                        "补偿任务已被领取",
                        detail={"task_id": task_id, "reason": "ALREADY_CLAIMED"})
            elif task["status"] != "compensation_pending":
                raise errors.HandoffConflict(
                    "任务没有待补偿项",
                    detail={"task_id": task_id, "status": task["status"],
                            "reason": "TASK_STATE"})
            if not task.get("compensation"):
                raise errors.HandoffConflict(
                    "补偿任务缺失",
                    detail={"task_id": task_id,
                            "reason": "COMPENSATION_MISSING"})

        credential = _issue_credential(handoff, task, wanted, ttl, moment, key)
        if wanted == COMPENSATE:
            comp = task["compensation"]
            older = _credential_stub(comp.get("credential"))
            if older:
                comp.setdefault("credential_history", []).append(older)
            comp["credential"] = credential
        else:
            older = _credential_stub(task.get("credential"))
            if older:
                task.setdefault("credential_history", []).append(older)
            task["credential"] = credential
        task["status"] = "claimed" if wanted == EXECUTE else "compensation_claimed"
        task["updated_at"] = _iso(moment)
        if wanted == COMPENSATE:
            task["compensation"]["status"] = "claimed"
            task["compensation"]["updated_at"] = _iso(moment)
        _bump(handoff, moment)
        _refresh_status(handoff)
        _remember(handoff, key, "claim", task_id, _claim_result(task, credential))
        return handoff

    after, reused = _mutate(handoff_id, "claim", key, task_id, mutate,
                            expected_revision=expected_revision)
    if reused:
        result = after["idempotency"][key]["result"]
        return {**copy.deepcopy(result), "revision": after["revision"],
                "handoff_id": handoff_id, "reused": True}
    task = _task_of(after, task_id)
    return {**_claim_result(task, _current_credential(task, seen["purpose"])),
            "revision": after["revision"], "handoff_id": handoff_id,
            "reused": False}


# ---------- 回执 ----------

def _credential_purpose(credential, task: dict) -> str:
    if isinstance(credential, dict) and credential.get("purpose") in (
            EXECUTE, COMPENSATE):
        return credential["purpose"]
    if task["status"] in ("compensation_pending", "compensation_claimed"):
        return COMPENSATE
    return EXECUTE


def _current_credential(task: dict, purpose: str) -> dict | None:
    if purpose == COMPENSATE:
        return (task.get("compensation") or {}).get("credential")
    return task.get("credential")


def _token_sha256(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _credential_stub(credential: dict | None) -> dict | None:
    """历史凭据只留摘要，原件不外存也不回显。"""
    if not credential:
        return None
    return {
        "credential_id": credential.get("credential_id"),
        "purpose": credential.get("purpose"),
        "manifest_sha256": credential.get("manifest_sha256"),
        "task_manifest_sha256": credential.get("task_manifest_sha256"),
        "issued_at": credential.get("issued_at"),
        "expires_at": credential.get("expires_at"),
        "token_sha256": _token_sha256(credential.get("token")),
    }


def _held_credentials(task: dict, purpose: str) -> list[dict]:
    """按签发顺序列出该用途下所有签发过的凭据摘要。

    在途凭据可能被续签覆盖，已结算后原凭据也会从任务上摘下；回执要能识别
    每一种历史凭据并把过期 / 被覆盖的留作冲突，而不是当成从未签发过。
    """
    held: list[dict] = []
    if purpose == COMPENSATE:
        comp = task.get("compensation") or {}
        held.extend(stub for stub in (comp.get("credential_history") or []))
        if comp.get("credential"):
            held.append(comp["credential"])
        for item in comp.get("receipts") or []:
            held.append({
                "credential_id": item.get("credential_id"),
                "purpose": COMPENSATE,
                "manifest_sha256": comp.get("manifest_sha256"),
                "task_manifest_sha256": task.get("manifest_sha256"),
                "expires_at": item.get("credential_expires_at"),
                "token_sha256": item.get("credential_token_sha256"),
            })
        return held
    held.extend(stub for stub in (task.get("credential_history") or []))
    if task.get("credential"):
        held.append(task["credential"])
    for item in task.get("receipts") or []:
        if item.get("purpose") == EXECUTE:
            held.append({
                "credential_id": item.get("credential_id"),
                "purpose": EXECUTE,
                "manifest_sha256": task.get("manifest_sha256"),
                "task_manifest_sha256": task.get("manifest_sha256"),
                "expires_at": item.get("credential_expires_at"),
                "token_sha256": item.get("credential_token_sha256"),
            })
    return held


def _match_credential(task: dict, credential, purpose: str) -> dict | None:
    token_sha = _token_sha256(credential.get("token"))
    for held in _held_credentials(task, purpose):
        held_hash = held.get("token_sha256") or _token_sha256(held.get("token"))
        if held_hash == token_sha and \
                (not credential.get("credential_id")
                 or credential.get("credential_id") == held.get("credential_id")):
            return held
    return None


def _structural_credential_problem(credential, task: dict,
                                   purpose: str) -> tuple[dict | None, str | None, str]:
    """凭据本身是否可识别：缺失、token 对不上、用途不符。

    这几项与任务当前状态无关，必须先判；过期与被覆盖则要在状态判定之后
    才判，因为结算过的任务本来就没有当前凭据。
    """
    if not isinstance(credential, dict) or not credential.get("token"):
        return None, "CREDENTIAL_INVALID", "回执缺少凭据"
    held = _match_credential(task, credential, purpose)
    if held is None:
        return None, "CREDENTIAL_INVALID", "凭据与已签发记录不符"
    if credential.get("purpose") != purpose:
        return held, "CREDENTIAL_PURPOSE", "凭据用途不符"
    return held, None, ""


def _check_credential(task: dict, credential, purpose: str,
                      moment: datetime) -> tuple[dict | None, str | None, str]:
    """返回 (matched, 冲突码, 明细)；凭据可用时冲突码为空。"""
    held, code, detail = _structural_credential_problem(credential, task, purpose)
    if code is not None:
        return held, code, detail
    expected_manifest = (task["compensation"]["manifest_sha256"]
                         if purpose == COMPENSATE else task["manifest_sha256"])
    if credential.get("manifest_sha256") != expected_manifest:
        return held, "CREDENTIAL_MANIFEST_MISMATCH", "凭据绑定的清单内容已变化"
    current = _current_credential(task, purpose)
    if not current or current.get("credential_id") != held.get("credential_id"):
        return held, "CREDENTIAL_SUPERSEDED", "凭据已被新一次领取覆盖"
    expires_at = held.get("expires_at") or credential.get("expires_at")
    if not expires_at or _parse(expires_at) <= moment:
        return held, "CREDENTIAL_EXPIRED", "凭据已过期"
    return held, None, ""


def _new_compensation(handoff: dict, task: dict, moment: datetime) -> dict:
    recall_revision = int((handoff.get("recall") or {}).get("revision_after") or 0)
    manifest = spec.compensation_manifest(handoff, task)
    return {
        "compensation_id": spec.compensation_id_for(
            handoff["handoff_id"], task["task_id"], recall_revision),
        "status": "pending",
        "manifest_sha256": spec.digest(manifest),
        "manifest": manifest,
        "recall_revision": recall_revision,
        "created_at": _iso(moment),
        "updated_at": _iso(moment),
        "credential": None,
        "receipts": [],
    }


def _receipt_credential_fields(credential: dict) -> dict:
    return {
        "credential_id": credential.get("credential_id"),
        "credential_token_sha256": _token_sha256(credential.get("token")),
        "credential_expires_at": credential.get("expires_at"),
    }


def _record_execute_receipt(handoff: dict, task: dict, outcome: str,
                            result: dict | None, credential: dict,
                            moment: datetime, key: str) -> dict:
    during_recall = handoff.get("recall") is not None
    receipt = {
        "receipt_id": _new_id("rc-"),
        "task_id": task["task_id"],
        "purpose": EXECUTE,
        "outcome": outcome,
        "result": copy.deepcopy(result or {}),
        "recorded_at": _iso(moment),
        "idempotency_key": key,
        "during_recall": during_recall,
        **_receipt_credential_fields(credential),
    }
    task.setdefault("receipts", []).append(receipt)
    archived = _credential_stub(task.get("credential"))
    if archived:
        task.setdefault("credential_history", []).append(archived)
    task["credential"] = None
    task["recall_pending_receipt"] = False
    if outcome == "executed":
        if during_recall:
            task["status"] = "compensation_pending"
            task["compensation"] = _new_compensation(handoff, task, moment)
        else:
            task["status"] = "executed"
    else:
        task["status"] = "cancelled" if during_recall else "pending"
    task["updated_at"] = _iso(moment)
    return receipt


def _record_compensation_receipt(task: dict, outcome: str, result: dict | None,
                                 credential: dict, moment: datetime,
                                 key: str) -> dict:
    comp = task["compensation"]
    receipt = {
        "receipt_id": _new_id("rc-"),
        "task_id": task["task_id"],
        "purpose": COMPENSATE,
        "outcome": outcome,
        "result": copy.deepcopy(result or {}),
        "recorded_at": _iso(moment),
        "idempotency_key": key,
        "during_recall": True,
        **_receipt_credential_fields(credential),
    }
    comp.setdefault("receipts", []).append(receipt)
    archived = _credential_stub(comp.get("credential"))
    if archived:
        comp.setdefault("credential_history", []).append(archived)
    comp["credential"] = None
    comp["updated_at"] = _iso(moment)
    if outcome == "done":
        comp["status"] = "done"
        task["status"] = "completed"
    else:
        comp["status"] = "pending"
        task["status"] = "compensation_pending"
    task["updated_at"] = _iso(moment)
    return receipt


def receipt(handoff_id: str, task_id: str, credential, outcome: str,
            result: dict | None = None, idempotency_key=None) -> dict:
    key = _idem_key(idempotency_key)
    seen = {"purpose": None, "conflict": None}

    def mutate(handoff: dict) -> dict:
        task = _task_of(handoff, task_id)
        moment = _now()
        purpose = _credential_purpose(credential, task)
        seen["purpose"] = purpose
        allowed = COMPENSATE_OUTCOMES if purpose == COMPENSATE else EXECUTE_OUTCOMES
        if outcome not in allowed:
            raise errors.BadInput(
                "回执 outcome 不合法",
                detail={"purpose": purpose, "value": outcome,
                        "allowed": list(allowed)})

        # 凭据结构性错误（缺凭据 / 对不上 / 用途不符）先判，与状态无关；
        # 再看状态，最后才看清单、覆盖与过期。
        held, conflict_code, conflict_detail = _structural_credential_problem(
            credential, task, purpose)
        if purpose == EXECUTE:
            state_ok = task["status"] == "claimed"
            state_detail = "任务不在在途状态，回执不予记账"
        else:
            state_ok = task["status"] == "compensation_claimed"
            state_detail = "补偿任务不在在途状态，回执不予记账"

        if conflict_code is not None:
            code, detail = conflict_code, conflict_detail
        elif not state_ok:
            code, detail = "RECEIPT_STATE", state_detail
        else:
            held, code, detail = _check_credential(
                task, credential, purpose, moment)
        if code is not None:
            item = _record_conflict(handoff, task_id, code, detail,
                                    held or credential, key)
            seen["conflict"] = copy.deepcopy(item)
            _bump(handoff, moment)
            _refresh_status(handoff)
            _remember(handoff, key, "receipt", task_id, {
                "task_id": task_id, "purpose": purpose,
                "previous_status": task["status"], "status": task["status"],
                "outcome": outcome, "accepted": False,
                "conflict": copy.deepcopy(item),
            })
            return handoff

        previous = task["status"]
        if purpose == COMPENSATE:
            _record_compensation_receipt(task, outcome, result, held, moment, key)
        else:
            _record_execute_receipt(handoff, task, outcome, result, held,
                                    moment, key)
        _bump(handoff, moment)
        _refresh_status(handoff)
        _remember(handoff, key, "receipt", task_id, {
            "task_id": task_id, "purpose": purpose,
            "previous_status": previous, "status": task["status"],
            "outcome": outcome, "accepted": True, "conflict": None,
        })
        return handoff

    after, reused = _mutate(handoff_id, "receipt", key, task_id, mutate,
                            expected_revision=None, allow_retry=True)
    if reused:
        payload = copy.deepcopy(after["idempotency"][key]["result"])
        return {**payload, "rollback": rollback_state(after),
                "revision": after["revision"], "handoff_id": handoff_id,
                "reused": True}
    task = _task_of(after, task_id)
    return {
        "task_id": task_id,
        "purpose": seen["purpose"],
        "status": task["status"],
        "accepted": seen["conflict"] is None,
        "conflict": seen["conflict"],
        "rollback": rollback_state(after),
        "revision": after["revision"],
        "handoff_id": handoff_id,
        "reused": False,
    }


# ---------- 撤回 ----------

def recall(handoff_id: str, expected_revision, idempotency_key=None) -> dict:
    key = _idem_key(idempotency_key)

    def mutate(handoff: dict) -> dict:
        if handoff.get("recall") is not None:
            raise errors.HandoffConflict(
                "整单已撤回，不能重复撤回",
                detail={"handoff_id": handoff_id, "status": handoff["status"],
                        "reason": "ALREADY_RECALLED"})
        moment = _now()
        revision_before = int(handoff["revision"])
        revision_after = revision_before + 1
        handoff["recall"] = {
            "recalled_at": _iso(moment),
            "revision_before": revision_before,
            "revision_after": revision_after,
            "cancelled": [],
            "awaiting_receipt": [],
            "compensations": [],
        }
        for task in handoff["tasks"]:
            if task["status"] == "pending":
                task["status"] = "cancelled"
                task["cancelled_at"] = _iso(moment)
                task["cancel_reason"] = "recalled_before_claim"
                task["updated_at"] = _iso(moment)
                handoff["recall"]["cancelled"].append(task["task_id"])
            elif task["status"] == "claimed":
                task["recall_pending_receipt"] = True
                task["updated_at"] = _iso(moment)
                handoff["recall"]["awaiting_receipt"].append(task["task_id"])
            elif task["status"] == "executed":
                task["compensation"] = _new_compensation(handoff, task, moment)
                task["status"] = "compensation_pending"
                task["updated_at"] = _iso(moment)
                handoff["recall"]["compensations"].append(task["task_id"])
        _bump(handoff, moment)
        _refresh_status(handoff)
        summary = {
            "handoff_id": handoff_id,
            "status": handoff["status"],
            "recall": copy.deepcopy(handoff["recall"]),
            "rollback": rollback_state(handoff),
        }
        _remember(handoff, key, "recall", None, summary)
        return handoff

    after, reused = _mutate(handoff_id, "recall", key, None, mutate,
                            expected_revision=expected_revision)
    if reused:
        result = after["idempotency"][key]["result"]
        return {**copy.deepcopy(result), "revision": after["revision"],
                "reused": True}
    return {
        "handoff_id": handoff_id,
        "status": after["status"],
        "revision": after["revision"],
        "recall": copy.deepcopy(after.get("recall")),
        "rollback": rollback_state(after),
        "reused": False,
    }


# ---------- 中断恢复 ----------

def recover(handoff_id: str | None = None) -> list[dict]:
    """把写账中断留下的待办补完；返回处理过的日志条目摘要。

      applied   —— after 已经落地（中断发生在改库之后、记账之前）
      recovered —— 库还停在 before，按日志把意图补写一次
      diverged  —— 两边都不是，说明被别的写抢先，留作待办人工核对
    """
    outcomes: list[dict] = []
    for entry in store.open_journals(handoff_id):
        current = store.get_handoff(entry["handoff_id"])
        after = entry.get("after") or {}
        before = entry.get("before") or {}
        if current is None:
            store.journal_finish(entry["journal_id"], "orphaned",
                                 _iso(_now()), "handoff missing")
            outcomes.append({"journal_id": entry["journal_id"],
                             "outcome": "orphaned"})
            continue
        digest_now = spec.digest(current)
        if after and int(current["revision"]) == int(after["revision"]) \
                and digest_now == spec.digest(after):
            store.journal_finish(entry["journal_id"], "applied", _iso(_now()),
                                 "recovered: already landed")
            outcomes.append({"journal_id": entry["journal_id"],
                             "outcome": "applied"})
            continue
        if before and int(current["revision"]) == int(before["revision"]) \
                and digest_now == spec.digest(before):
            if store.cas_handoff(entry["handoff_id"], int(before["revision"]),
                                 after):
                store.journal_finish(entry["journal_id"], "applied",
                                     _iso(_now()),
                                     "recovered: completed interrupted write")
                outcomes.append({"journal_id": entry["journal_id"],
                                 "outcome": "recovered"})
            else:
                outcomes.append({"journal_id": entry["journal_id"],
                                 "outcome": "retry"})
            continue
        store.journal_finish(entry["journal_id"], "diverged", _iso(_now()),
                             "state moved past journaled revision")
        outcomes.append({"journal_id": entry["journal_id"],
                         "outcome": "diverged"})
    return outcomes


def open_todos(handoff_id: str | None = None) -> list[dict]:
    recover(handoff_id)
    return store.open_journals(handoff_id)
