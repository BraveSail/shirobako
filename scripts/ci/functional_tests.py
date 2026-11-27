"""Run both complete pytest suites in the dedicated offline test container."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
SUITES = (("test/", "pyproject.toml"), ("lib/test/", "lib/pyproject.toml"))


def passed_count(report: Path) -> int:
    """Reject missing, malformed, empty or nonpassing pytest JUnit reports."""
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"Cannot read JUnit report {report}: {exc}") from exc

    if root.tag == "testsuites" and len(root) == 1:
        root = root[0]
    if root.tag != "testsuite":
        raise ValueError(f"Expected one pytest testsuite in {report}")

    cases = root.findall("testcase")
    counts = {name: root.get(name) for name in ("tests", "failures", "errors", "skipped")}
    # Pytest 9 counts successful subtests in tests= but omits their testcase
    # elements by default. Failure/skip entries and counters must still be zero.
    total = int(counts["tests"] or 0)
    unclean = any(counts[key] != "0" for key in ("failures", "errors", "skipped"))
    if not cases or total < len(cases) or unclean or any(
        case.find(status) is not None
        for case in cases
        for status in ("failure", "error", "skipped")
    ):
        raise ValueError(f"JUnit report {report} is not a complete passing suite: {counts}")
    return len(cases)


def run_suite(suite: str, config: str) -> None:
    # Each subprocess gets fresh state and no inherited pytest options,
    # credentials, proxies, user configuration or application cache.
    with tempfile.TemporaryDirectory(prefix="shirobako-functional-") as directory:
        work = Path(directory)
        tmp = work / "tmp"
        tmp.mkdir()
        home = work / "home"
        home.mkdir()
        report = work / "pytest.xml"
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_DATA_HOME": str(work / "app-data"),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "PARSEHUB_DUMP_RAW": "0",
            "API_ID": "12345",
            "API_HASH": "0" * 32,
            "BOT_TOKEN": "12345:offline",
            "DATA_PATH": str(work / "data"),
            "DOWNLOAD_DIR": str(work / "downloads"),
            "DATABASE_URL": f"sqlite+aiosqlite:///{work / 'bot.db'}",
        }
        print(f"Running full suite: {suite}", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pytest", "-c", config, "-o", "addopts=", "-o", "strict_xfail=true",
             "-o", f"cache_dir={work / 'pytest-cache'}", "-ra", "--junitxml", str(report), suite],
            cwd=ROOT, env=env, check=True,
        )
        print(f"{suite}: {passed_count(report)} passed with no skips or expected failures", flush=True)


def main() -> int:
    failed = False
    for suite, config in SUITES:
        try:
            run_suite(suite, config)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            print(f"{suite}: {exc}", file=sys.stderr, flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
