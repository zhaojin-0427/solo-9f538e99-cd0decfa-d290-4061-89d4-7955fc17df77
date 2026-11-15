import json

import pytest

from app.engine import analyze
from app.spec import ContractError, parse_contract

CONTRACT = {
    "fields": [
        {"name": "厂区", "aliases": ["工厂", "site"], "type": "text", "required": True},
        {"name": "月份", "aliases": ["统计月份", "period"], "type": "month", "required": True},
        {"name": "用电量(kWh)", "aliases": ["电量", "kwh"], "type": "decimal", "required": True},
        {"name": "抄表次数", "aliases": ["次数"], "type": "integer", "required": False},
        {"name": "备注", "aliases": ["说明"], "type": "text", "required": False},
    ]
}


def contract(data=None):
    return parse_contract(json.dumps(data or CONTRACT))


def run(csv_text, data=None):
    return analyze(contract(data), csv_text.encode("utf-8"))


def test_happy_path_maps_aliases_and_reorders():
    result = run(
        "说明,site,业绩快照,period,电量,次数\n"
        "一班,华东厂,忽略我,2025-01,12.5,3\n"
        "二班,华南厂,忽略我,2025-02,8,\n"
    )
    assert result.valid, result.problems
    assert [a.field.name for a in result.mapping] == [
        "厂区", "月份", "用电量(kWh)", "抄表次数", "备注",
    ]
    lines = result.output_csv.decode("utf-8-sig").splitlines()
    assert lines[0] == "厂区,月份,用电量(kWh),抄表次数,备注"
    assert lines[1] == "华东厂,2025-01,12.5,3,一班"
    assert lines[2] == "华南厂,2025-02,8,,二班"
    # Unmatched extra column is ignored, not mapped.
    assert any("业绩快照" in c for c in result.ignored_columns)


def test_header_matching_is_case_and_width_insensitive():
    result = run("ＳＩＴＥ,ＰＥＲＩＯＤ,KWH\n华东厂,2025-03,4\n")
    assert result.valid, result.problems
    assert result.output_csv.decode("utf-8-sig").splitlines()[1] == "华东厂,2025-03,4,,"


def test_missing_required_column_rejected():
    result = run("site,period\n华东厂,2025-01\n")
    assert not result.valid
    assert result.output_csv is None
    assert {f.name for f in result.missing_fields} == {"用电量(kWh)"}
    assert any("用电量(kWh)" in p for p in result.problems)


def test_two_source_columns_for_one_field_is_rejected():
    result = run("site,period,kwh,电量\n华东厂,2025-01,1,1\n")
    assert not result.valid
    assert result.output_csv is None
    assert any("电量" in p and "kwh" in p for p in result.problems)


def test_one_header_shared_by_two_optional_fields_is_ambiguous():
    data = {
        "fields": [
            {"name": "厂区", "aliases": [], "type": "text", "required": True},
            {"name": "备注", "aliases": ["厂区"], "type": "text", "required": False},
        ]
    }
    # Required coverage forces the single source column onto 厂区, so exactly
    # one complete mapping exists and it is accepted without guessing.
    result = run("厂区\n华东厂\n", data)
    assert result.valid, result.problems
    assert [a.field.name for a in result.mapping] == ["厂区"]


def test_one_header_shared_by_two_optional_fields_without_required_peer():
    data = {
        "fields": [
            {"name": "甲", "aliases": ["共同"], "type": "text", "required": False},
            {"name": "乙", "aliases": ["共同"], "type": "text", "required": False},
        ]
    }
    result = run("共同\n值\n", data)
    assert not result.valid
    assert result.output_csv is None
    assert any("同时匹配" in p for p in result.ambiguities)


