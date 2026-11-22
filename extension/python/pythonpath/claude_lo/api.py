"""Claude Messages API client that uses only the Python standard library.

The official `anthropic` SDK is not used on purpose: LibreOffice ships its own
Python on Windows and macOS with no pip, and the SDK's dependencies include
compiled modules that can't be bundled in one cross-platform .oxt.

Nothing in this module logs, prints or puts the API key in an error message.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5-5"
MODELS = (
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-2", "Claude Fable 5.2"),
)
# Claude Haiku 5.5 has no server-side refusal fallback.
NO_FALLBACK_MODELS = frozenset(["claude-haiku-5-5"])
MAX_TOKENS = 64000
TIMEOUT_SECONDS = 600
MAX_RETRIES = 2
MAX_RETRY_DELAY = 30


class ApiError(Exception):
    """A failure the user should see. `str(error)` is safe to display."""

    def __init__(self, message, status=None, kind=None, retryable=False, retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after


class Cancelled(Exception):
    """The user stopped the request."""


def api_url():
    """The endpoint. CLAUDE_LO_API_URL lets the tests point at a local mock, and is used only
    for an address on this machine, so an environment variable can't redirect the API key."""
    override = os.environ.get("CLAUDE_LO_API_URL")
    if override:
        try:
            host = urllib.parse.urlsplit(override).hostname
        except ValueError:
            host = None
        if host in ("127.0.0.1", "localhost", "::1"):
            return override
    return API_URL


def build_request(api_key, model, system, messages, tools=None):
    """Return (body, headers) for a streaming Messages request."""
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "system": system,
        "messages": messages,
        # Cache the growing conversation prefix between turns.
        "cache_control": {"type": "ephemeral"},
    }
    if tools:
        body["tools"] = tools
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }
    if model not in NO_FALLBACK_MODELS:
        # If a safety classifier declines the request, the API re-runs it on
        # the fallback model Anthropic recommends, inside the same call.
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = FALLBACK_BETA
    return body, headers


def iter_sse(lines):
    """Yield the decoded JSON of each server-sent event in a byte-line stream."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line == "" and data:
            payload = "\n".join(data)
            data = []
            yield json.loads(payload)
    if data:
        yield json.loads("\n".join(data))


class MessageAccumulator(object):
    """Rebuilds the assistant message from stream events."""

    def __init__(self):
        self.content = []
        self.model = None
        self.stop_reason = None
        self.stop_details = None
        self.usage = {}
        self._partial_json = {}

    def handle(self, event):
        """Apply one event. Returns the new visible text, if the event had any."""
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message", {})
            self.model = message.get("model")
            self.usage.update(message.get("usage") or {})
        elif kind == "content_block_start":
            index = event["index"]
            while len(self.content) <= index:
                self.content.append(None)
            self.content[index] = dict(event["content_block"])
        elif kind == "content_block_delta":
            block = self.content[event["index"]]
            delta = event.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
                return delta["text"]
            if dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            elif dtype == "input_json_delta":
                index = event["index"]
                self._partial_json[index] = self._partial_json.get(index, "") + delta["partial_json"]
        elif kind == "content_block_stop":
            index = event["index"]
            if index in self._partial_json:
                # Raises ValueError on bad JSON, which stream_message reports.
                self.content[index]["input"] = json.loads(self._partial_json.pop(index) or "{}")
        elif kind == "message_delta":
            delta = event.get("delta", {})
            self.stop_reason = delta.get("stop_reason", self.stop_reason)
            self.stop_details = delta.get("stop_details", self.stop_details)
            self.usage.update(event.get("usage") or {})
        elif kind == "error":
            error = event.get("error", {})
            raise _api_error(None, error.get("type"), error.get("message"))
        return None

    def text(self):
        """The reply text. After a mid-reply fallback, only the fallback model's text."""
        blocks = [b for b in self.content if b]
        for i in range(len(blocks) - 1, -1, -1):
            if blocks[i].get("type") == "fallback":
                blocks = blocks[i + 1:]
                break
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def tool_uses(self):
        """The tool calls to run: the `tool_use` blocks after any fallback."""
        blocks = [b for b in self.content if b]
        for i in range(len(blocks) - 1, -1, -1):
            if blocks[i].get("type") == "fallback":
                blocks = blocks[i + 1:]
                break
        return [b for b in blocks if b.get("type") == "tool_use"]

    def history_blocks(self):
        """The content to send back as the assistant turn on later requests.

        Thinking blocks are returned unchanged, as the API requires. After a
        fallback, the declined model's thinking and tool blocks before the last
        `fallback` block are left out, which is the documented echo rule.
        """
        blocks = [b for b in self.content if b]
        last_fallback = -1
        for i, block in enumerate(blocks):
            if block.get("type") == "fallback":
                last_fallback = i
        out = []
        for i, block in enumerate(blocks):
            btype = block.get("type")
            if i < last_fallback and btype in ("thinking", "redacted_thinking", "tool_use"):
                continue
            if btype == "text" and not block.get("text"):
                continue
            out.append(block)
        return out


