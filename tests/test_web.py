import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

CONTRACT = {
    "fields": [
        {"name": "厂区", "aliases": ["工厂", "site"], "type": "text", "required": True},
        {"name": "月份", "aliases": ["统计月份", "period"], "type": "month", "required": True},
        {"name": "用电量(kWh)", "aliases": ["电量", "kwh"], "type": "decimal", "required": True},
        {"name": "抄表次数", "aliases": ["次数"], "type": "integer", "required": False},
    ]
}

GOOD_CSV = (
    "site,period,kwh,次数,备注列\n"
    "华东厂,2025-01,12.5,3,忽略\n"
    "华南厂,2025-02,8.25,,\n"
)

BAD_CSV = (
    "site,period,kwh,次数\n"
    "华东厂,2025-01,12.5,3\n"
    "华南厂,2025-02,八,\n"
)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def upload(client, csv_text, contract=CONTRACT, contract_bytes=None, ledger_bytes=None):
    if contract_bytes is None:
        contract_bytes = json.dumps(contract, ensure_ascii=False).encode("utf-8")
    if ledger_bytes is None:
        ledger_bytes = csv_text.encode("utf-8")
    return client.post(
        "/standardize",
        files={
            "contract": ("contract.json", contract_bytes, "application/json"),
            "ledger": ("ledger.csv", ledger_bytes, "text/csv"),
        },
    )


def test_index_renders_form(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "字段合同" in response.text
    assert 'enctype="multipart/form-data"' in response.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_valid_upload_shows_mapping_and_enables_download(client):
    response = upload(client, GOOD_CSV)
    assert response.status_code == 200
    assert "校验通过" in response.text
    assert "下载标准列 CSV" in response.text
    assert "已忽略的额外列" in response.text

    href = response.text.split('href="/download/', 1)[1].split('"', 1)[0]
    download = client.get(f"/download/{href}")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]
    assert download.content.decode("utf-8-sig").splitlines()[0] == "厂区,月份,用电量(kWh),抄表次数"
    assert download.content.decode("utf-8-sig").splitlines()[1] == "华东厂,2025-01,12.5,3"


def test_invalid_upload_lists_errors_and_offers_no_download(client):
    response = upload(client, BAD_CSV)
    assert response.status_code == 422
    assert "校验未通过" in response.text
    assert "未生成任何结果文件" in response.text
    assert "/download/" not in response.text
    assert "类型不符" in response.text
    assert "<td>3</td>" in response.text


def test_ambiguous_mapping_is_reported_without_download(client):
    contract = {
        "fields": [
            {"name": "甲", "aliases": ["x", "y"], "type": "text", "required": True},
            {"name": "乙", "aliases": ["x", "y"], "type": "text", "required": True},
        ]
    }
    response = upload(client, "x,y\nA,B\n", contract=contract)
    assert response.status_code == 422
    assert "映射歧义" in response.text
    assert "候选完整映射" in response.text
    assert "/download/" not in response.text


def test_missing_required_column(client):
    response = upload(client, "site,period\n华东厂,2025-01\n")
    assert response.status_code == 422
    assert "缺失的必填列" in response.text


def test_malformed_contract_json_is_reported(client):
    response = upload(client, GOOD_CSV, contract_bytes=b"{oops")
    assert response.status_code == 422
    assert "无法处理上传文件" in response.text


def test_missing_files_are_reported(client):
    response = client.post(
        "/standardize",
        files={"contract": ("contract.json", b" ", "application/json")},
    )
    assert response.status_code == 422
    assert "请上传台账 CSV 文件" in response.text


def test_expired_or_unknown_token(client):
    response = client.get("/download/not-a-real-token")
    assert response.status_code == 404
    assert "过期" in response.text


def test_upload_size_limit(client):
    big = b"site,period,kwh\n" + b"x,2025-01,1\n" * (11 * 1024 * 1024 // 12)
    response = upload(client, "", ledger_bytes=big)
    assert response.status_code == 422
    assert "上限" in response.text


def test_ledger_with_bom(client):
    response = upload(client, "", ledger_bytes=GOOD_CSV.encode("utf-8-sig"))
    assert response.status_code == 200


def test_html_escaping_of_supplied_headers(client):
    csv_text = "site,period,kwh,<script>alert(1)</script>\n华东厂,2025-01,1,x\n"
    response = upload(client, csv_text)
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_non_ascii_upload_name_still_downloads(client):
    contract_bytes = json.dumps(CONTRACT, ensure_ascii=False).encode("utf-8")
    response = client.post(
        "/standardize",
        files={
            "contract": ("字段合同.json", contract_bytes, "application/json"),
            "ledger": ("华东厂台账.csv", GOOD_CSV.encode("utf-8"), "text/csv"),
        },
    )
    assert response.status_code == 200
    href = response.text.split('href="/download/', 1)[1].split('"', 1)[0]
    download = client.get(f"/download/{href}")
    assert download.status_code == 200
    disposition = download.headers["content-disposition"]
    disposition.encode("latin-1")  # must be a legal HTTP header value
    assert disposition.endswith('_standardized.csv"')


def test_multiline_quoted_cell_reports_physical_line(client):
    csv_text = 'site,note,period,kwh\n"华东厂","第一行\n第二行",2025-01,1\n"华南厂","ok",2025-02,bad\n'
    response = upload(client, csv_text)
    assert response.status_code == 422
    assert "<td>4</td>" in response.text
