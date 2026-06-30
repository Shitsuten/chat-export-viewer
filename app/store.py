import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent))
DB_PATH = ROOT / "conversations.db"


class ConversationNotFound(LookupError):
    pass


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_store() -> None:
    with _connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conv_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                starred INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                latest_session_id TEXT
            );
            CREATE TABLE IF NOT EXISTS session_aliases (
                session_id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE,
                source_id TEXT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                text TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                traces_json TEXT NOT NULL DEFAULT '[]',
                timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conv_id
                ON messages(conv_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS messages_source_id
                ON messages(conv_id, source_id)
                WHERE source_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS messages_source_id_lookup
                ON messages(source_id)
                WHERE source_id IS NOT NULL;
            """
        )
        message_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "attachments_json" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'
                """
            )
        if "traces_json" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN traces_json TEXT NOT NULL DEFAULT '[]'
                """
            )


def conversation_list() -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """
            SELECT conv_id, title, starred, created_at, updated_at,
                   latest_session_id
            FROM conversations
            ORDER BY starred DESC, updated_at DESC
            """
        ).fetchall()
    def api_time(value: str) -> str | int:
        return int(value) if value and value.isdigit() else value

    return [
        {
            "conv_id": row["conv_id"],
            "session_id": row["conv_id"],
            "title": row["title"],
            "starred": bool(row["starred"]),
            "created_at": api_time(row["created_at"]),
            "last_modified": api_time(row["updated_at"]),
            "updated_at": api_time(row["updated_at"]),
            "latest_session_id": row["latest_session_id"],
        }
        for row in rows
    ]


def resolve_conversation(identifier: str | None) -> str | None:
    if not identifier:
        return None
    with _connect() as db:
        row = db.execute(
            "SELECT conv_id FROM conversations WHERE conv_id = ?",
            (identifier,),
        ).fetchone()
        if row:
            return row["conv_id"]
        row = db.execute(
            "SELECT conv_id FROM session_aliases WHERE session_id = ?",
            (identifier,),
        ).fetchone()
        return row["conv_id"] if row else None


def ensure_conversation(
    conversation_id: str | None = None,
    title: str = "新对话",
) -> str:
    conv_id = resolve_conversation(conversation_id)
    if conversation_id and not conv_id:
        raise ConversationNotFound("conversation not found")
    if conv_id:
        return conv_id
    conv_id = str(uuid.uuid4())
    now = _now()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO conversations
                (conv_id, title, starred, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (conv_id, title[:120] or "新对话", now, now),
        )
    return conv_id


def begin_turn(
    message: str,
    conversation_id: str | None = None,
    legacy_session_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    conv_id = resolve_conversation(conversation_id or legacy_session_id)
    now = _now()
    with _connect() as db:
        if conv_id:
            row = db.execute(
                "SELECT latest_session_id FROM conversations WHERE conv_id = ?",
                (conv_id,),
            ).fetchone()
            if not row:
                raise ConversationNotFound("conversation not found")
            resume_id = row["latest_session_id"]
        else:
            conv_id = str(uuid.uuid4())
            resume_id = None
            db.execute(
                """
                INSERT INTO conversations
                    (conv_id, title, starred, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (conv_id, message.strip()[:120] or "新对话", now, now),
            )
        title_row = db.execute(
            "SELECT title FROM conversations WHERE conv_id = ?",
            (conv_id,),
        ).fetchone()
        if title_row and title_row["title"] == "新对话":
            title = message.strip()[:120]
            if not title and attachments:
                title = attachments[0].get("name", "附件")
            db.execute(
                "UPDATE conversations SET title = ? WHERE conv_id = ?",
                (title or "新对话", conv_id),
            )
        db.execute(
            """
            INSERT INTO messages(
                conv_id, role, text, thinking, attachments_json, timestamp
            )
            VALUES (?, 'user', ?, '', ?, ?)
            """,
            (
                conv_id,
                message,
                json.dumps(attachments or [], ensure_ascii=False),
                now,
            ),
        )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, conv_id),
        )
    return conv_id, resume_id


def complete_turn(
    conv_id: str,
    session_id: str,
    text: str,
    thinking: str,
    traces: list | None = None,
) -> None:
    now = _now()
    with _connect() as db:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE conv_id = ?",
            (conv_id,),
        ).fetchone():
            raise ConversationNotFound("conversation not found")
        db.execute(
            """
            UPDATE conversations
            SET latest_session_id = ?, updated_at = ?
            WHERE conv_id = ?
            """,
            (session_id, now, conv_id),
        )
        db.execute(
            """
            INSERT INTO session_aliases(session_id, conv_id)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET conv_id = excluded.conv_id
            """,
            (session_id, conv_id),
        )
        traces_str = json.dumps(traces or [], ensure_ascii=False)
        db.execute(
            """
            INSERT INTO messages(
                conv_id, role, text, thinking, attachments_json, traces_json, timestamp
            )
            VALUES (?, 'assistant', ?, ?, '[]', ?, ?)
            """,
            (conv_id, text, thinking, traces_str, now),
        )


