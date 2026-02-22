import os
import time
import logging

logger = logging.getLogger(__name__)

# Lock files older than this are considered stale (seconds)
STALE_LOCK_AGE = 60


class FileLock:
    """
    A context manager for file-based locking to prevent race conditions.
    """

    def __init__(self, file_path, timeout=10, delay=0.1):
        self.lock_file_path = f"{file_path}.lock"
        self.timeout = timeout
        self.delay = delay
        self._is_locked = False
        self.fd = None

    def _remove_stale_lock(self):
        """Remove lock file if it appears stale (older than STALE_LOCK_AGE seconds)."""
        try:
            if os.path.exists(self.lock_file_path):
                age = time.time() - os.path.getmtime(self.lock_file_path)
                if age > STALE_LOCK_AGE:
                    logger.warning(
                        f"Removing stale lock file ({age:.0f}s old): {self.lock_file_path}"
                    )
                    os.remove(self.lock_file_path)
        except OSError:
            pass  # File may have been removed by another process

    def acquire(self):
        """
        Acquire the lock, waiting until the file is free or timeout is reached.
        """
        start_time = time.time()
        stale_checked = False
        while True:
            try:
                # Atomically create and open the lock file
                self.fd = os.open(
                    self.lock_file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                self._is_locked = True
                logger.debug(f"Acquired lock on {self.lock_file_path}")
                return
            except FileExistsError:
                # On first contention, check for stale locks
                if not stale_checked:
                    self._remove_stale_lock()
                    stale_checked = True
                    continue  # Retry immediately after stale removal

                if time.time() - start_time >= self.timeout:
                    logger.error(f"Timeout acquiring lock on {self.lock_file_path}")
                    raise TimeoutError(
                        f"Could not acquire lock on {self.lock_file_path}"
                    )
                time.sleep(self.delay)
            except Exception as e:
                logger.error(
                    f"Unexpected error acquiring lock on {self.lock_file_path}: {e}"
                )
                raise

    def release(self):
        """
        Release the lock by closing the file descriptor and deleting the lock file.
        """
        if self._is_locked and self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass  # Already closed
            try:
                os.remove(self.lock_file_path)
            except OSError:
                pass  # Already removed
            self._is_locked = False
            self.fd = None
            logger.debug(f"Released lock on {self.lock_file_path}")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __del__(self):
        # Ensure the lock is released if the object is destroyed
        self.release()
