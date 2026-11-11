"""Reentrant lock shared by web workers, background reconciliation and CLI jobs."""
import threading
from . import db, file_lock

class MutationLock:
    def __init__(self):
        self.thread_lock=threading.RLock()
        self.state=threading.local()
    def __enter__(self):
        self.thread_lock.acquire()
        try:
            depth=getattr(self.state,'depth',0)
            if not depth:
                db.DATA.mkdir(parents=True,exist_ok=True)
                handle=(db.DATA/'mutation.lock').open('a+b')
                try:file_lock.acquire(handle)
                except BaseException:handle.close();raise
                self.state.handle=handle
            self.state.depth=depth+1
            return self
        except BaseException:self.thread_lock.release();raise
    def __exit__(self,*args):
        self.state.depth-=1
        if not self.state.depth:
            file_lock.release(self.state.handle)
            self.state.handle.close()
        self.thread_lock.release()
