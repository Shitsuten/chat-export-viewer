import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Any
from starlette.formparsers import MultiPartParser

MultiPartParser.max_part_size = 1024 * 1024 * 1024

from app import auth
from app.export_importer import parse_export_upload
from app.export_importer import parse_memory_payloads_upload
from app.memory import (
    MAX_MEMORY_CHARS,
    add_saved_memory,
    build_profile_context,
    import_claude_export_memories,
    import_claude_memory_payloads,
    read_memory,
    read_profile,
    write_memory,
    write_profile,
)
from app.sessions import (
    remove_session,
    session_list,
    session_messages,
    set_session_starred,
    set_session_title,
)
from app.splash import current_period, random_line
from app.store import (
    ConversationNotFound,
    begin_turn,
    complete_turn,
    conversation_messages,
    ensure_conversation,
    import_conversations,
    initialize_store,
    prepare_edit_turn,
    prepare_retry_turn,
    restore_branch,
)
from app.uploads import (
    remove_conversation_uploads,
    save_uploads,
    validated_attachments,
    validated_file,
)


logger = logging.getLogger(__name__)
STATIC = ROOT / "static"
initialize_store()


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")
OUTER_AUTH_COOKIE = "claude_outer_auth"


def outer_auth_token() -> str:
    return hmac.new(
        os.environ["CHAT_SECRET"].encode(),
        f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}:outer-v1".encode(),
        "sha256",
    ).hexdigest()


def basic_auth_ok(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    return bool(sep) and hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(password, BASIC_AUTH_PASSWORD)


@app.middleware("http")
async def outer_basic_auth(request: Request, call_next):
    if not BASIC_AUTH_USER or not BASIC_AUTH_PASSWORD or request.url.path in ("/health", "/marked.min.js", "/favicon.ico", "/static/manifest.webmanifest", "/static/css/typography-locked.css", "/static/design-system.css"):
        return await call_next(request)
    token = request.cookies.get(OUTER_AUTH_COOKIE, "")
    if token and hmac.compare_digest(token, outer_auth_token()):
        return await call_next(request)
    if not basic_auth_ok(request.headers.get("authorization", "")):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Claude"'},
        )
    response = await call_next(request)
    response.set_cookie(
        OUTER_AUTH_COOKIE,
        outer_auth_token(),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


def require_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


class AuthBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    edit_message_id: int | None = Field(default=None, ge=1)
    retry_message_id: int | None = Field(default=None, ge=1)
    model: str = Field(default="gpt-4.1-mini", max_length=128)
    effort: str = Field(default="medium", max_length=16)
    extended: bool = True
    attachments: list[str] = Field(default_factory=list, max_length=10)
    provider: str = Field(default="openai-compatible", max_length=64)
    endpoint: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=4096)
    external_model: str = Field(default="", max_length=128)
    system_prompt: str = Field(default="", max_length=40_000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=200_000)


