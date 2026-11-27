"""Tests for how the weekly E2E run reports what went wrong.

The report is the whole point of running weekly: nobody reads a scheduled
task's exit code. "AutoCam needs attention" and "the pipeline broke" call for
completely different responses, so the run has to tell them apart.
"""

import pytest

from scripts import run_weekly_e2e as runner


@pytest.fixture(autouse=True)
def no_stale_logs(tmp_path, monkeypatch):
    """Point the tray log somewhere empty for every test.

    Classification reads that log, and a real one left over from a previous
    run would leak into the result — which is exactly the bug the
    per-run-logs-only rule exists to prevent.
    """
    monkeypatch.setattr(runner, "TRAY_LOG", tmp_path / "absent.log")


class TestAutocamIsCalledOutByName:
    """The case this script was written for.

    AutoCam refusing to run had been surfacing as a generic timeout, so the
    actual cause — a vendor update prompt — never reached anyone.
    """

    def test_the_update_prompt_is_recognised(self):
        outcome = runner.classify_failure(
            "Autocam status: 'Please update Autocam or contact support.'\n1 failed"
        )

        assert outcome.title == "AutoCam needs attention"
        assert not outcome.ok

    def test_the_report_quotes_autocam_verbatim(self):
        """Paraphrasing loses the difference between 'update me' and 'renew me'."""
        outcome = runner.classify_failure(
            "Autocam status: 'Please update Autocam or contact support.'\n1 failed"
        )

        assert "Please update Autocam or contact support." in outcome.detail
        assert "No game will render" in outcome.detail

    @pytest.mark.parametrize(
        "text",
        [
            "Your license has expired",
            "Your licence has expired",
            "Invalid license key",
            "Please contact support",
        ],
    )
    def test_other_refusals_are_the_same_class_of_problem(self, text):
        assert runner.classify_failure(f"{text}\n1 failed").title == (
            "AutoCam needs attention"
        )

    def test_it_is_louder_than_a_routine_failure(self):
        autocam = runner.classify_failure("Please update Autocam\n1 failed")
        assert autocam.priority >= 4


class TestOrdinaryFailuresAreNotBlamedOnAutocam:
    def test_a_pipeline_stall_reports_as_itself(self):
        outcome = runner.classify_failure(
            "E   AssertionError: E2E pipeline test failed\n1 failed"
        )

        assert outcome.title == "Weekly E2E failed"
        assert "AutoCam" not in outcome.title

    def test_a_healthy_status_line_is_not_a_refusal(self):
        """AutoCam's normal panel says 'Status: Running ... ETA:'."""
        outcome = runner.classify_failure(
            "Status: | Running | Processed: | 1234 | ETA: | 00:12:00\n1 failed"
        )

        assert outcome.title == "Weekly E2E failed"

    def test_the_report_carries_enough_to_start_debugging(self):
        outcome = runner.classify_failure(
            "\n".join(f"line {i}" for i in range(40)) + "\nE  AssertionError: boom"
        )

        assert "AssertionError: boom" in outcome.detail


class TestUnmetPrerequisitesAreNotPipelineFailures:
    """A run that never started says nothing about the pipeline.

    Reporting "the E2E failed" when Docker was simply off would train the
    reader to ignore the notification.
    """

    def test_missing_clips_is_reported_as_a_skip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "CLIPS_DIR", tmp_path / "empty")
        (tmp_path / "empty").mkdir()
        monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            runner.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})()
        )

        outcome = runner.check_prerequisites()

        assert outcome is not None
        assert "no clips staged" in outcome.title
        assert "make_test_clips" in outcome.detail

    def test_docker_absent_is_reported_as_a_skip(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda _: None)

        outcome = runner.check_prerequisites()

        assert outcome is not None
        assert "no Docker" in outcome.title

    def test_everything_ready_blocks_nothing(self, tmp_path, monkeypatch):
        clips = tmp_path / "clips"
        clips.mkdir()
        (clips / "Rec01.mp4").write_bytes(b"x")
        monkeypatch.setattr(runner, "CLIPS_DIR", clips)
        monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            runner.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})()
        )

        assert runner.check_prerequisites() is None


def test_the_appending_application_log_is_never_consulted(tmp_path, monkeypatch):
    """Guard for the bug found while writing this.

    The application log is a TimedRotatingFileHandler and survives across
    runs, so reading it made every later failure inherit an earlier run's
    AutoCam message — a generic pipeline break got reported as "AutoCam needs
    attention". Only per-run logs may be classified.

    Asserted behaviourally: put the AutoCam text somewhere that is NOT the
    per-run tray log, and a generic failure must still classify as generic.
    """
    stale = tmp_path / "video_grouper_e2e_test.log"
    stale.write_text(
        "Autocam status: 'Please update Autocam or contact support.'",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "TRAY_LOG", tmp_path / "absent.log")

    outcome = runner.classify_failure("E   AssertionError: something else\n1 failed")

    assert outcome.title == "Weekly E2E failed", (
        "a stale log from an earlier run leaked into this run's diagnosis"
    )


class TestATimedOutRunStillReports:
    """A scheduled job must report, never traceback.

    The first live run hit the timeout and died with an unhandled
    TimeoutExpired, which also discarded everything pytest had written — so
    the one run that most needed diagnosing produced none.
    """

    def _run_with_timeout(self, monkeypatch, captured_output, tmp_path):
        import subprocess as sp

        def raise_timeout(*_args, **_kwargs):
            raise sp.TimeoutExpired(cmd="pytest", timeout=2700, output=captured_output)

        monkeypatch.setattr(runner.subprocess, "run", raise_timeout)
        monkeypatch.setattr(runner, "check_prerequisites", lambda: None)
        monkeypatch.setattr(runner, "reset_simulator", lambda: None)
        monkeypatch.setattr(runner.sys, "argv", ["x", "--no-notify"])
        return runner.main()

    def test_it_exits_cleanly_instead_of_raising(self, monkeypatch, tmp_path):
        code = self._run_with_timeout(monkeypatch, "1 failed", tmp_path)
        assert code == 1

    def test_the_report_says_it_timed_out(self, monkeypatch, tmp_path, capsys):
        self._run_with_timeout(monkeypatch, "1 failed", tmp_path)
        assert "timed out" in capsys.readouterr().out

    def test_output_captured_before_the_kill_is_still_classified(
        self, monkeypatch, tmp_path, capsys
    ):
        """An AutoCam refusal is diagnosable even when the suite never returned."""
        self._run_with_timeout(
            monkeypatch,
            "Autocam status: 'Please update Autocam or contact support.'",
            tmp_path,
        )
        out = capsys.readouterr().out
        assert "AutoCam needs attention" in out
        assert "timed out" in out
