"""
Quiet memory — vector-based conversational recall.

Dropped conversation turns get chunked, embedded, and stored.
Search returns the most relevant fragments by cosine similarity.

Uses all-MiniLM-L12-v2 (384-dim) for embeddings, numpy for
similarity, and SQLite for storage. No external services needed.

Usage:
    from memory import ingest_messages, search

    # After trimming context:
    ingest_messages(dropped_messages, source="session-abc")

    # Before a turn, to surface relevant memories:
    results = search("the porch system", top_k=5)
"""

import json
import logging
import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("quiet.memory")

# ── paths ──────────────────────────────────────────────────────
DB_PATH = Path.home() / "quiet" / "memory.db"

# ── model (lazy-loaded) ───────────────────────────────────────
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L12-v2")
        log.info("Loaded embedding model (dim=%d)", _model.get_embedding_dimension())
    return _model


# ── database ──────────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT,
            speaker     TEXT,
            timestamp   TEXT,
            text        TEXT NOT NULL,
            embedding   BLOB NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_source
        ON chunks(source)
    """)
    db.commit()
    return db


# ── embedding helpers ─────────────────────────────────────────
def _embed(texts: list[str]) -> np.ndarray:
    """Embed a list of strings, returns (N, 384) float32 array."""
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True,
                        show_progress_bar=False)


def _blob(vec: np.ndarray) -> bytes:
    """Pack a 1-D float32 array into bytes for SQLite."""
    return vec.astype(np.float32).tobytes()


def _unblob(data: bytes) -> np.ndarray:
    """Unpack bytes back to a float32 array."""
    return np.frombuffer(data, dtype=np.float32)


# ── chunking ──────────────────────────────────────────────────
def _extract_text(msg: dict) -> str:
    """Pull the text content out of a message dict."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


def _chunk_messages(messages: list[dict]) -> list[dict]:
    """Convert message dicts into chunk dicts ready for embedding.

    Groups user→assistant pairs where possible, since the exchange
    is more meaningful than either message alone. Single messages
    (e.g. a user message without a response) become solo chunks.
    """
    chunks = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        text = _extract_text(msg)
        ts = msg.get("timestamp", "")

        if not text.strip():
            i += 1
            continue

        # Try to pair user + assistant
        if (role == "user" and i + 1 < len(messages)
                and messages[i + 1].get("role") == "assistant"):
            next_msg = messages[i + 1]
            next_text = _extract_text(next_msg)
            if next_text.strip():
                combined = f"[Amy]: {text}\n\n[Nyx]: {next_text}"
                chunks.append({
                    "speaker": "exchange",
                    "timestamp": ts or msg.get("created_at", ""),
                    "text": combined,
                })
                i += 2
                continue

        # Solo message
        speaker = "Amy" if role == "user" else "Nyx" if role == "assistant" else role
        chunks.append({
            "speaker": speaker,
            "timestamp": ts or msg.get("created_at", ""),
            "text": f"[{speaker}]: {text}",
        })
        i += 1

    return chunks


# ── public API ────────────────────────────────────────────────
def ingest_messages(messages: list[dict], source: str = "trim") -> int:
    """Chunk, embed, and store a list of conversation messages.

    Returns the number of chunks stored.
    """
    chunks = _chunk_messages(messages)
    if not chunks:
        return 0

    texts = [c["text"] for c in chunks]

    # Embed in batches of 64 to manage memory
    BATCH = 64
    all_vecs = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start:start + BATCH]
        vecs = _embed(batch)
        all_vecs.append(vecs)
    embeddings = np.vstack(all_vecs)

    db = _get_db()
    for chunk, vec in zip(chunks, embeddings):
        db.execute(
            "INSERT INTO chunks (source, speaker, timestamp, text, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, chunk["speaker"], chunk["timestamp"],
             chunk["text"], _blob(vec))
        )
    db.commit()
    db.close()
    log.info("Ingested %d chunks from source=%s", len(chunks), source)
    return len(chunks)


def ingest_jsonl(path: Path, source: str = None) -> int:
    """Ingest messages from a JSONL file (e.g. archived or backup).

    Skips the header line if it has 'model'/'identity' fields.
    Returns the number of chunks stored.
    """
    if source is None:
        source = path.stem

    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip metadata headers
            if "model" in obj and "identity" in obj and "role" not in obj:
                continue
            if obj.get("role") in ("user", "assistant"):
                messages.append(obj)

    return ingest_messages(messages, source=source)


def search(query: str, top_k: int = 5,
           min_score: float = 0.25) -> list[dict]:
    """Find the most relevant memory chunks for a query.

    Returns a list of dicts with keys: text, score, speaker,
    timestamp, source.
    """
    db = _get_db()
    rows = db.execute(
        "SELECT id, source, speaker, timestamp, text, embedding FROM chunks"
    ).fetchall()
    db.close()

    if not rows:
        return []

    # Embed the query
    q_vec = _embed([query])[0]  # (384,)

    # Cosine similarity against all chunks
    # (embeddings are already normalized, so dot product = cosine sim)
    results = []
    for row_id, source, speaker, ts, text, emb_blob in rows:
        vec = _unblob(emb_blob)
        score = float(np.dot(q_vec, vec))
        if score >= min_score:
            results.append({
                "id": row_id,
                "text": text,
                "score": score,
                "speaker": speaker,
                "timestamp": ts,
                "source": source,
            })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def stats() -> dict:
    """Return basic stats about the memory store."""
    db = _get_db()
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    sources = db.execute(
        "SELECT source, COUNT(*) FROM chunks GROUP BY source"
    ).fetchall()
    db.close()
    return {
        "total_chunks": total,
        "sources": {s: c for s, c in sources},
        "db_path": str(DB_PATH),
    }


# ── CLI ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 memory.py ingest <file.jsonl> [source-name]")
        print("  python3 memory.py search <query> [top_k]")
        print("  python3 memory.py stats")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "ingest":
        path = Path(sys.argv[2])
        source = sys.argv[3] if len(sys.argv) > 3 else None
        n = ingest_jsonl(path, source=source)
        print(f"Ingested {n} chunks from {path}")

    elif cmd == "search":
        query = sys.argv[2]
        top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        results = search(query, top_k=top_k)
        if not results:
            print("No results.")
        for r in results:
            print(f"[{r['score']:.3f}] ({r['source']}, {r['speaker']}, {r['timestamp'][:19] if r['timestamp'] else '?'})")
            # Show first 300 chars
            preview = r['text'][:300]
            if len(r['text']) > 300:
                preview += "..."
            print(f"  {preview}\n")

    elif cmd == "stats":
        s = stats()
        print(json.dumps(s, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
