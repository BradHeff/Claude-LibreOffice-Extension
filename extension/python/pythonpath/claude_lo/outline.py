"""The text view of a slide. No UNO in here."""

LAYOUT_NAMES = {0: "Title Slide", 1: "Title, Content", 19: "Title Only", 20: "Blank"}
# The tool's layout names, and the LibreOffice layout number for each.
LAYOUT_CODES = {"title": 0, "title_content": 1, "title_only": 19, "blank": 20}


def utf16_len(text):
    """The length of text in UTF-16 units, which is how LibreOffice counts text positions.
    An emoji is two units, though Python counts one character."""
    return len(text.encode("utf-16-le")) // 2


def layout_name(code):
    return LAYOUT_NAMES.get(code, "layout %s" % code)


def body_lines(paragraphs):
    """Bullet lines for (level, text) pairs, indented two spaces per level."""
    return ["%s- %s" % ("  " * level, text) for level, text in paragraphs]


def parse_body(lines):
    """Turn lines indented two spaces per level into (level, text) pairs.

    A leading "- " or "* " on a line is dropped, since the slide draws its own
    bullets.
    """
    result = []
    for line in lines:
        stripped = line.lstrip(" ")
        level = min((len(line) - len(stripped)) // 2, 8)
        text = stripped.rstrip()
        if text[:2] in ("- ", "* "):
            text = text[2:]
        result.append((level, text))
    return result


def render_slide(number, total, layout, title, body, notes, others, extras, shapes=None,
                 transition=None, animations=None):
    """Describe one slide.

    `title` and `notes` are strings, or None when the slide has no such slot.
    `body` is a list of (level, text) pairs, or None when there is no body
    slot. `others` is a list of text strings from other shapes and `extras` a
    list of descriptions of shapes without text, such as "1 image". `shapes` is
    an optional list of lines describing the title, body and drawn shapes;
    `transition` is a description of the slide's transition and `animations`
    a list of its animation effects in play order.
    """
    lines = ["[Slide %d of %d]  layout: %s" % (number, total, layout_name(layout))]
    if title is not None:
        lines.append("  Title: %s" % (title.replace("\n", " ") if title else "(empty)"))
    if body is not None:
        if any(text for _, text in body):
            lines.append("  Body:")
            lines += ["    " + line for line in body_lines(body)]
        else:
            lines.append("  Body: (empty)")
    if notes:
        lines.append("  Notes: %s" % notes.replace("\n", " / "))
    for text in others:
        lines.append('  Other text: "%s"' % text.replace("\n", " / "))
    if shapes:
        lines.append("  Shapes (position and size as % of the slide):")
        lines += ["    - " + line for line in shapes]
    if transition:
        lines.append("  Transition: %s" % transition)
    if animations:
        lines.append("  Animations (in order):")
        lines += ["    %d. %s" % (i, text) for i, text in enumerate(animations, 1)]
    if extras:
        lines.append("  Also on this slide: %s" % ", ".join(extras))
    return "\n".join(lines)


def describe_extras(counts):
    """{"image": 2, "shape": 1} -> ["2 images", "1 shape"]."""
    return ["%d %s%s" % (count, kind, "" if count == 1 else "s")
            for kind, count in sorted(counts.items())]
