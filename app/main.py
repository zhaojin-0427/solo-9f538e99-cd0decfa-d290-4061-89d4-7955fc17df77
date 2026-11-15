"""FastAPI web entry point for the carbon-ledger standardizer."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .engine import Analysis, LedgerError, analyze
from .spec import Contract, ContractError, parse_contract

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB per file
RESULT_TTL_SECONDS = 30 * 60
MAX_RESULTS = 200

app = FastAPI(title="碳核算台账标准化", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@dataclass
class StoredResult:
    filename: str
    payload: bytes
    created_at: float


class ResultStore:
    """Tiny in-memory store for validated downloads (nothing touches disk)."""

    def __init__(self, ttl: float = RESULT_TTL_SECONDS, capacity: int = MAX_RESULTS):
        self._items: dict[str, StoredResult] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._capacity = capacity

    def put(self, filename: str, payload: bytes) -> str:
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            while len(self._items) >= self._capacity:
                oldest = min(self._items, key=lambda key: self._items[key].created_at)
                del self._items[oldest]
            self._items[token] = StoredResult(filename, payload, now)
        return token

    def get(self, token: str) -> StoredResult | None:
        with self._lock:
            self._prune(time.monotonic())
            return self._items.get(token)

    def _prune(self, now: float) -> None:
        expired = [
            key for key, item in self._items.items() if now - item.created_at > self._ttl
        ]
        for key in expired:
            del self._items[key]


store = ResultStore()


def _safe_stem(filename: str | None) -> str:
    """ASCII-only stem: the Content-Disposition header must be latin-1 encodable."""
    name = Path(filename or "ledger").name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    cleaned = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "_" for ch in stem
    )
    cleaned = cleaned.strip("_")[:80]
    return cleaned or "ledger"


def _render(
    request: Request,
    template: str,
    context: dict,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=context,
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _render(request, "index.html", {"request": request})


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.post("/standardize", response_class=HTMLResponse)
async def standardize(
    request: Request,
    contract_upload: UploadFile | None = File(default=None, alias="contract"),
    ledger_upload: UploadFile | None = File(default=None, alias="ledger"),
):
    upload_errors: list[str] = []

    contract_raw = b""
    if contract_upload is not None:
        contract_raw = await contract_upload.read(MAX_UPLOAD_BYTES + 1)
    ledger_raw = b""
    if ledger_upload is not None:
        ledger_raw = await ledger_upload.read(MAX_UPLOAD_BYTES + 1)

    if len(contract_raw) > MAX_UPLOAD_BYTES:
        upload_errors.append("字段合同文件超过 10 MiB 上限")
    elif not contract_raw:
        upload_errors.append("请上传字段合同 JSON 文件")
    if len(ledger_raw) > MAX_UPLOAD_BYTES:
        upload_errors.append("台账文件超过 10 MiB 上限")
    elif not ledger_raw:
        upload_errors.append("请上传台账 CSV 文件")

    contract: Contract | None = None
    if not upload_errors:
        try:
            contract = parse_contract(contract_raw)
        except ContractError as exc:
            upload_errors.append(str(exc))

    analysis: Analysis | None = None
    if not upload_errors and contract is not None:
        try:
            analysis = analyze(contract, ledger_raw)
        except LedgerError as exc:
            upload_errors.append(str(exc))

    download_url = None
    if analysis is not None and analysis.valid and analysis.output_csv is not None:
        token = store.put(
            f"{_safe_stem(ledger_upload.filename if ledger_upload else None)}"
            "_standardized.csv",
            analysis.output_csv,
        )
        download_url = f"/download/{token}"

    status = 200 if analysis is not None and analysis.valid else 422
    return _render(
        request,
        "result.html",
        {
            "request": request,
            "upload_errors": upload_errors,
            "analysis": analysis,
            "contract": contract,
            "download_url": download_url,
            "contract_name": (contract_upload.filename if contract_upload else None)
            or "—",
            "ledger_name": (ledger_upload.filename if ledger_upload else None) or "—",
        },
        status_code=status,
    )


@app.get("/download/{token}")
async def download(token: str):
    item = store.get(token)
    if item is None:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<p>下载链接已过期或不存在，请重新上传并校验。</p>",
            status_code=404,
        )
    return Response(
        content=item.payload,
        media_type="text/csv; charset=utf-8",
        headers={
            # Filename is restricted to [A-Za-z0-9_-] plus a fixed suffix.
            "Content-Disposition": f'attachment; filename="{item.filename}"',
            "Cache-Control": "no-store",
        },
    )
