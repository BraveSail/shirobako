"""Run on the serialized GHCR writer, with a full checkout of current main."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

IMAGE = "ghcr.io/bravesail/shirobako"
SOURCE = "https://github.com/bravesail/shirobako"
ARCHES = ("amd64", "arm64")
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
PROBE = "io.github.bravesail.shirobako.startup-check"
# v1 covered only startup; v2 requires functional CI and both startup checks.
VERIFICATION_VERSION = "v2"
DOCS = ("README.md", "README.zh-CN.md", "LOCAL_FORK_MODIFICATIONS.md", "doc/**/*.md")


def regctl(*args, data=None):
    return subprocess.run(["regctl", *args], input=data, stdout=subprocess.PIPE, check=True).stdout


def digest_of(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tag_digest(tag):
    result = subprocess.run(
        ["regctl", "manifest", "head", f"{IMAGE}:{tag}", "--require-digest"],
        capture_output=True, text=True,
    )
    if result.returncode:
        # A missing manifest is the only error that authorizes a first write.
        if f"failed to request manifest head {IMAGE}:{tag}: request failed: not found [http 404]" in result.stderr:
            return None
        raise RuntimeError(result.stderr)
    digest = result.stdout.strip()
    if not re.fullmatch("sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Invalid registry digest for {tag}: {digest}")
    return digest


def image_revision(config):
    labels = config["config"]["Labels"]
    revision = labels["org.opencontainers.image.revision"]
    if labels["org.opencontainers.image.source"].lower() != SOURCE or not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Image has no valid repository/revision label")
    return revision


def assemble_index(layout, sha, artifacts):
    descriptors = []
    for arch in ARCHES:
        artifact = artifacts / ("verified-" + arch)
        config_id = (artifact / "config.id").read_text().strip()
        local = f"{layout}:{arch}"
        regctl("image", "import", local, artifact / "image.tar")
        raw = regctl("manifest", "get", local, "--platform", f"linux/{arch}", "--format", "raw-body")
        manifest = json.loads(raw)
        digest = digest_of(raw)
        config = json.loads(regctl("image", "inspect", f"{layout}@{digest}"))
        if (
            manifest["config"]["digest"] != config_id
            or (config["os"], config["architecture"]) != ("linux", arch)
            or image_revision(config) != sha
        ):
            raise ValueError(f"{arch} archive does not match the tested image")
        descriptors.append({
            "mediaType": manifest["mediaType"], "digest": digest, "size": len(raw),
            "platform": {"os": "linux", "architecture": arch},
        })

    index = {
        "schemaVersion": 2, "mediaType": INDEX_TYPE,
        "annotations": {"org.opencontainers.image.revision": sha, PROBE: VERIFICATION_VERSION},
        "manifests": descriptors,
    }
    raw = json.dumps(index, separators=(",", ":")).encode()
    regctl("manifest", "put", layout + ":verified", "--content-type", index["mediaType"], data=raw)
    return digest_of(raw)


def verified_digest(sha, release):
    sha_tag = "sha-" + sha
    digest = tag_digest(sha_tag)
    if digest is None:
        if release:
            raise ValueError(f"Publish and verify {sha_tag} on main before releasing it")
        with tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as tmp:
            layout = f"ocidir://{tmp}"
            digest = assemble_index(layout, sha, Path(os.environ["RUNNER_TEMP"], "images"))
            copy_tag(f"{layout}@{digest}", sha_tag, digest)
    ref = f"{IMAGE}@{digest}"
    raw = regctl("manifest", "get", ref, "--format", "raw-body")
    index = json.loads(raw)
    annotations = index.get("annotations", {})
    platforms = sorted((m["platform"]["os"], m["platform"]["architecture"]) for m in index.get("manifests", []))
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != INDEX_TYPE
        or platforms != [("linux", arch) for arch in ARCHES]
        or annotations.get(PROBE) != VERIFICATION_VERSION
        or annotations.get("org.opencontainers.image.revision") != sha
    ):
        raise ValueError(f"{ref} is not the verified two-platform index for {sha}")
    if digest_of(raw) != digest:
        raise ValueError(f"Registry digest does not match the index body for {ref}")
    return digest


def git_test(*args):
    result = subprocess.run(["git", *args])
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def copy_tag(ref, tag, digest):
    regctl("image", "copy", ref, f"{IMAGE}:{tag}")
    if tag_digest(tag) != digest:
        raise ValueError(f"Registry did not return the expected digest for {tag}")


def main():
    sha = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a full commit SHA")
    event = os.environ["GITHUB_EVENT_NAME"]
    if event not in {"push", "workflow_dispatch", "release"}:
        raise ValueError(f"Unsupported publication event: {event}")
    release = event == "release"
    # An old workflow rerun fetches today's writer from main, but it cannot
    # attest that the functional job ran. Do not let it mint a v2 digest.
    if not release and any(os.environ.get(job) != "success" for job in ("CI_FUNCTIONAL_RESULT", "CI_SMOKE_RESULT")):
        raise ValueError("Functional and smoke jobs must both succeed before publishing")
    if release:
        tag = os.environ["RELEASE_TAG"]
        valid = re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", tag)
        if not valid or tag in {"main", "latest"} or tag.startswith("sha-"):
            raise ValueError("Invalid or reserved release tag")
    digest = verified_digest(sha, release)
    ref = f"{IMAGE}@{digest}"
    print(f"Verified index: {ref}", flush=True)
    if release:
        previous = tag_digest(tag)
        if previous is None:
            copy_tag(ref, tag, digest)
        elif previous != digest:
            raise ValueError(f"Version {tag} already points to another digest")
        return

    if not git_test("merge-base", "--is-ancestor", sha, "HEAD") or not git_test(
        "diff", "--quiet", sha, "HEAD", "--", ".", *[f":(top,glob,exclude){p}" for p in DOCS]
    ):
        print("Main has different build inputs or history; keeping the SHA tag only.")
        return

    # Check both aliases before writing either. This also handles a previous
    # interruption between tag writes and a manually published docs-only tip.
    pending = []
    for tag in ("main", "latest"):
        previous = tag_digest(tag)
        if previous is not None:
            old_ref = f"{IMAGE}@{previous}"
            if previous == digest:
                continue
            old_sha = image_revision(json.loads(regctl("image", "inspect", "--platform", "linux/amd64", old_ref)))
            if not git_test("merge-base", "--is-ancestor", old_sha, sha):
                print(f"{tag} is newer or on another history; keeping the SHA tag only.")
                return
        pending.append(tag)
    for tag in pending:
        copy_tag(ref, tag, digest)


if __name__ == "__main__":
    main()