def import_conversations(conversations: list[dict[str, Any]]) -> dict[str, Any]:
    imported = 0
    imported_messages = 0
    updated_messages = 0
    with _connect() as db:
        for conversation in conversations:
            messages = conversation.get("messages") or []
            if not messages:
                continue
            conversation_source = conversation.get("source_id")
            existing_conv_ids: set[str] = set()
            if conversation_source:
                prefix = f"import:{conversation_source}:%"
                existing_conv_ids = {
                    row["conv_id"]
                    for row in db.execute(
                        "SELECT DISTINCT conv_id FROM messages WHERE source_id LIKE ?",
                        (prefix,),
                    ).fetchall()
                }
            if existing_conv_ids:
                for message in messages:
                    message_source = message.get("source_id")
                    if not message_source:
                        continue
                    source_id = f"import:{conversation_source}:{message_source}"
                    traces = message.get("traces") or []
                    cursor = db.execute(
                        """
                        UPDATE messages
                        SET thinking = CASE
                                WHEN ? != '' THEN ?
                                ELSE thinking
                            END,
                            traces_json = CASE
                                WHEN ? != '[]' THEN ?
                                ELSE traces_json
                            END
                        WHERE source_id = ?
                        """,
                        (
                            str(message.get("thinking") or ""),
                            str(message.get("thinking") or ""),
                            json.dumps(traces, ensure_ascii=False),
                            json.dumps(traces, ensure_ascii=False),
                            source_id,
                        ),
                    )
                    updated_messages += cursor.rowcount
                continue
            conv_id = str(uuid.uuid4())
            created_at = str(conversation.get("created_at") or _now())
            updated_at = str(conversation.get("updated_at") or created_at)
            title = str(conversation.get("title") or "Imported chat").strip()[:120] or "Imported chat"
            source_type = conversation.get("source_type")
            if source_type:
                title = f"{title}"
            db.execute(
                """
                INSERT INTO conversations
                    (conv_id, title, starred, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (conv_id, title, created_at, updated_at),
            )
            for index, message in enumerate(messages):
                role = message.get("role") if message.get("role") in {"user", "assistant"} else "user"
                text = str(message.get("text") or "")
                thinking = str(message.get("thinking") or "")
                attachments = message.get("attachments") or []
                traces = message.get("traces") or []
                timestamp = str(message.get("timestamp") or created_at)
                source_id = message.get("source_id")
                if source_id:
                    source_id = f"import:{conversation.get('source_id') or conv_id}:{source_id}"
                else:
                    source_id = f"import:{conversation.get('source_id') or conv_id}:{index}"
                db.execute(
                    """
                    INSERT OR IGNORE INTO messages(
                        conv_id, source_id, role, text, thinking,
                        attachments_json, traces_json, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conv_id,
                        source_id,
                        role,
                        text,
                        thinking,
                        json.dumps(attachments, ensure_ascii=False),
                        json.dumps(traces, ensure_ascii=False),
                        timestamp,
                    ),
                )
                imported_messages += 1
            imported += 1
    return {"imported": imported, "messages": imported_messages, "updatedMessages": updated_messages}


def conversation_messages(
    conv_id: str,
    before_id: int | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    page_size = max(1, min(limit or 0, 200)) if limit else None
    with _connect() as db:
        if page_size:
            params: list[Any] = [resolved]
            before_clause = ""
            if before_id is not None:
                before_clause = "AND id < ?"
                params.append(before_id)
            params.append(page_size + 1)
            rows = db.execute(
                f"""
                SELECT id, role, text, thinking, attachments_json, traces_json, timestamp
                FROM messages
                WHERE conv_id = ?
                {before_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            has_more = len(rows) > page_size
            rows = rows[:page_size]
            rows = list(reversed(rows))
        else:
            rows = db.execute(
                """
                SELECT id, role, text, thinking, attachments_json, traces_json, timestamp
                FROM messages
                WHERE conv_id = ?
                ORDER BY id
                """,
                (resolved,),
            ).fetchall()
            has_more = False
    messages = []
    for row in rows:
        item = dict(row)
        try:
            item["attachments"] = json.loads(item.pop("attachments_json"))
        except (json.JSONDecodeError, TypeError):
            item["attachments"] = []
        try:
            item["traces"] = json.loads(item.pop("traces_json"))
        except (json.JSONDecodeError, TypeError):
            item["traces"] = []
        messages.append(item)
    next_before_id = messages[0]["id"] if has_more and messages else None
    return messages, has_more, next_before_id


def rename_conversation(conv_id: str, title: str) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _connect() as db:
        db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE conv_id = ?",
            (title.strip(), _now(), resolved),
        )


def star_conversation(conv_id: str, starred: bool) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _connect() as db:
        db.execute(
            "UPDATE conversations SET starred = ? WHERE conv_id = ?",
            (int(starred), resolved),
        )


def delete_conversation(conv_id: str) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _connect() as db:
        db.execute("DELETE FROM conversations WHERE conv_id = ?", (resolved,))
