"""M8 入口：执行交接页与交接接口。

页面只展示状态与数值；判定与状态推进都在 service 里。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import errors, store, web
from app.modules.m8_handoff import service, spec

router = APIRouter()

ALLOWED_CREATE = {"run_id"}
ALLOWED_CLAIM = {"expected_revision", "purpose", "ttl_seconds",
                 "idempotency_key"}
ALLOWED_RECEIPT = {"credential", "outcome", "result", "idempotency_key"}
ALLOWED_RECALL = {"expected_revision", "idempotency_key"}


def _reject_unknown(body: dict, allowed: set[str]) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})


@router.get("/handoff", response_class=HTMLResponse)
def handoff_page(request: Request):
    run = store.latest_run()
    items = service.list_all()
    current = items[0] if items else None
    if current is None and run is not None:
        current = service.get_detail(service.ensure_for_run(run)["handoff_id"])
    return web.page_or_404(
        request,
        "handoff.html",
        "handoff",
        run,
        handoffs=items,
        current=current,
    )


@router.post("/api/handoffs")
def create_handoff(payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, ALLOWED_CREATE)
    run_id = body.get("run_id")
    created = service.create(str(run_id) if run_id else None)
    return {
        "handoff_id": created["handoff"]["handoff_id"],
        "run_id": created["handoff"]["run_id"],
        "reused": created["reused"],
        "revision": created["handoff"]["revision"],
        "status": created["handoff"]["status"],
        "manifest_sha256": created["handoff"]["manifest_sha256"],
    }


@router.get("/api/handoffs")
def list_handoffs(limit: int = 30):
    if limit < 1 or limit > 200:
        raise errors.BadInput("limit 超出范围", detail={"value": limit})
    return {"handoffs": service.list_all(limit)}


@router.get("/api/handoffs/{handoff_id}")
def handoff_detail(handoff_id: str):
    return service.get_detail(handoff_id)


@router.get("/api/handoffs/{handoff_id}/manifest.json")
def handoff_manifest(handoff_id: str):
    handoff = service._load_or_404(handoff_id)
    manifest = handoff.get("manifest")
    if manifest is None:  # 早期记录没存清单时按任务重算，摘要仍由内容决定
        manifest = spec.build_manifest(handoff, handoff["tasks"])
    return {
        "handoff_id": handoff_id,
        "manifest_sha256": spec.digest(manifest),
        "manifest": manifest,
    }


@router.get("/api/handoffs/{handoff_id}/todos")
def handoff_todos(handoff_id: str):
    service._load_or_404(handoff_id)
    todos = service.open_todos(handoff_id)
    return {
        "handoff_id": handoff_id,
        "open": len(todos),
        "rows": [
            {"journal_id": item["journal_id"], "op": item["op"],
             "status": item["status"], "created_at": item["created_at"]}
            for item in todos
        ],
    }


@router.post("/api/handoffs/{handoff_id}/recall")
def recall_handoff(handoff_id: str, payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, ALLOWED_RECALL)
    return service.recall(handoff_id, body.get("expected_revision"),
                          idempotency_key=body.get("idempotency_key"))


@router.post("/api/handoffs/{handoff_id}/tasks/{task_id}/claim")
def claim_task(handoff_id: str, task_id: str, payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, ALLOWED_CLAIM)
    return service.claim(
        handoff_id,
        task_id,
        body.get("expected_revision"),
        purpose=body.get("purpose"),
        ttl_seconds=body.get("ttl_seconds"),
        idempotency_key=body.get("idempotency_key"),
    )


@router.post("/api/handoffs/{handoff_id}/tasks/{task_id}/receipt")
def task_receipt(handoff_id: str, task_id: str, payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, ALLOWED_RECEIPT)
    outcome = str(body.get("outcome") or "").strip()
    if not outcome:
        raise errors.BadInput("回执必须带 outcome", detail={"field": "outcome"})
    result = body.get("result")
    if result is not None and not isinstance(result, dict):
        raise errors.BadInput("result 必须是对象", detail={"field": "result"})
    return service.receipt(
        handoff_id,
        task_id,
        body.get("credential"),
        outcome,
        result=result,
        idempotency_key=body.get("idempotency_key"),
    )
