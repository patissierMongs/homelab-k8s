"""
RAG Pipeline Worker
- Batch processes video/audio/PDF files from Nextcloud via WebDAV
- Tracks processed files to avoid duplicates
- Extracts audio via ffmpeg, transcribes via faster-whisper
- Extracts text from PDFs
- Generates embeddings via Ollama (qwen3-embedding)
- Stores in Qdrant vector DB with Nextcloud file IDs for direct linking
"""

import os
import asyncio
import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import httpx
import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rag-worker")

# Config from env
WHISPER_URL = os.getenv("WHISPER_URL", "http://faster-whisper:8000")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "/root/.cache/huggingface/hub/models--deepdml--faster-whisper-large-v3-turbo-ct2/snapshots/4df90f75321148c3a29a9e2351b7ddf8f5b115a8")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama.llm.svc.cluster.local:11434")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen3-embedding:0.6b")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "media_transcripts")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))

# GPU priority: proxy stats endpoint to check if user LLM is active
PRIORITY_PROXY_URL = os.getenv("PRIORITY_PROXY_URL", "")

# Nextcloud WebDAV config
NC_URL = os.getenv("NC_URL", "https://cloud.reimu-chan.mooo.com")
NC_USER = os.getenv("NC_USER", "")
NC_PASS = os.getenv("NC_PASS", "")

# NFS-mounted Nextcloud data directory
NC_DATA_DIR = os.getenv("NC_DATA_DIR", "")

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".wav", ".mp3", ".flac", ".ogg", ".m4a"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
PDF_EXTENSIONS = {".pdf"}
ALL_EXTENSIONS = MEDIA_EXTENSIONS | PDF_EXTENSIONS

app = FastAPI(title="RAG Pipeline Worker")
qdrant = QdrantClient(url=QDRANT_URL)
_executor = ThreadPoolExecutor(max_workers=2)


async def _yield_to_user():
    """GPU 사용 전 사용자 LLM 요청이 있으면 완료될 때까지 대기."""
    if not PRIORITY_PROXY_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            while True:
                resp = await client.get(f"{PRIORITY_PROXY_URL}/proxy/stats")
                stats = resp.json()
                if stats.get("user_count", 0) == 0:
                    return
                log.info("GPU yield: 사용자 LLM 요청 활성 — 배치 대기 중")
                await asyncio.sleep(2)
    except Exception:
        pass  # proxy 연결 실패 시 그냥 진행

# ── In-memory job tracking ────────────────────────────────────────
import threading
from enum import Enum

class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    EMBEDDING = "embedding"
    STORING = "storing"
    DONE = "done"
    FAILED = "failed"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

def _job_update(source_path: str, **kwargs):
    with _jobs_lock:
        if source_path not in _jobs:
            _jobs[source_path] = {
                "source_path": source_path,
                "file_name": "",
                "file_type": "",
                "status": JobStatus.QUEUED,
                "progress": "",
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "chunks": 0,
                "text_length": 0,
            }
        _jobs[source_path].update(kwargs)


# ── Collection management ──────────────────────────────────────────

def _ensure_collection_sync(dimension: int):
    collections = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION_NAME not in collections:
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
        )
        log.info(f"Created collection '{COLLECTION_NAME}' with dim={dimension}")


async def ensure_collection(dimension: int):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _ensure_collection_sync, dimension)


def _is_already_processed_sync(source_path: str) -> bool:
    try:
        results = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[FieldCondition(key="source_path", match=MatchValue(value=source_path))]
            ),
            limit=1,
        )
        return len(results[0]) > 0
    except Exception:
        return False


