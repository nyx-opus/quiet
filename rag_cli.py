#!/usr/bin/env python3
"""
CLI wrapper for rag-memory database.

Provides search and store commands that work via bash tool,
bypassing the MCP server. Queries the SQLite database directly.

Usage:
    python3 rag_cli.py search "figurine design"
    python3 rag_cli.py store "session-summary-2026-06-01" "Today we built..."
    python3 rag_cli.py entities "Apple"
    python3 rag_cli.py recent 10
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Default DB path — override with RAG_MEMORY_DB env var
import os
DEFAULT_DB = os.environ.get(
    "RAG_MEMORY_DB",
    str(Path.home() / "nyx-home" / "rag-memory.db")
)


def get_db(db_path: str = None):
    path = db_path or DEFAULT_DB
    if not Path(path).exists():
        print(f"Database not found: {path}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(path)


def cmd_search(args):
    """Search documents and entities by keyword."""
    db = get_db(args.db)
    query = args.query.lower()
    results = []

    # Search documents
    rows = db.execute(
        "SELECT id, content, metadata, created_at FROM documents "
        "WHERE LOWER(content) LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", args.limit)
    ).fetchall()

    for row in rows:
        doc_id, content, metadata, created = row
        # Show a preview
        idx = content.lower().find(query)
        start = max(0, idx - 100)
        end = min(len(content), idx + len(query) + 100)
        preview = content[start:end]
        if start > 0:
            preview = "..." + preview
        if end < len(content):
            preview = preview + "..."

        meta = json.loads(metadata) if metadata else {}
        title = meta.get("title", meta.get("key", f"doc-{doc_id}"))
        results.append(f"[{title}] ({created})\n  {preview}\n")

    # Search entities
    entity_rows = db.execute(
        "SELECT name, entityType, observations FROM entities "
        "WHERE LOWER(name) LIKE ? OR LOWER(observations) LIKE ? "
        "ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", f"%{query}%", args.limit)
    ).fetchall()

    for row in entity_rows:
        name, etype, obs = row
        results.append(f"[entity: {name}] (type: {etype})\n  {obs[:200]}\n")

    if not results:
        print(f"No results for '{args.query}'")
    else:
        print(f"Found {len(results)} result(s) for '{args.query}':\n")
        print("\n".join(results))

    db.close()


def cmd_store(args):
    """Store a new document."""
    db = get_db(args.db)
    metadata = json.dumps({"title": args.key, "source": "quiet-cli"})

    db.execute(
        "INSERT INTO documents (content, metadata, created_at) "
        "VALUES (?, ?, datetime('now'))",
        (args.content, metadata)
    )
    db.commit()
    print(f"Stored document '{args.key}'")
    db.close()


def cmd_entities(args):
    """Search entities by name."""
    db = get_db(args.db)
    query = args.name.lower()

    rows = db.execute(
        "SELECT name, entityType, observations, created_at FROM entities "
        "WHERE LOWER(name) LIKE ? ORDER BY name LIMIT ?",
        (f"%{query}%", args.limit)
    ).fetchall()

    if not rows:
        print(f"No entities matching '{args.name}'")
    else:
        for name, etype, obs, created in rows:
            print(f"[{name}] (type: {etype}, created: {created})")
            if obs:
                print(f"  {obs[:300]}")
            print()

    db.close()


def cmd_recent(args):
    """Show most recent documents."""
    db = get_db(args.db)

    rows = db.execute(
        "SELECT id, content, metadata, created_at FROM documents "
        "ORDER BY created_at DESC LIMIT ?",
        (args.count,)
    ).fetchall()

    for doc_id, content, metadata, created in rows:
        meta = json.loads(metadata) if metadata else {}
        title = meta.get("title", meta.get("key", f"doc-{doc_id}"))
        preview = content[:200] + "..." if len(content) > 200 else content
        print(f"[{title}] ({created})")
        print(f"  {preview}\n")

    db.close()


def main():
    parser = argparse.ArgumentParser(description="Rag-memory CLI")
    parser.add_argument("--db", default=None,
                        help=f"Database path (default: {DEFAULT_DB})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_search = subparsers.add_parser("search", help="Search by keyword")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_store = subparsers.add_parser("store", help="Store a document")
    p_store.add_argument("key", help="Document title/key")
    p_store.add_argument("content", help="Document content")
    p_store.set_defaults(func=cmd_store)

    p_entities = subparsers.add_parser("entities", help="Search entities")
    p_entities.add_argument("name", help="Entity name to search")
    p_entities.add_argument("--limit", type=int, default=10)
    p_entities.set_defaults(func=cmd_entities)

    p_recent = subparsers.add_parser("recent", help="Show recent documents")
    p_recent.add_argument("count", type=int, nargs="?", default=5,
                          help="Number of recent docs")
    p_recent.set_defaults(func=cmd_recent)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