def _api_error(status, kind, message):
    retryable = status in (408, 409, 429) or (status is not None and status >= 500)
    if kind in ("overloaded_error", "api_error", "rate_limit_error"):
        retryable = True
    if status == 401 or kind == "authentication_error":
        text = "The API key was rejected. Check it in Settings."
    elif status == 403 or kind == "permission_error":
        text = "This API key isn't allowed to use that model or feature."
    elif status == 429 or kind == "rate_limit_error":
        text = "Rate limit or spend limit reached. Wait a moment and try again."
    elif status == 529 or kind == "overloaded_error":
        text = "The Claude API is overloaded. Try again shortly."
    elif status == 413 or kind == "request_too_large":
        text = "The request is too large. Send less of the document."
    elif status == 400 and message and "too long" in str(message).lower():
        text = "This chat has grown too long. Start a new chat."
        message = None
    else:
        text = "The Claude API returned an error"
        if status:
            text += " (%s)" % status
        text += "."
    if message and status not in (401,):
        text += " " + str(message)
    return ApiError(text, status=status, kind=kind, retryable=retryable)


def _error_from_http(error):
    kind = message = None
    try:
        payload = json.loads(error.read().decode("utf-8"))
        kind = payload.get("error", {}).get("type")
        message = payload.get("error", {}).get("message")
    except Exception:
        pass
    result = _api_error(error.code, kind, message)
    headers = error.headers
    if headers is not None:
        if headers.get("x-should-retry") == "false":
            result.retryable = False
        try:
            result.retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
    return result


def _wait(delay, sleep, cancelled):
    """Wait before a retry. With a `cancelled` check the wait is cut into short slices, so Stop works."""
    if not cancelled:
        sleep(delay)
        return
    remaining = float(delay)
    while remaining > 0:
        if cancelled():
            raise Cancelled()
        step = min(0.2, remaining)
        sleep(step)
        remaining -= step
    if cancelled():
        raise Cancelled()


def stream_message(api_key, model, system, messages, on_text=None, cancelled=None,
                   opener=None, sleep=time.sleep, tools=None, on_block=None):
    """Send one streaming request and return the finished MessageAccumulator.

    `on_text(delta)` is called as text arrives. `cancelled()` returning True
    stops the request with Cancelled. `on_block(type)` is called when Claude
    starts a new part of its reply ("thinking", "text", "tool_use"). A request
    is retried only before any of the reply has arrived.
    """
    if not api_key:
        raise ApiError("No API key is set. Add one in Settings.")
    opener = opener or urllib.request.urlopen
    body, headers = build_request(api_key, model, system, messages, tools)
    data = json.dumps(body).encode("utf-8")
    attempt = 0
    while True:
        if cancelled and cancelled():
            raise Cancelled()
        request = urllib.request.Request(api_url(), data=data, headers=headers, method="POST")
        try:
            response = opener(request, timeout=TIMEOUT_SECONDS)
            break
        except urllib.error.HTTPError as error:
            failure = _error_from_http(error)
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            failure = ApiError("Could not reach the Claude API: %s" % reason, retryable=True)
        if not failure.retryable or attempt >= MAX_RETRIES:
            raise failure
        delay = failure.retry_after if failure.retry_after is not None else 2 ** attempt
        if delay > MAX_RETRY_DELAY:
            raise failure
        _wait(delay, sleep, cancelled)
        attempt += 1

    message = MessageAccumulator()
    try:
        for event in iter_sse(response):
            if cancelled and cancelled():
                raise Cancelled()
            delta = message.handle(event)
            if on_block and event.get("type") == "content_block_start":
                on_block(event["content_block"].get("type"))
            if delta and on_text:
                on_text(delta)
    except (OSError, ValueError) as error:
        raise ApiError("The connection to the Claude API was lost: %s" % error)
    finally:
        response.close()
    if message.stop_reason is None:
        raise ApiError("The reply ended early. Try again.")
    return message
