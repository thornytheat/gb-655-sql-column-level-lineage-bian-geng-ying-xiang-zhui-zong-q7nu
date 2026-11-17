"""交接清单：把真实批次里的受影响下游折成逐任务规格与摘要。

只读取批次里已有的影响面、引用解析和下游登记，不改血缘判定，也不扩展别名链。
任务身份由交接单身份与下游节点名派生，同一批次重复生成得到同一批任务。
"""

from __future__ import annotations

import hashlib
import json


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def handoff_id_for(run_id: str) -> str:
    return "ho-" + hashlib.sha256(f"handoff|{run_id}".encode("utf-8")).hexdigest()[:12]


def task_id_for(handoff_id: str, consumer: str) -> str:
    return "ht-" + hashlib.sha256(
        f"{handoff_id}|{consumer}".encode("utf-8")
    ).hexdigest()[:12]


def compensation_id_for(handoff_id: str, task_id: str, recall_revision: int) -> str:
    seed = f"{handoff_id}|{task_id}|compensation|{recall_revision}"
    return "hc-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _ref_structure(run: dict, consumer: str) -> list[dict]:
    """该下游每个引用在批次里的实际解析结果，只回读，不重新判定。"""
    resolution = run.get("resolution") or {}
    resolved = {(item["consumer"], item["ref"]): item
                for item in (resolution.get("resolved") or [])}
    broken = {(item["consumer"], item["ref"]): item
              for item in (resolution.get("broken") or [])}
    out: list[dict] = []
    for item in run.get("refs", {}).get("consumers") or []:
        if item["name"] != consumer:
            continue
        for ref in item.get("refs") or []:
            hit = resolved.get((consumer, ref))
            if hit is not None:
                out.append({
                    "ref": ref,
                    "resolved": True,
                    "via": hit.get("via"),
                    "field_id": hit.get("field_id"),
                    "new_name": hit.get("new_name"),
                    "detail": hit.get("detail") or "",
                })
                continue
            miss = broken.get((consumer, ref)) or {}
            out.append({
                "ref": ref,
                "resolved": False,
                "via": miss.get("via") or "unknown_name",
                "field_id": None,
                "new_name": None,
                "detail": miss.get("detail") or "",
            })
    return out


def _consumer(run: dict, name: str) -> dict:
    for item in run.get("refs", {}).get("consumers") or []:
        if item["name"] == name:
            return item
    return {}


def task_spec(run: dict, handoff_id: str, row: dict) -> dict:
    """一条下游任务：影响面证据 + 下游登记与引用解析后的实际结构。"""
    name = row["name"]
    consumer = _consumer(run, name)
    structure = {
        "consumer": name,
        "kind": consumer.get("kind") or row.get("kind") or "task",
        "sql": consumer.get("sql") or "",
        "upstream": list(consumer.get("upstream") or []),
        "downstream": list(consumer.get("downstream") or []),
        "refs": _ref_structure(run, name),
        "depth": row.get("depth"),
        "via": row.get("via"),
        "evidence": list(row.get("fields") or []),
    }
    task_id = task_id_for(handoff_id, name)
    spec = {
        "task_id": task_id,
        "consumer": name,
        "kind": structure["kind"],
        "structure": structure,
    }
    spec["manifest_sha256"] = digest(spec)
    return spec


def build_manifest(run: dict, tasks: list[dict]) -> dict:
    """领取凭据绑定的清单：批次身份 + 逐任务规格，不含任何状态字段。"""
    return {
        "run_id": run["run_id"],
        "batch_key": run.get("batch_key"),
        "input_sha256": run.get("input_sha256"),
        "ruleset": run.get("ruleset"),
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
        "effective_from": run.get("effective_from"),
        "effective_to": run.get("effective_to"),
        "tasks": [
            {
                "task_id": task["task_id"],
                "consumer": task["consumer"],
                "kind": task["kind"],
                "manifest_sha256": task["manifest_sha256"],
                "structure": task["structure"],
            }
            for task in tasks
        ],
    }


def compensation_manifest(handoff: dict, task: dict) -> dict:
    """补偿任务挂靠原任务与撤回版本，凭据只能绑定这份清单。"""
    return {
        "handoff_id": handoff["handoff_id"],
        "run_id": handoff["run_id"],
        "task_id": task["task_id"],
        "consumer": task["consumer"],
        "mode": "compensation",
        "recall_revision": (handoff.get("recall") or {}).get("revision_after"),
        "task_manifest_sha256": task["manifest_sha256"],
        "handoff_manifest_sha256": handoff["manifest_sha256"],
    }
