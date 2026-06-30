from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import PurePosixPath
from typing import Any


TEXT_EXTENSIONS = {".json", ".jsonl", ".txt", ".md", ".markdown"}


def stable_id(*parts: str) -> str:
    text = "\n".join(str(part) for part in parts if part is not None)
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:24]


def parse_export_upload(name: str, data: bytes) -> list[dict[str, Any]]:
    lower = name.lower()
    if lower.endswith(".zip"):
        return _parse_zip(name, data)
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".gz")):
        parsed = _parse_tar_or_gzip(name, data)
        if parsed:
            return parsed
    return _parse_document(name, data)


def parse_memory_payloads_upload(name: str, data: bytes) -> list[Any]:
    lower = name.lower()
    if lower.endswith(".zip"):
        payloads: list[Any] = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or _skip_archive_member(info.filename):
                    continue
                with archive.open(info) as source:
                    payloads.extend(_memory_payload_from_document(info.filename, source.read()))
        return payloads
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".gz")):
        try:
            payloads = []
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                for member in archive.getmembers():
                    if not member.isfile() or _skip_archive_member(member.name):
                        continue
                    source = archive.extractfile(member)
                    if source:
                        payloads.extend(_memory_payload_from_document(member.name, source.read()))
            return payloads
        except tarfile.TarError:
            if lower.endswith(".gz"):
                try:
                    return _memory_payload_from_document(name[:-3] or "decompressed", gzip.decompress(data))
                except OSError:
                    return []
    return _memory_payload_from_document(name, data)


def _parse_zip(name: str, data: bytes) -> list[dict[str, Any]]:
    conversations: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        known_export = _looks_like_claude_export([info.filename for info in archive.infolist()])
        for info in archive.infolist():
            if info.is_dir():
                continue
            if _skip_archive_member(info.filename):
                continue
            if not _should_parse_member(info.filename, known_export):
                continue
            with archive.open(info) as source:
                conversations.extend(_parse_document(f"{name}/{info.filename}", source.read()))
    return conversations