async def is_already_processed(source_path: str) -> bool:
    """Check if a file has already been processed by looking in Qdrant."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _is_already_processed_sync, source_path)


def delete_file_vectors(source_path: str):
    """Delete all vectors for a given source file (for reprocessing)."""
    try:
        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="source_path", match=MatchValue(value=source_path))]
            ),
        )
        log.info(f"Deleted existing vectors for {source_path}")
    except Exception as e:
        log.warning(f"Could not delete vectors for {source_path}: {e}")


# ── Audio extraction ───────────────────────────────────────────────

def _extract_audio_sync(video_path: str, audio_path: str):
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1",
        "-y", audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    log.info(f"Audio extracted: {audio_path}")


async def extract_audio(video_path: str, audio_path: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _extract_audio_sync, video_path, audio_path)


# ── PDF extraction ─────────────────────────────────────────────────

def _extract_pdf_text_sync(pdf_path: str) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text.strip()
    except ImportError:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pdftotext failed: {result.stderr}")
        return result.stdout.strip()


async def extract_pdf_text(pdf_path: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _extract_pdf_text_sync, pdf_path)


# ── STT ────────────────────────────────────────────────────────────

def _is_garbage_transcription(text: str) -> bool:
    """Detect degenerate STT output (e.g. '어 어 어 어...' hallucination loops)."""
    if not text.strip():
        return True
    words = text.split()
    if len(words) < 3:
        return False
    from collections import Counter
    freq = Counter(words)
    most_common_count = freq.most_common(1)[0][1]
    if most_common_count / len(words) > 0.5:
        log.warning(f"Garbage detected: '{freq.most_common(1)[0][0]}' repeated {most_common_count}/{len(words)} times")
        return True
    return False


async def transcribe(audio_path: str) -> str:
    async with httpx.AsyncClient(timeout=600.0) as client:
        with open(audio_path, "rb") as f:
            resp = await client.post(
                f"{WHISPER_URL}/v1/audio/transcriptions",
                files={"file": ("audio.wav", f, "audio/wav")},
                data={
                    "model": WHISPER_MODEL,
                    "language": "ko",
                    "vad_filter": "true",
                    "word_timestamps": "true",
                },
            )
        resp.raise_for_status()
        text = resp.json().get("text", "")
        log.info(f"Transcribed {len(text)} chars")
        if _is_garbage_transcription(text):
            log.warning(f"Garbage transcription detected, discarding")
            return ""
        return text


# ── Embedding ──────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start = end - CHUNK_OVERLAP
    return [c for c in chunks if c.strip()]


EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "64"))


async def get_embeddings(texts: list[str]) -> list[list[float]]:
    embeddings = []
    async with httpx.AsyncClient(timeout=300.0) as client:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i:i + EMBED_BATCH_SIZE]
            resp = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": batch},
            )
            resp.raise_for_status()
            embeddings.extend(resp.json()["embeddings"])
            if len(texts) > EMBED_BATCH_SIZE:
                log.info(f"Embedded batch {i//EMBED_BATCH_SIZE + 1}/{(len(texts)-1)//EMBED_BATCH_SIZE + 1}")
    log.info(f"Generated {len(embeddings)} embeddings (dim={len(embeddings[0])})")
    return embeddings


# ── Core processing ────────────────────────────────────────────────

def _extract_owner(source_path: str) -> str:
    """Extract Nextcloud user from source_path (format: 'username/path/to/file')."""
    if "/" in source_path:
        return source_path.split("/")[0]
    return ""


async def process_file(file_path: str, original_name: str, source_path: str = "",
                       nc_file_id: int = 0, file_type: str = "media"):
    """Full pipeline: file -> text extraction -> embed -> store."""
    log.info(f"Processing [{file_type}]: {original_name}")
    sp = source_path or original_name
    _job_update(sp, file_name=original_name, file_type=file_type,
                status=JobStatus.EXTRACTING, started_at=datetime.now(timezone.utc).isoformat())

    ext = Path(original_name).suffix.lower()
    text = ""

    try:
        if file_type == "pdf" or ext in PDF_EXTENSIONS:
            _job_update(sp, progress="PDF 텍스트 추출 중")
            text = await extract_pdf_text(file_path)
        elif ext in VIDEO_EXTENSIONS:
            _job_update(sp, status=JobStatus.EXTRACTING, progress="오디오 추출 중 (ffmpeg)")
            tmp_audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            try:
                await extract_audio(file_path, tmp_audio)
                await _yield_to_user()  # STT(GPU) 전 사용자 양보
                _job_update(sp, status=JobStatus.TRANSCRIBING, progress="STT 처리 중 (whisper)")
                text = await transcribe(tmp_audio)
            finally:
                if os.path.exists(tmp_audio):
                    os.remove(tmp_audio)
        elif ext in AUDIO_EXTENSIONS:
            _job_update(sp, status=JobStatus.TRANSCRIBING, progress="STT 처리 중 (whisper)")
            text = await transcribe(file_path)
        else:
            _job_update(sp, status=JobStatus.FAILED, error="unsupported format",
                        finished_at=datetime.now(timezone.utc).isoformat())
            return {"status": "unsupported", "file": original_name}

        if not text.strip():
            log.warning(f"Empty text for {original_name}")
            _job_update(sp, status=JobStatus.FAILED, error="empty text",
                        finished_at=datetime.now(timezone.utc).isoformat())
            return {"status": "empty", "file": original_name}
    except Exception as e:
        _job_update(sp, status=JobStatus.FAILED, error=str(e)[:200],
                    finished_at=datetime.now(timezone.utc).isoformat())
        raise

    # Chunk
    chunks = chunk_text(text)
    log.info(f"Split into {len(chunks)} chunks")

    # Embed
    await _yield_to_user()  # 임베딩(GPU) 전 사용자 양보
    _job_update(sp, status=JobStatus.EMBEDDING, progress=f"임베딩 생성 중 ({len(chunks)} 청크)")
    embeddings = await get_embeddings(chunks)

    # Ensure collection
    await ensure_collection(len(embeddings[0]))

    # Delete old vectors if reprocessing
    if source_path:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, delete_file_vectors, source_path)

    # Store in Qdrant
    _job_update(sp, status=JobStatus.STORING, progress="Qdrant 저장 중")
    file_hash = hashlib.md5((source_path or original_name).encode()).hexdigest()[:12]
    points = [
        PointStruct(
            id=int(hashlib.md5(f"{file_hash}_{i}".encode()).hexdigest()[:16], 16) % (2**63),
            vector=emb,
            payload={
                "text": chunk,
                "source": original_name,
                "source_path": source_path or original_name,
                "owner": _extract_owner(source_path or original_name),
                "nc_file_id": nc_file_id,
                "file_type": file_type,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "full_text_length": len(text),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
    ]

    loop = asyncio.get_event_loop()
    UPSERT_BATCH = 256
    for batch_start in range(0, len(points), UPSERT_BATCH):
        batch = points[batch_start:batch_start + UPSERT_BATCH]
        await loop.run_in_executor(_executor, qdrant.upsert, COLLECTION_NAME, batch)
    log.info(f"Stored {len(points)} vectors for {original_name}")

    _job_update(sp, status=JobStatus.DONE, progress="완료",
                chunks=len(chunks), text_length=len(text),
                finished_at=datetime.now(timezone.utc).isoformat())

    return {
        "status": "success",
        "file": original_name,
        "source_path": source_path,
        "file_type": file_type,
        "chunks": len(chunks),
        "text_length": len(text),
    }


# ── Nextcloud WebDAV integration ──────────────────────────────────

async def list_nextcloud_files(user: str = "") -> list[dict]:
    """List all processable files from Nextcloud via OCS API."""
    target_user = user or NC_USER
    if not target_user or not NC_PASS:
        return []

    files = []
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        # Use Nextcloud search API to find media and PDF files
        for ext in ALL_EXTENSIONS:
            try:
                resp = await client.request(
                    "SEARCH",
                    f"{NC_URL}/remote.php/dav/",
                    auth=(target_user, NC_PASS),
                    headers={"Content-Type": "application/xml"},
                    content=f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:basicsearch>
    <d:select><d:prop><d:displayname/><oc:fileid/><d:getcontentlength/></d:prop></d:select>
    <d:from><d:scope><d:href>/files/{target_user}</d:href><d:depth>infinity</d:depth></d:scope></d:from>
    <d:where>
      <d:like><d:prop><d:displayname/></d:prop><d:literal>%{ext}</d:literal></d:like>
    </d:where>
  </d:basicsearch>
</d:searchrequest>""",
                )
                if resp.status_code == 207:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(resp.text)
                    ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
                    for response in root.findall(".//d:response", ns):
                        href = response.find("d:href", ns)
                        fileid = response.find(".//oc:fileid", ns)
                        size = response.find(".//d:getcontentlength", ns)
                        if href is not None and fileid is not None:
                            path = href.text
                            name = Path(path).name
                            files.append({
                                "name": name,
                                "path": path,
                                "fileid": int(fileid.text) if fileid.text else 0,
                                "size": int(size.text) if size is not None and size.text else 0,
                                "ext": Path(name).suffix.lower(),
                            })
            except Exception as e:
                log.warning(f"Error searching for {ext}: {e}")

    return files