def test_ambiguous_complete_mappings_are_listed_not_guessed():
    data = {
        "fields": [
            {"name": "甲", "aliases": ["x"], "type": "text", "required": True},
            {"name": "乙", "aliases": ["y"], "type": "text", "required": True},
        ]
    }
    # Both source columns accept both fields; two complete solutions exist.
    data["fields"][0]["aliases"] = ["x", "y"]
    data["fields"][1]["aliases"] = ["x", "y"]
    result = run("x,y\nA,B\n", data)
    assert not result.valid
    assert result.output_csv is None
    assert result.ambiguities
    assert len(result.alternatives) == 2
    pairs = {
        tuple((a.field.name, a.source_header) for a in alt)
        for alt in result.alternatives
    }
    assert pairs == {
        (("甲", "x"), ("乙", "y")),
        (("甲", "y"), ("乙", "x")),
    }


def test_column_order_is_never_used_to_break_ties():
    # Same ambiguity, headers physically reordered: still ambiguous, and the
    # alternative set is symmetric rather than "first column wins".
    data = {
        "fields": [
            {"name": "甲", "aliases": ["x", "y"], "type": "text", "required": True},
            {"name": "乙", "aliases": ["x", "y"], "type": "text", "required": True},
        ]
    }
    a = run("y,x\nB,A\n", data)
    b = run("x,y\nA,B\n", data)
    assert not a.valid and not b.valid
    assert len(a.alternatives) == len(b.alternatives) == 2


def test_duplicate_headers_rejected():
    result = run("site,site,period,kwh\n华东厂,华东厂,2025-01,1\n")
    assert not result.valid
    assert result.output_csv is None
    assert any("重复表头" in p for p in result.header_issues)


def test_duplicate_headers_rejected_even_when_unrelated():
    result = run("site,period,kwh,extra,extra\n华东厂,2025-01,1,1,2\n")
    assert not result.valid
    assert any("重复表头" in p for p in result.header_issues)


def test_empty_required_cell_rejects_whole_file():
    result = run(
        "site,period,kwh\n"
        "华东厂,2025-01,1\n"
        "华南厂,2025-02,\n"
        "华北厂,2025-03,3\n"
    )
    assert not result.valid
    assert result.output_csv is None
    assert len(result.row_errors) == 1
    error = result.row_errors[0]
    assert (error.row, error.field) == (3, "用电量(kWh)")
    assert "必填" in error.message


def test_empty_optional_cell_allowed():
    result = run("site,period,kwh,备注\n华东厂,2025-01,1,\n")
    assert result.valid, result.problems


@pytest.mark.parametrize(
    "field_csv,expected_field",
    [
        ("site,period,kwh,次数\n华东厂,2025-01,1,1.5\n", "抄表次数"),
        ("site,period,kwh\n华东厂,2025-01,十二\n", "用电量(kWh)"),
        ("site,period,kwh\n华东厂,2025-01,NaN\n", "用电量(kWh)"),
        ("site,period,kwh\n华东厂,2025-01,inf\n", "用电量(kWh)"),
        ("site,period,kwh\n华东厂,2025-01,1/2\n", "用电量(kWh)"),
        ("site,period,kwh\n华东厂,2025-13,1\n", "月份"),
    ],
)
def test_type_mismatch_rejects_whole_file(field_csv, expected_field):
    result = run(field_csv)
    assert not result.valid
    assert result.output_csv is None
    assert any(e.field == expected_field for e in result.row_errors)


def test_invalid_upload_generates_no_token_worthy_output():
    result = run("site,period,kwh\n华东厂,2025-01,x\n")
    assert not result.valid and result.output_csv is None


@pytest.mark.parametrize(
    "value,ok",
    [
        ("0", True),
        ("-12", True),
        ("+5", True),
        ("12.5", False),
        ("1e3", False),
        (" 7 ", True),
    ],
)
def test_integer_grammar(value, ok):
    result = run(f"site,period,kwh,次数\n华东厂,2025-01,1,{value}\n")
    assert result.valid is ok


