"""Single-process, disk-backed temporary batches. Run exactly one Uvicorn worker."""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))) / "batches"
CHUNK_BYTES = 8 * 1024**2
MAX_FILES = int(os.environ.get("MAX_FILES", "1000"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "200")) * 1024**2
MAX_BATCH_BYTES = int(os.environ.get("MAX_BATCH_MB", "5120")) * 1024**2
MAX_JOBS = int(os.environ.get("MAX_JOBS", "10"))
TTL = int(os.environ.get("RETENTION_SECONDS", "3600"))
MIN_FREE_BYTES = int(os.environ.get("MIN_FREE_MB", "1024")) * 1024**2
CONVERSION_TIMEOUT = int(os.environ.get("CONVERSION_TIMEOUT", "120"))
WORKERS = int(os.environ.get("CONVERSION_WORKERS", "2"))
log = logging.getLogger("uvicorn.error")


class InputFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=MAX_FILE_BYTES)


class NewBatch(BaseModel):
    files: list[InputFile] = Field(min_length=1, max_length=MAX_FILES)
    quality: int = Field(default=90, ge=1, le=100)


@dataclass
class Batch:
    id: str
    files: list[dict]
    quality: int
    state: str = "uploading"
    touched: float = field(default_factory=time.time)
    error: str | None = None
    task: asyncio.Task | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    downloads: int = 0

    @property
    def path(self):
        return DATA / self.id

    def snapshot(self):
        return {"id": self.id, "state": self.state, "files": self.files,
                "error": self.error, "quality": self.quality,
                "expiresAt": self.touched + TTL,
                "downloadUrl": f"/api/jobs/{self.id}/download" if self.state == "done" else None}


jobs: dict[str, Batch] = {}
converter = ""
slots: asyncio.Semaphore


def remove(batch):
    shutil.rmtree(batch.path, ignore_errors=True)
    jobs.pop(batch.id, None)


def clean_expired():
    for batch in list(jobs.values()):
        if (batch.state in {"uploading", "done", "failed"}
                and not batch.lock.locked() and not batch.downloads
                and time.time() - batch.touched > TTL):
            remove(batch)


async def janitor():
    while True:
        await asyncio.sleep(30)
        clean_expired()


