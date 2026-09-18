"""Strict static parser for explicitly synthetic C1 fixtures; no browser APIs.

DOM identity/role/visibility conventions are the frozen fixture contract only.
Computed CSS, production selectors, capture clocks and live lifecycle remain
unverified. Output is scrubbed source-local data, never a canonical commit.
"""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re

from companion_mind.journal.codec import REDACTED
from .normalize import Attachment, Capture, CaptureError, clean_text, label, normalize_text, replay_captures
from .vendor_profile import load_profile, validate_profile

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
EXCLUDED = {"script", "style", "template", "noscript", "iframe", "object", "svg", "input", "button"}


@dataclass
class _Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)


class _DOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if len(attrs) != len(dict(attrs)) or len(self.stack) > 64 or self.count >= 20000:
            raise CaptureError("PROFILE_DRIFT")
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        self.count += 1
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID or len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise CaptureError("PROFILE_DRIFT")
        self.stack.pop()

    def handle_data(self, text):
        self.stack[-1].children.append(text)


def _hidden(node):
    style = re.sub(r"\s+", "", (node.attrs.get("style") or "").lower())
    return (node.tag in EXCLUDED or "hidden" in node.attrs or "inert" in node.attrs or
            (node.attrs.get("aria-hidden") or "").lower() == "true" or
            node.attrs.get("data-c1-visible") == "false" or
            "display:none" in style or "visibility:hidden" in style)


def _walk(node):
    if _hidden(node):
        return
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _raw_text(node):
    if _hidden(node):
        return ""
    return "".join(c if isinstance(c, str) else _raw_text(c) for c in node.children)


def _markdown(node):
    if _hidden(node) or "data-c1-attachment" in node.attrs or "data-c1-role-label" in node.attrs:
        return ""
    if node.tag == "pre":
        code = _raw_text(node).replace("\r\n", "\n").replace("\r", "\n")
        runs = re.findall(r"`+", code)
        fence = "`" * max(3, max((len(x)+1 for x in runs), default=3))
        return "\n\n"+fence+"\n"+code+"\n"+fence+"\n\n"
    # Render then normalize whitespace: adjacent spans may split one token.
    text = "".join(c if isinstance(c, str) else _markdown(c) for c in node.children)
    if node.tag == "code":
        fence = "`" * max(1, max((len(x)+1 for x in re.findall(r"`+", text)), default=1))
        return fence + text + fence
    if node.tag in {"strong", "b"}:
        return "**"+text+"**"
    if node.tag in {"em", "i"}:
        return "*"+text+"*"
    if node.tag == "br":
        return "\n"
    if node.tag in {"ul", "ol"}:
        items = [c for c in node.children if isinstance(c, _Node) and c.tag == "li" and not _hidden(c)]
        return "\n\n" + "\n".join((f"{i}. " if node.tag == "ol" else "- ")+_markdown(c).strip()
                                     for i, c in enumerate(items, 1)) + "\n\n"
    if node.tag == "blockquote":
        return "\n\n" + "\n".join("> "+line for line in text.strip().splitlines()) + "\n\n"
    if node.tag in {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6"}:
        return "\n\n" + text + "\n\n"
    if node.tag not in {"span", "a", "li", "hr"}:
        raise CaptureError("PROFILE_DRIFT")
    return text


def _payload(node):
    # Check joined visible characters BEFORE adding markdown delimiters. A bold
    # or code span splitting a credential label must not evade the scrubber.
    visible = normalize_text(_raw_text(node))
    if clean_text(visible) != visible:
        return REDACTED
    text = _markdown(node)
    # Keep fenced code lines verbatim; normalize prose without losing paragraphs.
    lines, fence = [], None
    for line in text.splitlines():
        if re.fullmatch(r"`{3,}", line):
            fence = line if fence is None else (None if fence == line else fence)
            lines.append(line)
        elif fence:
            lines.append(line)
        else:
            lines.append(re.sub(r"[\t ]+", " ", line).strip())
    clean = "\n".join(lines).strip()
    # Paragraph compaction is outside code blocks only.
    out, fence, blanks = [], None, 0
    for line in clean.split("\n"):
        if re.fullmatch(r"`{3,}", line):
            fence = line if fence is None else (None if fence == line else fence)
        blanks = blanks+1 if not line and not fence else 0
        if blanks <= 1:
            out.append(line)
    return clean_text("\n".join(out))


def _integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,8}", value):
        raise CaptureError("PROFILE_DRIFT")
    return int(value)


