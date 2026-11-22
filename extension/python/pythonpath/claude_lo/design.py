"""Pure helpers for styling slides: colours and percentage geometry. No UNO in here."""

import re

_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def parse_color(text):
    """'#00E5FF', '00E5FF' or '#0EF' -> the integer LibreOffice uses (0x00E5FF)."""
    match = _HEX.match((text or "").strip())
    if not match:
        raise ValueError('"%s" is not a colour. Use a hex code such as #00E5FF.' % text)
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return int(digits, 16)


def color_hex(value):
    """0x00E5FF -> '#00E5FF'. LibreOffice's -1 means "automatic"."""
    return "automatic" if value is None or value < 0 else "#%06X" % (value & 0xFFFFFF)


def to_units(percent, total):
    """A percentage of the slide's width or height, in the slide's own units (1/100 mm)."""
    return int(round(percent / 100.0 * total))


def to_percent(units, total):
    return round(units * 100.0 / total, 1) if total else 0.0


def number(value):
    """12.0 -> '12', 12.5 -> '12.5'."""
    return ("%.1f" % value).rstrip("0").rstrip(".")


def check_box(x, y, width, height, allow_zero=False):
    """Raise ValueError unless the box sits on the slide (all values are percentages)."""
    for label, value in (("x", x), ("y", y), ("width", width), ("height", height)):
        if value < 0:
            raise ValueError("%s must not be negative." % label)
    if not allow_zero and (width <= 0 or height <= 0):
        raise ValueError("width and height must be more than 0.")
    if x + width > 100.5 or y + height > 100.5:
        raise ValueError("The shape runs off the slide: x + width and y + height must each be at "
                         "most 100 (they are percentages of the slide).")


def describe_shape(label, kind, x, y, width, height, fill=None):
    """One line for the slide view, e.g. '"Top bar" (rectangle) at 0, 0, size 100 x 2, fill #00E5FF'."""
    line = '"%s" (%s) at %s, %s, size %s x %s' % (label, kind, number(x), number(y),
                                                  number(width), number(height))
    return line + (", fill %s" % fill if fill else "")


def spill(inner, outer, tolerance):
    """The sides where box `inner` reaches past box `outer` by more than `tolerance`.
    Boxes are (left, top, right, bottom)."""
    sides = []
    if inner[0] < outer[0] - tolerance:
        sides.append("left")
    if inner[2] > outer[2] + tolerance:
        sides.append("right")
    if inner[1] < outer[1] - tolerance:
        sides.append("top")
    if inner[3] > outer[3] + tolerance:
        sides.append("bottom")
    return sides


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def overlap_area(a, b):
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _luminance(color):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel((color >> 16) & 255) + 0.7152 * channel((color >> 8) & 255)
            + 0.0722 * channel(color & 255))


def contrast(a, b):
    """The WCAG contrast ratio of two colours, from 1 (the same) to 21 (black on white)."""
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)
