"""Thread-safe macOS display state read through CoreGraphics."""

import ctypes
import datetime
import sys
import threading
import time


DISPLAY_RECONCILE_INTERVAL_SECONDS = 0.5


def current_display_state():
    if sys.platform != "darwin":
        return True

    try:
        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        core_graphics.CGMainDisplayID.restype = ctypes.c_uint32
        core_graphics.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
        core_graphics.CGDisplayIsAsleep.restype = ctypes.c_bool
        display_id = core_graphics.CGMainDisplayID()
        return not bool(core_graphics.CGDisplayIsAsleep(display_id))
    except (AttributeError, OSError):
        return True


class ScreenState:
    def __init__(self, on_change=None):
        self._lock = threading.Lock()
        self._screen_on = current_display_state()
        self._updated_at = datetime.datetime.now()
        self._on_change = on_change
        self._started = False

    def set(self, screen_on):
        screen_on = bool(screen_on)
        with self._lock:
            changed = screen_on != self._screen_on
            self._screen_on = screen_on
            self._updated_at = datetime.datetime.now()
        if changed and self._on_change:
            self._on_change(screen_on)

    def snapshot(self):
        with self._lock:
            return {
                "ok": True,
                "screen_on": self._screen_on,
                "updated_at": self._updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            }

    def start(self):
        if self._started or sys.platform != "darwin":
            return
        self._started = True
        try:
            import AppKit  # noqa: F401
        except ImportError:
            threading.Thread(target=self._poll_loop, daemon=True).start()
            return
        threading.Thread(target=self._notification_loop, daemon=True).start()

    def set_on_change(self, on_change):
        self._on_change = on_change

    def _notification_loop(self):
        from AppKit import (
            NSWorkspace,
            NSWorkspaceScreensDidSleepNotification,
            NSWorkspaceScreensDidWakeNotification,
        )
        from Foundation import NSDate, NSObject, NSRunLoop

        state = self

        class ScreenObserver(NSObject):
            def screenDidSleep_(self, _notification):
                state.set(False)

            def screenDidWake_(self, _notification):
                state.set(True)

        observer = ScreenObserver.alloc().init()
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer, "screenDidSleep:", NSWorkspaceScreensDidSleepNotification, None
        )
        center.addObserver_selector_name_object_(
            observer, "screenDidWake:", NSWorkspaceScreensDidWakeNotification, None
        )

        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        callback_type = ctypes.CFUNCTYPE(
            None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
        )

        @callback_type
        def display_reconfigured(_display_id, _flags, _user_info):
            state.set(current_display_state())

        core_graphics.CGDisplayRegisterReconfigurationCallback.argtypes = [
            callback_type,
            ctypes.c_void_p,
        ]
        core_graphics.CGDisplayRegisterReconfigurationCallback.restype = ctypes.c_int32
        core_graphics.CGDisplayRegisterReconfigurationCallback(display_reconfigured, None)
        run_loop = NSRunLoop.currentRunLoop()
        while True:
            cycle_started = time.monotonic()
            run_loop.runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(
                    DISPLAY_RECONCILE_INTERVAL_SECONDS
                )
            )
            # ScreensDidSleep is not reliably delivered to this background
            # run loop when only the display sleeps. Reconcile with the actual
            # CoreGraphics state so preventing system sleep does not leave the
            # cached display state stuck on.
            state.set(current_display_state())
            # A background NSRunLoop can return immediately when it has no
            # input source. Keep reconciliation bounded instead of spinning on
            # CoreGraphics and consuming a CPU core.
            elapsed = time.monotonic() - cycle_started
            remaining = DISPLAY_RECONCILE_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def _poll_loop(self):
        while True:
            self.set(current_display_state())
            time.sleep(DISPLAY_RECONCILE_INTERVAL_SECONDS)
