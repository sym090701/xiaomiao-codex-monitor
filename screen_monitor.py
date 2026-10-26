#!/usr/bin/env python3
"""Standalone diagnostic for the macOS screen-state listener."""

import argparse
import datetime
import signal

from screen_state import ScreenState


def print_state(screen_on):
    state = "on" if screen_on else "off"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] 屏幕 {state.upper()}", flush=True)


def monitor():
    state = ScreenState(print_state)
    state.start()
    initial = state.snapshot()["screen_on"]
    print(f"当前屏幕: {'ON' if initial else 'OFF'}")
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print("正在监听 macOS 屏幕睡眠/唤醒事件。")
    print("按 Ctrl+C 停止。", flush=True)

    while running:
        signal.pause()


def main():
    parser = argparse.ArgumentParser(description="测试 macOS 屏幕睡眠/唤醒监听。")
    parser.parse_args()
    monitor()


if __name__ == "__main__":
    main()
