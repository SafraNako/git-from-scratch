"""The built-in verification battery: the known-answer part needs no git, and a failing check must be reported as such."""
import io
import random

from kendi_git import checks, cli


class Lines:
    def __init__(self):
        self.text = []

    def line(self, text=""):
        self.text.append(text)


def test_known_answer_checks_pass_without_git():
    for name, fn in checks.KNOWN:
        assert fn(None, random.Random(1), 1), name


def test_a_failing_check_is_reported_and_fails_the_run(monkeypatch):
    def broken(tmp, rng, scale):
        raise AssertionError("planted failure")

    monkeypatch.setattr(checks, "GIT", None)                                   # known answers only, no oracle
    monkeypatch.setattr(checks, "KNOWN", [("planted", broken)] + checks.KNOWN)
    out = Lines()
    code = checks.run(out, quick=True)
    assert code == 1
    joined = "\n".join(out.text)
    assert "✗ planted: AssertionError: planted failure" in joined and "denetim başarılı" in joined and "git bulunamadı" in joined


def test_the_command_line_runs_the_battery_and_exits_zero_when_all_pass(monkeypatch):
    monkeypatch.setattr(checks, "DIFFERENTIAL", [])
    out = io.BytesIO()
    code = cli.main(["dogrulama", "--quick"], stdout=out, stderr=io.BytesIO())
    assert code == 0 and b"3/3 denetim ba" in out.getvalue()
