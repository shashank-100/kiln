"""Agent session logs → normalized events.

Only observable content is kept: messages, tool calls with their arguments, and
tool results. Hidden reasoning is never assumed to be available and thinking
blocks are dropped. Known secret formats are redacted before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

PARSER = "claude-code/1"
MAX_INLINE = 32 * 1024

_SECRETS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)((?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?)[^\s'\"]{8,}"),
]


def redact(text: str) -> str:
    for pat in _SECRETS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


@dataclass
class Event:
    seq: int
    id: str
    ts: str
    kind: str  # message | tool_call | tool_result
    role: str  # user | assistant | tool
    text: str = ""
    tool: str = ""
    call_id: str = ""
    args: dict = field(default_factory=dict)
    is_error: bool = False
    output_ref: str = ""  # sha256 of the full output when it was too large to inline
    sidechain: bool = False
    source: str = ""  # file:line it came from


@dataclass
class Parsed:
    session_id: str
    cwd: str
    events: list[Event]
    blobs: dict[str, str]
    malformed_lines: int
    dangling_calls: list[str]
    first_ts: str
    last_ts: str

    @property
    def completeness(self) -> str:
        if self.malformed_lines or self.dangling_calls:
            return "partial"
        return "complete" if self.events else "empty"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(b.get("text", b.get("content", ""))) for b in content if isinstance(b, dict))
    return ""


def parse_claude_code(path: Path) -> Parsed:
    events: list[Event] = []
    blobs: dict[str, str] = {}
    seen: set[str] = set()
    calls: dict[str, str] = {}
    malformed = 0
    session_id = cwd = first_ts = last_ts = ""

    def add(**kw) -> None:
        if kw["id"] in seen:
            return
        seen.add(kw["id"])
        events.append(Event(seq=len(events), **kw))

    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1  # usually the last line of an interrupted write
            continue
        if rec.get("type") not in ("user", "assistant") or rec.get("isMeta"):
            continue
        session_id = session_id or rec.get("sessionId", "")
        cwd = cwd or rec.get("cwd", "")
        ts = rec.get("timestamp", "")
        first_ts = first_ts or ts
        last_ts = ts or last_ts
        msg = rec.get("message") or {}
        content = msg.get("content")
        base = dict(ts=ts, sidechain=bool(rec.get("isSidechain")), source=f"{path.name}:{lineno}")
        uid = rec.get("uuid", f"line{lineno}")
        if isinstance(content, str):
            add(id=uid, kind="message", role=msg.get("role", rec["type"]), text=redact(content), **base)
            continue
        for i, block in enumerate(content or []):
            kind = block.get("type")
            bid = f"{uid}:{i}"
            if kind == "text" and block.get("text", "").strip():
                add(id=bid, kind="message", role=msg.get("role", rec["type"]), text=redact(block["text"]), **base)
            elif kind == "tool_use":
                calls[block["id"]] = block.get("name", "")
                args = json.loads(redact(json.dumps(block.get("input") or {})))
                add(id=bid, kind="tool_call", role="assistant", tool=block.get("name", ""),
                    call_id=block["id"], args=args, **base)  # fmt: skip
            elif kind == "tool_result":
                output = redact(_text(block.get("content")))
                ref = ""
                if len(output) > MAX_INLINE:
                    ref = hashlib.sha256(output.encode()).hexdigest()
                    blobs[ref] = output
                    output = output[:MAX_INLINE] + f"\n[... truncated, full output in blob {ref[:12]}]"
                call_id = block.get("tool_use_id", "")
                add(id=bid, kind="tool_result", role="tool", tool=calls.pop(call_id, ""),
                    call_id=call_id, text=output, is_error=bool(block.get("is_error")),
                    output_ref=ref, **base)  # fmt: skip
            # thinking / redacted_thinking: not observable, deliberately skipped
    return Parsed(session_id, cwd, events, blobs, malformed, sorted(calls), first_ts, last_ts)


def write(parsed: Parsed, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as f:
        for e in parsed.events:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
    for digest, body in parsed.blobs.items():
        blob = out_dir / "blobs" / f"{digest}.txt"
        blob.parent.mkdir(exist_ok=True)
        blob.write_text(body, encoding="utf-8")
    (out_dir / "session.json").write_text(
        json.dumps(
            {
                "parser": PARSER,
                "session_id": parsed.session_id,
                "cwd": parsed.cwd,
                "events": len(parsed.events),
                "completeness": parsed.completeness,
                "malformed_lines": parsed.malformed_lines,
                "dangling_tool_calls": parsed.dangling_calls,
                "first_ts": parsed.first_ts,
                "last_ts": parsed.last_ts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return events_path


def read_events(out_dir: Path) -> Iterator[dict]:
    with (out_dir / "events.jsonl").open(encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def claude_code_logs(cwd: Path, since: float) -> list[Path]:
    """Claude Code logs for `cwd` written at or after `since` (epoch seconds)."""
    folder = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.glob("*.jsonl") if p.stat().st_mtime >= since),
        key=lambda p: p.stat().st_mtime,
    )


def first_request(out_dir: Path) -> str:
    """The developer's opening request: the first user message that isn't a tool result."""
    for e in read_events(out_dir):
        if e["kind"] == "message" and e["role"] == "user" and not e["sidechain"]:
            text = e["text"].strip()
            if text and not text.startswith("<"):
                return text
    return ""
