"""The Claude sidebar panel for LibreOffice Writer (UNO component).

Threading: the API request runs on a worker thread. Everything that touches
the document or a control runs on LibreOffice's main thread, reached through
`_on_main`.

The transcript is a rich text control. It can't be scrolled from code, so the
panel shows the current exchange from the top and only ever adds text at the
end while a reply streams in; "Show earlier" switches to the full history.
"""

import threading
import time
import traceback

import uno
import unohelper
from com.sun.star.awt import (XActionListener, XCallback, XKeyListener, XMouseListener,
                              XWindowListener)
from com.sun.star.awt.FontSlant import ITALIC as SLANT_ITALIC, NONE as SLANT_NONE
from com.sun.star.lang import XComponent, XServiceInfo
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement, XUIElementFactory
from com.sun.star.ui.UIElementType import TOOLPANEL

from claude_lo import api, apps, chat, common, outline, render, writer
from claude_lo.settings import Settings

IMPLEMENTATION_NAME = "io.github.bradheff.claude_libreoffice.PanelFactory"
SERVICE_NAME = "com.sun.star.ui.UIElementFactory"

PRIVACY_NOTE = ("Your message, and the document text you choose to send, go to "
                "Anthropic's API. Claude can edit the open document when you ask it to.")
HINT = "Enter sends. Shift+Enter starts a new line."
NO_KEY = "Add your Anthropic API key in Settings to start."

ACTIVITY = {
    "waiting": "Sending to Claude",
    "thinking": "Claude is thinking",
    "writing": "Claude is writing",
    "preparing": "Claude is working out an edit",
    "editing": "Editing the document (please wait)",
}
SPINNER = "◐◓◑◒"

# Warm neutrals with a terracotta accent, in dark and light versions.
PALETTES = {
    True: {"bg": 0x262624, "surface": 0x30302E, "border": 0x4A4944, "text": 0xF0EEE6,
           "muted": 0xA6A39A, "accent": 0xD97757, "on_accent": 0xFFFFFF, "error": 0xF0857A,
           "button": 0x3A3936, "hover": 0x484742, "accent_hover": 0xE38C6E, "dim": 0x6E6C65},
    False: {"bg": 0xFAF9F5, "surface": 0xFFFFFF, "border": 0xD9D5C8, "text": 0x1F1E1D,
            "muted": 0x6F6D66, "accent": 0xC96442, "on_accent": 0xFFFFFF, "error": 0xB3261E,
            "button": 0xEFEDE4, "hover": 0xE2DFD3, "accent_hover": 0xB5573A, "dim": 0xA9A69C},
}
MONO_FONT = "Liberation Mono"
KEY_RETURN = 1280
KEY_ESCAPE = 1281
MOD_SHIFT = 1


class _Callback(unohelper.Base, XCallback):
    def __init__(self, function):
        self.function = function

    def notify(self, data):
        try:
            self.function()
        except Exception:
            traceback.print_exc()


class _Press(unohelper.Base, XMouseListener):
    """Lights a button when the pointer is over it. The button's own action does the work."""

    def __init__(self, panel, name):
        self.panel, self.name = panel, name

    def mousePressed(self, event):
        pass

    def mouseReleased(self, event):
        pass

    def mouseEntered(self, event):
        self.panel._paint(self.name, hover=True)

    def mouseExited(self, event):
        self.panel._paint(self.name)

    def disposing(self, event):
        pass