def parse_dom(html, *, profile=None, contract_version="canonical_event/v1"):
    """Parse a single synthetic snapshot atomically, failing without partial output."""
    p = load_profile() if profile is None else validate_profile(profile)
    if contract_version != "canonical_event/v1":
        raise CaptureError("CONTRACT_CHANGE_REQUIRED")
    if not isinstance(html, str):
        raise CaptureError("PROFILE_DRIFT")
    try:
        if len(html.encode("utf-8")) > 2_000_000:
            raise CaptureError("PROFILE_DRIFT")
    except UnicodeEncodeError:
        raise CaptureError("PROFILE_DRIFT") from None
    dom = _DOM()
    dom.feed(html)
    dom.close()
    if len(dom.stack) != 1:
        raise CaptureError("PROFILE_DRIFT")
    strategy = p["conversation_root"]
    roots = [n for n in _walk(dom.root) if n.attrs.get(strategy["attribute"]) == strategy["value"]]
    if len(roots) != 1:
        raise CaptureError("PROFILE_DRIFT")
    root = roots[0]
    if (root.attrs.get("data-c1-profile") != p["profile_version"] or
            root.attrs.get("data-c1-vendor") != "chatgpt" or
            root.attrs.get("data-c1-mode") not in p["supported_modes"]):
        raise CaptureError("PROFILE_DRIFT")
    scope = label(root.attrs.get("data-c1-scope"))
    stable, temporary = root.attrs.get("data-c1-id"), root.attrs.get("data-c1-temp-id")
    if bool(stable) == bool(temporary):
        raise CaptureError("AMBIGUOUS_IDENTITY")
    conversation = label(stable or temporary)
    nodes = [n for n in _walk(root) if p["message_identity"]["attribute"] in n.attrs]
    if not nodes or any(n.tag == "article" and "data-c1-message" not in n.attrs for n in _walk(root)):
        raise CaptureError("PROFILE_DRIFT")
    captures = []
    for node in nodes:
        if node.tag != "article" or any(n is not node and "data-c1-message" in n.attrs for n in _walk(node)):
            raise CaptureError("PROFILE_DRIFT")
        message = label(node.attrs.get(p["message_identity"]["attribute"]))
        role = node.attrs.get("data-c1-role")
        labels = [n.attrs["data-c1-role-label"] for n in _walk(node) if "data-c1-role-label" in n.attrs]
        if role not in {"user", "assistant"} or labels != [role]:
            raise CaptureError("AMBIGUOUS_ROLE")
        position = _integer(node.attrs.get("data-c1-order"))
        bodies = [n for n in _walk(node) if "data-c1-content" in n.attrs]
        if len(bodies) != 1:
            raise CaptureError("PROFILE_DRIFT")
        payload = _payload(bodies[0])
        state, terminal = node.attrs.get("data-c1-state"), None
        if role == "user":
            if state != "complete" or "data-c1-streaming" in node.attrs:
                raise CaptureError("PROFILE_DRIFT")
            terminal, control = "complete", "TERMINAL_OBSERVED"
        elif state == "streaming":
            if node.attrs.get("data-c1-streaming") != "true" or "data-c1-terminal" in node.attrs:
                raise CaptureError("PROFILE_DRIFT")
            control = "STREAMING"
        elif state in {"complete", "partial", "failed"}:
            if node.attrs.get("data-c1-terminal") != state or "data-c1-streaming" in node.attrs:
                raise CaptureError("PROFILE_DRIFT")
            elapsed = _integer(node.attrs.get("data-c1-stable-ms"))
            if state == "failed" and payload:
                raise CaptureError("AMBIGUOUS_TERMINAL")
            if state != "failed" and not payload:
                raise CaptureError("AMBIGUOUS_TERMINAL")
            if elapsed < p["terminal_evidence"]["candidate_T_stable_ms"]:
                control = "STABILIZING"
            else:
                terminal, control = state, "TERMINAL_OBSERVED"
        else:
            raise CaptureError("PROFILE_DRIFT")
        attachments = []
        for a in _walk(node):
            if "data-c1-attachment" not in a.attrs:
                continue
            attachment_id = label(a.attrs["data-c1-attachment"])
            filename = clean_text(a.attrs["data-c1-filename"]) if a.attrs.get("data-c1-filename") else None
            media = a.attrs.get("data-c1-media-type")
            if media is not None and not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media):
                raise CaptureError("PROFILE_DRIFT")
            # No URL enters the persistable representation, even when public.
            href = a.attrs.get("href") or ""
            safe = bool(re.fullmatch(r"https?://[^\s?#@]+", href))
            available = a.attrs.get("data-c1-available", "true")
            if available not in {"true", "false"}:
                raise CaptureError("PROFILE_DRIFT")
            attachments.append(Attachment(attachment_id, filename, media,
                               f"message:{message}:attachment:{attachment_id}",
                               "REFERENCE_ONLY" if safe and available == "true" else "UNAVAILABLE"))
        if len({a.attachment_id for a in attachments}) != len(attachments):
            raise CaptureError("AMBIGUOUS_IDENTITY")
        redacted = REDACTED in payload or any(a.filename and REDACTED in a.filename for a in attachments)
        captures.append(Capture(scope, conversation, "stable" if stable else "temporary", message,
                               role, position, payload, tuple(attachments), terminal, control,
                               "redacted" if redacted else "none", p["profile_id"], p["profile_version"],
                               p["profile_fingerprint"]))
    return replay_captures(captures)
