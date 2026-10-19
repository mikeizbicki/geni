"""The stderr policy a doctest cannot reach: a fake terminal, a repaint in
place, and the clock thread that runs while dic waits for its first token.

The rules -- which colour, how much verbosity -- are stated in tty.py's own
docstrings; these tests are that a stream and a live thread take the branch
those rules describe.
"""
import time

from dic import tty


def test_report_is_coloured_on_a_terminal_and_plain_on_a_pipe(stream):
    err = stream(tty=True)
    tty.report(0, 1, "cost: $0.01", err=err, env={})   # too quiet to say anything
    assert err.getvalue() == ""
    tty.report(1, 1, "cost: $0.01", err=err, env={})
    assert err.getvalue() == f"{tty.COST}cost: $0.01\n{tty.RESET}"

    pipe = stream()
    tty.report(1, 1, "cost: $0.01", err=pipe, env={})
    assert pipe.getvalue() == "cost: $0.01\n"


def test_DIC_COLOR_overrides_what_the_stream_says(stream):
    forced = stream()                          # a pipe, so colour would be off...
    tty.report(1, 1, "hi", err=forced, env={"DIC_COLOR": "always"})
    assert forced.getvalue().startswith(tty.COST)

    off = stream(tty=True)                     # a terminal, so colour would be on...
    tty.report(1, 1, "hi", err=off, env={"DIC_COLOR": "never"})
    assert off.getvalue() == "hi\n"


def test_NO_COLOR_uncolours_a_terminal(stream):
    err = stream(tty=True)
    tty.report(1, 1, "hi", err=err, env={"NO_COLOR": "1"})
    assert err.getvalue() == "hi\n"


def test_a_meter_writes_nothing_until_something_arrives(stream):
    block = tty.Line(stream(), {})
    tty.pv_update(block.row("thinking"), final=True)
    assert block.err.getvalue() == ""       # a model that never reasoned shows no line


def test_a_meter_counts_bytes_repaints_in_place_and_closes_once(stream):
    block = tty.Line(stream(), {})
    meter = block.row("response")
    tty.pv_update(meter, "hello")
    assert block.err.getvalue().startswith("\r\033[Kresponse: 5.0  B 0:00:00 [")

    tty.pv_update(meter, "a")                  # within 0.1s: not painted again
    tty.pv_update(meter, "b")
    assert "response: 7.0  B" not in block.err.getvalue()

    tty.pv_update(meter, final=True)
    block.close()
    closed = block.err.getvalue()
    assert closed.rstrip().endswith("/s]")
    assert closed.count("\n") == 2             # one line per repaint, two repaints


def test_the_wait_clock_is_never_drawn_when_the_answer_is_quick(stream, monkeypatch):
    monkeypatch.setattr(tty, "WAIT_DELAY", 60)    # nothing here is slow
    err = stream()
    tty.pv_waiter(err=err, env={})()
    assert err.getvalue() == ""


def test_the_wait_clock_counts_in_tenths_and_closes_its_own_line(stream, monkeypatch):
    monkeypatch.setattr(tty, "WAIT_DELAY", 0)
    err = stream()
    stop = tty.pv_waiter(err=err, env={})
    time.sleep(0.3)                               # long enough for two or three ticks
    stop()
    text = err.getvalue()
    assert text.startswith("\r\033[Kttft: 0:00:00.")
    # every write is one whole line of the block, so the clock was repainted
    # in place and no write left a half-drawn line behind it
    assert text.endswith("\n")
    assert text.count("\n") == text.count("\r\033[K")


def test_status_ends_the_clock_and_keeps_one_line_on_stderr(stream, monkeypatch):
    """A poll status supersedes the bare clock and repaints the same line.

    fal and openai-videos poll for minutes, so the line the wait clock started
    is the line their status goes on: the clock is finished, each status is
    painted over it, and the line is closed once when the caller is done.
    """
    monkeypatch.setattr(tty, "WAIT_DELAY", 0)
    err = stream()
    line = tty.Line(err=err, env={})
    line.wait()
    time.sleep(0.3)                          # long enough for the clock to paint

    line.status("fal: IN_QUEUE 0:00:01")
    line.status("fal: COMPLETED 0:00:02")
    line.close()

    text = err.getvalue()
    assert "ttft: 0:00:00." in text           # the clock was showing, and stopped
    assert "fal: IN_QUEUE" in text and "fal: COMPLETED" in text
    # every write is one whole line of the block, and the last of them is the
    # final status: the clock and the two statuses are one row, repainted
    assert text.count("\n") == text.count("\r\033[K")
    assert text.rstrip("\n").endswith("fal: COMPLETED 0:00:02")


def test_close_of_a_line_that_never_painted_writes_nothing(stream):
    """A fast call shows no clock, so there is no line to close."""
    err = stream()
    tty.Line(err=err, env={}).close()
    assert err.getvalue() == ""


def test_a_status_without_a_clock_paints_and_closes_once(stream):
    """A job that starts before WAIT_DELAY paints the status alone."""
    err = stream()
    line = tty.Line(err=err, env={})
    line.status("videos: in_progress 0:00:00")
    line.close()

    text = err.getvalue()
    assert text.startswith("\r\033[Kvideos: in_progress")
    assert text.endswith("\n") and text.count("\n") == 1