@asynccontextmanager
async def lifespan(app):
    global converter, slots
    converter = os.environ.get("HEIF_CLI") or shutil.which("heif-dec") or shutil.which("heif-convert")
    if not converter:
        raise RuntimeError("Install libheif-examples (Linux) or brew install libheif (macOS).")
    if min(MAX_FILES, MAX_JOBS, TTL, WORKERS, CONVERSION_TIMEOUT) < 1:
        raise RuntimeError("Limits and worker count must be positive.")
    DATA.mkdir(parents=True, exist_ok=True)
    # Metadata is intentionally ephemeral; remove only our generated batch folders.
    for path in DATA.iterdir():
        if path.is_dir() and re.fullmatch(r"[0-9a-f]{48}", path.name):
            shutil.rmtree(path)
    jobs.clear()
    slots = asyncio.Semaphore(WORKERS)
    cleaner = asyncio.create_task(janitor())
    yield
    cleaner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cleaner
    tasks = []
    for batch in list(jobs.values()):
        batch.cancelled.set()
        if batch.task:
            tasks.append(batch.task)
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def headers(request, call_next):
    # Blocks cross-origin browser mutations, including multipart/form submissions.
    if request.method in {"POST", "PUT", "DELETE"}:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Use this app from its own website."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {f"https://{request.headers.get('host')}", f"http://{request.headers.get('host')}"}:
            return JSONResponse({"detail": "Origin does not match this website."}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


def get_batch(job_id):
    batch = jobs.get(job_id)
    if not batch:
        raise HTTPException(404, "This batch expired or the server restarted. Please upload again.")
    return batch


def ensure_space():
    if shutil.disk_usage(DATA).free < MIN_FREE_BYTES + CHUNK_BYTES:
        raise HTTPException(507, "The server is low on storage. Try again after older batches expire.")


def safe_stem(name):
    leaf = name.replace("\\", "/").split("/")[-1]
    stem = re.sub(r"[^\w .()-]", "_", Path(leaf).stem, flags=re.UNICODE).strip(" .")
    # Byte bound also accommodates filesystems with a 255-byte filename limit.
    return (stem.encode("utf-8")[:120].decode("utf-8", errors="ignore") or "photo")


@app.get("/healthz")
async def health():
    return {"status": "ok", "converter": Path(converter).name}


@app.get("/api/config")
async def config():
    return {"chunkBytes": CHUNK_BYTES, "maxFiles": MAX_FILES, "maxFileBytes": MAX_FILE_BYTES,
            "maxBatchBytes": MAX_BATCH_BYTES, "retentionSeconds": TTL}


@app.post("/api/jobs", status_code=201)
async def create(request: Request):
    # Bound the manifest even when Content-Length is absent.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "The file list is too large.")
    try:
        manifest = NewBatch.model_validate_json(raw)
    except ValueError:
        raise HTTPException(422, "Invalid file list, file size, or JPEG quality.")
    if len(jobs) >= MAX_JOBS:
        raise HTTPException(429, "The server is busy. Try again after a batch finishes and is deleted.")
    if sum(f.size for f in manifest.files) > MAX_BATCH_BYTES:
        raise HTTPException(413, "This batch exceeds the total upload limit.")
    if any(Path(f.name).suffix.lower() not in {".heic", ".heif"} for f in manifest.files):
        raise HTTPException(422, "Choose HEIC or HEIF images only.")
    ensure_space()
    batch = Batch(secrets.token_hex(24), [
        {"name": f.name, "size": f.size, "uploaded": 0, "status": "waiting", "error": None}
        for f in manifest.files], manifest.quality)
    batch.path.mkdir(mode=0o700)
    (batch.path / "input").mkdir()
    (batch.path / "output").mkdir()
    jobs[batch.id] = batch
    return batch.snapshot()


@app.get("/api/jobs/{job_id}")
async def status(job_id: str):
    return get_batch(job_id).snapshot()


@app.put("/api/jobs/{job_id}/files/{index}")
async def upload(job_id: str, index: int, request: Request, offset: int = 0):
    batch = get_batch(job_id)
    async with batch.lock:
        if batch.state != "uploading" or batch.cancelled.is_set():
            raise HTTPException(409, "This batch is no longer accepting uploads.")
        if not 0 <= index < len(batch.files):
            raise HTTPException(404, "File not found.")
        item = batch.files[index]
        if offset != item["uploaded"]:
            raise HTTPException(409, "Upload offset changed. Refresh progress and retry.")
        ensure_space()
        payload = bytearray()
        try:
            async with asyncio.timeout(90):
                async for part in request.stream():
                    payload.extend(part)
                    if len(payload) > CHUNK_BYTES or offset + len(payload) > item["size"]:
                        raise HTTPException(413, "Upload chunk exceeds the allowed size.")
        except TimeoutError:
            raise HTTPException(408, "Upload timed out. Retry this chunk.")
        if not payload:
            raise HTTPException(400, "Empty upload chunk.")
        path = batch.path / "input" / f"{index}.heic"
        try:
            with path.open("r+b" if path.exists() else "wb") as stream:
                stream.seek(offset)
                stream.write(payload)
                stream.truncate()
        except OSError:
            raise HTTPException(507, "Could not store this upload. The server may be out of space.")
        item["uploaded"] += len(payload)
        item["status"] = "uploaded" if item["uploaded"] == item["size"] else "uploading"
        batch.touched = time.time()
        return {"uploaded": item["uploaded"]}


async def read_decoder_error(stream):
    # Always drain the pipe, including after the capture limit, so a verbose
    # decoder cannot deadlock or make diagnostics consume unbounded memory.
    captured = bytearray()
    while chunk := await stream.read(8192):
        captured.extend(chunk[:max(0, 4096 - len(captured))])
    return captured.decode("utf-8", errors="replace")


def decoder_failure(returncode, diagnostic, batch):
    if returncode < 0:
        return (f"The decoder was stopped by signal {-returncode}. "
                "Check the container's memory limit and server logs.")
    # Do not reveal storage paths or the batch's bearer secret in error reports.
    diagnostic = diagnostic.replace(str(batch.path), "[batch]").replace(batch.id, "[batch]")
    diagnostic = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", diagnostic)
    diagnostic = " ".join("".join(c for c in diagnostic if c.isprintable() or c.isspace()).split())
    if diagnostic:
        return f"The decoder could not convert this image (exit {returncode}): {diagnostic[:1000]}"
    return f"The decoder produced no complete JPG (exit {returncode})."


async def decode(batch, index):
    folder = batch.path / "output" / str(index)
    folder.mkdir()
    source = batch.path / "input" / f"{index}.heic"
    output = folder / f"{index + 1:04d}-{safe_stem(batch.files[index]['name'])}.jpg"
    with source.open("rb") as stream:
        is_jpeg = stream.read(3) == b"\xff\xd8\xff"
    # Some exports contain JPEG data while retaining a .heic/.heif name.
    # Validate them, then copy without another lossy encode or metadata changes.
    command = ([sys.executable, str(ROOT / "prepare_jpeg.py"), str(source), str(output)]
               if is_jpeg else [converter, "-q", str(batch.quality), str(source), str(output)])
    # No shell and no user-controlled options or paths are passed to the CLI.
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    diagnostic = asyncio.create_task(read_decoder_error(proc.stderr))
    wait = asyncio.create_task(proc.wait())
    cancel = asyncio.create_task(batch.cancelled.wait())
    try:
        done, _ = await asyncio.wait({wait, cancel}, timeout=CONVERSION_TIMEOUT,
                                     return_when=asyncio.FIRST_COMPLETED)
        if cancel in done or not done:
            if proc.returncode is None:
                proc.kill()
            await wait
            raise RuntimeError("Conversion cancelled." if cancel in done else "Conversion timed out.")
        outputs = sorted(folder.glob("*.jpg"))
        if proc.returncode != 0 or not outputs or any(p.stat().st_size == 0 for p in outputs):
            raise RuntimeError(decoder_failure(proc.returncode, await diagnostic, batch))
        if is_jpeg:
            batch.files[index]["preservedJpeg"] = True
        return outputs
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        await diagnostic
        cancel.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel
        source.unlink(missing_ok=True)


def build_zip(batch):
    # JPG is already compressed; ZIP_STORED avoids a second costly compression pass.
    # ZipFile enables ZIP64 by default, and writes to disk without holding images in RAM.
    with zipfile.ZipFile(batch.path / "photos.zip.part", "w", compression=zipfile.ZIP_STORED) as archive:
        for photo in sorted((batch.path / "output").glob("*/*.jpg")):
            if batch.cancelled.is_set():
                return
            archive.write(photo, photo.name)
        failures = [{"name": f["name"], "error": f["error"]} for f in batch.files if f["error"]]
        if failures:
            archive.writestr("conversion-errors.json", json.dumps(failures, ensure_ascii=False, indent=2))
    (batch.path / "photos.zip.part").replace(batch.path / "photos.zip")
    shutil.rmtree(batch.path / "output")


async def process(batch):
    try:
        async with slots:
            if batch.cancelled.is_set():
                return
            batch.state = "converting"
            for index, item in enumerate(batch.files):
                if batch.cancelled.is_set():
                    return
                ensure_space()
                item["status"] = "converting"
                try:
                    outputs = await decode(batch, index)
                    item["status"] = "done"
                    item["outputs"] = len(outputs)
                except RuntimeError as exc:
                    item["status"], item["error"] = "failed", str(exc)
                    shutil.rmtree(batch.path / "output" / str(index), ignore_errors=True)
                batch.touched = time.time()
            if not any(f["status"] == "done" for f in batch.files):
                batch.state, batch.error = "failed", "None of these files could be converted. See the file errors below."
                return
            batch.state = "zipping"
            await asyncio.to_thread(build_zip, batch)
            batch.state = "done"
    except Exception:
        log.exception("Batch conversion failed")
        batch.state, batch.error = "failed", "Conversion stopped. Check server storage and logs, then try again."
    finally:
        batch.touched = time.time()
        if batch.cancelled.is_set():
            remove(batch)
        elif batch.state == "failed":
            shutil.rmtree(batch.path / "input", ignore_errors=True)
            shutil.rmtree(batch.path / "output", ignore_errors=True)
            (batch.path / "photos.zip.part").unlink(missing_ok=True)


@app.post("/api/jobs/{job_id}/start", status_code=202)
async def start(job_id: str):
    batch = get_batch(job_id)
    async with batch.lock:
        if batch.cancelled.is_set():
            raise HTTPException(409, "This batch is being deleted.")
        if batch.state != "uploading":
            return batch.snapshot()  # Safe to retry if the previous response was lost.
        if any(f["uploaded"] != f["size"] for f in batch.files):
            raise HTTPException(409, "Wait for every image to finish uploading.")
        batch.state = "queued"
        batch.task = asyncio.create_task(process(batch))
        return batch.snapshot()


@app.delete("/api/jobs/{job_id}", status_code=202)
async def delete(job_id: str):
    batch = get_batch(job_id)
    async with batch.lock:
        if batch.downloads:
            raise HTTPException(409, "Wait for the ZIP download to finish before deleting this batch.")
        batch.cancelled.set()
        if batch.task and not batch.task.done():
            batch.state = "cancelling"
        else:
            remove(batch)
    return {"status": "deleted or scheduled for deletion"}


class DownloadResponse(FileResponse):
    def __init__(self, batch):
        self.batch = batch
        super().__init__(batch.path / "photos.zip", media_type="application/zip", filename="converted-photos.zip")

    async def __call__(self, scope, receive, send):
        self.batch.downloads += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.batch.downloads -= 1
            self.batch.touched = time.time()


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str):
    batch = get_batch(job_id)
    if batch.state != "done" or batch.cancelled.is_set():
        raise HTTPException(409, "Your ZIP is not ready yet.")
    batch.touched = time.time()
    return DownloadResponse(batch)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="frontend")
