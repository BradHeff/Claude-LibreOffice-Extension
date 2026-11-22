"""Conversation state, shared prompt pieces and the tool loop. No UNO and no network in here."""

import hashlib
import re

from claude_lo import api

CONTEXT_AUTO = "auto"
CONTEXT_NONE = "none"
CONTEXT_SELECTION = "selection"
CONTEXT_DOCUMENT = "document"

# About 150,000 tokens of text. Above this the user is asked to send less,
# rather than the text being cut off without them knowing.
MAX_CONTEXT_CHARS = 600000
# A turn that keeps calling tools is stopped after this many requests.
MAX_ROUNDS = 25

DEFAULT_TAGS = {CONTEXT_SELECTION: "writer_selection", CONTEXT_DOCUMENT: "writer_document"}

EDITING_RULES = """\
You can change the {noun} yourself with the editing tools. When the person \
asks for a change, make it with the tools; they are asking you to do the \
edit, so describing steps for them to carry out is not what they want. All \
the edits you make in one turn can be undone together with a single Edit > \
Undo, so go ahead with changes they have asked for. If a request could mean \
several different edits, and choosing wrong would change the {noun}'s \
meaning, ask which they mean first. Change only what was asked for; if you \
notice something else that the change makes inconsistent, mention it and let \
them decide.
"""

MARKDOWN_RULES = """\
The panel renders simple Markdown: bold, italic, inline code, short headings, \
and bullet or numbered lists. It does not render Markdown tables or nested \
formatting, so keep to those. Keep replies short: the panel is narrow.
"""

LANGUAGE_RULE = ("Match the {noun}'s language, spelling conventions and tone unless asked "
                 "otherwise.")


def editing_rules(noun):
    return EDITING_RULES.replace("{noun}", noun)


_DOC_TEXT = re.compile(r"<document_text>\n?(.*?)\n?</document_text>", re.S)


class ContextTooLarge(Exception):
    pass


def document_text(reply):
    """The text the buttons put into the document: the tagged part, else the whole reply."""
    found = _DOC_TEXT.findall(reply)
    if found:
        return found[-1]
    return reply.strip()


def display_text(reply):
    """The reply as shown in the panel, with the tags removed."""
    return reply.replace("<document_text>", "").replace("</document_text>", "")


class Conversation(object):
    """An append-only message history. Earlier turns are never edited, which
    the API's thinking-block checks and prompt caching both rely on."""

    def __init__(self):
        self.messages = []
        self._last_context = None
        self._undo = (0, None)

    def reset(self):
        self.__init__()

    def forget_context(self):
        """Send the document text again with the next message. Used after Claude has edited the
        file, because an Undo can bring the text back to what it was before the edit."""
        self._last_context = None

    def add_user(self, question, context_kind=CONTEXT_NONE, context_text="", tags=None):
        """Append the user's turn, with document text when there is any.

        `tags` maps the context kinds to the tag names the app's prompt uses.
        """
        undo = (len(self.messages), self._last_context)
        parts = []
        tag = (tags or DEFAULT_TAGS).get(context_kind)
        if tag and context_text.strip():
            if len(context_text) > MAX_CONTEXT_CHARS:
                raise ContextTooLarge(
                    "That is too much text to send at once (%d characters). "
                    "Select a smaller part of the document." % len(context_text))
            key = (context_kind, hashlib.sha256(context_text.encode("utf-8")).hexdigest())
            if key == self._last_context:
                parts.append("(The %s is unchanged since you last saw it.)" % context_kind)
            else:
                # Stop document text from closing the tag it is wrapped in.
                safe = context_text.replace("</%s>" % tag, "</ %s>" % tag)
                parts.append("<%s>\n%s\n</%s>" % (tag, safe, tag))
                self._last_context = key
        parts.append(question)
        self._undo = undo
        self.messages.append({"role": "user", "content": "\n\n".join(parts)})

    def rollback_user(self):
        """Remove everything since the last user turn began, after a failed request."""
        length, context = self._undo
        del self.messages[length:]
        self._last_context = context

    def add_assistant(self, blocks):
        self.messages.append({"role": "assistant", "content": blocks})

    def add_tool_results(self, results):
        self.messages.append({"role": "user", "content": results})


def tools_for(choice, tools, read_only):
    """The tools Claude gets for a message. When the person chose to send no sheet content, or only
    the selection, the tools that read other parts of the file are left out."""
    if choice in (CONTEXT_NONE, CONTEXT_SELECTION):
        return [t for t in tools if t["name"] not in read_only]
    return list(tools)


def run_turn(api_key, model, conversation, execute_tool, system, tools, on_text=None,
             cancelled=None, stream=api.stream_message, on_activity=None, usage=None):
    """Run the turn whose user message is already in `conversation`.

    Calls the API, runs any tools Claude asks for through
    `execute_tool(name, input) -> (text, is_error)`, sends the results back,
    and repeats until Claude finishes. Returns (final message, all reply text).
    The conversation is left complete on success; on an exception or a refusal
    the caller rolls it back. `on_activity(phase)` reports progress: "waiting",
    "thinking", "writing", "preparing" (an edit) or "editing".
    """
    def report(phase):
        if on_activity:
            on_activity(phase)

    phases = {"thinking": "thinking", "redacted_thinking": "thinking", "text": "writing",
              "tool_use": "preparing"}
    texts = []
    for round_number in range(MAX_ROUNDS):
        if texts and on_text:
            on_text("\n\n")
        report("waiting" if round_number == 0 else "thinking")
        message = stream(api_key, model, system, conversation.messages,
                         on_text=on_text, cancelled=cancelled, tools=tools,
                         on_block=lambda kind: report(phases.get(kind, "thinking")))
        if usage is not None:
            # Input counts what was sent fresh, written to the cache and read from it.
            usage["input_tokens"] = usage.get("input_tokens", 0) + sum(
                int(message.usage.get(key) or 0) for key in
                ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            usage["output_tokens"] = usage.get("output_tokens", 0) + int(message.usage.get("output_tokens") or 0)
        if message.stop_reason == "refusal":
            return message, ""
        if message.text():
            texts.append(message.text())
        blocks = message.history_blocks()
        if message.stop_reason != "tool_use" or not message.tool_uses():
            # A reply cut off at the length limit may end in an unfinished tool call.
            blocks = [b for b in blocks if b.get("type") != "tool_use"]
            if blocks:
                conversation.add_assistant(blocks)
            return message, "\n\n".join(texts)
        conversation.add_assistant(blocks)
        results = []
        for call in message.tool_uses():
            if cancelled and cancelled():
                raise api.Cancelled()
            report("editing")
            text, is_error = execute_tool(call["name"], call.get("input") or {})
            result = {"type": "tool_result", "tool_use_id": call["id"], "content": text}
            if is_error:
                result["is_error"] = True
            results.append(result)
        conversation.add_tool_results(results)
    raise api.ApiError("Claude made too many edits in one go and was stopped. "
                       "Check the document, then ask again for what is left.")
