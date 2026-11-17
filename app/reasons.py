"""原因码唯一来源。

每个隔离行、每条判定、每次拒绝都必须落到这里的码上，前端只做展示。
severity：info 只是说明，warn 需复核，break 直接判破坏。
"""

from __future__ import annotations

# code -> (中文标签, 级别)
CODES: dict[str, tuple[str, str]] = {
    # 采信
    "ENCODING_FALLBACK": ("编码回退命中", "info"),
    "BOM_STRIPPED": ("已去字节序标记", "info"),
    "LINE_MALFORMED": ("列定义不完整", "break"),
    "DUPLICATE_COLUMN": ("同表重复列名", "break"),
    "FIELD_ID_DERIVED": ("字段身份由表名列名派生", "warn"),
    "FIELD_ID_DUP": ("字段身份在同一版本出现两次", "break"),
    "UNKNOWN_TYPE": ("类型不在类型格内", "warn"),
    "EMPTY_INPUT": ("没有列定义", "warn"),
    "TABLE_UNPARSED": ("建表语句无法解析", "break"),
    "REQUIRED_TOKEN_UNKNOWN": ("必填记号无法识别", "warn"),
    # 类型格
    "TYPE_SAME": ("类型未变", "info"),
    "TYPE_WIDEN": ("类型放宽", "info"),
    "TYPE_NARROW": ("类型收窄", "break"),
    "TYPE_CROSS_FAMILY": ("跨族类型替换", "break"),
    "TYPE_UNKNOWN": ("类型不可比对", "break"),
    # 命名与必填
    "RENAME_ONLY": ("仅改名", "info"),
    "RENAME_CASE_ONLY": ("仅大小写改名", "info"),
    "RENAME_AND_RETYPE": ("同时改名与换类型", "break"),
    "REQUIRED_LOOSENED": ("必填改为可空", "info"),
    "REQUIRED_TIGHTENED": ("可空改为必填", "break"),
    "NAME_DUP_DIFF_ID": ("同名但身份不同", "break"),
    "ID_REUSED": ("身份被挪到别的表", "break"),
    "FIELD_ADDED": ("新增字段", "info"),
    "FIELD_DELETED": ("删除字段", "info"),
    "FIELD_DELETED_REFERENCED": ("删除仍被引用的字段", "break"),
    "MAPPING_CONFLICT": ("多个旧名映射到同一新名", "break"),
    "ALIAS_CYCLE": ("别名链成环", "break"),
    # 引用图
    "REF_BROKEN": ("引用的字段不存在", "break"),
    "REF_ONE_SIDED": ("上下游只登记了单向", "break"),
    "REF_CYCLE": ("下游依赖成环", "break"),
    "REF_SELF_LOOP": ("下游依赖指向自己", "warn"),
    # 版本与批次
    "VERSION_STALE": ("基于旧版本提交", "break"),
    "VERSION_OVERLAP": ("生效区间与已有版本重叠", "warn"),
    "VERSION_APPLIED": ("版本已登记", "info"),
    "RUN_REUSED": ("同一输入命中已算批次", "info"),
    "WINDOW_INVERTED": ("生效区间起点不早于终点", "break"),
    # 结论
    "COMPATIBLE": ("兼容", "info"),
    "COMPATIBLE_BY_MAPPING": ("加别名后兼容", "warn"),
    "BREAKING": ("破坏", "break"),
    # 执行交接
    "HANDOFF_STALE": ("交接版本过期", "break"),
    "HANDOFF_RECALLED": ("整单已撤回", "warn"),
    "ALREADY_CLAIMED": ("任务已被领取", "warn"),
    "ALREADY_RECALLED": ("整单已撤回过", "warn"),
    "TASK_STATE": ("任务状态不允许该动作", "warn"),
    "IDEMPOTENCY_CONFLICT": ("幂等键改作他用", "warn"),
    "CREDENTIAL_INVALID": ("凭据与签发记录不符", "warn"),
    "CREDENTIAL_PURPOSE": ("凭据用途不符", "warn"),
    "CREDENTIAL_MANIFEST_MISMATCH": ("凭据绑定清单已变化", "break"),
    "CREDENTIAL_SUPERSEDED": ("凭据已被重新领取覆盖", "warn"),
    "CREDENTIAL_EXPIRED": ("凭据已过期", "warn"),
    "RECEIPT_STATE": ("任务状态不接受回执", "warn"),
    "COMPENSATION_MISSING": ("补偿任务缺失", "break"),
}

# 类型收窄细分，挂在明细里，不进码表
NARROW_DETAILS = {
    "range_narrow": "取值范围变小",
    "int_digits_drop": "整数位变少",
    "scale_drop": "小数位变少",
    "length_narrow": "长度上限变小",
    "temporal_narrow": "时间精度回退",
    "precision_loss": "精度变粗",
    "exactness_loss": "精确表示范围变小",
}

SEVERITY_ORDER = {"info": 0, "warn": 1, "break": 2}


def label(code: str) -> str:
    return CODES.get(code, (code, "info"))[0]


def severity(code: str) -> str:
    return CODES.get(code, (code, "info"))[1]


def narrow_label(detail: str | None) -> str:
    if not detail:
        return "—"
    return NARROW_DETAILS.get(detail, detail)


def is_breaking(code: str) -> bool:
    return severity(code) == "break"


def known_codes() -> list[str]:
    return sorted(CODES)
