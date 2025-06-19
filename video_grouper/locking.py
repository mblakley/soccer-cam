import os
import time
import logging

logger = logging.getLogger(__name__)

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

    def acquire(self):
        """
        Acquire the lock, waiting until the file is free or timeout is reached.
        """
        start_time = time.time()
        while True:
            try:
                # Atomically create and open the lock file
                self.fd = os.open(self.lock_file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self._is_locked = True
                logger.debug(f"Acquired lock on {self.lock_file_path}")
                return
            except FileExistsError:
                if time.time() - start_time >= self.timeout:
                    logger.error(f"Timeout acquiring lock on {self.lock_file_path}")
                    raise TimeoutError(f"Could not acquire lock on {self.lock_file_path}")
                time.sleep(self.delay)
            except Exception as e:
                logger.error(f"Unexpected error acquiring lock on {self.lock_file_path}: {e}")
                raise

    def release(self):
        """
        Release the lock by closing the file descriptor and deleting the lock file.
        """
        if self._is_locked and self.fd is not None:
            try:
                os.close(self.fd)
                os.remove(self.lock_file_path)
                self._is_locked = False
                self.fd = None
                logger.debug(f"Released lock on {self.lock_file_path}")
            except Exception as e:
                logger.error(f"Error releasing lock on {self.lock_file_path}: {e}")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __del__(self):
        # Ensure the lock is released if the object is destroyed
        self.release() 