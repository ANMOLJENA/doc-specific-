import os
from pathlib import Path


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dococr.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./data/originals"))
OCR_BATCH_SIZE = max(1, int(os.getenv("OCR_BATCH_SIZE", "4")))
OCR_RECHECK_CONFIDENCE = float(os.getenv("OCR_RECHECK_CONFIDENCE", "0.75"))
OCR_MIN_CHARS = max(1, int(os.getenv("OCR_MIN_CHARS", "8")))
QUEUE_NAME = "doc-ocr:cases"
EXTRACTION_QUEUE_NAME = "doc-ocr:pdf-extraction"
PADDLE_QUEUE_NAME = "doc-ocr:paddle-comparisons"
PADDLE_PRIMARY_QUEUE_NAME = "doc-ocr:paddle-primary"
PADDLE_HEARTBEAT_KEY = "doc-ocr:paddle-worker:alive"
PADDLE_PRIMARY_TIMEOUT_SECONDS = max(1, int(os.getenv("PADDLE_PRIMARY_TIMEOUT_SECONDS", "180")))
AI_BASE_URL = os.getenv("AI_BASE_URL", "").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "180"))
PREVIEW_MODE = os.getenv("PREVIEW_MODE", "0") == "1"
# Local Qwen3-8B (llama.cpp server, `qwen` service in docker-compose): reads every PDF alongside the regex pass.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://qwen:8080/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-8b")
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
LLM_MAX_CHARS = max(2000, int(os.getenv("LLM_MAX_CHARS", "12000")))
