"""Turn Claude's Markdown-style reply into paragraphs the panel can draw.

Each line of the reply becomes one paragraph, which lets the panel add text as
it streams in without redrawing what is already shown. No UNO in here.
"""

import re

PLAIN, BOLD, ITALIC, CODE = "", "b", "i", "code"

_INLINE = re.compile(
    r"\*\*(?P<b>.+?)\*\*"
    r"|`(?P<code>[^`]+)`"
    r"|(?<![\*\w])\*(?!\s)(?P<i>[^*]+?)(?<!\s)\*(?!\w)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*•]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def inline_runs(text):
    """Split a line into (text, style) runs for bold, italic and inline code."""
    runs = []
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            runs.append((text[position:match.start()], PLAIN))
        for style in (BOLD, CODE, ITALIC):
            if match.group(style or "b") is not None and style:
                runs.append((match.group(style), style))
                break
        position = match.end()
    if position < len(text):
        runs.append((text[position:], PLAIN))
    return runs


class LineParser(object):
    """Parses one line at a time, remembering whether it is inside a code block."""

    def __init__(self):
        self.in_code = False
        self.after_blank = False

    def parse(self, line):
        """Return a paragraph dict, or None for a line that draws nothing.

        A paragraph is {"kind", "runs", "indent", "gap"}: kind is "p", "h",
        "bullet", "number" or "code"; gap is True after a blank line.
        """
        if line.strip().startswith("```"):
            self.in_code = not self.in_code
            self.after_blank = True
            return None
        if self.in_code:
            return self._make("code", [(line, CODE)])
        if not line.strip() or _RULE.match(line):
            self.after_blank = True
            return None
        match = _HEADING.match(line)
        if match:
            return self._make("h", [(run, BOLD) for run, _ in inline_runs(match.group(1))])
        match = _BULLET.match(line)
        if match:
            runs = [("•  ", PLAIN)] + inline_runs(match.group(2))
            return self._make("bullet", runs, len(match.group(1)) // 2)
        match = _NUMBER.match(line)
        if match:
            runs = [(match.group(2) + ".  ", PLAIN)] + inline_runs(match.group(3))
            return self._make("number", runs, len(match.group(1)) // 2)
        return self._make("p", inline_runs(line.strip()))

    def _make(self, kind, runs, indent=0):
        gap, self.after_blank = self.after_blank, False
        return {"kind": kind, "runs": runs, "indent": indent, "gap": gap}
