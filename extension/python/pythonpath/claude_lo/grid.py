"""Cell addresses and the text view of a spreadsheet range. No UNO in here."""

import re

MAX_COLUMNS = 16384
MAX_ROWS = 1048576
MAX_CELL_TEXT = 500

_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")
_NUMBERISH = re.compile(r"^[-+]?\d[\d,]*\.?\d*(e[-+]?\d+)?$|^[-+]?\.\d+$", re.I)


def col_letters(index):
    """0 -> "A", 25 -> "Z", 26 -> "AA"."""
    if index < 0:
        raise ValueError("Column index can't be negative.")
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def col_index(letters):
    """"A" -> 0, "AA" -> 26. Raises ValueError for anything that isn't a column."""
    text = letters.strip().replace("$", "").upper()
    if not text.isalpha() or len(text) > 3:
        raise ValueError('"%s" is not a column letter such as A or AB.' % letters)
    number = 0
    for char in text:
        number = number * 26 + ord(char) - 64
    if number > MAX_COLUMNS:
        raise ValueError('Column "%s" is past the last column of the sheet.' % letters)
    return number - 1


def cell_name(col, row):
    """Zero-based (col, row) -> "B7"."""
    return "%s%d" % (col_letters(col), row + 1)


def _split_sheet(text):
    """Split "Sheet 1.A1:B2", "'Sheet 1'!A1" or "A1:B2" into (sheet or None, reference)."""
    text = text.strip()
    if text.startswith("'"):
        end = text.find("'", 1)
        while end != -1 and text[end + 1:end + 2] == "'":  # '' is an escaped quote
            end = text.find("'", end + 2)
        if end == -1 or text[end + 1:end + 2] not in (".", "!"):
            raise ValueError('Could not read the sheet name in "%s".' % text)
        return text[1:end].replace("''", "'"), text[end + 2:]
    for separator in ("!", "."):
        if separator in text:
            sheet, _, reference = text.rpartition(separator)
            if sheet and reference:
                return sheet, reference
    return None, text


def parse_cell(text):
    """"B7" or "Sheet 1!B7" -> (sheet or None, col, row), zero-based."""
    sheet, reference = _split_sheet(text)
    match = _CELL.match(reference)
    if not match:
        raise ValueError('"%s" is not a cell address such as B7.' % text)
    col, row = col_index(match.group(1)), int(match.group(2)) - 1
    if row < 0 or row >= MAX_ROWS:
        raise ValueError('Row %s is outside the sheet.' % match.group(2))
    return sheet, col, row


def parse_range(text):
    """"A1:C5", "B7" or "Budget!A1:C5" -> (sheet or None, c1, r1, c2, r2), zero-based."""
    sheet, reference = _split_sheet(text)
    first, colon, last = reference.partition(":")
    _, c1, r1 = parse_cell(first)
    _, c2, r2 = parse_cell(last) if colon else (None, c1, r1)
    return sheet, min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)


def split_ranges(text):
    """"A3:A8, D3:D8" or "A3:A8;D3:D8" -> ["A3:A8", "D3:D8"]. Commas inside a quoted sheet name stay."""
    parts, current, quoted = [], "", False
    for char in text:
        if char == "'":
            quoted = not quoted
        if char in ",;" and not quoted:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return [part for part in parts if part]


def range_name(c1, r1, c2, r2):
    """Zero-based corners -> "A1:C5", or "B7" for one cell."""
    first = cell_name(c1, r1)
    return first if (c1, r1) == (c2, r2) else "%s:%s" % (first, cell_name(c2, r2))