class ClaudePanel(unohelper.Base, XUIElement, XToolPanel, XSidebarPanel, XComponent,
                  XActionListener, XWindowListener, XKeyListener):
    def __init__(self, ctx, frame, parent, url):
        self.ctx = ctx
        self.smgr = ctx.ServiceManager
        self.Frame = frame
        self.ResourceURL = url
        self.Type = TOOLPANEL
        self.conversation = chat.Conversation()
        self.settings = Settings(self._profile_folder())
        self._async = self.smgr.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
        self._lock = threading.Lock()
        self._pending = ""
        self._flush_scheduled = False
        self._cancel = threading.Event()
        self._busy = False
        self._disposed = False
        self._undo_doc = None
        self._tracking_before = None
        self._edits = 0
        self._usage = {}
        self._baseline = None  # the layout problems the file had before this turn's first edit
        self._layout_left = 0
        self._last_reply = ""
        # Each turn: {"question", "note", "items": [("text", str) | ("edit", str) | ("error", str)]}
        self._turns = []
        self._show_all = False
        self._phase = "waiting"
        self._started = 0.0
        # Streaming state for the transcript.
        self._parser = render.LineParser()
        self._partial = ""
        self._has_provisional = False
        self._provisional_length = 0
        self._empty = True
        self._buttons = {}
        self.app = self._find_app() or writer
        self._build(parent)

    # --- window -----------------------------------------------------------

    def _profile_folder(self):
        paths = self.smgr.createInstanceWithContext("com.sun.star.util.PathSubstitution", self.ctx)
        return uno.fileUrlToSystemPath(paths.substituteVariables("$(user)", True))

    def _control(self, name, kind, service=None, **properties):
        model = self.smgr.createInstanceWithContext(
            service or "com.sun.star.awt.UnoControl%sModel" % kind, self.ctx)
        for key, value in properties.items():
            setattr(model, key, value)
        control = self.smgr.createInstanceWithContext(
            model.DefaultControl if service else "com.sun.star.awt.UnoControl%s" % kind, self.ctx)
        control.setModel(model)
        self.Window.addControl(name, control)
        return control

    def _button(self, name, label, primary=False):
        """A push button, so the keyboard (Tab, Space, Enter) and screen readers can reach it."""
        button = self._control(name, "Button", Label=label, Tabstop=True, FocusOnClick=False)
        if primary:
            button.getModel().FontWeight = 150.0
        button.setActionCommand(name)
        button.addActionListener(self)
        self._buttons[name] = (button, primary)
        self._paint(name)
        button.addMouseListener(_Press(self, name))
        return button

    def _paint(self, name, hover=False):
        if self._disposed or name not in self._buttons:
            return
        button, primary = self._buttons[name]
        model, colours = button.getModel(), self.colours
        if not model.Enabled:
            model.BackgroundColor, model.TextColor = colours["button"], colours["dim"]
        elif primary:
            model.BackgroundColor = colours["accent_hover" if hover else "accent"]
            model.TextColor = colours["on_accent"]
        else:
            model.BackgroundColor = colours["hover" if hover else "button"]
            model.TextColor = colours["text"]

    def _enable(self, name, enabled):
        self._buttons[name][0].getModel().Enabled = enabled
        self._paint(name)

    def _theme(self):
        """Pick the dark or light palette, and the UI font, from LibreOffice's theme."""
        dark, font, height = False, "", 10.0
        try:
            style = self.Window.getPeer().StyleSettings
            colour = style.DialogColor
            red, green, blue = (colour >> 16) & 255, (colour >> 8) & 255, colour & 255
            dark = (red * 299 + green * 587 + blue * 114) / 1000 < 128
            font = style.LabelFont.Name
            if 6 <= style.LabelFont.Height <= 24:
                height = float(style.LabelFont.Height)
        except Exception:
            pass
        self.colours = PALETTES[dark]
        self.font, self.font_height = font, height

    def _build(self, parent):
        self.Window = self.smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlContainer", self.ctx)
        container_model = self.smgr.createInstanceWithContext(
            "com.sun.star.awt.UnoControlContainerModel", self.ctx)
        self.Window.setModel(container_model)
        self.Window.createPeer(parent.getToolkit(), parent)
        self._theme()
        colours = self.colours
        container_model.BackgroundColor = colours["bg"]

        self.new_chat = self._button("new", "New chat")
        self.earlier = self._button("earlier", "Show earlier")
        self.configure = self._button("settings", "Settings")
        self.transcript = self._control(
            "transcript", None, service="com.sun.star.form.component.RichTextControl",
            RichText=True, MultiLine=True, ReadOnly=True, VScroll=True, Border=0,
            BackgroundColor=colours["bg"], HideInactiveSelection=True)
        self.activity = self._control("activity", "FixedText", Label="",
                                      TextColor=colours["accent"], BackgroundColor=colours["bg"])
        self.input = self._control("input", "Edit", MultiLine=True, Border=2,
                                   BorderColor=colours["border"], TextColor=colours["text"],
                                   BackgroundColor=colours["surface"], HelpText=HINT)
        self.input.addKeyListener(self)
        self.context = self._control("context", "ListBox", Dropdown=True, Border=2,
                                     BorderColor=colours["border"], TextColor=colours["text"],
                                     BackgroundColor=colours["surface"],
                                     HelpText="What Claude is sent from your file")
        self.context.addItems(tuple(label for _, label in self.app.CHOICES), 0)
        self.context.selectItemPos(0, True)
        self.send = self._button("send", "Send", primary=True)
        self.insert = self._button("insert", "Insert at cursor")
        self.replace = self._button("replace", "Replace selection")
        self.status = self._control("status", "FixedText", MultiLine=True, Label="",
                                    TextColor=colours["muted"], BackgroundColor=colours["bg"])

        self._apply_app_layout()
        self._set_busy(False)
        self._set_status(HINT if self.settings.api_key() else NO_KEY)
        self._redraw()
        self.Window.addWindowListener(self)
        self.Window.setVisible(True)
        self._layout()

    def _find_app(self):
        """The adapter for this window's document, or None while it can't be told."""
        try:
            controller = self.Frame.getController() if self.Frame else None
            return apps.adapter_for(controller.getModel() if controller else None)
        except Exception:
            return None

    def _apply_app_layout(self):
        """Show or hide the Writer-only buttons for the current app."""
        for name in ("insert", "replace"):
            self._buttons[name][0].setVisible(self.app.SUPPORTS_INSERT)

    def _use_app(self, app):
        """Switch to another app's labels and tools, once the document type is known."""
        if app is self.app:
            return
        self.app = app
        self.context.removeItems(0, self.context.getItemCount())
        self.context.addItems(tuple(label for _, label in app.CHOICES), 0)
        self.context.selectItemPos(0, True)
        self._apply_app_layout()
        self._layout()
        if not self._turns:
            self._redraw()

    def _layout(self):
        size = self.Window.getPosSize()
        width, height = size.Width, size.Height
        if width <= 0 or height <= 0:
            return
        gap, row, flags = 8, 28, 15
        inner = max(width - 2 * gap, 60)
        third = (inner - 2 * gap) // 3
        for i, button in enumerate((self.new_chat, self.earlier, self.configure)):
            button.setPosSize(gap + i * (third + gap), gap, third, row, flags)
        top = gap + row + gap

        y = height - gap - 36
        self.status.setPosSize(gap, y, inner, 36, flags)
        half = (inner - gap) // 2
        if self.app.SUPPORTS_INSERT:
            y -= gap + row
            self.insert.setPosSize(gap, y, half, row, flags)
            self.replace.setPosSize(gap + half + gap, y, inner - half - gap, row, flags)
        send_width = min(84, inner // 3)
        y -= gap + row
        self.context.setPosSize(gap, y, inner - send_width - gap, row, flags)
        self.send.setPosSize(gap + inner - send_width, y, send_width, row, flags)
        y -= gap + 84
        self.input.setPosSize(gap, y, inner, 84, flags)
        y -= 4 + 20
        self.activity.setPosSize(gap, y, inner, 20, flags)
        self.transcript.setPosSize(gap, top, inner, max(y - top - 4, 20), flags)

    # --- XUIElement, XToolPanel, XSidebarPanel, XComponent ----------------

    def getRealInterface(self):
        return self

    def createAccessible(self, parent):
        return self.Window.getAccessibleContext()

    def getHeightForWidth(self, width):
        return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 460, -1, 760)

    def getMinimalWidth(self):
        return 240

    def dispose(self):
        self._disposed = True
        self._cancel.set()
        if self.Window is not None:
            self.Window.dispose()
            self.Window = None

    def addEventListener(self, listener):
        pass

    def removeEventListener(self, listener):
        pass

    # --- listeners --------------------------------------------------------

    def windowResized(self, event):
        self._layout()

    def windowMoved(self, event):
        pass

    def windowShown(self, event):
        self._layout()

    def windowHidden(self, event):
        pass

    def disposing(self, event):
        pass

    def keyPressed(self, event):
        # Enter sends; Shift+Enter is left alone to add a line. The send runs
        # after the key has been handled, so the line break it adds is dropped.
        if event.KeyCode == KEY_RETURN and not event.Modifiers & MOD_SHIFT:
            self._on_main(lambda: self._handle("enter"))
        elif event.KeyCode == KEY_ESCAPE and self._busy:
            self._on_main(lambda: self._handle("send"))  # while busy, the send button is Stop

    def keyReleased(self, event):
        pass

    def actionPerformed(self, event):
        self._handle(event.ActionCommand)

    def _handle(self, command):
        if self._disposed:
            return
        if command in self._buttons and not self._buttons[command][0].getModel().Enabled:
            return
        try:
            if command == "send":
                if self._busy:
                    self._cancel.set()
                    self._set_status("Stopping…")
                else:
                    self._send()
            elif command == "enter":
                if not self._busy:
                    self._send()
            elif command == "insert":
                self._apply(writer.insert_at_cursor)
            elif command == "replace":
                self._apply(writer.replace_selection)
            elif command == "new":
                self._new_chat()
            elif command == "earlier":
                self._show_all = not self._show_all
                self._redraw()
            elif command == "settings":
                self._open_settings()
        except common.DocumentError as error:
            self._set_status(str(error))
        except Exception:
            traceback.print_exc()
            self._set_status("Something went wrong. Nothing was changed.")

    # --- helpers ----------------------------------------------------------

    def _on_main(self, function):
        self._async.addCallback(_Callback(function), None)

    def _doc(self):
        """The open document, with the panel switched to match its type."""
        controller = self.Frame.getController() if self.Frame else None
        doc = controller.getModel() if controller else None
        app = apps.adapter_for(doc)
        if app is None:
            raise common.DocumentError(
                "Claude works in Writer, Calc and Impress documents. This window is something else.")
        self._use_app(app)
        return doc

    def _set_status(self, text):
        self.status.getModel().Label = text

    def _set_busy(self, busy):
        self._busy = busy
        self.send.getModel().Label = "Stop" if busy else "Send"
        self._enable("new", not busy)
        self._enable("earlier", not busy and len(self._turns) > 1)
        self.earlier.getModel().Label = "Show latest" if self._show_all else "Show earlier"
        has_reply = bool(self._last_reply) and not busy
        self._enable("insert", has_reply)
        self._enable("replace", has_reply)
        if not busy:
            self.activity.getModel().Label = ""

    # --- transcript drawing -----------------------------------------------

    def _paragraph(self, runs, colour=None, bold=False, italic=False, size=0.0, top=0, bottom=120,
                   left=0, hanging=0):
        """Add one paragraph at the end of the transcript."""
        model = self.transcript.getModel()
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        if not self._empty:
            model.insertString(cursor, "\n", False)
        self._empty = False
        cursor.ParaTopMargin = top
        cursor.ParaBottomMargin = bottom
        cursor.ParaLeftMargin = left
        cursor.ParaFirstLineIndent = -hanging
        base = self.colours["text"] if colour is None else colour
        for text, style in runs:
            cursor.CharColor = self.colours["accent"] if style == render.CODE else base
            cursor.CharWeight = 150.0 if bold or style == render.BOLD else 100.0
            cursor.CharPosture = SLANT_ITALIC if italic or style == render.ITALIC else SLANT_NONE
            cursor.CharHeight = self.font_height + size
            if style == render.CODE:
                cursor.CharFontName = MONO_FONT
            elif self.font:
                cursor.CharFontName = self.font
            model.insertString(cursor, text, False)

    def _markdown_paragraph(self, paragraph):
        kind, top = paragraph["kind"], 160 if paragraph["gap"] else 0
        indent = 350 * paragraph["indent"]
        if kind == "h":
            self._paragraph(paragraph["runs"], bold=True, size=1.0, top=top + 120, bottom=100)
        elif kind in ("bullet", "number"):
            self._paragraph(paragraph["runs"], top=top, bottom=60, left=450 + indent, hanging=450)
        elif kind == "code":
            self._paragraph(paragraph["runs"], top=top, bottom=0, left=250)
        else:
            self._paragraph(paragraph["runs"], top=top)

    def _drop_provisional(self):
        """Remove the unfinished last line, so it can be drawn again with more text."""
        if not self._has_provisional:
            return
        cursor = self.transcript.getModel().createTextCursor()
        cursor.gotoEnd(False)
        # The line plus the paragraph break before it. goLeft takes a 16-bit count.
        remaining = self._provisional_length + 1
        while remaining > 0:
            cursor.goLeft(min(remaining, 30000), True)
            remaining -= 30000
        cursor.setString("")
        self._has_provisional = False

    def _stream_text(self, text, final=False):
        """Draw reply text as it arrives. Finished lines are drawn once and left alone."""
        self._drop_provisional()
        lines = (self._partial + text).split("\n")
        self._partial = "" if final else lines.pop()
        for line in lines:
            paragraph = self._parser.parse(chat.display_text(line))
            if paragraph:
                self._markdown_paragraph(paragraph)
        if self._partial.strip():
            # Draw the unfinished line plainly; it is redrawn when it grows.
            shown = chat.display_text(self._partial)
            self._paragraph([(shown, render.PLAIN)], top=160 if self._parser.after_blank else 0)
            self._provisional_length = outline.utf16_len(shown)
            self._has_provisional = True

    def _note(self, text, error=False):
        self._stream_text("", final=True)
        self._paragraph([(("⚠  " if error else "✓  ") + text, render.PLAIN)],
                        colour=self.colours["error" if error else "muted"],
                        size=-1.0, top=40, bottom=40, left=450, hanging=450)
        self._parser.after_blank = True

    def _draw_turn_heading(self, turn, first):
        note = "  ·  with %s" % turn["note"] if turn["note"] else ""
        self._paragraph([("You", render.BOLD), (note, render.PLAIN)], colour=self.colours["muted"],
                        size=-1.0, top=0 if first else 500, bottom=60)
        for line in turn["question"].split("\n"):
            self._paragraph([(line, render.PLAIN)], bottom=40)
        self._paragraph([("Claude", render.BOLD)], colour=self.colours["accent"],
                        size=-1.0, top=260, bottom=60)
        self._parser = render.LineParser()
        self._partial = ""
        self._has_provisional = False

    def _redraw(self):
        """Draw the transcript from scratch: the latest exchange, or all of them."""
        model = self.transcript.getModel()
        cursor = model.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.setString("")
        self._empty = True
        self._has_provisional = False
        turns = self._turns if self._show_all else self._turns[-1:]
        if not turns:
            self._paragraph([("Claude", render.BOLD)], colour=self.colours["accent"], size=3.0,
                            bottom=160)
            self._paragraph([(self.app.WELCOME, render.PLAIN)], colour=self.colours["muted"])
        for i, turn in enumerate(turns):
            self._draw_turn_heading(turn, i == 0)
            for kind, text in turn["items"]:
                if kind == "text":
                    self._stream_text(text, final=True)
                else:
                    self._note(text, error=kind == "error")
        self._set_busy(self._busy)

    # --- actions ----------------------------------------------------------

    def _send(self):
        question = self.input.getModel().Text.strip()
        if not question:
            self.input.getModel().Text = ""
            return
        api_key = self.settings.api_key()
        if not api_key:
            self._set_status(NO_KEY)
            return
        doc = self._doc()
        app = self.app
        kind = app.CHOICES[max(self.context.getSelectedItemPos(), 0)][0]
        # Tools that read other parts of the file are left out when the person chose to send none of it.
        tools = chat.tools_for(kind, app.TOOLS, getattr(app, "READ_ONLY_TOOLS", frozenset()))
        if hasattr(app, "pin"):
            app.pin(doc)
        text = ""
        note = ""
        if kind != chat.CONTEXT_NONE:
            if kind in (chat.CONTEXT_AUTO, chat.CONTEXT_SELECTION):
                text = app.selection_text(doc)
                if kind == chat.CONTEXT_AUTO:
                    kind = chat.CONTEXT_SELECTION if text else chat.CONTEXT_DOCUMENT
            if kind == chat.CONTEXT_DOCUMENT:
                text = app.whole_text(doc)
            note = app.context_note(kind, text) if text.strip() else ""
            if not text.strip():
                kind = chat.CONTEXT_NONE
        try:
            self.conversation.add_user(question, kind, text, app.TAGS)
        except chat.ContextTooLarge as error:
            self._set_status(str(error))
            return

        self.input.getModel().Text = ""
        self._turns.append({"question": question, "note": note, "items": []})
        self._show_all = False
        self._pending = ""
        self._edits = 0
        self._baseline = None
        self._layout_left = 0
        self._cancel.clear()
        self._phase = "waiting"
        self._started = time.time()
        self._set_busy(True)
        self._set_status("")
        self._redraw()
        self._tick()
        model = self.settings.model(api.DEFAULT_MODEL)
        worker = threading.Thread(target=self._work, args=(api_key, model, tools))
        worker.daemon = True
        worker.start()
        ticker = threading.Thread(target=self._ticker)
        ticker.daemon = True
        ticker.start()

    def _ticker(self):
        while self._busy and not self._disposed:
            time.sleep(0.5)
            self._on_main(self._tick)

    def _tick(self):
        if self._disposed or not self._busy:
            return
        elapsed = time.time() - self._started
        frame = SPINNER[int(elapsed * 2) % len(SPINNER)]
        self.activity.getModel().Label = "%s  %s…  %ds" % (
            frame, ACTIVITY[self._phase], int(elapsed))

    def _set_phase(self, phase):
        self._phase = phase

    def _work(self, api_key, model, tools):
        result = error = None
        reply = ""
        self._usage = {}
        try:
            result, reply = chat.run_turn(api_key, model, self.conversation, self._execute_tool,
                                          self.app.SYSTEM_PROMPT, tools,
                                          on_text=self._on_text, cancelled=self._cancel.is_set,
                                          on_activity=self._set_phase, usage=self._usage,
                                          review=self._review)
        except api.Cancelled:
            error = "Stopped."
        except api.ApiError as failure:
            error = str(failure)
        except Exception:
            traceback.print_exc()
            error = "Something went wrong while talking to Claude."
        self._on_main(lambda: self._finish(result, reply, error))

    def _execute_tool(self, name, arguments):
        """Called on the worker thread: run one edit on the main thread and wait for it."""
        done = threading.Event()
        outcome = {}

        def run():
            try:
                outcome["text"] = self._run_tool(name, arguments)
            except common.DocumentError as problem:
                outcome["error"] = str(problem)
            except Exception:
                traceback.print_exc()
                outcome["error"] = "The edit failed inside LibreOffice."
            finally:
                done.set()

        self._on_main(run)
        while not done.wait(0.2):
            if self._disposed:
                raise api.Cancelled()
        if "error" in outcome:
            return outcome["error"], True
        return outcome["text"], False

    def _run_tool(self, name, arguments):
        """Run one tool. Returns the text for Claude; the transcript gets a shorter note."""
        doc = self._doc()
        definition = next((t for t in self.app.TOOLS if t["name"] == name), None)
        if definition is None:
            raise common.DocumentError("There is no tool called %s." % name)
        common.check_arguments(definition, arguments)  # before any undo group is opened
        reads = name in getattr(self.app, "READ_ONLY_TOOLS", ())
        if self._undo_doc is None and not reads:
            if hasattr(self.app, "layout_problems"):
                try:
                    self._baseline = set(key for key, _ in self.app.layout_problems(doc))
                except Exception:
                    traceback.print_exc()
            common.begin_undo(doc)
            self._undo_doc = doc
            if self.app.SUPPORTS_TRACKING and self.settings.track_changes():
                self._tracking_before = writer.set_tracking(doc, True)
        outcome = self.app.run_tool(doc, name, arguments)
        result, note = outcome if isinstance(outcome, tuple) else (outcome, outcome)
        if not reads:
            self._edits += 1
        self._flush()
        self._turns[-1]["items"].append(("edit", note))
        self._note(note)
        return result

    def _review(self, final):
        """Called on the worker thread when Claude finishes: measure the file on the main thread and
        return the problems this turn made, for Claude to fix. Nothing in the file is changed."""
        if not self._edits or self._baseline is None or not hasattr(self.app, "layout_problems"):
            return []
        done = threading.Event()
        found = {"problems": []}

        def run():
            try:
                problems = [text for key, text in self.app.layout_problems(self._doc())
                            if key not in self._baseline][:12]
                found["problems"] = problems
                self._flush()
                count = len(problems)
                plural = "" if count == 1 else "s"
                if not problems:
                    self._add_note("Checked the layout: no problems found")
                elif final:
                    self._layout_left = count
                    self._add_note("Layout check: %d problem%s still there. Please check:" % (count, plural),
                                   error=True)
                else:
                    self._add_note("Layout check found %d problem%s; asking Claude to fix %s"
                                   % (count, plural, "it" if count == 1 else "them"), error=True)
                for text in problems:
                    self._add_note(text, error=True)
            except Exception:
                traceback.print_exc()
            finally:
                done.set()

        self._on_main(run)
        while not done.wait(0.2):
            if self._disposed:
                raise api.Cancelled()
        return found["problems"]

    def _add_note(self, text, error=False):
        self._turns[-1]["items"].append(("error" if error else "edit", text))
        self._note(text, error=error)

    def _on_text(self, delta):
        with self._lock:
            self._pending += delta
            if self._flush_scheduled:
                return
            self._flush_scheduled = True
        self._on_main(self._flush)

    def _flush(self):
        with self._lock:
            delta, self._pending = self._pending, ""
            self._flush_scheduled = False
        if self._disposed or not self._busy or not delta:
            return
        items = self._turns[-1]["items"]
        if items and items[-1][0] == "text":
            items[-1] = ("text", items[-1][1] + delta)
        else:
            items.append(("text", delta))
        self._stream_text(delta)

    def _finish(self, result, reply, error):
        tracked = False
        if self._undo_doc is not None:
            try:
                if self._tracking_before is not None:
                    writer.set_tracking(self._undo_doc, self._tracking_before)
                    self._tracking_before = None
                    tracked = True
                common.end_undo(self._undo_doc)
            except Exception:
                traceback.print_exc()
            self._undo_doc = None
        if self._disposed:
            return
        self._flush()
        self._stream_text("", final=True)
        undo_hint = ""
        if self._edits:
            undo_hint = " Claude made %d change%s to the %s; Edit > Undo reverts %s." % (
                self._edits, "" if self._edits == 1 else "s", self.app.NOUN,
                "it" if self._edits == 1 else "them in one step")
            if tracked:
                undo_hint += " They are tracked changes: accept or reject them in Edit > Track Changes."
        if result is not None and result.stop_reason == "refusal":
            error = "Claude declined this request. Try rewording it."
        elif result is not None and not reply and not self._edits:
            error = "Claude returned an empty reply. Try again."
        if error:
            self.conversation.rollback_user()
            self._turns[-1]["items"].append(("error", error))
            self._note(error, error=True)
            self._set_status(error + undo_hint)
        else:
            if reply:
                self._last_reply = reply
            status = "Done."
            if result.stop_reason == "max_tokens":
                status = "The reply was cut off at the length limit."
            if self._usage.get("input_tokens") or self._usage.get("output_tokens"):
                status += " %s tokens in, %s out." % (format(self._usage.get("input_tokens", 0), ","),
                                                      format(self._usage.get("output_tokens", 0), ","))
            if self._layout_left:
                status += " %d layout problem%s left; see the transcript." % (
                    self._layout_left, "" if self._layout_left == 1 else "s")
            self._set_status(status + undo_hint)
        if self._edits:
            # An Undo can bring the text back to what Claude was last sent, so send it again next time.
            self.conversation.forget_context()
        self._set_busy(False)

    def _apply(self, action):
        if not self._last_reply:
            return
        action(self._doc(), chat.document_text(self._last_reply))
        self._set_status("Added to the document. Edit > Undo removes it in one step.")

    def _new_chat(self):
        self.conversation.reset()
        self._turns = []
        self._last_reply = ""
        self._show_all = False
        self._set_status(HINT)
        self._redraw()

    # --- settings dialog --------------------------------------------------

    def _open_settings(self):
        smgr, ctx = self.smgr, self.ctx
        model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
        model.Title = "Claude settings"
        model.Width, model.Height = 230, 176

        def add(name, kind, x, y, width, height, **properties):
            item = model.createInstance("com.sun.star.awt.UnoControl%sModel" % kind)
            item.PositionX, item.PositionY, item.Width, item.Height = x, y, width, height
            for key, value in properties.items():
                setattr(item, key, value)
            model.insertByName(name, item)

        saved = self.settings.has_saved_key()
        add("key_label", "FixedText", 8, 8, 214, 10, Label="Anthropic API key")
        add("key", "Edit", 8, 20, 214, 14, EchoChar=42)
        add("key_note", "FixedText", 8, 36, 214, 18, MultiLine=True,
            Label="A key is saved. Leave this blank to keep it." if saved else
            "Create a key at console.anthropic.com. It is stored in your LibreOffice profile.")
        add("clear", "CheckBox", 8, 56, 214, 10, Label="Remove the saved key", Enabled=saved)
        add("model_label", "FixedText", 8, 72, 214, 10, Label="Model")
        add("model", "ListBox", 8, 84, 214, 14, Dropdown=True)
        add("track", "CheckBox", 8, 104, 214, 10, Label="Record Claude's edits as tracked changes (Writer)",
            State=1 if self.settings.track_changes() else 0)
        add("privacy", "FixedText", 8, 120, 214, 28, MultiLine=True, Label=PRIVACY_NOTE)
        add("ok", "Button", 118, 154, 50, 14, Label="OK", PushButtonType=1, DefaultButton=True)
        add("cancel", "Button", 172, 154, 50, 14, Label="Cancel", PushButtonType=2)

        dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        dialog.setModel(model)
        models = dialog.getControl("model")
        models.addItems(tuple(label for _, label in api.MODELS), 0)
        ids = [model_id for model_id, _ in api.MODELS]
        current = self.settings.model(api.DEFAULT_MODEL)
        models.selectItemPos(ids.index(current) if current in ids else 0, True)
        dialog.createPeer(smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx), None)
        try:
            if dialog.execute() != 1:
                return
            key = dialog.getControl("key").getModel().Text.strip()
            if dialog.getControl("clear").getState() == 1:
                key = ""
            elif not key:
                key = None
            self.settings.save(api_key=key, model=ids[max(models.getSelectedItemPos(), 0)],
                               track_changes=dialog.getControl("track").getState() == 1)
            self._set_status("Settings saved." if self.settings.api_key() else NO_KEY)
        finally:
            dialog.dispose()


class PanelFactory(unohelper.Base, XUIElementFactory, XServiceInfo):
    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, url, arguments):
        frame = parent = None
        for argument in arguments:
            if argument.Name == "Frame":
                frame = argument.Value
            elif argument.Name == "ParentWindow":
                parent = argument.Value
        return ClaudePanel(self.ctx, frame, parent, url)

    def getImplementationName(self):
        return IMPLEMENTATION_NAME

    def supportsService(self, name):
        return name == SERVICE_NAME

    def getSupportedServiceNames(self):
        return (SERVICE_NAME,)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PanelFactory, IMPLEMENTATION_NAME, (SERVICE_NAME,))
