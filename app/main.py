import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import fitz
import pytesseract
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from PIL import Image
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DATA = Path(os.getenv("DATA_DIR", "/data"))
INCOMING = DATA / "incoming"
PROCESSED = DATA / "processed"
FAILED = DATA / "failed"
METADATA = DATA / "metadata"
DB_PATH = DATA / "documents.db"
LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://host.docker.internal:1234/v1").rstrip("/")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
TOP_K = int(os.getenv("TOP_K", "6"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "4500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "600"))
AUTO_CLASSIFY = os.getenv("AUTO_CLASSIFY", "true").lower() == "true"
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() == "true"
OCR_LANG = os.getenv("OCR_LANG", "deu+eng")
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
OCR_MIN_TEXT_CHARS = int(os.getenv("OCR_MIN_TEXT_CHARS", "40"))

for p in [INCOMING, PROCESSED, FAILED, METADATA, LOG_DIR]:
    p.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "pdf-rag.log", encoding="utf-8")],
)
log = logging.getLogger("pdf-rag")

app = FastAPI(title="Local PDF RAG", version="1.1.0")
qdrant = QdrantClient(url=QDRANT_URL)
embedder: Optional[SentenceTransformer] = None
import_lock = threading.Lock()

COLLECTIONS = {
    "programming": "knowledge_programming",
    "sysadmin": "knowledge_sysadmin",
    "security": "knowledge_security",
    "general": "knowledge_general",
}

CATEGORY_KEYWORDS = {
    "programming": ["python", "javascript", "typescript", "java", "rust", "golang", "c++", "powershell", "bash", "programm", "code", "api", "fastapi", "react"],
    "sysadmin": ["linux", "windows server", "active directory", "samba", "docker", "kubernetes", "systemd", "vmware", "hyper-v", "dns", "dhcp", "storage", "backup", "nginx", "apache"],
    "security": ["security", "pentest", "ethical hacking", "nmap", "metasploit", "burp", "wireshark", "owasp", "cve", "exploit", "hardening", "forensic", "vulnerability", "firewall", "suricata"],
}