def number_text(value):
    """150000.0 -> "150000", 0.1 + 0.2 -> "0.3", 1e-7 -> "1e-07"."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return "%.15g" % value


_RISKY_FUNCTIONS = re.compile(r"\b(WEBSERVICE|DDE|FILTERXML)\s*\(", re.I)
_EXTERNAL_REFERENCE = re.compile(r"'(?:file|https?|ftp):[^']*'\s*#", re.I)


def risky_formula(text):
    """Why a formula is refused, or "" when it is fine. Formulas that fetch from another machine, or
    read another file, could send cell contents out, so Claude may not write them."""
    if not text.startswith("="):
        return ""
    found = _RISKY_FUNCTIONS.search(text)
    if found:
        return "%s can contact another machine, so it isn't allowed in formulas Claude writes." % found.group(1).upper()
    if _EXTERNAL_REFERENCE.search(text):
        return "A reference to another file isn't allowed in formulas Claude writes."
    return ""


_SHEET_REFERENCE = re.compile(r"('(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_]*)!(?=\$?[A-Za-z]{1,3}(?:\$?\d|:))")
_CELL_LIKE = re.compile(r"^[A-Za-z]{1,3}\d+$")


def calc_references(formula):
    """Write references to other sheets the way LibreOffice reads them: Budget!B3 and 'My Sheet'!A1:B2
    (as in Excel) become $Budget.B3 and $'My Sheet'.A1:B2. Text in double quotes is left alone, and so
    is A1:B5!B2:C6, where ! is LibreOffice's intersection operator."""
    if not formula.startswith("=") or "!" not in formula:
        return formula

    def sheet(match):
        name = match.group(1)
        return match.group(0) if _CELL_LIKE.match(name) else "$%s." % name

    parts = formula.split('"')
    for i in range(0, len(parts), 2):  # the even parts are outside string literals
        parts[i] = _SHEET_REFERENCE.sub(sheet, parts[i])
    return '"'.join(parts)


_PERCENT = re.compile(r"^[-+]?\d[\d,]*(\.(\d+))?\s*%$")
_CURRENCY = re.compile(r"^-?\$\s*\d[\d,]*(\.\d+)?$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_TIME = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?$")
_SLASH_DATE = re.compile(r"^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$")


def typed_format(text):
    """The number format a person would get by typing this into a cell (45% shows as 45%, not 0.45),
    or None to leave the cell's format alone."""
    text = text.strip()
    match = _PERCENT.match(text)
    if match:
        return "0.%s%%" % ("0" * len(match.group(2))) if match.group(2) else "0%"
    if _CURRENCY.match(text):
        return "$#,##0.00"
    if _THOUSANDS.match(text):
        return "#,##0.00" if "." in text else "#,##0"
    match = _TIME.match(text)
    if match:
        seconds = ":SS" if match.group(1) else ""
        return "H:MM%s AM/PM" % seconds if match.group(2) else "HH:MM%s" % seconds
    return None


def slash_date(text):
    """True for a date such as 03/04/2026, which reads as 4 March in one country and 3 April in
    another, so it isn't written."""
    return bool(_SLASH_DATE.match(text.strip()))


def display(formula, value, shown=None):
    """The text for one cell.

    `formula` is what the formula bar holds, `value` the cell's value (a float,
    a string, or None for an empty cell), and `shown` the text the cell
    displays, which is used for formula results and for dates.
    """
    if formula.startswith("="):
        result = shown if shown is not None else ("" if value is None else str(value))
        return "%s → %s" % (formula, result)
    if isinstance(value, float):
        return shown if shown is not None else number_text(value)
    if isinstance(value, str):
        # Text that would read back as a number is marked, as in a spreadsheet.
        text = "'" + value if _NUMBERISH.match(value.strip()) else value
        return text
    return ""


def clean(text):
    """Make cell text safe for a one-line row."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " / ").replace("|", "\\|")
    if len(text) > MAX_CELL_TEXT:
        text = text[:MAX_CELL_TEXT] + "…[+%d characters]" % (len(text) - MAX_CELL_TEXT)
    return text


def render(c1, r1, rows, columns=None):
    """Draw cell texts as a grid with column letters and row numbers.

    `rows` is a list of lists of strings, starting at zero-based column `c1`
    and row `r1`. Rows with no content are left out; the row numbers show gaps.
    `columns` lists the zero-based column of each cell position, when some columns are left out.
    """
    width = max((len(row) for row in rows), default=0)
    letters = [col_letters(columns[i] if columns else c1 + i) for i in range(width)]
    lines = ["     | " + " | ".join(letters)]
    for offset, row in enumerate(rows):
        if not any(row):
            continue
        cells = [clean(text) for text in row] + [""] * (width - len(row))
        lines.append("%4d | %s" % (r1 + offset + 1, " | ".join(cells)))
    return "\n".join(lines)
