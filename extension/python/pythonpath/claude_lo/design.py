"""Pure helpers for styling slides: colours and percentage geometry. No UNO in here."""

import math
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


def describe_shape(label, kind, x, y, width, height, fill=None, notes=()):
    """One line for the slide view, e.g. '"Top bar" (rectangle) at 0, 0, size 100 x 2, fill #00E5FF'.
    `notes` are extras such as "rotated 30°" or "glow #8A2BE2"."""
    line = '"%s" (%s) at %s, %s, size %s x %s' % (label, kind, number(x), number(y),
                                                  number(width), number(height))
    return line + (", fill %s" % fill if fill else "") + "".join(", " + note for note in notes)


def rotate_angle(degrees):
    """Degrees clockwise -> LibreOffice's RotateAngle (hundredths of a degree, anticlockwise)."""
    return int(round((-degrees % 360) * 100)) % 36000


def clockwise(angle):
    """LibreOffice's RotateAngle -> whole degrees clockwise, 0 to 359."""
    return int(round(-angle / 100.0)) % 360


def _turn(dx, dy, angle):
    """Turn an offset (y down, as on the slide) anticlockwise by RotateAngle `angle`."""
    a = math.radians(angle / 100.0)
    return dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)


def turned_corner(x, y, width, height, angle):
    """Where LibreOffice puts the position of a box at x, y turned by `angle` about its centre: the
    turned top-left corner. Slide units, not percentages (a turn isn't the same in both)."""
    if not angle:
        return x, y
    dx, dy = _turn(-width / 2.0, -height / 2.0, angle)
    return int(round(x + width / 2.0 + dx)), int(round(y + height / 2.0 + dy))


def unturned_corner(x, y, width, height, angle):
    """The top-left corner of a turned shape's own box, from the position LibreOffice reports."""
    if not angle:
        return x, y
    dx, dy = _turn(-width / 2.0, -height / 2.0, angle)
    return int(round(x - dx - width / 2.0)), int(round(y - dy - height / 2.0))


def turned_hull(x, y, width, height, angle):
    """(left, top, right, bottom) of the box at x, y turned by `angle` about its centre."""
    if not angle:
        return (x, y, x + width, y + height)
    a = math.radians(angle / 100.0)
    half_w = (abs(width * math.cos(a)) + abs(height * math.sin(a))) / 2.0
    half_h = (abs(width * math.sin(a)) + abs(height * math.cos(a))) / 2.0
    cx, cy = x + width / 2.0, y + height / 2.0
    return (int(round(cx - half_w)), int(round(cy - half_h)), int(round(cx + half_w)),
            int(round(cy + half_h)))


def effect_margins(glow, shadow, blur, dx, dy):
    """How far a glow and a shadow reach past a shape on each side (left, top, right, bottom), as
    LibreOffice adds them to its BoundRect."""
    sides = [glow] * 4
    if shadow:
        for i, reach in enumerate((blur - dx, blur - dy, blur + dx, blur + dy)):
            sides[i] = max(sides[i], reach)
    return tuple(max(0, side) for side in sides)


def shrink(box, margins):
    """Box (left, top, right, bottom) made smaller by margins (left, top, right, bottom)."""
    return (box[0] + margins[0], box[1] + margins[1], box[2] - margins[2], box[3] - margins[3])


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


def blend(top, bottom, transparency):
    """The colour a fill `transparency` per cent see-through shows over `bottom`."""
    t = min(max(transparency, 0), 100) / 100.0
    return sum(int(round(((top >> shift) & 255) * (1 - t) + ((bottom >> shift) & 255) * t)) << shift
               for shift in (16, 8, 0))


def contrast(a, b):
    """The WCAG contrast ratio of two colours, from 1 (the same) to 21 (black on white)."""
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)
