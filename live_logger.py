#!/usr/bin/env python3
"""Mirror Blender Python output to a Text Editor block and a local log file."""

import os
import sys
from datetime import datetime

import bpy  # pyright: ignore[reportMissingImports] -- provided by Blender

# ============================================================
# LIVE BLENDER CONSOLE LOGGER
# Blender 5.2.2
# ============================================================

TEXT_NAME = "LIVE_CONSOLE_LOG"

LOG_FOLDER = os.path.join(os.path.expanduser("~"), "Documents", "Blender_Logs")

os.makedirs(LOG_FOLDER, exist_ok=True)

LOG_FILE = os.path.join(LOG_FOLDER, "blender_live_console.log")


# CLEAN UP PREVIOUS LOGGER

old_logger = bpy.app.driver_namespace.get("LIVE_CONSOLE_LOGGER")

if old_logger:
    try:
        old_logger.restore()
    except Exception:
        pass


# TEXT BLOCK

text_block = bpy.data.texts.get(TEXT_NAME)

if text_block is None:
    text_block = bpy.data.texts.new(TEXT_NAME)
else:
    text_block.clear()


header = (
    "============================================================\n"
    "BLENDER LIVE CONSOLE LOG\n"
    f"Started: {datetime.now().isoformat(timespec='seconds')}\n"
    f"Blender: {bpy.app.version_string}\n"
    f"Log file: {LOG_FILE}\n"
    "============================================================\n\n"
)

text_block.write(header)


# LOGGER


class TeeStream:
    def __init__(self, original_stream, text_block, log_file):
        self.original_stream = original_stream
        self.text_block = text_block
        self.log_file = log_file
        self.pending = ""

    def write(self, message):
        if not message:
            return 0

        # Original Windows console

        try:
            self.original_stream.write(message)
            self.original_stream.flush()
        except Exception:
            pass

        # Blender Text Editor

        try:
            self.text_block.write(message)
        except Exception:
            pass

        # Disk log

        try:
            with open(self.log_file, "a", encoding="utf-8") as file:
                file.write(message)
        except Exception:
            pass

        return len(message)

    def flush(self):
        try:
            self.original_stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self.original_stream, "encoding", "utf-8")


class LiveConsoleLogger:
    def __init__(self):
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr

        # Reset disk log
        with open(LOG_FILE, "w", encoding="utf-8") as file:
            file.write(header)

        self.stdout_tee = TeeStream(self.original_stdout, text_block, LOG_FILE)

        self.stderr_tee = TeeStream(self.original_stderr, text_block, LOG_FILE)

    def install(self):
        sys.stdout = self.stdout_tee
        sys.stderr = self.stderr_tee

    def restore(self):
        if sys.stdout is self.stdout_tee:
            sys.stdout = self.original_stdout

        if sys.stderr is self.stderr_tee:
            sys.stderr = self.original_stderr


logger = LiveConsoleLogger()
logger.install()


bpy.app.driver_namespace["LIVE_CONSOLE_LOGGER"] = logger


# OPTIONAL: OPEN A TEXT EDITOR AREA AUTOMATICALLY


def show_log_in_text_editor():
    screen = bpy.context.screen

    if screen is None:
        return

    # Prefer an existing Text Editor.
    for area in screen.areas:
        if area.type == "TEXT_EDITOR":
            area.spaces.active.text = text_block
            return

    # Otherwise change the smallest non-essential area.
    candidates = [
        area for area in screen.areas if area.type not in {"TOPBAR", "STATUSBAR", "PROPERTIES"}
    ]

    if not candidates:
        return

    area = min(candidates, key=lambda a: a.width * a.height)

    area.type = "TEXT_EDITOR"
    area.spaces.active.text = text_block


show_log_in_text_editor()


# TEST

print("\nLIVE CONSOLE LOGGER ACTIVE")
print("------------------------------------")
print("Blender Text block:", TEXT_NAME)
print("Disk log:", LOG_FILE)
print("------------------------------------")
print("Anything printed through Python stdout/stderr will now appear here too.")
print()
