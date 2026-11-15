"""Parsing and validation of the field contract (JSON).

The contract is the single source of truth for the standard column set: target
names, accepted aliases, value types and required-ness.  The mapping engine
never invents columns or infers types from the ledger itself.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any

ALLOWED_TYPES = ("text", "integer", "decimal", "month")

TYPE_SYNONYMS = {
    "text": "text",
    "string": "text",
    "str": "text",
    "文本": "text",
    "字符串": "text",
    "integer": "integer",
    "int": "integer",
    "整数": "integer",
    "decimal": "decimal",
    "number": "decimal",
    "float": "decimal",
    "finite_decimal": "decimal",
    "小数": "decimal",
    "有限小数": "decimal",
    "month": "month",
    "month_str": "month",
    "ym": "month",
    "year_month": "month",
    "月份": "month",
    "年月": "month",
}

_TRUE_TOKENS = {"true", "yes", "y", "1", "是", "必须", "必填", "required", "on"}
_FALSE_TOKENS = {"false", "no", "n", "0", "否", "可选", "非必填", "optional", "off", ""}

_NAME_KEYS = ("name", "standard", "standard_name", "column", "target", "标准列", "列名")
_ALIAS_KEYS = ("aliases", "alias", "别名")
_TYPE_KEYS = ("type", "dtype", "data_type", "类型")
_REQUIRED_KEYS = ("required", "is_required", "必填", "必填性")
_FIELD_LIST_KEYS = ("fields", "columns", "standard_columns", "字段", "列")


class ContractError(ValueError):
    """The field contract is malformed or self-contradictory."""


def normalize_header(value: str) -> str:
    """Fold width, case and repeated whitespace; used for alias matching only."""
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


@dataclass(frozen=True)
class FieldSpec:
    name: str
    aliases: tuple[str, ...]
    type: str
    required: bool

    @property
    def accepted_headers(self) -> tuple[str, ...]:
        # The standard name itself is always an acceptable source header.
        seen: list[str] = []
        for candidate in (self.name, *self.aliases):
            norm = normalize_header(candidate)
            if norm and norm not in seen:
                seen.append(norm)
        return tuple(seen)


@dataclass(frozen=True)
class Contract:
    fields: tuple[FieldSpec, ...]

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _as_bool(value: Any, ctx: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    raise ContractError(f"{ctx}: required 必须为布尔值 true/false（或 是/否、1/0）")


def _clean_text(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{ctx}: 必须为非空字符串")
    return value.strip()


def _clean_type(value: Any, ctx: str) -> str:
    if isinstance(value, str):
        canonical = TYPE_SYNONYMS.get(value.strip().casefold())
        if canonical:
            return canonical
    raise ContractError(
        f"{ctx}: 类型 {value!r} 不受支持，仅限 text/integer/decimal/month"
        "（文本/整数/有限小数/月份）"
    )


def parse_contract(raw: bytes | str) -> Contract:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"字段合同不是合法的 UTF-8 JSON：{exc}") from exc

    if not isinstance(document, dict):
        raise ContractError("字段合同顶层必须是 JSON 对象")

    raw_fields = _pick(document, _FIELD_LIST_KEYS)
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ContractError(
            "字段合同缺少非空的 fields/columns 数组（标准列定义列表）"
        )

    fields: list[FieldSpec] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw_fields):
        ctx = f"fields[{index}]"
        if not isinstance(item, dict):
            raise ContractError(f"{ctx}: 必须是对象")

        raw_name = _pick(item, _NAME_KEYS)
        name = _clean_text(raw_name, f"{ctx}.name")
        norm_name = normalize_header(name)
        if norm_name in seen_names:
            raise ContractError(f"{ctx}: 标准列名 {name!r} 重复")
        seen_names.add(norm_name)

        field_type = _clean_type(_pick(item, _TYPE_KEYS), f"{ctx}.type")

        raw_aliases = _pick(item, _ALIAS_KEYS)
        if raw_aliases is None or raw_aliases == "":
            raw_aliases = []
        elif isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        if not isinstance(raw_aliases, list):
            raise ContractError(f"{ctx}.aliases: 必须是字符串数组")
        aliases: list[str] = []
        for a_index, alias in enumerate(raw_aliases):
            cleaned = _clean_text(alias, f"{ctx}.aliases[{a_index}]")
            norm = normalize_header(cleaned)
            if norm not in aliases:
                aliases.append(norm)

        required = _as_bool(_pick(item, _REQUIRED_KEYS), f"{ctx}.required")
        fields.append(
            FieldSpec(
                name=name,
                aliases=tuple(aliases),
                type=field_type,
                required=required,
            )
        )

    # Alias sets may legitimately overlap across fields in a sloppy contract;
    # that is not a hard contract error.  The mapping engine detects it and
    # reports the ambiguity instead of choosing a winner.
    return Contract(fields=tuple(fields))
