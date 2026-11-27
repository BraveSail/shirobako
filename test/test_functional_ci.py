"""The full-suite runner must not accept empty, failed or skipped results."""

from pathlib import Path
from subprocess import CalledProcessError

import pytest
import yaml


def _report(path, states):
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    attrs = {kind: str(states.count(kind)) for kind in ("failures", "errors", "skipped")}
    suite = Element("testsuite", {"tests": str(len(states)), **attrs})
    for i, state in enumerate(states):
        case = SubElement(suite, "testcase", name=str(i))
        if state != "passed":
            SubElement(case, {"failures": "failure", "errors": "error", "skipped": "skipped"}[state])
    ElementTree(suite).write(path)


@pytest.mark.parametrize("states", [(), ("failures",), ("errors",), ("passed", "skipped")])
def test_functional_gate_refuses_incomplete_results(tmp_path, states):
    from scripts.ci.functional_tests import passed_count

    report = tmp_path / "pytest.xml"
    _report(report, states)
    with pytest.raises(ValueError):
        passed_count(report)


def test_functional_gate_accepts_complete_nonempty_suite(tmp_path):
    from scripts.ci.functional_tests import passed_count

    report = tmp_path / "pytest.xml"
    _report(report, ("passed", "passed"))
    assert passed_count(report) == 2


@pytest.mark.parametrize("xml", [
    None,
    "<testsuites/>",
    "<testsuite",
    '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
    '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase><skipped/></testcase></testsuite>',
])
def test_functional_gate_refuses_invalid_reports(tmp_path, xml):
    from scripts.ci.functional_tests import passed_count

    report = tmp_path / "pytest.xml"
    if xml is not None:
        report.write_text(xml)
    with pytest.raises(ValueError):
        passed_count(report)


def test_image_workflow_requires_functional_and_smoke_for_main_promotion():
    workflow_path = Path(__file__).resolve().parents[1] / ".github/workflows/docker-image.yml"
    workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["pull_request"] == {"branches": ["main"]}
    assert "pull_request_target" not in workflow["on"]
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    functional = jobs["functional"]
    assert functional["permissions"] == {"contents": "read"}
    assert functional["runs-on"] == "ubuntu-24.04"
    assert functional["if"] == "github.event_name != 'release'"
    assert "--network none" in "\n".join(step.get("run", "") for step in functional["steps"])
    publisher = jobs["publish"]
    assert set(publisher["needs"]) == {"verify", "functional"}
    condition = publisher["if"]
    assert "github.event_name == 'release' ||" in condition
    assert "needs.verify.result == 'success'" in condition
    assert "needs.functional.result == 'success'" in condition
    assert "github.event_name == 'push'" in condition
    assert "github.event_name == 'workflow_dispatch' && inputs.publish" in condition
    assert "github.ref == 'refs/heads/main'" in condition
    writer = next(step for step in publisher["steps"] if step.get("run") == "python3 scripts/ci/publish_image.py")
    assert writer["env"]["CI_FUNCTIONAL_RESULT"] == "${{ needs.functional.result }}"
    assert writer["env"]["CI_SMOKE_RESULT"] == "${{ needs.verify.result }}"


def test_runner_uses_both_suites_without_inherited_filters_or_credentials(tmp_path, monkeypatch, capfd):
    from scripts.ci import functional_tests as ci

    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "do-not-inherit")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    for suite, config in (("test", "pyproject.toml"), ("lib/test", "lib/pyproject.toml")):
        test_dir = tmp_path / suite
        test_dir.mkdir(parents=True)
        (tmp_path / config).write_text('[tool.pytest.ini_options]\naddopts = "-k never_selected"\n')
        (test_dir / "test_probe.py").write_text(
            "import os\n\n"
            "def test_probe():\n"
            "    assert os.environ['BOT_TOKEN'] == '12345:offline'\n"
            "    assert os.environ['HOME'].startswith(os.path.dirname(os.environ['TMPDIR']))\n"
        )
    assert ci.main() == 0
    assert capfd.readouterr().out.count("1 passed with no skips or expected failures") == 2


@pytest.mark.parametrize(("body", "error"), [("assert False", ValueError), ("pass", CalledProcessError)])
def test_runner_rejects_expected_failures_and_unexpected_passes(tmp_path, monkeypatch, body, error):
    from scripts.ci import functional_tests as ci

    monkeypatch.setattr(ci, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "test").mkdir()
    (tmp_path / "test/test_probe.py").write_text(
        f"import pytest\n\n@pytest.mark.xfail\ndef test_probe():\n    {body}\n"
    )
    with pytest.raises(error):
        ci.run_suite("test/", "pyproject.toml")


@pytest.mark.parametrize("success", [True, False])
def test_runner_handles_actual_pytest_subtest_report(tmp_path, monkeypatch, success):
    from scripts.ci import functional_tests as ci

    monkeypatch.setattr(ci, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "test").mkdir()
    (tmp_path / "test/test_probe.py").write_text(
        f"def test_probe(subtests):\n    with subtests.test('probe'):\n        assert {success}\n"
    )
    if success:
        ci.run_suite("test/", "pyproject.toml")
    else:
        with pytest.raises(CalledProcessError):
            ci.run_suite("test/", "pyproject.toml")
