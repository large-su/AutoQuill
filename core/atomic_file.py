"""Replace a local state file after a complete, durable write."""

import os
import time
from pathlib import Path
from uuid import uuid4


def atomic_write_text(path, text, *, attempts=3):
    """Write UTF-8 beside the target and raise the last I/O error on failure."""
    path = Path(path)
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return True
        except OSError:
            if attempt + 1 == attempts:
                raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        time.sleep(0.2 * (2 ** attempt))
