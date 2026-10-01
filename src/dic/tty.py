"""Everything dic prints that is not the answer: colour, errors, the cost
line, and the pv-style progress meters.

One module, because the two halves of the meters would otherwise live in two
files: store.py held the formatting (pv_bytes, pv_clock, pv_line) while
dic.py held the painting that consumes it.  Nothing here reads sqlite or the
network, so importing it costs one file of pure stdlib.

See SPEC.md, "Color", "Progress meters" and "Verbosity".
"""
import base64, os, sys, time

BLUE = "\033[38;5;39m"       # model output
ORANGE = "\033[38;5;208m"    # the cost summary
RED = "\033[31m"             # errors
THINKING = "\033[38;5;245m"             # reasoning: faded gray on the usual background
RESET = "\033[0m"


def use_color(stream, env=None):
    """Whether to emit ANSI colour on stream.

    $DIC_COLOR (never|auto|always) wins, then $NO_COLOR, then isatty, so a
    pipe gets clean text without the caller having to ask and a pager can ask
    for colour anyway.  dic never prints uncoloured text to a terminal: every
    stream has a meaning (blue output, orange cost, red error).
    """
    env = os.environ if env is None else env
    mode = env.get("DIC_COLOR", "auto")
    if mode in ("never", "always"):
        return mode == "always"
    return stream.isatty() and not env.get("NO_COLOR")


class DicError(Exception):
    """A failure dic reports: what die() prints, raised instead of exiting.

    Library code raises it, so that a failed call cannot kill the caller's
    process, and the entry point hands it to die(), so that what a failure
    looks like on the terminal and which status it exits with are each said
    exactly once, at the top, where the caller and its streams are known.
    """


def die(msg, err=None, env=None):
    """Report an error in red on stderr and exit nonzero: the CLI's exit.

    dic() raises DicError instead of calling this, and the entry point
    catches that error and passes it here, so a library call never ends in
    sys.exit and the CLI never ends without one.
    """
    err = sys.stderr if err is None else err
    line = f"dic: {msg}\n"
    err.write(RED + line + RESET if use_color(err, env) else line)
    sys.exit(1)


def report(verbosity, level, msg, err=None, env=None):
    """Write msg to stderr in orange when verbosity has reached level.

    All of dic's stderr goes through here, so colour policy and verbosity
    policy are each stated exactly once.

    >>> report(0, 1, "not printed")
    """
    if verbosity < level:
        return
    err = sys.stderr if err is None else err
    line = f"{msg}\n"
    err.write(ORANGE + line + RESET if use_color(err, env) else line)
    err.flush()


def osc52(text, out=None):
    """Copy text to the terminal's own clipboard with one OSC 52 sequence.

    The terminal emulator is what performs the copy, so dic needs no
    subprocess (`xclip`, `pbcopy`), no library and no idea whether the
    session is X11, Wayland, macOS or an ssh connection.  A terminal that
    does not implement the sequence ignores the bytes, which is the right
    failure: the answer is still on the screen and nothing has broken.  The
    payload is base64 and carries no newline, so the sequence is one line,
    and it is written to the stream the answer went to, after the answer is
    complete and never before it.

    >>> import io
    >>> buf = io.StringIO()
    >>> osc52("hi", buf)
    >>> buf.getvalue()
    '\\x1b]52;c;aGk=\\x07'
    >>> osc52("", buf)          # nothing to copy, nothing written
    >>> buf.getvalue()
    '\\x1b]52;c;aGk=\\x07'
    """
    if not text:
        return
    out = sys.stdout if out is None else out
    out.write("\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a")
    out.flush()


