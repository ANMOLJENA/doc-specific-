import json
import time
import uuid

from app.config import (PADDLE_HEARTBEAT_KEY, PADDLE_PRIMARY_QUEUE_NAME,
                        PADDLE_PRIMARY_TIMEOUT_SECONDS, PADDLE_QUEUE_NAME,
QUEUE_NAME, EXTRACTION_QUEUE_NAME, REDIS_URL)


class CaseCancelled(RuntimeError):
    pass


def cancel_key(case_id: str) -> str:
    return f"doc-ocr:cancel:{case_id}"


def request_case_cancel(case_id: str) -> None:
    redis = client()
    redis.setex(cancel_key(case_id), 86400, "1")
    redis.lrem(QUEUE_NAME, 0, json.dumps({"case_id": case_id}))


def clear_case_cancel(case_id: str) -> None:
    client().delete(cancel_key(case_id))


def case_cancelled(case_id: str) -> bool:
    return bool(client().exists(cancel_key(case_id)))


def client():
    from redis import Redis
    return Redis.from_url(REDIS_URL, decode_responses=True)


def enqueue_case(case_id: str) -> None:
    client().rpush(QUEUE_NAME, json.dumps({"case_id": case_id}))


def enqueue_extraction(job_id: str) -> None:
    client().rpush(EXTRACTION_QUEUE_NAME, json.dumps({"job_id": job_id}))


def enqueue_paddle_comparison(case_id: str, page: int) -> None:
    client().rpush(PADDLE_QUEUE_NAME, json.dumps({"case_id": case_id, "page": page}))


def request_paddle_ocr(storage_key: str, cancel_check=None) -> dict:
    """Ask the local Paddle worker to read one original; fail to Surya on timeout."""
    redis = client()
    if not redis.exists(PADDLE_HEARTBEAT_KEY):
        raise RuntimeError("Paddle OCR worker is not available")
    reply_key = f"doc-ocr:paddle-reply:{uuid.uuid4()}"
    redis.rpush(PADDLE_PRIMARY_QUEUE_NAME, json.dumps({
        "storage_key": storage_key, "reply_key": reply_key,
        "expires_at": time.time() + PADDLE_PRIMARY_TIMEOUT_SECONDS,
    }))
    deadline = time.monotonic() + PADDLE_PRIMARY_TIMEOUT_SECONDS
    reply = None
    while time.monotonic() < deadline:
        if cancel_check and cancel_check():
            redis.delete(reply_key)
            raise CaseCancelled("OCR run cancelled by user")
        reply = redis.blpop(reply_key, timeout=1)
        if reply:
            break
    if not reply:
        raise TimeoutError("Paddle OCR did not respond before the fallback timeout")
    payload = json.loads(reply[1])
    if payload.get("error"):
        raise RuntimeError("Paddle OCR failed")
    return payload
