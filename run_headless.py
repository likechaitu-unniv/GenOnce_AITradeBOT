"""
Console entry point for Phase 1 (no GUI yet - Phase 2 will add a Web GUI on
top of engine.py/settings.json, reusing the same stop_event.set() pattern
used here for the manual STOP signal, rule 6).

Usage:
    python run_headless.py

While running, type "stop" + Enter (or press Ctrl+C) at any time to trigger
an immediate square-off of any open position and stop the run.
"""

import sys
import threading

import engine


def _stop_listener(stop_event):
    """Background thread: typing 'stop' + Enter sets the shared stop_event.
    This stands in for Phase 2's GUI STOP button, which will just call the
    same stop_event.set()."""
    for line in sys.stdin:
        if line.strip().lower() == "stop":
            print("[run_headless] STOP command received")
            stop_event.set()
            return


def main():
    stop_event = threading.Event()

    listener = threading.Thread(target=_stop_listener, args=(stop_event,), daemon=True)
    listener.start()

    print("[run_headless] starting engine. Type 'stop' + Enter (or Ctrl+C) any time to square off and exit.")

    try:
        result = engine.run(stop_event=stop_event, settings_path="settings.json", log=print)
        print(f"[run_headless] run finished: {result}")
    except KeyboardInterrupt:
        print("[run_headless] Ctrl+C received - stopping")
        stop_event.set()
    except Exception as exc:  # noqa: BLE001 - top-level guard so a crash is visible, not silent
        print(f"[run_headless] run failed: {exc!r}")
        raise


if __name__ == "__main__":
    main()