def pv_bytes(n):
    """A byte count as pv prints it: the number, then the unit in three columns.

    >>> pv_bytes(58), pv_bytes(2048)
    ('58.0  B', '2.0KiB')
    """
    value, unit = float(n), "B"
    for bigger in ("KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f}{unit:>3}"


def pv_clock(seconds, tenths=False):
    """H:MM:SS, as pv -t prints it; optionally to a tenth of a second.

    A tenth is visible movement on a clock nobody is timing anything with,
    which is the point of the one that runs while dic waits.

    >>> pv_clock(64.7), pv_clock(64.7, tenths=True)
    ('0:01:04', '0:01:04.7')
    """
    whole = int(seconds)
    out = f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"
    return out + f".{int((seconds - whole) * 10)}" if tenths else out


def pv_line(name, nbytes, seconds):
    """One `pv -N name -btr` status line.

    Used for the reasoning stream, which is progress and not content: the
    meter says how much a model thought without scrolling its answer away.

    >>> pv_line("thinking", 58, 4.0)
    'thinking: 58.0  B 0:00:04 [14.5  B/s]'
    """
    rate = nbytes / seconds if seconds else 0
    return (f"{name}: {pv_bytes(nbytes)} {pv_clock(seconds)}"
            f" [{pv_bytes(rate)}/s]")


def pv_paint(state, line, err=None, env=None):
    """Rewrite one status line in place on stderr, in the reasoning gray.

    The line is padded to the widest one written so far, since a line that
    shrinks would otherwise leave the tail of the previous one behind.
    """
    err = sys.stderr if err is None else err
    state["width"] = max(state.get("width", 0), len(line))
    line = line.ljust(state["width"])
    err.write("\r" + (THINKING + line + RESET if use_color(err, env) else line))
    err.flush()


WAIT_DELAY = 0.5     # a first token faster than this needs no reassurance


class Line:
    """stderr's one repaintable line: the ttft clock, a meter, a poll status.

    Progress is not content, so everything whose only job is to say that a
    call is still running repaints the same line instead of printing a new
    one: the answer never scrolls away and no two writers fight over one row
    of the terminal.  The ttft clock starts it, and the first token, meter
    update or job status ends it; one width is kept for all of them, so a
    line that shrinks never leaves the tail of the one before it behind.
    """
    def __init__(self, err=None, env=None):
        self.err = sys.stderr if err is None else err
        self.env = env
        self.state = {}             # pv_paint's width, shared by every writer
        self.start = time.time_ns()
        self.stop = None            # the clock thread's flag, once it is running
        self.thread = None
        self.clock = False          # whether the clock owns the line now
        self.extra = ""             # attribution appended to the ttft line

    def restart(self):
        """Start this line's clock over: a tool loop is more than one request.

        A call that stopped to run a tool waits again for its next first
        token, so the ttft clock is the round's and not the call's.
        """
        self.start = time.time_ns()
        self.extra = ""

    def elapsed(self):
        """Seconds since this line was made: one clock for every writer."""
        return (time.time_ns() - self.start) / 1e9

    def paint(self, line):
        """Repaint the line in place, in the reasoning gray."""
        pv_paint(self.state, line, self.err, self.env)

    def close(self):
        """End the line, so the next thing written starts below it."""
        if self.state.get("width"):
            self.err.write("\n")
            self.err.flush()
            self.state["width"] = 0

    def wait(self):
        """Show a ticking ttft clock until the first token; nothing if it is fast.

        A slow first token is indistinguishable from a hung program, so this
        needs a thread: the main thread is blocked in a socket read until
        exactly the moment the clock should stop.  A call whose first token
        beats WAIT_DELAY never paints at all.
        """
        import threading
        self.stop = threading.Event()

        def tick():
            while not self.stop.wait(0.1):
                if self.elapsed() >= WAIT_DELAY:
                    self.clock = True
                    tail = f"  {self.extra}" if self.extra else ""
                    self.paint(
                        f"ttft: {pv_clock(self.elapsed(), tenths=True)}{tail}")

        self.thread = threading.Thread(target=tick, daemon=True)
        self.thread.start()

    def stop_clock(self):
        """Stop the clock; whether it was showing anything on the line."""
        if self.stop is None:
            return False
        self.stop.set()
        self.thread.join()
        self.stop = self.thread = None
        showing, self.clock = self.clock, False
        return showing

    def first_token(self):
        """The first token arrived: finish and close the line, if one is showing.

        `extra` -- something learned while the wait was running, like the
        upstream a router chose -- is appended to the clock, so one line
        carries both the number and what explains it.  The line exists to
        explain a stall, so a call whose clock never painted drops the
        attribution with it.
        """
        if self.stop_clock():
            tail = f"  {self.extra}" if self.extra else ""
            self.paint(f"ttft: {pv_clock(self.elapsed(), tenths=True)}{tail}")
        self.close()

    def status(self, msg):
        """A job status: end the clock like a token does, then repaint the line.

        An adaptor whose call() polls says what it is waiting for here, so a
        long job's progress is one more thing on stderr's one line and not a
        line of its own; the first status supersedes the bare clock exactly
        as the first token would.
        """
        if self.stop_clock():
            self.paint(f"ttft: {pv_clock(self.elapsed(), tenths=True)}")
            self.close()
        self.paint(msg)


def pv_waiter(err=None, env=None):
    """Tick a bare clock until the first token arrives; return its stopper.

    A caller that already owns a Line drives it with Line.wait and
    Line.first_token directly; this is the one-line way to ask for just a
    clock, and it is what the meter in client.py used to be.
    """
    line = Line(err, env)
    line.wait()
    return line.first_token


def pv_update(state, text=None, final=False, err=None, env=None):
    """Repaint one `pv -N <state["name"]> -btr` meter on stderr.

    text adds its bytes to the meter, creating it on the first call; final
    closes the line so the finished meter stays visible above whatever comes
    next.  A stream that never arrives therefore writes nothing at all, and
    repaints are capped at ten a second so a fast stream is not spent on
    escape codes.
    """
    if state.get("done"):
        return
    err = sys.stderr if err is None else err
    now = time.time_ns()
    if text is not None:
        state["bytes"] = state.get("bytes", 0) + len(
            text if isinstance(text, bytes) else text.encode())
        state.setdefault("t0", now)
    if "t0" not in state:
        return
    if not final and now - state.get("t_paint", 0) < 10 ** 8:
        return
    state["t_paint"] = now
    pv_paint(state, pv_line(state["name"], state["bytes"],
                            (now - state["t0"]) / 1e9), err, env)
    if final:
        err.write("\n")
        state["done"] = True
        err.flush()


def label(item):
    """One priced item as the cost line names it: 'in', 'out', 'cache-read'.

    A refinement of a direction is named by the refinement, because that is the
    part of it worth reading; a bare direction keeps its own name, because
    there is nothing else it could mean.

    >>> label({"key": "in.cache_read"}), label({"key": "out"})
    ('cache-read', 'out')
    """
    head, _, tail = item["key"].partition(".")
    return (tail or head).replace("_", "-")


def summary(usage, items, mid, stamps, verbosity, partial=False):
    """The one-line cost and mid report written to stderr.

    `items` is what `price.rate` made of the call: one entry per usage name a
    price rule priced, with the rule that priced it, the quantity, the rate and
    the cost.  Their sum is the only total dic ever prints, and a model with no
    price rules has no items and costs nothing.

    `partial` says the call was cut short -- a ^C, or a stream that failed --
    before the frame carrying its usage arrived.  Most protocols report their
    usage once, at the very end of the stream, so a call to one of them that
    was cut short knows nothing and says so rather than claiming to have been
    free.  A protocol that reports as it goes, like Anthropic's message_start,
    is unknown only where it is, and its input dollars are the real ones.

    At -v the same line carries the timings of the call and at -vv the
    itemization behind the total, which is what a human wants while an ad-hoc
    `dic --stats` is an aggregate over many calls.

    >>> stamps = {"t_start": 0, "t_request": 10**7, "t_first": 2 * 10**8,
    ...           "t_last": 10**9, "t_done": 11 * 10**8}
    >>> items = [{"rule": "in", "key": "in", "qty": 1000, "rate": 3.0,
    ...           "cost": 0.003},
    ...          {"rule": "out", "key": "out", "qty": 500, "rate": 15.0,
    ...           "cost": 0.0075}]
    >>> summary({"in": 1000, "out": 500}, items, "01ABC", stamps, 1)
    'cost: $0.0105 (input: $0.0030, output: $0.0075) --mid=01ABC'
    >>> summary({}, [], None, stamps, 1)
    'cost: $0.0000 (input: $0.0000, output: $0.0000)'
    >>> summary({}, [{"rule": "provider", "key": "cost", "qty": 0.0245,
    ...               "rate": 1.0, "cost": 0.0245}], None, stamps, 1)
    'cost: $0.0245 (reported by the provider)'
    >>> summary({}, [], None, stamps, 1, partial=True)
    'cost: unknown (input: unknown, output: unknown)'
    >>> summary({"in": 1000}, items[:1], None, stamps, 1, partial=True)
    'cost: $0.0030 (input: $0.0030, output: unknown)'
    >>> summary({"in": 100, "in.cache_read": 900}, items, None, stamps, 1)
    'cost: $0.0105 (input: $0.0030, output: $0.0075) cache-read 90%'
    >>> summary({"out": 800}, [], "01ABC", stamps, 2).split(" | ")[1]
    'overhead 10ms, ttft 200ms, 1000 tok/s, total 1100ms'
    >>> summary({"in": 1000, "out": 500}, items, None, stamps, 2).split(" | ")[0]
    'cost: $0.0105 (in 1000@3, out 500@15)'
    """
    def cost_of(direction):
        priced = [i for i in items if i["key"].split(".")[0] == direction]
        if priced:
            return f"${sum(i['cost'] for i in priced):.4f}"
        return "unknown" if partial else "$0.0000"

    if any(i["key"] == "cost" for i in items):
        # the provider priced the call itself and reported no breakdown, so
        # there is no in/out pair to show and the total is the whole story
        money = "reported by the provider"
    elif verbosity >= 2 and items:
        money = ", ".join(f"{label(i)} {i['qty']}@{i['rate']:g}" for i in items)
    else:
        money = f"input: {cost_of('in')}, output: {cost_of('out')}"
    total = (f"${sum(i['cost'] for i in items):.4f}" if items
             else ("unknown" if partial else "$0.0000"))
    line = f"cost: {total} ({money})"
    read = usage.get("in.cache_read") or 0
    if read:
        # a hit is the reason to cache at all, so it is on the line everyone
        # sees and not behind a -v: the read share of this call's input
        line += f" cache-read {100 * read / (read + (usage.get('in') or 0)):.0f}%"
    if mid:                     # a cancelled call has no row, and so no mid
        line += f" --mid={mid}"
    if verbosity < 2:
        return line

    def ms(start, end):
        return ((stamps.get(end) or 0) - (stamps.get(start) or 0)) / 1e6

    # a rate over a call that stopped to run a tool is not a rate, so the
    # rounds' streams are summed and not their ends subtracted
    stream_ms = (stamps.get("t_stream")
                 or (stamps.get("t_last") or 0)
                 - (stamps.get("t_first") or 0)) / 1e6
    counted = (usage.get("out") or 0) + (usage.get("out.reasoning") or 0)
    speed = counted / (stream_ms / 1000) if stream_ms else 0
    return line + (f" | overhead {ms('t_start', 't_request'):.0f}ms,"
                   f" ttft {ms('t_start', 't_first'):.0f}ms,"
                   f" {speed:.0f} tok/s, total {ms('t_start', 't_done'):.0f}ms")
