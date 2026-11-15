"""FastAPI web entry point for the carbon-ledger standardizer."""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .engine import Analysis, LedgerError, analyze
from .spec import Contract, ContractError, parse_contract

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB per file
RESULT_TTL_SECONDS = 30 * 60
MAX_RESULTS = 200
RESULT_SWEEP_INTERVAL_SECONDS = 60.0
# Result payloads live on disk, never in the process heap: a month-end batch
# of multi-MiB ledgers must not grow memory with the total size of stored
# CSVs (the container is capped at 384 MiB).  Only tiny metadata records are
# kept in memory.  In the image this path is backed by a named volume (tmpfs
# would count against the memory limit and is too small anyway).
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR") or (BASE_DIR.parent / "var" / "results"))

app = FastAPI(title="碳核算台账标准化", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@dataclass
class StoredResult:
    """Metadata for one downloadable CSV; the payload itself stays on disk."""

    filename: str
    path: Path
    created_at: float
    size: int


class ResultStore:
    """Disk-backed store for validated downloads.

    Tokens map to small metadata records in memory; the CSV bytes are written
    to ``directory`` (atomically, via a temporary file plus rename) and
    streamed back from an already-open file handle.  This gives three
    guarantees:

    * process memory stays flat no matter how many results accumulate;
    * a download that has already started always runs to completion, even if
      the entry expires and is cleaned up mid-stream — unlinking the path
      does not disturb the open descriptor;
    * a save failure raises before any token exists, so no download link is
      ever produced for a result that was not persisted.

    Tokens are process-local, so files left by a previous run are wiped on
    startup and "restart invalidates links" still holds.
    """

    def __init__(
        self,
        directory: Path | str,
        ttl: float = RESULT_TTL_SECONDS,
        capacity: int = MAX_RESULTS,
        sweep_interval: float | None = RESULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._clear_stale_files()
        self._items: dict[str, StoredResult] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._capacity = capacity
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None
        if sweep_interval:
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                args=(sweep_interval,),
                name="result-store-sweeper",
                daemon=True,
            )
            self._sweeper.start()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def close(self) -> None:
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=2)
            self._sweeper = None

    def put(self, filename: str, payload: bytes) -> str:
        """Persist ``payload`` and return its download token.

        The token is registered only after the file is fully written to disk;
        any ``OSError`` (disk full, read-only volume, ...) propagates to the
        caller, which must then not offer a download link.
        """
        token = secrets.token_urlsafe(24)
        final = self._dir / f"{token}.csv"
        staging = self._dir / f".{token}.part"
        try:
            with open(staging, "wb") as handle:
                handle.write(payload)
            os.replace(staging, final)
        except OSError:
            staging.unlink(missing_ok=True)
            raise
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            while len(self._items) >= self._capacity:
                oldest = min(
                    self._items, key=lambda key: self._items[key].created_at
                )
                self._drop_locked(oldest)
            self._items[token] = StoredResult(
                filename=filename, path=final, created_at=now, size=len(payload)
            )
        return token

    def open_download(self, token: str) -> tuple[StoredResult, BinaryIO] | None:
        """Return metadata plus an open read handle, or None if unknown/expired.

        The expiry check and the ``open`` run under the same lock as the
        cleanup, so a handle is never handed out for an expired entry and an
        entry cannot vanish between the check and the ``open``.  Once the
        handle exists, expiry cleanup may unlink the path but the stream
        keeps reading the unlinked file to its end.
        """
        with self._lock:
            self._prune_locked(time.monotonic())
            item = self._items.get(token)
            if item is None:
                return None
            try:
                handle = open(item.path, "rb")
            except OSError:
                return None
            return item, handle

    def _sweep_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            with self._lock:
                self._prune_locked(time.monotonic())

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, item in self._items.items()
            if now - item.created_at > self._ttl
        ]
        for key in expired:
            self._drop_locked(key)

    def _drop_locked(self, token: str) -> None:
        item = self._items.pop(token, None)
        if item is None:
            return
        try:
            item.path.unlink(missing_ok=True)
        except OSError:
            # Startup wipes the directory, so a stray file is bounded; never
            # let a cleanup failure break requests.
            pass

    def _clear_stale_files(self) -> None:
        for child in self._dir.iterdir():
            if child.is_file():
                try:
                    child.unlink()
                except OSError:
                    pass


store = ResultStore(RESULTS_DIR)


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
    save_error: str | None = None
    if analysis is not None and analysis.valid and analysis.output_csv is not None:
        try:
            token = store.put(
                f"{_safe_stem(ledger_upload.filename if ledger_upload else None)}"
                "_standardized.csv",
                analysis.output_csv,
            )
        except OSError:
            # A result that could not be persisted must never yield a link.
            save_error = "结果文件保存失败（服务器存储不可用），未生成下载链接，请重试。"
        else:
            download_url = f"/download/{token}"

    if save_error is not None:
        status = 500
    elif analysis is not None and analysis.valid:
        status = 200
    else:
        status = 422
    return _render(
        request,
        "result.html",
        {
            "request": request,
            "upload_errors": upload_errors,
            "save_error": save_error,
            "analysis": analysis,
            "contract": contract,
            "download_url": download_url,
            "contract_name": (contract_upload.filename if contract_upload else None)
            or "—",
            "ledger_name": (ledger_upload.filename if ledger_upload else None) or "—",
        },
        status_code=status,
    )


def _stream_file(handle: BinaryIO, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """Yield the result in chunks so memory stays flat for any file size."""
    try:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


@app.get("/download/{token}")
async def download(token: str):
    opened = store.open_download(token)
    if opened is None:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<p>下载链接已过期或不存在，请重新上传并校验。</p>",
            status_code=404,
        )
    item, handle = opened
    return StreamingResponse(
        _stream_file(handle),
        media_type="text/csv; charset=utf-8",
        headers={
            # Filename is restricted to [A-Za-z0-9_-] plus a fixed suffix.
            "Content-Disposition": f'attachment; filename="{item.filename}"',
            "Cache-Control": "no-store",
            "Content-Length": str(item.size),
        },
    )