def _parse_tar_or_gzip(name: str, data: bytes) -> list[dict[str, Any]]:
    conversations: list[dict[str, Any]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            known_export = _looks_like_claude_export([member.name for member in members])
            for member in members:
                if not member.isfile():
                    continue
                if _skip_archive_member(member.name):
                    continue
                if not _should_parse_member(member.name, known_export):
                    continue
                source = archive.extractfile(member)
                if source:
                    conversations.extend(_parse_document(f"{name}/{member.name}", source.read()))
            return conversations
    except tarfile.TarError:
        pass
    if name.lower().endswith(".gz"):
        try:
            return _parse_document(name[:-3] or "decompressed", gzip.decompress(data))
        except OSError:
            return []
    return []


def _parse_document(name: str, data: bytes) -> list[dict[str, Any]]:
    if _skip_archive_member(name):
        return []
    text = data.decode("utf-8", "replace")
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".jsonl":
        return _parse_jsonl(name, text)
    if suffix == ".json":
        try:
            return _parse_json_payload(name, json.loads(text))
        except json.JSONDecodeError:
            return _parse_plain(name, text)
    return _parse_plain(name, text)


def _memory_payload_from_document(name: str, data: bytes) -> list[Any]:
    if _skip_archive_member(name) or PurePosixPath(name).suffix.lower() != ".json":
        return []
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return []
    if PurePosixPath(name).name.lower() == "memories.json" and _looks_like_memory_payload(payload):
        return [payload]
    project_memory = _project_memory_payload(name, payload)
    if project_memory:
        return [project_memory]
    return []


def _project_memory_payload(name: str, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if not {"uuid", "name", "prompt_template"} & set(payload):
        return None
    project_id = str(payload.get("uuid") or stable_id(name, payload.get("name", "")))
    title = str(payload.get("name") or PurePosixPath(name).stem)
    parts = [f"Project: {title}", f"ID: {project_id}"]
    description = _text(payload.get("description")).strip()
    if description:
        parts.append(f"Description:\n{description}")
    prompt = _text(payload.get("prompt_template")).strip()
    if prompt:
        parts.append(f"Prompt template:\n{prompt}")
    docs = payload.get("docs")
    if isinstance(docs, list):
        doc_parts = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            filename = _text(doc.get("filename")).strip() or "document"
            content = _text(doc.get("content")).strip()
            if content:
                doc_parts.append(f"{filename}:\n{content}")
        if doc_parts:
            parts.append("Project docs:\n" + "\n\n".join(doc_parts))
    content = "\n\n".join(part for part in parts if part).strip()
    if not content:
        return None
    return {"project_memories": {project_id: content}}


def _looks_like_memory_payload(payload: Any) -> bool:
    if isinstance(payload, dict):
        return "conversations_memory" in payload or "project_memories" in payload
    if isinstance(payload, list):
        return any(_looks_like_memory_payload(item) for item in payload)
    return False


def _skip_archive_member(name: str) -> bool:
    path = PurePosixPath(name)
    parts = set(path.parts)
    base = path.name
    return "__MACOSX" in parts or base.startswith("._") or base in {".DS_Store"}


def _looks_like_claude_export(names: list[str]) -> bool:
    normalized = {PurePosixPath(name).name.lower() for name in names}
    return "conversations.json" in normalized or "memories.json" in normalized or any(
        "design_chats/" in name.replace("\\", "/") and name.lower().endswith(".json")
        for name in names
    )


def _should_parse_member(name: str, known_export: bool) -> bool:
    path = PurePosixPath(name)
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        return False
    if not known_export:
        return True
    normalized = path.as_posix().lower()
    if path.name.lower() in {"conversations.json", "memories.json"}:
        return True
    if "/design_chats/" in f"/{normalized}" and suffix == ".json":
        return True
    if "/projects/" in f"/{normalized}" and suffix == ".json":
        return True
    return False


def _parse_jsonl(name: str, text: str) -> list[dict[str, Any]]:
    conversations: list[dict[str, Any]] = []
    loose_messages: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = _parse_json_payload(f"{name}:{index + 1}", item)
        if parsed:
            conversations.extend(parsed)
        elif isinstance(item, dict):
            loose_messages.append(_message(item, index))
    if loose_messages:
        conversations.append(_conversation(name, {"messages": loose_messages, "source_type": "jsonl"}))
    return conversations


def _parse_json_payload(name: str, payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        if all(isinstance(item, dict) and _looks_like_message(item) for item in payload):
            return [_conversation(name, {"messages": [_message(item, i) for i, item in enumerate(payload)]})]
        result: list[dict[str, Any]] = []
        for index, item in enumerate(payload):
            if isinstance(item, dict):
                result.extend(_parse_object(f"{name}#{index + 1}", item))
        return result
    if isinstance(payload, dict):
        return _parse_object(name, payload)
    return []


def _parse_object(name: str, obj: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(obj.get("mapping"), dict):
        return [_chatgpt_conversation(name, obj)]
    if isinstance(obj.get("chat_messages"), list):
        return [_claude_conversation(name, obj, "chat_messages")]
    if isinstance(obj.get("messages"), list):
        return [_claude_conversation(name, obj, "messages")]
    for key in ("conversations", "chats", "items", "data"):
        if isinstance(obj.get(key), list):
            return _parse_json_payload(f"{name}/{key}", obj[key])
    if _looks_like_message(obj):
        return [_conversation(name, {"messages": [_message(obj, 0)]})]
    return []


def _claude_conversation(name: str, obj: dict[str, Any], key: str) -> dict[str, Any]:
    messages = [_message(item, i) for i, item in enumerate(obj.get(key, [])) if isinstance(item, dict)]
    return _conversation(
        name,
        {
            "source_id": str(obj.get("uuid") or obj.get("id") or stable_id(name)),
            "title": obj.get("name") or obj.get("title"),
            "summary": obj.get("summary") or "",
            "created_at": obj.get("created_at") or obj.get("createdAt"),
            "updated_at": obj.get("updated_at") or obj.get("updatedAt"),
            "messages": messages,
            "source_type": "claude-export",
        },
    )


def _chatgpt_conversation(name: str, obj: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    for node in obj.get("mapping", {}).values():
        if isinstance(node, dict) and isinstance(node.get("message"), dict):
            nodes.append((node["message"].get("create_time") or 0, node["message"]))
    nodes.sort(key=lambda item: item[0] or 0)
    return _conversation(
        name,
        {
            "source_id": str(obj.get("id") or obj.get("conversation_id") or stable_id(name, obj.get("title", ""))),
            "title": obj.get("title"),
            "created_at": _timestamp_to_iso(obj.get("create_time")),
            "updated_at": _timestamp_to_iso(obj.get("update_time")),
            "messages": [_chatgpt_message(item, i) for i, (_, item) in enumerate(nodes)],
            "source_type": "chatgpt-export",
        },
    )


def _plain(name: str, role: str, body: str, index: int) -> dict[str, Any]:
    return {
        "source_id": stable_id(name, str(index), body[:120]),
        "role": _role(role),
        "text": body.strip(),
        "thinking": "",
        "attachments": [],
        "timestamp": None,
    }


def _parse_plain(name: str, text: str) -> list[dict[str, Any]]:
    parts = re.split(r"(?im)^\s*(user|human|assistant|claude|system|tool)\s*:\s*", text)
    messages: list[dict[str, Any]] = []
    if len(parts) > 1:
        if parts[0].strip():
            messages.append(_plain(name, "system", parts[0], 0))
        for index in range(1, len(parts), 2):
            body = parts[index + 1] if index + 1 < len(parts) else ""
            if body.strip():
                messages.append(_plain(name, parts[index], body, index))
    elif text.strip():
        messages.append(_plain(name, "system", text, 0))
    return [_conversation(name, {"title": PurePosixPath(name).name, "messages": messages, "source_type": "plain-text"})] if messages else []


def _conversation(name: str, data: dict[str, Any]) -> dict[str, Any]:
    messages = [
        item
        for item in data.get("messages", [])
        if item.get("text") or item.get("thinking") or item.get("traces") or item.get("attachments")
    ]
    title = re.sub(r"\s+", " ", str(data.get("title") or PurePosixPath(name).stem or "Imported chat")).strip()[:120]
    created = data.get("created_at") or next((item.get("timestamp") for item in messages if item.get("timestamp")), None)
    updated = data.get("updated_at") or next((item.get("timestamp") for item in reversed(messages) if item.get("timestamp")), created)
    return {
        "source_id": data.get("source_id") or stable_id(name, title, str(created), str(len(messages))),
        "title": title or "Imported chat",
        "summary": data.get("summary") or "",
        "source": name,
        "source_type": data.get("source_type") or "imported",
        "created_at": created,
        "updated_at": updated,
        "messages": messages,
    }


def _message(message: dict[str, Any], index: int) -> dict[str, Any]:
    nested = message.get("content") if isinstance(message.get("content"), dict) else {}
    content = message.get("content")
    text = _visible_text(message, nested, content)
    thinking, thinking_summary, traces = _thinking_and_traces(content)
    role = _role(message.get("role") or nested.get("role") or message.get("sender") or message.get("author"))
    timestamp = message.get("created_at") or message.get("createdAt") or message.get("timestamp") or nested.get("timestamp")
    attachments = _attachments(message.get("attachments")) + _attachments(nested.get("attachments"))
    if thinking_summary:
        traces.insert(0, {"type": "summary", "text": thinking_summary})
    return {
        "source_id": str(message.get("uuid") or message.get("id") or nested.get("id") or stable_id(role, str(index), text[:120])),
        "role": role,
        "text": text.strip(),
        "thinking": thinking,
        "attachments": attachments,
        "traces": traces,
        "timestamp": timestamp,
    }


def _chatgpt_message(message: dict[str, Any], index: int) -> dict[str, Any]:
    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), dict) else {}
    parts = content.get("parts")
    text = "\n\n".join(_text(part) for part in parts if _text(part)) if isinstance(parts, list) else _text(content)
    return {
        "source_id": str(message.get("id") or stable_id(str(index), text[:120])),
        "role": _role(author.get("role")),
        "text": text.strip(),
        "thinking": "",
        "attachments": [],
        "traces": [],
        "timestamp": _timestamp_to_iso(message.get("create_time")) or message.get("created_at"),
    }


def _visible_text(message: dict[str, Any], nested: dict[str, Any], content: Any) -> str:
    if isinstance(content, list):
        block_text = "\n\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        if block_text.strip():
            return block_text
    direct = message.get("text") or nested.get("content") or nested.get("text")
    if isinstance(direct, str) and direct.strip():
        return direct
    return _text(content) or _text(message.get("message")) or ""


def _thinking_and_traces(content: Any) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(content, list):
        return "", "", []
    thinking_parts: list[str] = []
    summaries: list[str] = []
    traces: list[dict[str, Any]] = []
    has_tool_trace = False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "thinking":
            thought = _trim(block.get("thinking"))
            block_summaries: list[str] = []
            if thought:
                thinking_parts.append(thought)
            for item in block.get("summaries") or []:
                if isinstance(item, dict) and _trim(item.get("summary")):
                    block_summaries.append(_trim(item.get("summary")))
                elif _trim(item):
                    block_summaries.append(_trim(item))
            summaries.extend(block_summaries)
            if thought:
                traces.append({
                    "type": "thinking",
                    "id": block.get("id") or stable_id("thinking", thought[:160], str(len(traces))),
                    "text": thought,
                    "summary": block_summaries[-1] if block_summaries else "",
                })
        elif block_type == "tool_use":
            has_tool_trace = True
            traces.append({
                "type": "tool_use",
                "id": block.get("id") or block.get("tool_use_id") or stable_id(block.get("name"), json.dumps(block.get("input"), ensure_ascii=False, sort_keys=True)),
                "name": block.get("name") or block.get("message") or "tool",
                "input": block.get("input"),
            })
        elif block_type == "tool_result":
            has_tool_trace = True
            traces.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id") or block.get("id"),
                "name": block.get("name") or block.get("message") or "tool",
                "content": _text(block.get("content"))[:4000],
                "is_error": bool(block.get("is_error")),
            })
    if has_tool_trace:
        return "", "", traces
    return "\n\n".join(thinking_parts).strip(), (summaries[-1] if summaries else ""), []


def _trim(value: Any) -> str:
    return str(value or "").strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n\n".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), str):
            return value["content"]
        if value.get("type") == "tool_use":
            return "[tool_use] " + str(value.get("name") or "tool")
        if value.get("type") == "tool_result":
            return "[tool_result]\n" + _text(value.get("content"))
        return "\n\n".join(part for part in (_text(item) for item in value.values()) if part)
    return ""


def _attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "name": item.get("name") or item.get("filename") or item.get("file_name") or "attachment",
                "path": item.get("path") or item.get("url") or "",
                "mime": item.get("mime_type") or item.get("type") or "",
                "is_image": str(item.get("mime_type") or item.get("type") or "").startswith("image/"),
            }
        )
    return result


def _role(value: Any) -> str:
    role = str(value or "").lower()
    if role in {"assistant", "claude", "model", "bot"}:
        return "assistant"
    return "user"


def _looks_like_message(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("role", "sender", "author")) and any(key in value for key in ("content", "text", "message"))


def _timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)
