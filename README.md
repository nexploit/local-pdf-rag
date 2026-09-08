# Local PDF RAG

A self-hosted local knowledge base for PDF collections using **RAG (Retrieval-Augmented Generation)**, Qdrant vector search, SentenceTransformers, OCR and an OpenAI-compatible API.

The project is designed for technical libraries such as programming, system administration, networking, IT security and ethical-hacking documentation.

> Local PDF RAG does **not** retrain or fine-tune your LLM. It indexes knowledge from your PDFs and retrieves relevant passages at query time. The retrieved context is then supplied to your local LLM.

## Features

- Automatic PDF folder watcher
- Text extraction with PyMuPDF
- OCR fallback for scanned pages using Tesseract (`deu+eng`)
- SHA-256 based duplicate detection
- Automatic document classification
- Chunking and multilingual embeddings
- Persistent Qdrant vector database
- Local LM Studio / OpenAI-compatible LLM integration
- Web chat interface
- Source and page references in answers
- OpenAI-compatible `/v1/chat/completions` API
- Swagger/OpenAPI documentation
- Persistent logging
- `processed` and `failed` document handling
- PowerShell installation and helper scripts
- Optional Docker image push to a private Nexus registry

## How it works

```text
                 data/incoming
                       |
                       v
                  PDF Watcher
                       |
             +---------+---------+
             |                   |
             v                   v
       Text extraction       OCR fallback
         (PyMuPDF)          (Tesseract)
             |                   |
             +---------+---------+
                       |
                       v
                 Classification
                       |
                       v
                    Chunks
                       |
                       v
                  Embeddings
                       |
                       v
               Qdrant Vector DB
                       |
              semantic retrieval
                       |
                       v
                  Local LLM
                  LM Studio
                       |
                       v
                Answer + sources
```

A successfully indexed PDF is moved from `data/incoming/` to `data/processed/`. Documents that cannot be processed are moved to `data/failed/`.

## Requirements

- Docker Engine or Docker Desktop with Linux containers
- Docker Compose v2
- LM Studio or another OpenAI-compatible local inference server
- A loaded chat/instruct model
- Recommended: several GB of free disk space for models, Qdrant data and PDF content

The supplied PowerShell helpers are intended for Windows 10/11, but the Docker Compose stack itself can also be used on Linux.

## Quick start

Clone the repository:

```bash
git clone <YOUR-GITHUB-REPOSITORY-URL>
cd local-pdf-rag
```

On Windows PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Or start it directly with Docker Compose:

```bash
cp .env.example .env
docker compose build
docker compose up -d
```

Start the LM Studio local server on port `1234` and load your preferred local chat model.

## Configuration

Copy `.env.example` to `.env` and adjust it when required:

```env
LMSTUDIO_URL=http://host.docker.internal:1234/v1
LMSTUDIO_MODEL=local-model
EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
AUTO_CLASSIFY=true
TOP_K=6
CHUNK_CHARS=4500
CHUNK_OVERLAP=600
OCR_ENABLED=true
OCR_LANG=deu+eng
OCR_DPI=200
OCR_MIN_TEXT_CHARS=40
```

`LMSTUDIO_MODEL` must match a model identifier accepted by your inference server.

## Import PDFs

Copy one or more PDF files into:

```text
data/incoming/
```

The running service automatically detects and processes them.

```text
PDF
 |
 +--> SHA-256 duplicate check
 |
 +--> Text extraction / OCR
 |
 +--> Classification
 |
 +--> Chunking
 |
 +--> Embeddings
 |
 +--> Qdrant
 |
 +--> data/processed/
```

If processing fails:

```text
data/failed/
```

For the first test, start with one or two PDFs and watch the logs:

```powershell
.\logs.ps1
```

or:

```bash
docker compose logs -f pdf-rag
```

A `GET /health ... 200 OK` entry only means that the service is healthy. It does **not** indicate that a PDF import has completed.

## Web UI and API

After startup:

| Service | Address |
|---|---|
| Web chat | `http://localhost:8787/` |
| Swagger/OpenAPI | `http://localhost:8787/docs` |
| OpenAI-compatible API | `http://localhost:8787/v1` |
| Qdrant dashboard | `http://localhost:6333/dashboard` |

The OpenAI-compatible model ID exposed by the RAG service is:

```text
local-pdf-rag
```

Supported endpoints include:

```text
GET  /v1/models
POST /v1/chat/completions
```

This makes the RAG service usable by clients that can connect to an OpenAI-compatible API.

## Test query

On Windows:

```powershell
.\test-query.ps1 "How do I harden SSH on Debian?"
```

The service searches Qdrant for relevant passages and passes those passages to the configured local LLM.

## OCR

OCR is enabled by default.

A page is sent through Tesseract when normal PDF text extraction returns fewer than `OCR_MIN_TEXT_CHARS` characters.

Default languages:

```env
OCR_LANG=deu+eng
```

This is useful for scanned manuals and books, but OCR processing is considerably slower than extracting embedded PDF text.

## Data persistence

The following directories are deliberately stored outside the containers:

```text
data/
qdrant/
logs/
```

Therefore rebuilding or replacing the application container does not automatically delete the vector database or imported files.

These runtime directories are excluded from Git by `.gitignore`. This is especially important for a public repository: **do not commit your private PDF collection, Qdrant database, logs or `.env` file.**

## Qdrant versions

This release pins the server and Python client to compatible versions:

```text
Qdrant server:  v1.19.0
qdrant-client:  1.19.0
```

Pinning both sides avoids accidental compatibility problems caused by using `latest`.

## Nexus registry

The repository includes an optional PowerShell helper for publishing the application image to a private Docker registry.

Example:

```powershell
docker login docker.nexus.int.nexploit.de
.\push-nexus.ps1
```

You can override the target:

```powershell
.\push-nexus.ps1 `
  -Registry "docker.example.internal" `
  -Repository "local-pdf-rag" `
  -Version "1.1.0"
```

For a public GitHub repository, replace private hostnames in examples if you do not want to disclose internal naming conventions.

## Project structure

```text
local-pdf-rag/
├── app/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── .env.example
├── .gitignore
├── docker-compose.yml
├── install.ps1
├── start.ps1
├── stop.ps1
├── logs.ps1
├── test-query.ps1
├── push-nexus.ps1
├── VERSION.txt
├── LICENSE
└── README.md
```

Runtime directories are created locally and are not part of the Git repository.

## Security notes

The service is primarily intended for trusted local networks.

Before exposing port `8787` outside the host or LAN, add appropriate controls such as:

- TLS
- Authentication
- Reverse proxy access controls
- Firewall rules
- Rate limiting

Do not expose Qdrant port `6333` directly to the Internet.

PDF files can contain sensitive or copyrighted information. Only index documents you are authorized to store and process.

## Troubleshooting

Check container state:

```bash
docker compose ps
```

Follow application logs:

```bash
docker compose logs -f pdf-rag
```

Follow Qdrant logs:

```bash
docker compose logs -f qdrant
```

Rebuild the application image:

```bash
docker compose build --no-cache pdf-rag
docker compose up -d
```

Check the local API:

```text
http://localhost:8787/health
```

If Docker Desktop returns an error involving `dockerDesktopLinuxEngine`, fix the Docker/WSL engine first. The installer cannot build the application image while the Docker daemon is unavailable.

## License

MIT License. See [LICENSE](LICENSE).

## Disclaimer

This project is provided for local knowledge retrieval and educational/administrative use. Content retrieved from PDFs can be outdated, incomplete or incorrect. Validate commands and security guidance before using them in production environments.