@pytest.mark.parametrize(
    "value,ok",
    [
        ("0", True),
        ("-0.5", True),
        (".5", True),
        ("5.", False),
        ("1e3", False),
        ("NaN", False),
        ("inf", False),
        ("1,000", False),
    ],
)
def test_decimal_grammar(value, ok):
    result = run(f"site,period,kwh\n华东厂,2025-01,{value}\n")
    assert result.valid is ok


@pytest.mark.parametrize("value", ["2025-01", "2025-12", "1999-06"])
def test_month_valid(value):
    assert run(f"site,period,kwh\n华东厂,{value},1\n").valid


@pytest.mark.parametrize(
    "value", ["2025-00", "2025-13", "2025-1", "25-01", "2025/01", "2025-01-01"]
)
def test_month_invalid(value):
    assert not run(f"site,period,kwh\n华东厂,{value},1\n").valid


def test_ragged_row_rejected():
    result = run("site,period,kwh\n华东厂,2025-01,1,extra\n")
    assert not result.valid
    assert any("列数不符" in e.message for e in result.row_errors)


def test_header_only_ledger_is_accepted_with_zero_rows():
    result = run("site,period,kwh\n")
    assert result.valid, result.problems
    assert result.row_count == 0
    assert result.output_csv.decode("utf-8-sig").splitlines() == ["厂区,月份,用电量(kWh),抄表次数,备注"]


def test_no_matching_columns_is_rejected():
    result = run("a,b,c\n1,2,3\n")
    assert not result.valid
    assert result.missing_fields
    assert result.output_csv is None


def test_bom_is_tolerated():
    raw = "﻿site,period,kwh\n华东厂,2025-01,1\n".encode("utf-8")
    result = analyze(contract(), raw)
    assert result.valid, result.problems


def test_non_utf8_ledger_is_reported():
    from app.engine import LedgerError

    with pytest.raises(LedgerError):
        analyze(contract(), "site,period,kwh\n华东厂,2025-01,1\n".encode("gbk"))


def test_contract_rejects_unknown_type():
    with pytest.raises(ContractError):
        contract({"fields": [{"name": "a", "type": "date", "required": True}]})


def test_contract_rejects_duplicate_standard_names():
    with pytest.raises(ContractError):
        contract(
            {
                "fields": [
                    {"name": "a", "type": "text", "required": True},
                    {"name": "A", "type": "text", "required": True},
                ]
            }
        )


def test_contract_rejects_bad_json_and_empty_fields():
    with pytest.raises(ContractError):
        parse_contract(b"{not json")
    with pytest.raises(ContractError):
        parse_contract(b'{"fields": []}')


def test_alias_case_duplicates_within_one_field_are_collapsed():
    data = {
        "fields": [
            {"name": "kwh", "aliases": ["KWH", " kwh "], "type": "decimal", "required": True}
        ]
    }
    parsed = contract(data)
    assert parsed.fields[0].accepted_headers == ("kwh",)


def test_all_empty_data_row_is_rejected_not_treated_as_blank():
    result = run("site,period,kwh\n,,\n")
    assert not result.valid
    assert result.output_csv is None
    assert {e.field for e in result.row_errors} == {"厂区", "月份", "用电量(kWh)"}
    assert all(e.row == 2 for e in result.row_errors)


def test_truly_blank_lines_are_ignored():
    result = run("site,period,kwh\n\n华东厂,2025-01,1\n\n")
    assert result.valid, result.problems
    assert result.row_count == 1


def test_optional_only_contract_with_no_matching_headers_is_rejected():
    data = {"fields": [{"name": "备注", "aliases": ["note"], "type": "text", "required": False}]}
    result = run("a,b\n1,2\n", data)
    assert not result.valid
    assert result.output_csv is None
    assert any("没有任何列命中" in p for p in result.problems)


def test_required_cell_with_whitespace_only_is_empty():
    result = run('site,period,kwh\n"华东厂","2025-01","   "\n')
    assert not result.valid
    assert result.row_errors[0].field == "用电量(kWh)"