class ToolCaptionBody(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    tool_input: Any = None
    tool_output: str = Field(default="", max_length=20000)


class MemoryBody(BaseModel):
    content: str = Field(max_length=MAX_MEMORY_CHARS)


class ProfileBody(BaseModel):
    fullName: str = Field(default="", max_length=200)
    nickname: str = Field(default="", max_length=200)
    savedMemories: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    preferences: dict[str, Any] = Field(default_factory=dict)
    claudeExportImport: dict[str, Any] = Field(default_factory=dict)
    updatedAt: int | None = None


class ThinkingSummaryBody(BaseModel):
    thinking: str = Field(min_length=1, max_length=50_000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class StarBody(BaseModel):
    starred: bool


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/static/manifest.webmanifest", include_in_schema=False)
async def manifest_webmanifest() -> Response:
    return Response(status_code=204)


@app.get("/static/css/typography-locked.css", include_in_schema=False)
async def typography_locked() -> Response:
    return Response(status_code=204)


@app.get("/marked.min.js")
async def marked_js() -> FileResponse:
    return FileResponse(
        STATIC / "marked.min.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/static/design-system.css")
async def design_system_css() -> FileResponse:
    return FileResponse(
        STATIC / "design-system.css",
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/auth")
async def login(body: AuthBody) -> dict:
    token = auth.issue_token(body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"token": token}


@app.post("/api/chat", dependencies=[Depends(require_auth)])
async def chat(body: ChatBody) -> StreamingResponse:
    branch_mode = body.edit_message_id is not None or body.retry_message_id is not None
    if body.edit_message_id is not None and body.retry_message_id is not None:
        raise HTTPException(status_code=400, detail="不能同时编辑和重试")
    if branch_mode and not (body.conversation_id or body.session_id):
        raise HTTPException(status_code=400, detail="编辑或重试缺少会话标识")
    if not branch_mode and not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="消息或附件不能为空")
    requested_conv_id = body.conversation_id or body.session_id
    if body.attachments and not requested_conv_id:
        raise HTTPException(status_code=400, detail="附件缺少会话标识")
    attachment_items = (
        validated_attachments(requested_conv_id, body.attachments)
        if body.attachments and requested_conv_id
        else []
    )
    return external_chat_stream(body, attachment_items, branch_mode=branch_mode)


def external_chat_stream(
    body: ChatBody,
    attachment_items: list[dict[str, Any]] | None = None,
    branch_mode: bool = False,
) -> StreamingResponse:
    async def sse():
        conv_id = None
        response_text = ""
        branch_id = None
        branch_committed = False
        active_attachments = attachment_items or []
        try:
            if body.edit_message_id is not None:
                branch = prepare_edit_turn(
                    body.conversation_id or body.session_id or "",
                    body.edit_message_id,
                    body.message.strip(),
                )
                conv_id = branch["conv_id"]
                branch_id = branch.get("branch_id")
                active_attachments = branch.get("attachments") or []
                user_message_id = branch["user_message_id"]
            elif body.retry_message_id is not None:
                branch = prepare_retry_turn(
                    body.conversation_id or body.session_id or "",
                    body.retry_message_id,
                )
                conv_id = branch["conv_id"]
                branch_id = branch.get("branch_id")
                active_attachments = branch.get("attachments") or []
                user_message_id = branch["user_message_id"]
            else:
                conv_id, _, user_message_id = begin_turn(
                    body.message.strip(),
                    body.conversation_id,
                    body.session_id,
                    active_attachments,
                )
            yield sse_event(
                "conversation",
                {"conversation_id": conv_id, "user_message_id": user_message_id},
            )
            response_text = call_external_chat_api(conv_id, body, active_attachments)
            for chunk in chunk_text(response_text):
                yield sse_event("delta", {"text": chunk})
                await asyncio.sleep(0)
            session_id = f"external-{uuid4()}"
            assistant_message_id = complete_turn(conv_id, session_id, response_text, "", [])
            branch_committed = True
            yield sse_event(
                "done",
                {
                    "conversation_id": conv_id,
                    "session_id": session_id,
                    "assistant_message_id": assistant_message_id,
                },
            )
        except ConversationNotFound:
            if branch_mode and not branch_committed:
                restore_branch(branch_id)
            yield sse_event("error", {"message": "会话不存在或已被删除"})
        except HTTPException as exc:
            if branch_mode and not branch_committed:
                restore_branch(branch_id)
            detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
            yield sse_event("error", {"message": detail})
        except ValueError as exc:
            if branch_mode and not branch_committed:
                restore_branch(branch_id)
            yield sse_event("error", {"message": str(exc) or "请求无效"})
        except Exception:
            if branch_mode and not branch_committed:
                restore_branch(branch_id)
            logger.exception("external chat failed")
            yield sse_event("error", {"message": "外部 API 暂时没有响应，请检查 endpoint、key 和模型名。"})

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def sse_event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def chunk_text(text: str, size: int = 48):
    for index in range(0, len(text), size):
        yield text[index:index + size]


def call_external_chat_api(
    conv_id: str,
    body: ChatBody,
    attachment_items: list[dict[str, Any]] | None = None,
) -> str:
    provider = body.provider.strip().lower()
    if provider in {"anthropic", "claude-api"}:
        return call_anthropic_api(conv_id, body, attachment_items or [])
    return call_openai_compatible_api(conv_id, body, attachment_items or [])


def recent_external_messages(
    conv_id: str,
    attachment_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    messages, _, _ = conversation_messages(conv_id, limit=60)
    result = []
    for message in messages:
        role = message.get("role")
        text = (message.get("text") or "").strip()
        if role in {"user", "assistant"} and text:
            result.append({"role": role, "content": text})
    note = attachment_context_note(attachment_items or [])
    if note:
        if result and result[-1]["role"] == "user":
            result[-1]["content"] = f"{result[-1]['content']}\n\n{note}".strip()
        else:
            result.append({"role": "user", "content": note})
    return result


def merged_system_prompt(body: ChatBody) -> str:
    parts = [
        build_profile_context(),
        body.system_prompt.strip(),
    ]
    return "\n\n".join(part for part in parts if part).strip()


def attachment_context_note(attachment_items: list[dict[str, Any]]) -> str:
    if not attachment_items:
        return ""
    lines = ["[User uploaded attachments. The pure API backend passes metadata only unless the provider supports fetching these paths.]"]
    for item in attachment_items:
        name = item.get("name") or item.get("filename") or Path(str(item.get("path", ""))).name
        mime = item.get("mime") or item.get("mime_type") or "application/octet-stream"
        path = item.get("path", "")
        lines.append(f"- {name} ({mime}) {path}")
    return "\n".join(lines)


def call_openai_compatible_api(
    conv_id: str,
    body: ChatBody,
    attachment_items: list[dict[str, Any]] | None = None,
) -> str:
    endpoint = (body.endpoint or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    api_key = body.api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("EXTERNAL_API_KEY") or ""
    model = body.external_model or body.model or os.environ.get("OPENAI_MODEL") or os.environ.get("EXTERNAL_MODEL") or "gpt-4.1-mini"
    messages = recent_external_messages(conv_id, attachment_items or [])
    system_prompt = merged_system_prompt(body)
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    result = post_json(f"{endpoint}/chat/completions", payload, headers)
    try:
        return (result["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"外部 API 返回格式无法识别：{result}") from exc


def call_anthropic_api(
    conv_id: str,
    body: ChatBody,
    attachment_items: list[dict[str, Any]] | None = None,
) -> str:
    endpoint = (body.endpoint or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    api_key = body.api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("EXTERNAL_API_KEY") or ""
    model = body.external_model or os.environ.get("ANTHROPIC_MODEL") or os.environ.get("EXTERNAL_MODEL") or "claude-sonnet-4-20250514"
    if not api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key 为空")
    payload: dict[str, Any] = {
        "model": model,
        "messages": recent_external_messages(conv_id, attachment_items or []),
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
    }
    system_prompt = merged_system_prompt(body)
    if system_prompt:
        payload["system"] = system_prompt
    result = post_json(
        f"{endpoint}/v1/messages",
        payload,
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        return "\n\n".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text").strip()
    except AttributeError as exc:
        raise HTTPException(status_code=502, detail=f"Anthropic 返回格式无法识别：{result}") from exc


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise HTTPException(status_code=exc.code, detail=detail) from exc


@app.post("/api/thinking-summary", dependencies=[Depends(require_auth)])
async def thinking_summary(body: ThinkingSummaryBody) -> dict:
    return {"summary": compact_summary(body.thinking, 42)}


@app.post("/api/tool-caption", dependencies=[Depends(require_auth)])
async def tool_caption(body: ToolCaptionBody) -> dict:
    return {"caption": compact_tool_caption(body)}


def compact_summary(text: str, limit: int = 42) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return ""
    first = re.split(r"(?<=[。！？.!?])\s+", compact, maxsplit=1)[0].strip()
    if len(first) <= limit:
        return first
    return first[: max(1, limit - 1)].rstrip() + "…"


def compact_tool_caption(body: ToolCaptionBody) -> str:
    output = re.sub(r"\s+", " ", body.tool_output or "").strip()
    if output:
        return compact_summary(f"{body.tool_name}: {output}", 56)
    if body.tool_input:
        try:
            raw_input = json.dumps(body.tool_input, ensure_ascii=False)
        except TypeError:
            raw_input = str(body.tool_input)
        return compact_summary(f"{body.tool_name}: {raw_input}", 56)
    return body.tool_name


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload(
    files: list[UploadFile] = File(...),
    conversation_id: str | None = Form(default=None),
) -> dict:
    conv_id = ensure_conversation(conversation_id)
    attachments = await save_uploads(conv_id, files)
    return {
        "conversation_id": conv_id,
        "attachments": attachments,
    }


@app.post("/api/import-export", dependencies=[Depends(require_auth)])
async def import_export(files: list[UploadFile] = File(...)) -> dict:
    parsed: list[dict[str, Any]] = []
    memory_payloads: list[Any] = []
    failures: list[dict[str, str]] = []
    for upload in files:
        data = await upload.read()
        try:
            parsed.extend(parse_export_upload(upload.filename or "upload", data))
            memory_payloads.extend(parse_memory_payloads_upload(upload.filename or "upload", data))
        except Exception as exc:
            logger.exception("import failed for %s", upload.filename)
            failures.append({"file": upload.filename or "upload", "error": str(exc)})
    result = import_conversations(parsed)
    profile_imported = profile_found = preference_updates = 0
    if memory_payloads:
        _, profile_imported, profile_found, preference_updates = import_claude_memory_payloads(memory_payloads)
    result["profileImported"] = profile_imported
    result["profileFound"] = profile_found
    result["preferenceUpdates"] = preference_updates
    result["failures"] = failures
    return result


@app.get(
    "/api/uploads/{conversation_id}/{filename}",
    dependencies=[Depends(require_auth)],
)
async def uploaded_file(conversation_id: str, filename: str) -> FileResponse:
    path = validated_file(conversation_id, filename)
    return FileResponse(path)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def sessions() -> dict:
    return {"sessions": session_list()}


@app.get("/api/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
async def messages(
    session_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict:
    try:
        items, has_more, next_before_id = session_messages(session_id, before_id, limit)
        return {
            "messages": items,
            "has_more": has_more,
            "next_before_id": next_before_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.patch("/api/sessions/{session_id}/title", dependencies=[Depends(require_auth)])
async def rename(session_id: str, body: RenameBody) -> dict:
    set_session_title(session_id, body.title)
    return {"renamed": True}


@app.patch("/api/sessions/{session_id}/star", dependencies=[Depends(require_auth)])
async def star(session_id: str, body: StarBody) -> dict:
    set_session_starred(session_id, body.starred)
    return {"starred": body.starred}


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete(session_id: str) -> dict:
    remove_session(session_id)
    remove_conversation_uploads(session_id)
    return {"deleted": True}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def get_memory() -> dict:
    return {"content": read_memory()}


@app.put("/api/memory", dependencies=[Depends(require_auth)])
async def put_memory(body: MemoryBody) -> dict:
    write_memory(body.content)
    return {"saved": True}


@app.get("/api/profile", dependencies=[Depends(require_auth)])
async def get_profile() -> dict:
    profile, imported_count, found_count = import_claude_export_memories(read_profile())
    return {
        "profile": profile,
        "importedCount": imported_count,
        "foundCount": found_count,
    }


@app.put("/api/profile", dependencies=[Depends(require_auth)])
async def put_profile(body: ProfileBody) -> dict:
    data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    profile = write_profile(data)
    return {"saved": True, "profile": profile}


@app.post("/api/profile/memory", dependencies=[Depends(require_auth)])
async def post_memory(body: dict) -> dict:
    content = body.get("content", "").strip()
    if not content:
        return {"saved": False, "reason": "empty content"}
    result = add_saved_memory(content)
    if result is None:
        return {"saved": False, "reason": "duplicate or limit reached"}
    return {"saved": True, "memory": result}


@app.get("/api/splash")
async def splash() -> dict:
    period = current_period()
    return {"period": period, "line": random_line(period)}


@app.get("/api/models")
async def models() -> dict:
    return {"models": api_models()}


def api_models() -> list[dict[str, Any]]:
    openai_model = os.environ.get("OPENAI_MODEL") or os.environ.get("EXTERNAL_MODEL") or "gpt-4.1-mini"
    anthropic_model = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514"
    return [
        {
            "id": openai_model,
            "label": openai_model,
            "desc": "OpenAI-compatible API",
            "thinking": "none",
            "primary": True,
        },
        {
            "id": anthropic_model,
            "label": anthropic_model,
            "desc": "Anthropic Messages API",
            "thinking": "none",
            "primary": False,
        },
    ]
