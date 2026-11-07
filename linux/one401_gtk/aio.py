"""A tiny awaitable for the GTK main loop, so the window's steps read top to bottom like MainForm's async methods.
Futures are resolved on the main thread (GLib.idle_add); spawn() drives a coroutine from one to the next."""
import threading
import traceback


class Future:
    def __init__(self):
        self.done = False
        self.value = None
        self.error = None
        self._callbacks = []

    def resolve(self, value=None, error=None):
        if self.done:
            return False
        self.done, self.value, self.error = True, value, error
        callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            cb(self)
        return False   # also a GLib source callback: run once

    def then(self, cb):
        if self.done:
            cb(self)
        else:
            self._callbacks.append(cb)

    def __await__(self):
        if not self.done:
            yield self
        if self.error is not None:
            raise self.error
        return self.value


def resolved(value=None):
    f = Future()
    f.resolve(value)
    return f


def spawn(coro, on_error=None):
    """Runs a coroutine whose awaits are Futures. Returns a Future of its result."""
    result = Future()

    def step(fut=None):
        try:
            if fut is None:
                nxt = coro.send(None)
            elif fut.error is not None:
                nxt = coro.throw(fut.error)
            else:
                nxt = coro.send(fut.value)
        except StopIteration as stop:
            result.resolve(stop.value)
            return
        except Exception as error:  # noqa: BLE001
            if on_error:
                on_error(error)
            else:
                traceback.print_exc()
            result.resolve(error=error)
            return
        nxt.then(step)
    step()
    return result


def in_thread(fn, *args, idle_add=None):
    """fn(*args) on a worker thread; the Future resolves on the main loop."""
    if idle_add is None:
        from gi.repository import GLib
        idle_add = GLib.idle_add
    fut = Future()

    def work():
        try:
            value = fn(*args)
            idle_add(lambda: fut.resolve(value))
        except Exception as error:  # noqa: BLE001
            idle_add(lambda e=error: fut.resolve(error=e))   # `error` is unbound after this block
    threading.Thread(target=work, daemon=True).start()
    return fut
