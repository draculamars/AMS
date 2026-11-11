"""One-byte advisory locks shared by Windows and POSIX Atlas processes."""
import errno
import os
import time


def acquire(handle, blocking=True):
    if os.name != 'nt':
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt
    handle.seek(0, os.SEEK_END)
    if not handle.tell():
        handle.write(b'\0'); handle.flush()
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError('Atlas lock is already held') from error
            time.sleep(.05)


def release(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
