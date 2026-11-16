from contextlib import contextmanager
from threading import Condition


class RWLock:
    """Writer-preferring lock: concurrent readers, one exclusive writer."""

    def __init__(self):
        self.condition = Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @contextmanager
    def read(self):
        with self.condition:
            while self.writer or self.waiting_writers:
                self.condition.wait()
            self.readers += 1
        try:
            yield
        finally:
            with self.condition:
                self.readers -= 1
                self.condition.notify_all()

    @contextmanager
    def write(self):
        with self.condition:
            self.waiting_writers += 1
            try:
                while self.writer or self.readers:
                    self.condition.wait()
                self.writer = True
            finally:
                self.waiting_writers -= 1
        try:
            yield
        finally:
            with self.condition:
                self.writer = False
                self.condition.notify_all()
