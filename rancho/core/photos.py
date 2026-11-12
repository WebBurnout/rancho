"""Full-resolution archive of every snapshot taken, for identifying people later.

Telegram only gets a downscaled copy. Files go to photos/YYYY-MM-DD_HHMMSS.mmm-Camera.jpg
in the checkout, so name order is time order. When the folder exceeds MAX_BYTES,
the oldest photos are deleted.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_BYTES = 100 * 10**9


class PhotoArchive:
    def __init__(
        self,
        root: Path,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.root = root
        self._now = now
        self._max_bytes = max_bytes
        # Oldest first. Bookkeeping happens only on the event loop; threads do the I/O.
        self._files: deque[tuple[Path, int]] = deque()
        self.total_bytes = 0

    def load(self) -> None:
        """Index existing photos. Blocking; call once at startup."""
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.jpg")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self._files.append((path, size))
            self.total_bytes += size
        logger.info("Photo archive: %d photos, %.1f GB", len(self._files), self.total_bytes / 10**9)

    async def save(self, camera: str, image: bytes) -> None:
        # A full disk must not stop the alert itself.
        try:
            path = await asyncio.to_thread(self._write, camera, image)
        except OSError as exc:
            logger.warning("Could not save photo from %s (%s)", camera, type(exc).__name__)
            return
        self._files.append((path, len(image)))
        self.total_bytes += len(image)
        expired = []
        while self.total_bytes > self._max_bytes and len(self._files) > 1:
            old, size = self._files.popleft()
            self.total_bytes -= size
            expired.append(old)
        if expired:
            await asyncio.to_thread(self._delete, expired)

    def _write(self, camera: str, image: bytes) -> Path:
        now = self._now()
        self.root.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in camera)
        path = self.root / f"{now:%Y-%m-%d_%H%M%S}.{now.microsecond // 1000:03d}-{safe}.jpg"
        path.write_bytes(image)
        return path

    @staticmethod
    def _delete(paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)
