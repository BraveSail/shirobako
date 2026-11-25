"""Offline CI probe, run inside the booting production image."""
import platform
import sqlite3
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

expected_machine = {"amd64": "x86_64", "arm64": "aarch64"}[sys.argv[1]]
if platform.machine() != expected_machine:
    sys.exit(f"Expected {expected_machine}, got {platform.machine()}")

# Only the real bot startup runs migrations. Opening read-only keeps the probe
# from creating a DB and accidentally passing on its own work.
with sqlite3.connect("file:data/db/database.db?mode=ro", uri=True) as db:
    versions = {row[0] for row in db.execute("SELECT version_num FROM alembic_version")}
heads = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
if not heads or versions != heads:
    sys.exit(f"DB revisions {versions} do not match packaged migration heads {heads}")

import parsehub, pyrogram, redis  # noqa: E402,F401

subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL)
subprocess.run(["bun", "--version"], check=True)
subprocess.run(
    ["bun", "-e", """
        import { SabrStream } from "googlevideo/sabr-stream";
        import { buildSabrFormat, EnabledTrackTypes } from "googlevideo/utils";
        if (typeof SabrStream !== "function" || typeof buildSabrFormat !== "function" || !EnabledTrackTypes)
            throw new Error("SABR exports unavailable");
    """],
    cwd="/app/tools/yt-sabr", check=True,
)