def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha256 TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            category TEXT,
            pages INTEGER,
            chunks INTEGER,
            status TEXT,
            error TEXT,
            ocr_pages INTEGER DEFAULT 0
        )
    """)
    try:
        con.execute("ALTER TABLE documents ADD COLUMN ocr_pages INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    con.commit()
    return con


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def ensure_model():
    global embedder
    if embedder is None:
        log.info("Lade Embedding-Modell: %s", EMBED_MODEL)
        embedder = SentenceTransformer(EMBED_MODEL)
    return embedder


def ensure_collections():
    model = ensure_model()
    dim = model.get_sentence_embedding_dimension()
    existing = {c.name for c in qdrant.get_collections().collections}
    for name in COLLECTIONS.values():
        if name not in existing:
            qdrant.create_collection(name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
            log.info("Qdrant Collection erstellt: %s", name)


def ocr_page(page: fitz.Page) -> str:
    scale = OCR_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    mode = "RGB" if pix.n >= 3 else "L"
    image = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
    return pytesseract.image_to_string(image, lang=OCR_LANG).strip()


def extract_pdf(path: Path):
    doc = fitz.open(path)
    pages = []
    ocr_pages = 0
    for idx, page in enumerate(doc):
        text = page.get_text("text").strip()
        if len(text) < OCR_MIN_TEXT_CHARS and OCR_ENABLED:
            try:
                ocr_text = ocr_page(page)
                if len(ocr_text) > len(text):
                    text = ocr_text
                    ocr_pages += 1
                    log.info("OCR: %s Seite %d", path.name, idx + 1)
            except Exception as e:
                log.warning("OCR fehlgeschlagen: %s Seite %d: %s", path.name, idx + 1, e)
        if text:
            pages.append((idx + 1, text))
    return len(doc), pages, ocr_pages


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return [x for x in out if len(x) >= 80]


def lmstudio_chat(messages, temperature=0.1, max_tokens=500):
    r = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={"model": LMSTUDIO_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def keyword_category(text: str) -> str:
    low = text.lower()
    scores = {k: sum(low.count(word) for word in words) for k, words in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def classify_document(filename: str, sample: str):
    fallback = keyword_category(filename + "\n" + sample[:12000])
    if not AUTO_CLASSIFY:
        return fallback, [], "keyword"
    prompt = f'''Klassifiziere dieses technische Dokument. Antworte NUR als valides JSON.
Erlaubte category-Werte: programming, sysadmin, security, general.
Gib außerdem bis zu 8 kurze topics zurück.
Format: {{"category":"sysadmin","topics":["linux","dns"]}}
Dateiname: {filename}
Textauszug:\n{sample[:10000]}'''
    try:
        raw = lmstudio_chat([{"role": "user", "content": prompt}], max_tokens=250)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        cat = data.get("category", fallback)
        if cat not in COLLECTIONS:
            cat = fallback
        topics = [str(x)[:80] for x in data.get("topics", [])][:8]
        return cat, topics, "lmstudio"
    except Exception as e:
        log.warning("LM-Studio Klassifizierung nicht verfügbar (%s); nutze Keywords.", e)
        return fallback, [], "keyword"


def point_id(sha: str, page: int, chunk_no: int) -> int:
    raw = hashlib.sha256(f"{sha}:{page}:{chunk_no}".encode()).digest()[:8]
    return int.from_bytes(raw, "big") & ((1 << 63) - 1)


def import_pdf(path: Path):
    with import_lock:
        if not path.exists() or path.suffix.lower() != ".pdf":
            return
        time.sleep(1.0)
        sha = sha256_file(path)
        con = db_conn()
        row = con.execute("SELECT status FROM documents WHERE sha256=?", (sha,)).fetchone()
        if row and row[0] == "ok":
            log.info("Bereits importiert, übersprungen: %s", path.name)
            target = PROCESSED / path.name
            if not target.exists():
                shutil.move(str(path), str(target))
            con.close()
            return
        try:
            total_pages, pages, ocr_pages = extract_pdf(path)
            if not pages:
                raise ValueError("Kein Text gefunden. PDF konnte auch mit OCR nicht ausgewertet werden.")
            sample = "\n".join(text for _, text in pages[:8])
            category, topics, classifier = classify_document(path.name, sample)
            ensure_collections()
            model = ensure_model()
            points = []
            chunks_total = 0
            for page_no, page_text in pages:
                chunks = chunk_text(page_text)
                if not chunks:
                    continue
                vectors = model.encode(chunks, normalize_embeddings=True, show_progress_bar=False)
                for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    chunks_total += 1
                    points.append(PointStruct(
                        id=point_id(sha, page_no, idx),
                        vector=vec.tolist(),
                        payload={
                            "sha256": sha, "filename": path.name, "page": page_no,
                            "chunk": idx, "category": category, "topics": topics,
                            "classifier": classifier, "text": chunk,
                        },
                    ))
                    if len(points) >= 64:
                        qdrant.upsert(COLLECTIONS[category], points=points, wait=True)
                        points = []
            if points:
                qdrant.upsert(COLLECTIONS[category], points=points, wait=True)
            meta = {
                "sha256": sha, "filename": path.name, "category": category,
                "topics": topics, "classifier": classifier, "pages": total_pages,
                "chunks": chunks_total, "ocr_pages": ocr_pages,
            }
            (METADATA / f"{sha}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            con.execute(
                "INSERT OR REPLACE INTO documents(sha256,filename,category,pages,chunks,status,error,ocr_pages) VALUES(?,?,?,?,?,'ok',NULL,?)",
                (sha, path.name, category, total_pages, chunks_total, ocr_pages),
            )
            con.commit()
            target = PROCESSED / path.name
            if target.exists():
                target = PROCESSED / f"{path.stem}_{sha[:8]}.pdf"
            shutil.move(str(path), str(target))
            log.info("Import OK: %s | %s | Seiten=%d | OCR=%d | Chunks=%d", path.name, category, total_pages, ocr_pages, chunks_total)
        except Exception as e:
            log.exception("Import fehlgeschlagen: %s", path.name)
            con.execute("INSERT OR REPLACE INTO documents(sha256,filename,status,error) VALUES(?,?,'failed',?)", (sha, path.name, str(e)))
            con.commit()
            target = FAILED / path.name
            if target.exists():
                target = FAILED / f"{path.stem}_{sha[:8]}.pdf"
            try:
                shutil.move(str(path), str(target))
            except Exception:
                pass
        finally:
            con.close()


class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".pdf"):
            threading.Thread(target=import_pdf, args=(Path(event.src_path),), daemon=True).start()

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.lower().endswith(".pdf"):
            threading.Thread(target=import_pdf, args=(Path(event.dest_path),), daemon=True).start()


def startup_worker():
    for _ in range(30):
        try:
            qdrant.get_collections()
            break
        except Exception:
            time.sleep(2)
    ensure_collections()
    for pdf in sorted(INCOMING.glob("*.pdf")):
        import_pdf(pdf)
    obs = Observer()
    obs.schedule(PDFHandler(), str(INCOMING), recursive=False)
    obs.start()
    log.info("PDF-Watcher aktiv: %s", INCOMING)


class AskRequest(BaseModel):
    question: str
    category: Optional[str] = None
    top_k: int = TOP_K
    use_llm: bool = True


class OpenAIMessage(BaseModel):
    role: str
    content: str


class OpenAIChatRequest(BaseModel):
    model: Optional[str] = "local-pdf-rag"
    messages: list[OpenAIMessage]
    temperature: Optional[float] = 0.15
    max_tokens: Optional[int] = 1800


def route_category(question: str) -> str:
    return keyword_category(question)


def search_chunks(question: str, category: str, top_k: int):
    model = ensure_model()
    vector = model.encode([question], normalize_embeddings=True, show_progress_bar=False)[0].tolist()
    categories = [category] if category in COLLECTIONS else list(COLLECTIONS.keys())
    hits = []
    for cat in categories:
        try:
            result = qdrant.query_points(collection_name=COLLECTIONS[cat], query=vector, limit=top_k, with_payload=True).points
            for h in result:
                hits.append((h.score, h.payload))
        except Exception:
            continue
    hits.sort(key=lambda x: x[0], reverse=True)
    return hits[:top_k]


def answer_question(question: str, category: Optional[str] = None, top_k: int = TOP_K):
    category = category or route_category(question)
    hits = search_chunks(question, category, max(1, min(top_k, 20)))
    sources, context_parts = [], []
    for i, (score, p) in enumerate(hits, 1):
        sources.append({
            "rank": i, "score": round(float(score), 4), "filename": p.get("filename"),
            "page": p.get("page"), "category": p.get("category"), "topics": p.get("topics", []),
        })
        context_parts.append(f"[Quelle {i}: {p.get('filename')}, Seite {p.get('page')}]\n{p.get('text','')}")
    if not hits:
        return category, "Keine passenden Inhalte in der PDF-Wissensbasis gefunden.", sources
    system = """Du bist ein lokaler technischer Assistent für Programmierung, Systemadministration und autorisierte IT-Sicherheit. Nutze primär den bereitgestellten PDF-Kontext. Erfinde keine Quellen. Wenn der Kontext nicht reicht, sage das klar. Nenne bei technischen Aussagen möglichst die zugehörige Quelle und Seite."""
    user = f"Frage:\n{question}\n\nPDF-Kontext:\n" + "\n\n".join(context_parts)
    answer = lmstudio_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.15, max_tokens=1800)
    return category, answer, sources


@app.on_event("startup")
def on_startup():
    threading.Thread(target=startup_worker, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse("""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Local PDF RAG</title><style>body{font-family:system-ui;max-width:980px;margin:35px auto;padding:0 18px;background:#111;color:#eee}textarea{width:100%;min-height:110px;background:#1c1c1c;color:#fff;border:1px solid #444;padding:12px;border-radius:8px}button{margin-top:10px;padding:10px 18px}pre{white-space:pre-wrap;background:#1b1b1b;padding:16px;border-radius:8px}.src{padding:8px 0;border-bottom:1px solid #333}.muted{color:#aaa}</style></head>
<body><h1>Local PDF RAG 1.1.0</h1><p class='muted'>Qwen/LM Studio + Qdrant + PDF/OCR</p><textarea id='q' placeholder='Frage zu deiner PDF-Sammlung...'></textarea><br><button onclick='ask()'>Fragen</button><h2>Antwort</h2><pre id='a'>Bereit.</pre><h2>Quellen</h2><div id='s'></div><script>
async function ask(){let q=document.getElementById('q').value;document.getElementById('a').textContent='Verarbeite...';document.getElementById('s').innerHTML='';try{let r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});let d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));document.getElementById('a').textContent=d.answer||'';for(let x of (d.sources||[])){let e=document.createElement('div');e.className='src';e.textContent=`${x.rank}. ${x.filename} — Seite ${x.page} — Score ${x.score}`;document.getElementById('s').appendChild(e)}}catch(e){document.getElementById('a').textContent='Fehler: '+e.message}}
</script><p><a href='/docs'>API-Dokumentation</a> · <a href='/documents'>Dokumente</a></p></body></html>""")


@app.get("/health")
def health():
    try:
        qdrant.get_collections(); qstatus = "ok"
    except Exception as e:
        qstatus = f"error: {e}"
    try:
        r = requests.get(f"{LMSTUDIO_URL}/models", timeout=3)
        lmstatus = "ok" if r.ok else f"http-{r.status_code}"
    except Exception:
        lmstatus = "unreachable"
    return {"status": "ok", "qdrant": qstatus, "lmstudio": lmstatus, "embedding_model": EMBED_MODEL, "ocr": OCR_ENABLED, "ocr_lang": OCR_LANG}


@app.get("/documents")
def documents():
    con = db_conn()
    rows = con.execute("SELECT sha256,filename,imported_at,category,pages,chunks,status,error,ocr_pages FROM documents ORDER BY imported_at DESC").fetchall()
    con.close()
    keys = ["sha256","filename","imported_at","category","pages","chunks","status","error","ocr_pages"]
    return [dict(zip(keys, r)) for r in rows]


@app.post("/reindex")
def reindex():
    count = 0
    for pdf in INCOMING.glob("*.pdf"):
        threading.Thread(target=import_pdf, args=(pdf,), daemon=True).start(); count += 1
    return {"queued": count}


@app.post("/ask")
def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(400, "question darf nicht leer sein")
    category = req.category or route_category(req.question)
    hits = search_chunks(req.question, category, max(1, min(req.top_k, 20)))
    if not req.use_llm:
        sources = []
        context = []
        for i, (score, p) in enumerate(hits, 1):
            sources.append({"rank": i, "score": round(float(score), 4), "filename": p.get("filename"), "page": p.get("page"), "category": p.get("category"), "topics": p.get("topics", [])})
            context.append(p.get("text", ""))
        return {"category": category, "sources": sources, "context": context}
    try:
        category, answer, sources = answer_question(req.question, category, req.top_k)
    except Exception as e:
        raise HTTPException(503, f"LM Studio nicht erreichbar oder Anfrage fehlgeschlagen: {e}")
    return {"category": category, "answer": answer, "sources": sources}


@app.get("/v1/models")
def openai_models():
    return {"object": "list", "data": [{"id": "local-pdf-rag", "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
def openai_chat(req: OpenAIChatRequest):
    user_messages = [m.content for m in req.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(400, "Mindestens eine user-Nachricht ist erforderlich")
    question = user_messages[-1]
    try:
        _, answer, sources = answer_question(question)
    except Exception as e:
        raise HTTPException(503, f"RAG/LM Studio Fehler: {e}")
    source_text = "\n\nQuellen:\n" + "\n".join(f"- {s['filename']}, Seite {s['page']}" for s in sources) if sources else ""
    content = answer + source_text
    return {
        "id": f"chatcmpl-{int(time.time())}", "object": "chat.completion", "created": int(time.time()),
        "model": "local-pdf-rag", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "rag_sources": sources,
    }
