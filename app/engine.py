"""Field mapping and ledger validation.

Rules enforced here (all-or-nothing, no partial output):

* Every CSV header that matches any alias (or a standard name) must be mapped
  one-to-one onto exactly one standard column.
* Every required standard column must have a source column.
* When more than one complete mapping exists, the ledger is rejected as
  ambiguous.  No column order, vote or sample-value heuristic is ever used.
* Duplicate headers, empty mandatory cells and any data-row type mismatch
  reject the whole file.
* Source columns matching no alias are ignored.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .spec import Contract, FieldSpec, normalize_header

INTEGER_RE = re.compile(r"[+-]?[0-9]+")
DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")

MAX_ALTERNATIVES = 20
MAX_SEARCH_NODES = 200_000


class LedgerError(ValueError):
    """The ledger file cannot be parsed at all (encoding/CSV syntax)."""


@dataclass
class RowError:
    row: int
    column: str
    field: str
    value: str
    message: str


@dataclass
class Assignment:
    field: FieldSpec
    source_header: str
    source_index: int  # 0-based position in the CSV header row


@dataclass
class Analysis:
    contract: Contract
    valid: bool = False
    headers: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    mapping: list[Assignment] = field(default_factory=list)
    missing_fields: list[FieldSpec] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    alternatives: list[list[Assignment]] = field(default_factory=list)
    header_issues: list[str] = field(default_factory=list)
    mapping_errors: list[str] = field(default_factory=list)
    row_errors: list[RowError] = field(default_factory=list)
    row_count: int = 0
    output_csv: bytes | None = None

    @property
    def problems(self) -> list[str]:
        """Flat list of every non-row problem that makes the upload unusable."""
        items = [*self.header_issues, *self.mapping_errors, *self.ambiguities]
        items.extend(
            f"必填标准列 {f.name!r} 没有来源列" for f in self.missing_fields
        )
        return items

    @property
    def error_count(self) -> int:
        count = len(self.problems) + len(self.row_errors)
        # Never report zero problems for a rejected upload.
        return count or (0 if self.valid else 1)

    @property
    def unmapped_optional(self) -> list[FieldSpec]:
        mapped = {a.field.name for a in self.mapping}
        return [f for f in self.contract.fields if f.name not in mapped]


def decode_csv(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LedgerError(
            "台账不是 UTF-8 编码（支持 UTF-8 与带 BOM 的 UTF-8）"
        ) from exc


def _raise_csv_field_limit() -> None:
    """Allow long cells (e.g. pasted remarks) in files up to the upload cap."""
    limit = 16 * 1024 * 1024
    if csv.field_size_limit() < limit:
        try:
            csv.field_size_limit(limit)
        except OverflowError:  # pragma: no cover - platform dependent
            pass


def _is_blank_line(row: list[str]) -> bool:
    """A physical line with no cells at all (csv yields []), or one whitespace cell.

    A delimiter-only row such as `,,""` is *not* blank: it is a data row whose
    required cells are empty and must therefore be rejected.
    """
    return len(row) == 0 or (len(row) == 1 and row[0].strip() == "")


@dataclass
class _Enumeration:
    solutions: list[dict[int, int]]
    truncated: bool
    hit_columns: list[int]


def _enumerate_mappings(
    candidates: list[list[int]],
    required: set[int],
    max_solutions: int = MAX_ALTERNATIVES,
) -> _Enumeration:
    """Return injective source-column -> field maps that cover every matched
    source column and every required field.

    Only columns with at least one candidate participate.  At most
    ``max_solutions`` solutions are kept; ``truncated`` says the search was cut
    short, which callers must treat as "cannot prove uniqueness".
    """
    hit_columns = [i for i, cand in enumerate(candidates) if cand]
    enumeration = _Enumeration(solutions=[], truncated=False, hit_columns=hit_columns)

    if not hit_columns:
        if not required:
            enumeration.solutions.append({})
        return enumeration

    order = sorted(hit_columns, key=lambda i: (len(candidates[i]), i))
    assignment: dict[int, int] = {}
    used: set[int] = set()
    nodes = 0

    # suffix_union[k] = union of candidates for order[k:], maintained
    # incrementally so feasibility checks are O(|fields|) instead of O(work).
    suffix_union: list[set[int]] = [set() for _ in range(len(order) + 1)]
    for position in range(len(order) - 1, -1, -1):
        suffix_union[position] = suffix_union[position + 1] | set(
            candidates[order[position]]
        )

    def feasible(index: int) -> bool:
        # Necessary-condition pruning (not full Hall's condition): the still
        # unmatched required fields must each have some remaining candidate.
        available_required = required - used
        return not available_required or available_required <= suffix_union[index]

    def backtrack(index: int) -> None:
        nonlocal nodes
        if enumeration.truncated:
            return
        nodes += 1
        if nodes > MAX_SEARCH_NODES or len(enumeration.solutions) >= max_solutions:
            enumeration.truncated = True
            return
        if index == len(order):
            if required <= used:
                enumeration.solutions.append(dict(assignment))
            return
        if not feasible(index):
            return
        col = order[index]
        for target in candidates[col]:
            if target in used:
                continue
            assignment[col] = target
            used.add(target)
            backtrack(index + 1)
            used.remove(target)
            del assignment[col]

    backtrack(0)
    return enumeration


def _assignments_for(
    contract: Contract, headers: list[str], solution: dict[int, int]
) -> list[Assignment]:
    result = [
        Assignment(
            field=contract.fields[field_index],
            source_header=headers[col],
            source_index=col,
        )
        for col, field_index in solution.items()
    ]
    result.sort(key=lambda a: contract.field_names.index(a.field.name))
    return result


def _validate_value(field_type: str, raw: str) -> str | None:
    """Return an error message, or None if the cell matches the type."""
    if field_type == "text":
        return None
    if field_type == "integer":
        if INTEGER_RE.fullmatch(raw):
            return None
        return f"类型不符：期望整数，实际 {raw!r}"
    if field_type == "decimal":
        if DECIMAL_RE.fullmatch(raw):
            return None
        return f"类型不符：期望有限小数，实际 {raw!r}"
    if field_type == "month":
        if MONTH_RE.fullmatch(raw):
            return None
        return f"类型不符：期望 YYYY-MM 月份，实际 {raw!r}"
    # parse_contract rejects unknown types, so this is unreachable.
    return f"未知类型 {field_type!r}"


def _display_header(header: str) -> str:
    return header.strip() if header.strip() else "（空表头）"


def _record_lines(text: str) -> list[tuple[list[str], int]]:
    """Parse CSV records together with the physical line each starts on.

    ``csv.reader.line_num`` counts physical lines, so a quoted cell containing
    newlines still yields the correct starting line for error reporting.
    """
    _raise_csv_field_limit()
    records: list[tuple[list[str], int]] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    previous = 0
    try:
        for row in reader:
            records.append((list(row), previous + 1))
            previous = reader.line_num
    except csv.Error as exc:
        raise LedgerError(f"CSV 解析失败：{exc}") from exc
    return records


def analyze(contract: Contract, raw_csv: bytes) -> Analysis:
    result = Analysis(contract=contract)
    records = _record_lines(decode_csv(raw_csv))
    if not records:
        result.header_issues.append("台账为空：缺少表头行")
        return result

    headers, _ = records[0]
    data_rows = records[1:]
    result.headers = headers

    # 1. Duplicate headers are always fatal, whether or not they hit an alias.
    seen: dict[str, list[int]] = {}
    for index, header in enumerate(headers):
        norm = normalize_header(header)
        if not norm:
            continue
        seen.setdefault(norm, []).append(index)
    for positions in seen.values():
        if len(positions) > 1:
            cols = "、".join(f"第 {p + 1} 列" for p in positions)
            result.header_issues.append(
                f"重复表头 {_display_header(headers[positions[0]])!r}（{cols}）"
            )

    # 2. Candidate fields for each source column.
    candidates: list[list[int]] = []
    for header in headers:
        norm = normalize_header(header)
        candidates.append(
            [i for i, f in enumerate(contract.fields) if norm and norm in f.accepted_headers]
        )
    result.ignored_columns = [
        f"第 {i + 1} 列 {_display_header(headers[i])}"
        for i, cand in enumerate(candidates)
        if not cand
    ]

    required = {i for i, f in enumerate(contract.fields) if f.required}
    enumeration = _enumerate_mappings(candidates, required)
    solutions = enumeration.solutions

    if not enumeration.hit_columns:
        result.mapping_errors.append(
            "台账没有任何列命中字段合同的别名，无法建立映射"
        )

    if not solutions:
        _diagnose_no_mapping(result, contract, headers, candidates, required)
        return result

    if enumeration.truncated or len(solutions) > 1:
        result.ambiguities = _describe_ambiguities(
            contract, headers, candidates, solutions, enumeration.truncated
        )
        result.alternatives = [
            _assignments_for(contract, headers, solution)
            for solution in solutions[:MAX_ALTERNATIVES]
        ]
        return result

    mapping = _assignments_for(contract, headers, solutions[0])
    result.mapping = mapping
    result.missing_fields = [
        f
        for f in contract.fields
        if f.required and not any(a.field.name == f.name for a in mapping)
    ]
    if result.missing_fields or result.header_issues or result.mapping_errors:
        return result

    # 3. Row-level validation.  Any single failure rejects the whole file.
    source_for = {a.field.name: a for a in mapping}
    row_errors: list[RowError] = []
    for row, line_number in data_rows:
        if _is_blank_line(row):
            continue  # blank physical line, not a data row
        result.row_count += 1
        if len(row) != len(headers):
            row_errors.append(
                RowError(
                    row=line_number,
                    column="—",
                    field="—",
                    value="",
                    message=f"列数不符：表头 {len(headers)} 列，该行 {len(row)} 列",
                )
            )
            continue
        for field_spec in contract.fields:
            assignment = source_for.get(field_spec.name)
            if assignment is None:
                continue  # optional standard column without a source
            value = row[assignment.source_index].strip()
            if not value:
                if field_spec.required:
                    row_errors.append(
                        RowError(
                            row=line_number,
                            column=_display_header(assignment.source_header),
                            field=field_spec.name,
                            value="",
                            message="必填单元格为空",
                        )
                    )
                continue
            message = _validate_value(field_spec.type, value)
            if message:
                row_errors.append(
                    RowError(
                        row=line_number,
                        column=_display_header(assignment.source_header),
                        field=field_spec.name,
                        value=value,
                        message=message,
                    )
                )

    result.row_errors = row_errors
    if row_errors:
        return result

    result.output_csv = _build_output(contract, mapping, data_rows)
    result.valid = True
    return result


def _diagnose_no_mapping(
    result: Analysis,
    contract: Contract,
    headers: list[str],
    candidates: list[list[int]],
    required: set[int],
) -> None:
    matched_targets = {i for cand in candidates for i in cand}
    for index in sorted(required - matched_targets):
        if contract.fields[index] not in result.missing_fields:
            result.missing_fields.append(contract.fields[index])

    by_field: dict[int, list[int]] = {}
    for col, cand in enumerate(candidates):
        for target in cand:
            by_field.setdefault(target, []).append(col)
    for target, cols in sorted(by_field.items()):
        if len(cols) > 1:
            names = "、".join(repr(_display_header(headers[c])) for c in cols)
            result.mapping_errors.append(
                f"源列 {names} 同时命中标准列 {contract.fields[target].name!r}，"
                "无法满足一一对应"
            )
    for col, cand in enumerate(candidates):
        if len(cand) > 1:
            names = "、".join(repr(contract.fields[t].name) for t in cand)
            result.mapping_errors.append(
                f"源列 {_display_header(headers[col])!r} 可对应多个标准列（{names}），"
                "无法确定"
            )
    if not result.mapping_errors and not result.missing_fields:
        result.mapping_errors.append("找不到满足约束的字段映射")


def _describe_ambiguities(
    contract: Contract,
    headers: list[str],
    candidates: list[list[int]],
    solutions: list[dict[int, int]],
    truncated: bool,
) -> list[str]:
    messages: list[str] = []
    if len(solutions) > 1:
        count = f"至少 {len(solutions)} 种" if truncated else f"{len(solutions)} 种"
        messages.append(
            f"存在 {count}完整映射，无法唯一确定；不做任何猜测，"
            "请调整别名或台账表头"
        )
    elif truncated:
        messages.append(
            "候选完整映射过多，无法在限定范围内证明唯一性；不做任何猜测，"
            "请收紧字段合同的别名"
        )
    for col, cand in enumerate(candidates):
        if len(cand) > 1:
            names = "、".join(repr(contract.fields[t].name) for t in cand)
            messages.append(
                f"源列 {_display_header(headers[col])!r} 同时匹配标准列 {names}"
            )
    target_columns: dict[int, list[int]] = {}
    for col, cand in enumerate(candidates):
        for target in cand:
            target_columns.setdefault(target, []).append(col)
    for target, cols in sorted(target_columns.items()):
        if len(cols) > 1:
            names = "、".join(repr(_display_header(headers[c])) for c in cols)
            messages.append(
                f"多个源列 {names} 均可映射到标准列 "
                f"{contract.fields[target].name!r}"
            )
    return messages


def _build_output(
    contract: Contract,
    mapping: list[Assignment],
    data_rows: list[tuple[list[str], int]],
) -> bytes:
    source_for = {a.field.name: a.source_index for a in mapping}
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(list(contract.field_names))
    for row, _ in data_rows:
        if _is_blank_line(row):
            continue
        writer.writerow(
            [
                row[source_for[name]].strip()
                if name in source_for
                else ""
                for name in contract.field_names
            ]
        )
    # UTF-8 BOM so Excel on Windows detects the encoding for Chinese headers.
    return buffer.getvalue().encode("utf-8-sig")
