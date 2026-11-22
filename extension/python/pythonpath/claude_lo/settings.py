"""Settings stored in the user's LibreOffice profile folder.

The API key is kept in a JSON file that only the user can read (mode 0600 on
Linux and macOS). It is plain text on disk. ANTHROPIC_API_KEY in the
environment is used when no key has been saved.
"""

import json
import os

FILE_NAME = "claude_libreoffice.json"


class Settings(object):
    def __init__(self, folder):
        self.path = os.path.join(folder, FILE_NAME)
        self._data = {}
        self._load()

    def _load(self):
        """Read the file again, so a change made through another window is seen here."""
        self._data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._data = data
        except (OSError, ValueError):
            pass

    def has_saved_key(self):
        self._load()
        return bool(self._data.get("api_key"))

    def api_key(self):
        self._load()
        return self._data.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""

    def model(self, default):
        self._load()
        return self._data.get("model") or default

    def track_changes(self):
        self._load()
        return bool(self._data.get("track_changes"))

    def save(self, api_key=None, model=None, track_changes=None):
        """Save the given values. `api_key=None` keeps the saved key; "" removes it."""
        self._load()
        if track_changes is not None:
            self._data["track_changes"] = bool(track_changes)
        if api_key is not None:
            if api_key:
                self._data["api_key"] = api_key
            else:
                self._data.pop("api_key", None)
        if model:
            self._data["model"] = model
        # Write a new file and swap it in, so a failed write leaves the saved key as it was.
        temporary = self.path + ".tmp"
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(self._data, out)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
