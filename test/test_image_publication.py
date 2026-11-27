"""Regression cases for the serialized writer, with real git history and no registry."""
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from scripts.ci import publish_image as ci


class ImagePublicationTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(chdir(self.enterContext(tempfile.TemporaryDirectory())))
        subprocess.run(["git", "init", "-q"], check=True)
        self.code = self.commit("bot.py")
        self.docs = self.commit("doc/rollout.md", "[skip ci]")
        self.tags = {}
        self.revisions = {}
        self.indexes = {}
        self.writes = []
        for sha in (self.code, self.docs):
            self.indexes[sha] = {
                "schemaVersion": 2,
                "mediaType": ci.INDEX_TYPE,
                "annotations": {ci.PROBE: "v2", "org.opencontainers.image.revision": sha},
                "manifests": [{"platform": {"os": "linux", "architecture": arch}} for arch in ci.ARCHES],
            }
            digest = ci.digest_of(json.dumps(self.indexes[sha]).encode())
            self.tags["sha-" + sha] = digest
            self.revisions[digest] = sha
        self.enterContext(patch.object(ci, "tag_digest", self.tags.get))
        self.enterContext(patch.object(ci, "regctl", self.registry))
        self.env = self.enterContext(patch.dict(os.environ))
        self.env.update(
            GITHUB_SHA=self.code, GITHUB_EVENT_NAME="push", CI_FUNCTIONAL_RESULT="success", CI_SMOKE_RESULT="success"
        )

    @staticmethod
    def commit(path, message="change"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(message)
        subprocess.run(["git", "add", path], check=True)
        subprocess.run(
            ["git", "-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", message],
            check=True,
        )
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    def registry(self, group, command, *args):
        if (group, command) == ("manifest", "get"):
            sha = self.revisions[args[0].split("@")[1]]
            return json.dumps(self.indexes[sha]).encode()
        if (group, command) == ("image", "inspect"):
            sha = self.revisions[args[-1].split("@")[1]]
            return json.dumps({"config": {"Labels": {"org.opencontainers.image.source": ci.SOURCE,
                                                     "org.opencontainers.image.revision": sha}}})
        if (group, command) == ("image", "copy"):
            digest = args[0].split("@")[1]
            tag = args[1].split(":")[-1]
            self.tags[tag] = digest
            self.writes.append(tag)
            return
        self.fail(f"Unexpected registry command: {group} {command}")

    def test_skipped_docs_then_old_rerun_after_manual_docs_publish(self):
        ci.main()
        self.assertEqual(self.writes, ["main", "latest"])
        self.env["GITHUB_SHA"] = self.docs
        ci.main()
        self.env["GITHUB_SHA"] = self.code
        ci.main()
        self.assertEqual(self.writes, ["main", "latest"] * 2)
        self.assertEqual(self.tags["latest"], self.tags["sha-" + self.docs])

    def test_skipped_code_requires_a_new_verified_build(self):
        self.commit("bot.py", "new code [skip ci]")
        ci.main()
        self.assertEqual(self.writes, [])

    def test_interrupted_alias_write_converges(self):
        self.tags["main"] = self.tags["sha-" + self.docs]
        self.tags["latest"] = self.tags["sha-" + self.code]
        ci.main()
        self.assertEqual(self.writes, [])
        self.env["GITHUB_SHA"] = self.docs
        ci.main()
        self.assertEqual(self.writes, ["latest"])

    def test_release_is_idempotent_and_refuses_version_reuse(self):
        self.env.update(
            GITHUB_EVENT_NAME="release", RELEASE_TAG="v1.0.0-rc.1",
            CI_FUNCTIONAL_RESULT="skipped", CI_SMOKE_RESULT="skipped",
        )
        ci.main()
        ci.main()
        self.env["GITHUB_SHA"] = self.docs
        with self.assertRaisesRegex(ValueError, "another digest"):
            ci.main()
        self.assertEqual(self.writes, ["v1.0.0-rc.1"])

    def test_release_requires_verified_index_and_valid_tag(self):
        self.env.update(GITHUB_EVENT_NAME="release", RELEASE_TAG="v1", GITHUB_SHA="0" * 40)
        with self.assertRaisesRegex(ValueError, "before releasing"):
            ci.main()
        for tag in ("main", "latest", "sha-x", "v1+build", "x" * 129):
            self.env["RELEASE_TAG"] = tag
            with self.assertRaisesRegex(ValueError, "reserved"):
                ci.main()
        self.assertEqual(self.writes, [])

    def test_legacy_sha_tag_fails_before_any_alias_write(self):
        self.env.update(GITHUB_EVENT_NAME="release", RELEASE_TAG="v1")
        with patch.object(ci, "regctl", return_value=b'{"schemaVersion":2,"layers":[]}'):
            with self.assertRaisesRegex(ValueError, "verified two-platform index"):
                ci.main()
        self.assertEqual(self.writes, [])

    def test_startup_only_index_cannot_be_promoted(self):
        self.env.update(GITHUB_EVENT_NAME="release", RELEASE_TAG="v1")
        old_index = {
            "schemaVersion": 2,
            "mediaType": ci.INDEX_TYPE,
            "annotations": {ci.PROBE: "v1", "org.opencontainers.image.revision": self.code},
            "manifests": [{"platform": {"os": "linux", "architecture": arch}} for arch in ci.ARCHES],
        }
        with patch.object(ci, "regctl", return_value=json.dumps(old_index).encode()):
            with self.assertRaisesRegex(ValueError, "not the verified two-platform index"):
                ci.main()
        self.assertEqual(self.writes, [])

    def test_main_cannot_replace_a_startup_only_sha_tag(self):
        index = self.indexes[self.code]
        index["annotations"][ci.PROBE] = "v1"
        digest = ci.digest_of(json.dumps(index).encode())
        self.tags["sha-" + self.code] = digest
        self.revisions[digest] = self.code
        with self.assertRaisesRegex(ValueError, "not the verified two-platform index"):
            ci.main()
        self.assertEqual(self.writes, [])

    def test_release_rejects_index_body_not_matching_pinned_digest(self):
        self.env.update(GITHUB_EVENT_NAME="release", RELEASE_TAG="v1")
        self.indexes[self.code]["annotations"]["unverified"] = "different body"
        with self.assertRaisesRegex(ValueError, "digest does not match"):
            ci.main()
        self.assertEqual(self.writes, [])

    def test_publisher_refuses_pull_request_event(self):
        self.env["GITHUB_EVENT_NAME"] = "pull_request"
        with self.assertRaisesRegex(ValueError, "Unsupported publication event"):
            ci.main()
        self.assertEqual(self.writes, [])

    def test_old_workflow_rerun_cannot_mint_a_v2_index(self):
        self.env.pop("CI_FUNCTIONAL_RESULT")
        self.env["RUNNER_TEMP"] = str(Path.cwd())
        digest = self.tags.pop("sha-" + self.code)
        with patch.object(ci, "assemble_index", return_value=digest) as assemble:
            with self.assertRaisesRegex(ValueError, "Functional and smoke jobs must both succeed"):
                ci.main()
            assemble.assert_not_called()
        self.assertEqual(self.writes, [])