async def download_nc_file(path: str) -> str:
    """Download a file from Nextcloud WebDAV to a temp path."""
    async with httpx.AsyncClient(timeout=600.0, verify=False) as client:
        resp = await client.get(
            f"{NC_URL}{path}",
            auth=(NC_USER, NC_PASS),
        )
        resp.raise_for_status()
        ext = Path(path).suffix
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False).name
        with open(tmp, "wb") as f:
            f.write(resp.content)
        return tmp


# ── API endpoints ──────────────────────────────────────────────────

@app.post("/process")
async def process_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_path: str = Form(""),
    nc_file_id: int = Form(0),
    file_type: str = Form(""),
):
    """Upload a file for processing. Accepts optional metadata from pusher."""
    tmp = tempfile.NamedTemporaryFile(suffix=Path(file.filename).suffix, delete=False).name
    with open(tmp, "wb") as f:
        content = await file.read()
        f.write(content)

    if not file_type:
        ext = Path(file.filename).suffix.lower()
        file_type = "pdf" if ext in PDF_EXTENSIONS else "media"

    background_tasks.add_task(
        _process_and_cleanup, tmp, file.filename, source_path, nc_file_id
    )
    return {"status": "queued", "file": file.filename, "source_path": source_path}


async def _process_and_cleanup(tmp_path: str, filename: str, source_path: str = "",
                                nc_file_id: int = 0):
    ext = Path(filename).suffix.lower()
    file_type = "pdf" if ext in PDF_EXTENSIONS else "media"
    sp = source_path or filename
    _job_update(sp, file_name=filename, file_type=file_type, status=JobStatus.QUEUED)
    try:
        result = await process_file(tmp_path, filename, source_path, nc_file_id, file_type)
        log.info(f"Result: {result}")
    except Exception as e:
        log.error(f"Failed to process {filename}: {e}")
        _job_update(sp, status=JobStatus.FAILED, error=str(e)[:200],
                    finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/batch")
async def batch_process(background_tasks: BackgroundTasks, user: str = "", force: bool = False):
    """Scan Nextcloud and process all unprocessed media/PDF files."""
    files = await list_nextcloud_files(user)
    if not files:
        return {"status": "no_files", "message": "No files found or WebDAV not configured"}

    queued = []
    skipped = []

    for f in files:
        source_path = f["path"]

        if not force and await is_already_processed(source_path):
            skipped.append(f["name"])
            continue

        # Download and queue processing
        try:
            tmp_path = await download_nc_file(source_path)
            background_tasks.add_task(
                _process_and_cleanup, tmp_path, f["name"], source_path, f["fileid"]
            )
            queued.append(f["name"])
        except Exception as e:
            log.error(f"Failed to download {f['name']}: {e}")

    return {
        "status": "batch_started",
        "queued": len(queued),
        "skipped": len(skipped),
        "queued_files": queued,
        "skipped_files": skipped,
    }


@app.get("/processed")
async def list_processed():
    """List all processed files."""
    try:
        # Iterate using scroll with offset to get ALL records
        files = {}
        offset = None
        while True:
            results, next_offset = qdrant.scroll(
                collection_name=COLLECTION_NAME,
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                sp = point.payload.get("source_path", "")
                if sp and sp not in files:
                    files[sp] = {
                        "source": point.payload.get("source", ""),
                        "source_path": sp,
                        "nc_file_id": point.payload.get("nc_file_id", 0),
                        "file_type": point.payload.get("file_type", ""),
                        "total_chunks": point.payload.get("total_chunks", 0),
                        "processed_at": point.payload.get("processed_at", ""),
                    }
            if next_offset is None:
                break
            offset = next_offset
        return {"count": len(files), "files": list(files.values())}
    except Exception:
        return {"count": 0, "files": []}


@app.post("/scan")
async def scan_nfs(background_tasks: BackgroundTasks, user: str = "", force: bool = False):
    """Scan NFS-mounted Nextcloud data and process unprocessed files directly."""
    if user:
        users = [user]
    else:
        users = _list_nc_users() if NC_DATA_DIR else [NC_USER]

    # Find all processable files across users
    all_files = []
    for u in users:
        data_dir = Path(NC_DATA_DIR) / u / "files" if NC_DATA_DIR else None
        if not data_dir or not data_dir.exists():
            continue
        for fpath in data_dir.rglob("*"):
            if fpath.is_file() and fpath.suffix.lower() in ALL_EXTENSIONS:
                rel_path = str(fpath.relative_to(data_dir))
                all_files.append({
                    "name": fpath.name,
                    "local_path": str(fpath),
                    "rel_path": f"{u}/{rel_path}",
                    "ext": fpath.suffix.lower(),
                    "size": fpath.stat().st_size,
                })

    queued = []
    skipped = []

    pending = []
    for f in all_files:
        if not force and await is_already_processed(f["rel_path"]):
            skipped.append(f["name"])
            continue
        pending.append(f)
        queued.append(f["name"])

    # 순차 처리를 단일 백그라운드 태스크로 실행
    if pending:
        background_tasks.add_task(_process_batch_sequential, pending)

    return {
        "status": "scan_started",
        "total": len(all_files),
        "queued": len(queued),
        "skipped": len(skipped),
        "queued_files": queued,
    }


async def _process_nfs_file(local_path: str, filename: str, rel_path: str, file_type: str):
    """Process a file directly from NFS mount (no copy needed for PDFs, copy for media)."""
    _job_update(rel_path, file_name=filename, file_type=file_type, status=JobStatus.QUEUED)
    try:
        result = await process_file(local_path, filename, rel_path, 0, file_type)
        log.info(f"Result: {result}")
    except Exception as e:
        log.error(f"Failed to process {filename}: {e}")
        _job_update(rel_path, status=JobStatus.FAILED, error=str(e)[:200],
                    finished_at=datetime.now(timezone.utc).isoformat())


async def _process_batch_sequential(files: list):
    """파일을 하나씩 순차 처리. 각 파일 전 사용자 GPU 양보."""
    log.info(f"Sequential batch: {len(files)} files")
    for f in files:
        await _yield_to_user()
        ext = f["ext"]
        file_type = "pdf" if ext in PDF_EXTENSIONS else "media"
        await _process_nfs_file(f["local_path"], f["name"], f["rel_path"], file_type)
    log.info("Sequential batch complete")


@app.get("/jobs")
async def list_jobs():
    """Current and recent job status."""
    with _jobs_lock:
        jobs = list(_jobs.values())
    return {"jobs": jobs}


SYSTEM_DIRS = {"appdata_", "ObsidianWebdav"}


def _list_nc_users() -> list[str]:
    """List actual Nextcloud users from NFS data dir."""
    if not NC_DATA_DIR:
        return []
    base = Path(NC_DATA_DIR)
    if not base.exists():
        return []
    users = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and (d / "files").exists():
            name = d.name
            if not any(name.startswith(prefix) for prefix in SYSTEM_DIRS):
                users.append(name)
    return users


@app.get("/status")
async def pipeline_status(user: str = ""):
    """Combined monitoring: disk files vs processed vs in-progress."""
    # 1. NFS에서 대상 파일 목록 (전체 유저 또는 특정 유저)
    if user:
        users = [user]
    else:
        users = _list_nc_users() if NC_DATA_DIR else [NC_USER]

    disk_files = []
    for u in users:
        data_dir = Path(NC_DATA_DIR) / u / "files" if NC_DATA_DIR else None
        if not data_dir or not data_dir.exists():
            continue
        for fpath in data_dir.rglob("*"):
            if fpath.is_file() and fpath.suffix.lower() in ALL_EXTENSIONS:
                rel = str(fpath.relative_to(data_dir))
                disk_files.append({
                    "name": fpath.name,
                    "path": f"{u}/{rel}",
                    "user": u,
                    "size": fpath.stat().st_size,
                    "type": "pdf" if fpath.suffix.lower() in PDF_EXTENSIONS else "media",
                })

    # 2. Qdrant에서 처리 완료 목록
    def _fetch_processed():
        result = {}
        try:
            offset = None
            while True:
                results, next_offset = qdrant.scroll(
                    collection_name=COLLECTION_NAME, limit=1000,
                    offset=offset, with_payload=True, with_vectors=False,
                )
                for point in results:
                    sp = point.payload.get("source_path", "")
                    if sp and sp not in result:
                        result[sp] = {
                            "chunks": point.payload.get("total_chunks", 0),
                            "processed_at": point.payload.get("processed_at", ""),
                        }
                if next_offset is None:
                    break
                offset = next_offset
        except Exception:
            pass
        return result

    loop = asyncio.get_event_loop()
    processed = await loop.run_in_executor(_executor, _fetch_processed)

    # 3. 현재 작업 상태
    with _jobs_lock:
        active_jobs = {k: v for k, v in _jobs.items()
                       if v["status"] not in (JobStatus.DONE, JobStatus.FAILED)}

    # 4. 파일별 상태 결합
    file_statuses = []
    for f in disk_files:
        path = f["path"]
        # Match against both "user/rel" and "rel" (legacy)
        rel_only = "/".join(path.split("/")[1:]) if "/" in path else path
        matched_path = path if path in processed else (rel_only if rel_only in processed else None)

        if path in active_jobs:
            st = active_jobs[path]["status"]
            progress = active_jobs[path].get("progress", "")
        elif matched_path:
            st = "done"
            progress = f'{processed[matched_path]["chunks"]} chunks'
        else:
            st = "pending"
            progress = "대기 중"
        file_statuses.append({
            "name": f["name"],
            "path": path,
            "user": f.get("user", ""),
            "size_mb": round(f["size"] / 1048576, 1),
            "type": f["type"],
            "status": st,
            "progress": progress,
        })

    # 정렬: 처리 중 > 대기 > 완료
    order = {"extracting": 0, "transcribing": 0, "embedding": 0, "storing": 0,
             "downloading": 0, "queued": 1, "pending": 2, "done": 3, "failed": 4}
    file_statuses.sort(key=lambda x: order.get(x["status"], 5))

    summary = {
        "total": len(disk_files),
        "processed": sum(1 for f in file_statuses if f["status"] == "done"),
        "pending": sum(1 for f in file_statuses if f["status"] == "pending"),
        "in_progress": sum(1 for f in file_statuses if f["status"] in
                           ("queued", "extracting", "transcribing", "embedding", "storing", "downloading")),
        "failed": sum(1 for f in file_statuses if f["status"] == "failed"),
    }

    return {"summary": summary, "files": file_statuses}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Web dashboard for monitoring RAG pipeline."""
    return DASHBOARD_HTML


@app.get("/health")
async def health():
    nfs_ok = bool(NC_DATA_DIR and Path(NC_DATA_DIR).exists())
    return {"status": "ok", "nfs_mounted": nfs_ok}


@app.get("/search")
async def search(q: str, user: str = "", limit: int = 5):
    """Search transcripts/documents by query. Requires 'user' param for access control."""
    if not user:
        return {"error": "user parameter required", "results": []}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": q},
        )
        resp.raise_for_status()
        query_embedding = resp.json()["embeddings"][0]

    results = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_embedding,
        query_filter=Filter(
            must=[FieldCondition(key="owner", match=MatchValue(value=user))]
        ),
        limit=limit,
        with_payload=True,
    )

    return {
        "query": q,
        "user": user,
        "results": [
            {
                "text": p.payload.get("text", ""),
                "source": p.payload.get("source", ""),
                "source_path": p.payload.get("source_path", ""),
                "owner": p.payload.get("owner", ""),
                "nc_file_id": p.payload.get("nc_file_id", 0),
                "file_type": p.payload.get("file_type", ""),
                "score": p.score,
            }
            for p in results.points
        ],
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAG Pipeline Monitor</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 20px; }
  h1 { font-size: 1.4rem; margin-bottom: 16px; color: #38bdf8; }
  .summary { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .card { background: #1e293b; border-radius: 8px; padding: 16px 20px; min-width: 120px; }
  .card .num { font-size: 2rem; font-weight: 700; }
  .card .label { font-size: 0.8rem; color: #94a3b8; margin-top: 4px; }
  .card.active .num { color: #facc15; }
  .card.done .num { color: #4ade80; }
  .card.pending .num { color: #94a3b8; }
  .card.failed .num { color: #f87171; }
  table { width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; }
  th { background: #334155; text-align: left; padding: 10px 14px; font-size: 0.8rem;
       color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 10px 14px; border-top: 1px solid #334155; font-size: 0.9rem; }
  tr:hover { background: #263450; }
  .badge { padding: 3px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; display: inline-block; }
  .badge.done { background: #166534; color: #4ade80; }
  .badge.pending { background: #334155; color: #94a3b8; }
  .badge.active { background: #854d0e; color: #facc15; animation: pulse 1.5s infinite; }
  .badge.failed { background: #7f1d1d; color: #f87171; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }
  .refresh { color: #64748b; font-size: 0.75rem; margin-top: 12px; }
  .type-icon { margin-right: 6px; }
</style>
</head>
<body>
<h1>RAG Pipeline Monitor</h1>
<div class="summary" id="summary"></div>
<table>
  <thead><tr><th>유저</th><th>파일</th><th>타입</th><th>크기</th><th>상태</th><th>진행</th></tr></thead>
  <tbody id="files"></tbody>
</table>
<div class="refresh" id="refresh"></div>
<script>
const STATUS_LABELS = {
  done: '완료', pending: '대기', queued: '큐', failed: '실패',
  extracting: '추출 중', transcribing: 'STT 중', embedding: '임베딩 중',
  storing: '저장 중', downloading: '다운로드 중'
};
const ACTIVE = new Set(['queued','extracting','transcribing','embedding','storing','downloading']);
function badgeClass(s) { return ACTIVE.has(s) ? 'active' : s === 'done' ? 'done' : s === 'failed' ? 'failed' : 'pending'; }
function typeIcon(t) { return t === 'pdf' ? '📄' : '🎬'; }

async function refresh() {
  try {
    const base = location.pathname.replace(new RegExp('/dashboard/?$'), '');
    const r = await fetch(base + '/status');
    const d = await r.json();
    const s = d.summary;
    document.getElementById('summary').innerHTML = `
      <div class="card"><div class="num">${s.total}</div><div class="label">전체 파일</div></div>
      <div class="card done"><div class="num">${s.processed}</div><div class="label">처리 완료</div></div>
      <div class="card active"><div class="num">${s.in_progress}</div><div class="label">처리 중</div></div>
      <div class="card pending"><div class="num">${s.pending}</div><div class="label">대기</div></div>
      <div class="card failed"><div class="num">${s.failed}</div><div class="label">실패</div></div>`;
    document.getElementById('files').innerHTML = d.files.map(f => `<tr>
      <td>${f.user || '-'}</td>
      <td><span class="type-icon">${typeIcon(f.type)}</span>${f.name}</td>
      <td>${f.type.toUpperCase()}</td>
      <td>${f.size_mb} MB</td>
      <td><span class="badge ${badgeClass(f.status)}">${STATUS_LABELS[f.status] || f.status}</span></td>
      <td>${f.progress}</td>
    </tr>`).join('');
    document.getElementById('refresh').textContent = '마지막 갱신: ' + new Date().toLocaleTimeString('ko-KR');
  } catch(e) { document.getElementById('refresh').textContent = '갱신 실패: ' + e; }
}
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


# ── Periodic auto-scan ────────────────────────────────────────────
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "600"))  # 10분

_scan_running = False


async def _auto_scan():
    """Periodically scan NFS for new files and process them."""
    global _scan_running
    await asyncio.sleep(30)  # 시작 후 30초 대기
    while True:
        try:
            if not _scan_running:
                _scan_running = True
                users = _list_nc_users() if NC_DATA_DIR else ([NC_USER] if NC_USER else [])
                all_files = []
                for u in users:
                    data_dir = Path(NC_DATA_DIR) / u / "files" if NC_DATA_DIR else None
                    if not data_dir or not data_dir.exists():
                        continue
                    for fpath in data_dir.rglob("*"):
                        if fpath.is_file() and fpath.suffix.lower() in ALL_EXTENSIONS:
                            rel_path = str(fpath.relative_to(data_dir))
                            all_files.append({
                                "name": fpath.name,
                                "local_path": str(fpath),
                                "rel_path": f"{u}/{rel_path}",
                                "ext": fpath.suffix.lower(),
                            })

                pending = []
                for f in all_files:
                    if await is_already_processed(f["rel_path"]):
                        continue
                    pending.append(f)

                if pending:
                    log.info(f"Auto-scan: {len(pending)} files to process (sequential)")
                    for f in pending:
                        await _yield_to_user()  # 각 파일 전 사용자 양보
                        ext = f["ext"]
                        file_type = "pdf" if ext in PDF_EXTENSIONS else "media"
                        await _process_nfs_file(f["local_path"], f["name"], f["rel_path"], file_type)
                _scan_running = False
        except Exception as e:
            log.error(f"Auto-scan error: {e}")
            _scan_running = False
        await asyncio.sleep(SCAN_INTERVAL)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_auto_scan())
    log.info(f"Auto-scan scheduler started (interval={SCAN_INTERVAL}s)")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
