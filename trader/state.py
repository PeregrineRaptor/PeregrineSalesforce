import json
import os
from pathlib import Path


class State:
    """All bot memory (paper account, OI history, cooldowns) in one JSON file, so restarts lose nothing."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)
