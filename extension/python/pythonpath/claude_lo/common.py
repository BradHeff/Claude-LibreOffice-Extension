"""Pieces shared by every app adapter (Writer, Calc, Impress). Runs inside LibreOffice."""

UNDO_TITLE = "Claude"


class DocumentError(Exception):
    """A problem to show the user, or to send back to Claude as a tool error."""


def short(text, limit=60):
    """A shortened copy of text for the transcript and tool results."""
    return text if len(text) <= limit else text[:limit - 3] + "..."


def begin_undo(doc):
    doc.getUndoManager().enterUndoContext(UNDO_TITLE)


def end_undo(doc):
    doc.getUndoManager().leaveUndoContext()


def tool(name, description, properties, required=None):
    """A tool definition. By default it is strict: every property is required and nothing else
    is allowed. Pass `required` (a shorter list) for a tool whose many options would make the
    API's compiled grammar too large; the handler must then supply a default for each option
    that is left out."""
    schema = {"type": "object", "properties": properties,
              "required": list(properties) if required is None else list(required),
              "additionalProperties": False}
    result = {"name": name, "description": description, "input_schema": schema}
    if required is None:
        result["strict"] = True
    return result


_TYPES = {"string": (str,), "boolean": (bool,), "array": (list, tuple), "object": (dict,)}


def _type_ok(value, wanted):
    if wanted == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if wanted == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if wanted in _TYPES:
        return isinstance(value, _TYPES[wanted])
    return True


def check_arguments(definition, arguments):
    """Check a tool call against its schema, so Claude gets an error it can act on instead of a
    bare failure. Raises DocumentError naming the argument and what is allowed."""
    schema = definition["input_schema"]
    name = definition["name"]
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            raise DocumentError('%s needs the argument "%s".' % (name, key))
    for key, value in arguments.items():
        if key not in properties:
            raise DocumentError('%s has no argument "%s". Its arguments are: %s.'
                                % (name, key, ", ".join(properties)))
        rule = properties[key]
        if not _type_ok(value, rule.get("type")):
            raise DocumentError('%s: "%s" must be a %s.' % (name, key, rule.get("type")))
        if "enum" in rule and value not in rule["enum"]:
            raise DocumentError('%s: "%s" must be one of %s, not "%s".'
                                % (name, key, ", ".join(rule["enum"]), value))
        if rule.get("type") == "array" and "items" in rule:
            wanted = rule["items"].get("type")
            if not all(_type_ok(item, wanted) for item in value):
                raise DocumentError('%s: every item of "%s" must be a %s.' % (name, key, wanted))


ON_OFF = {"type": "string", "enum": ["keep", "on", "off"]}
POSITION = {"type": "string", "enum": ["before", "after"]}
