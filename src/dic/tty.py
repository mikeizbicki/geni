"""Everything dic prints that is not the answer: colour, errors, the cost
line, and the pv-style progress block.

Progress is a block: one Line, and under it one Row per wait -- the ttft
clock, one per MCP handshake, the thinking and response meters, a queued
job's status.  Every row is repainted together, so the answer never scrolls
away and no two writers fight over one row of the terminal.

One module, because the two halves of the meters would otherwise live in two
files: store.py held the formatting (pv_bytes, pv_clock, pv_line) while
dic.py held the painting that consumes it.  Nothing here reads sqlite or the
network, so importing it costs one file of pure stdlib.

See SPEC.md, "Color", "Progress meters" and "Verbosity".
"""
import base64, os, shlex, shutil, subprocess, sys, threading, time
import weakref

def parse_colors(text):
    """A GENI_COLORS string as {role: SGR escape}, in LS_COLORS' syntax.

    ':'-separated role=SGR entries, which is what dircolors(1),
    GCC_COLORS and GREP_COLORS all use, so a shell has one habit for
    reading a palette and not three.  An empty SGR means the role is
    uncoloured, which is what 'trace=' is: a name reserved by the
    transcript rule rather than a code.

    >>> parse_colors("error=01;31:payload=38;5;39")
    {'error': '\\x1b[01;31m', 'payload': '\\x1b[38;5;39m'}
    >>> parse_colors("trace=")
    {'trace': ''}
    >>> parse_colors("")
    {}
    """
    out = {}
    for entry in text.split(":"):
        role, sep, code = entry.partition("=")
        if sep:
            out[role.strip()] = f"\033[{code}m" if code else ""
    return out


def reset_for(palette):
    """The reset sequence for a palette, or '' when there is nothing to reset.

    A palette whose every value is empty is no colour at all, and a
    reset written after an empty code is a stray escape in a pipe: the
    reset is emitted only where it turns something off.

    >>> reset_for({"payload": "\\x1b[38;5;39m"})
    '\\x1b[0m'
    >>> reset_for({"trace": ""})
    ''
    >>> reset_for({})
    ''
    """
    return "\033[0m" if any(palette.values()) else ""


# One palette, read once, from the shell that set it: dic.sh exports
# GENI_COLORS with the same entries, so the trace and the answer cannot
# disagree about what a role means.  Unset or empty is no colour anywhere,
# which is the default of a pip-installed dic that has never sourced dic.sh.
#
# The constants are the roles and not the colours: `dic-info` on the
# shell side and INFO here are the same role, whichever SGR each names,
# so the two readers of one palette cannot drift apart in vocabulary
# even though one is bash and the other python.
PALETTE = parse_colors(os.environ.get("GENI_COLORS", ""))

ERROR = PALETTE.get("error", "")       # errors
WARNING = PALETTE.get("warning", "")   # a recoverable failure
HINT = PALETTE.get("hint", "")         # an aside, fainter than what it follows
INFO = PALETTE.get("info", "")         # progress: reasoning, the pv meters
PAYLOAD = PALETTE.get("payload", "")   # model output
COST = PALETTE.get("cost", "")         # the cost summary
ID = PALETTE.get("id", "")             # a message id, as git hashes one
REF = PALETTE.get("ref", "")           # a decoration, as git refs
RESET = reset_for(PALETTE)


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
    err.write(ERROR + line + RESET if use_color(err, env) else line)
    sys.exit(1)


# The live block on each stream, if there is one.  `report` has only the
# stream to go on, and a message written beside a block would be overwritten
# by the block's next repaint, so the block is found through the stream it
# paints.  Weak, so a stream that outlives its Line is not kept alive by it.
_LIVE = weakref.WeakKeyDictionary()


def _live(stream):
    """The Line painting this stream, or None.

    A stream weakref cannot hold -- an io.StringIO in a test -- has no block
    and cannot have one, so it is reported as having none rather than as an
    error.

    >>> _live(object()) is None
    True
    """
    try:
        return _LIVE.get(stream)
    except TypeError:
        return None


def report(verbosity, level, msg, err=None, env=None):
    """Write msg to stderr in orange when verbosity has reached level.

    All of dic's stderr goes through here, so colour policy and verbosity
    policy are each stated exactly once.  A message written while a progress
    block is live goes through the block and not beside it: the block moves
    the cursor up by its own height to repaint, and a line it did not put
    there would be overwritten by the next repaint.

    >>> report(0, 1, "not printed")
    """
    if verbosity < level:
        return
    err = sys.stderr if err is None else err
    line = f"{msg}\n"
    text = COST + line + RESET if use_color(err, env) else line
    block = _live(err)
    if block is not None:
        block.write(text)
        return
    err.write(text)
    err.flush()
def pager_command(env=None):
    """The pager to pipe through, or None: $DIC_PAGER, then $PAGER, then less.

    A pager is only offered when the program it names is installed, because
    a command that turned out not to be would lose the log rather than print
    it.  The default flags are git's own: -F quits at once when the whole log
    fits one screen, -R keeps whatever colour the caller saw, and -X leaves
    the screen's contents alone, so a reader who quit the pager to read the
    shell underneath does not find it cleared.

    >>> pager_command({"PAGER": "cat"})
    'cat'
    >>> pager_command({"DIC_PAGER": "cat", "PAGER": "less"})
    'cat'
    >>> pager_command({"PAGER": "no-such-pager-xyz"}) in (None, "less -FRX")
    True
    >>> pager_command({"PAGER": "cat -n"})
    'cat -n'
    """
    env = os.environ if env is None else env
    for variable in ("DIC_PAGER", "PAGER"):
        command = env.get(variable)
        if command and shutil.which(shlex.split(command)[0]):
            return command
    return "less -FRX" if shutil.which("less") else None


def page(text, command):
    """Write text through a pager, falling back to stdout when it will not run.

    A pager that exits first -- a reader who quit three lines in -- is not a
    failure to report, and a pager that was never there is the log arriving
    all at once, which is worse than the log.
    """
    sys.stdout.flush()
    try:
        subprocess.run(shlex.split(command), input=text.encode("utf-8"),  # noqa: S603
                       check=False)
    except OSError:
        sys.stdout.write(text)


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


class Line:
    """stderr's repaintable block: a column of Rows and the one cursor.

    Everything whose only job is to say that a call is still running
    repaints the same block instead of printing a line of its own.  One row
    per wait is what makes a parallel call readable -- four MCP handshakes
    are four rows with four clocks, and one may still be resolving while
    another is already saying hello -- and a call that starts one wait takes
    the short path: with one row and no height, the first paint is the
    carriage return and the rewrite it has always been.

    The rows are stable.  A row is frozen where it stands when its wait ends
    and is never reordered or removed, because the block moves the cursor
    back up by its own height to repaint and a row that moved would be a
    reader following the wrong line.  A row with nothing to show is not
    drawn and does not count toward that height, so a call whose first token
    beat the clock has no row at all.

    A block is a column of rows and one cursor, and the cursor is below the
    block whenever a writer with one looks at it: a message reported while
    rows are live moves to the top of the column, takes a line there, and
    the rows are redrawn under it.

    >>> import io
    >>> out = io.StringIO()
    >>> block = Line(out, {})
    >>> block.row("a").paint("a: 0:00:01.0")
    >>> block.height
    1
    >>> block.write("a message\\n")
    >>> print(repr(out.getvalue()))
    '\\r\\x1b[Ka: 0:00:01.0\\n\\x1b[1A\\ra message\\n\\r\\x1b[Ka: 0:00:01.0\\n'
    >>> block.close()
    >>> block.height, block.drawn()
    (0, [])
    """

    def __init__(self, err=None, env=None):
        self.err = sys.stderr if err is None else err
        self.env = env
        self.lock = threading.RLock()
        self.rows = []
        self.height = 0             # lines the block occupies, cursor below
        self.thread = None          # the one ticker, while something waits
        self.primary = self.row("ttft")
        try:
            _LIVE[self.err] = self
        except TypeError:           # a stream weakref cannot hold
            pass

    # ---------------------------------------------------------------- rows

    def row(self, key, label="ttft"):
        """The row named `key`: made now, or reset for what is about to use it.

        Rows are named rather than counted because a caller that comes back
        -- the next round of a tool loop, the next page of a server's tools
        -- is asking for the row it already has, and a block that grew a
        line per round would scroll the answer off the screen to say the
        same thing twice.  The row that comes back is fresh, because the
        caller that names a row a second time is starting a second thing on
        it.
        """
        with self.lock:
            for row in self.rows:
                if row.key == key:
                    row.restart()
                    return row
            row = Row(self, key, label)
            self.rows.append(row)
            return row

    # ------------------------------------------------------------- drawing

    def drawn(self):
        """The rows with something to show: the height the cursor moves back up."""
        return [row for row in self.rows if row.text or row.clock]

    def repaint(self):
        """Redraw the whole block in place, cursor below it.

        The cursor is moved back up by the height the block had, and every
        row is written again, so one row changing is one repaint of the
        column and no row is ever left half written.  \\033[K clears each
        line as it is reached, which is what a row that got shorter needs,
        and what the old padding-to-the-widest-line hack was for.
        """
        with self.lock:
            rows = self.drawn()
            if not rows:
                self.height = 0
                return
            colour = use_color(self.err, self.env)
            out = [f"\033[{self.height}A" if self.height else ""]
            for row in rows:
                text = row.clock_text() if row.clock else row.text
                out.append("\r\033[K")
                out.append(f"{INFO}{text}{RESET}" if colour and text else text)
                out.append("\n")
            self.err.write("".join(out))
            self.err.flush()
            self.height = len(rows)

    def write(self, text):
        """Write text above the block, and put the block back under it.

        Every stderr writer with a block on its stream comes through here,
        because the block moves the cursor up to repaint and a line it did
        not put there would be overwritten by the next repaint.  text ends
        with a newline; without one it would take the first row's place
        instead of a line of its own.

        >>> import io
        >>> out = io.StringIO()
        >>> Line(out, {}).write("plain\\n")
        >>> out.getvalue()
        'plain\\n'
        """
        with self.lock:
            if not self.height:
                self.err.write(text)
                self.err.flush()
                return
            self.err.write(f"\033[{self.height}A\r{text}")
            self.height = 0
            self.repaint()

    def close(self):
        """Let the block stand: stop the ticker and leave the rows on screen.

        The rows are already where they belong -- the cursor is below them --
        so closing writes nothing.  It clears them, stops their clocks and
        forgets the height, so the next thing written starts on the line
        after the block rather than moving the cursor back up into it, and
        the next round's clock starts a block of its own below.
        """
        with self.lock:
            for row in self.rows:
                row.waiting = row.clock = False
                row.text = ""
            self.height = 0

    # -------------------------------------------------------- the ticker

    def ensure_ticker(self):
        """Start the one ticker, if none is running.  Called under the lock."""
        if self.thread is None:
            self.thread = threading.Thread(target=self.tick, daemon=True)
            self.thread.start()

    def tick(self):
        """Repaint every waiting clock at 10Hz, and exit when none is left.

        One thread for the whole block and not one per row: the rows share
        one cursor, so a second thread could only add a second repaint of
        the same lines, and the lock the cursor already needs is the lock
        the rows are started and retired under.  A wait that beats
        WAIT_DELAY is never marked as painting and so is never drawn, which
        is what keeps a fast call from writing at all.
        """
        while True:
            time.sleep(0.1)
            with self.lock:
                waiting = [row for row in self.rows if row.waiting]
                if not waiting:
                    self.thread = None
                    return
                for row in waiting:
                    if row.elapsed() >= WAIT_DELAY:
                        row.clock = True
                if any(row.clock for row in waiting):
                    self.repaint()

    # --------------------------------------------- the primary's own row

    # The row a Line answers with its own methods is the first one it made,
    # so a caller that starts one wait -- which is every caller that is not
    # running several at once -- reaches one row by the one name it did.

    def restart(self):
        """Start the primary row over: see Row.restart."""
        self.primary.restart()

    def waiting_for(self, label, extra=""):
        """Name what the primary row's clock waits for: see Row.waiting_for."""
        self.primary.waiting_for(label, extra)

    def begin(self, label, extra=""):
        """Start a wait on the primary row: see Row.begin."""
        self.primary.begin(label, extra)

    def arrived(self):
        """End the primary row's wait: see Row.arrived."""
        self.primary.arrived()

    def first_token(self):
        """Freeze the primary row with its final time: see Row.first_token."""
        self.primary.first_token()

    def status(self, msg):
        """Show a job status on the primary row: see Row.status."""
        self.primary.status(msg)

    def elapsed(self):
        """Seconds since the primary row's clock last restarted."""
        return self.primary.elapsed()

    def paint(self, text):
        """Show text on the primary row: see Row.paint."""
        self.primary.paint(text)

    def wait(self):
        """Start the primary row's clock: see Row.wait."""
        self.primary.wait()

    @property
    def extra(self):
        """What the primary row's clock is waiting on, beside the seconds."""
        return self.primary.extra

    @extra.setter
    def extra(self, value):
        self.primary.extra = value

    @property
    def label(self):
        """What the primary row's clock is waiting for."""
        return self.primary.label

    @label.setter
    def label(self, value):
        self.primary.label = value


class Row:
    """One line of a Line: a wait, a meter, or a job being queued.

    A row is where a caller's state lives -- what it is waiting for, since
    when, and what it has to show -- and its Line is what knows how to put
    the column on the screen.  Every method the single-line interface had is
    here, so a writer handed a row cannot tell it from the Line it used to
    be handed, and dic.net and dic.mcp are unchanged by there being more
    than one of them.

    >>> import io
    >>> row = Line(io.StringIO(), {}).row("a")
    >>> row.name, row.text, row.waiting
    ('a', '', False)
    """

    def __init__(self, line, key, label="ttft"):
        self.line = line
        self.key = key
        self.name = key         # what pv_update's meter calls this row
        self.label = label      # what this row's clock is waiting for
        self.extra = ""         # who is answering, beside the seconds
        self.start = time.time_ns()
        self.waiting = False    # the clock is running
        self.clock = False      # the ticker has painted it, and repaints it
        self.text = ""          # the last thing painted, or the frozen total
        self.bytes = 0          # a meter's own: how much has arrived
        self.t0 = 0             # and when the first of it did
        self.painted = 0        # and when it was last on the screen

    def elapsed(self):
        """Seconds since this row's clock last restarted."""
        return (time.time_ns() - self.start) / 1e9

    def restart(self):
        """Start over for a new wait: a fresh clock, name and meter.

        A tool loop is more than one request and a block is more than one
        round, so the row a call already has is the row its next round uses:
        the clock, and the bytes a meter has counted, are this wait's and not
        the call's.
        """
        self.start = time.time_ns()
        self.label, self.extra = "ttft", ""
        self.waiting = self.clock = False
        self.text = ""
        self.bytes = self.t0 = self.painted = 0

    def waiting_for(self, label, extra=""):
        """Name what the clock is waiting for, without restarting it.

        A dns wait and the request that follows it are one wait from the
        user's side, so the number on the line keeps counting from where it
        was and only the word in front of it moves.
        """
        self.label, self.extra = label, extra

    def begin(self, label, extra=""):
        """Start a wait on this row: a fresh clock, and a name for it.

        Each wait gets its own clock rather than sharing the row's -- a tool
        loop is more than one request and an MCP handshake is several round
        trips -- so the number a user reads is the one this wait took and not
        the sum of every wait before it.  `label` names what is being waited
        for and `extra` who is answering -- `dns gitmcp.io`, `mcp-handshake
        gitmcp.io`, `ttft api.groq.com` -- so one row carries both the number
        and what explains it, and a parallel call carries one such row each.
        """
        self.restart()
        self.waiting_for(label, extra)
        self.wait()

    def arrived(self):
        """The wait is over: freeze the clock where it stopped.

        A wait that returned, one that was cancelled and one whose deadline
        passed are the same instant from the user's side, so every caller
        ends its own wait here whether or not it was answered, and ends only
        its own: the rows beside it keep their clocks.
        """
        self.first_token()

    def wait(self):
        """Show a ticking clock on this row until it is stopped.

        A slow first token is indistinguishable from a hung program, so this
        needs a thread: the main thread is blocked in a socket read until
        exactly the moment the clock should stop.  A wait that beats
        WAIT_DELAY never paints at all, and one thread ticks every row of a
        parallel call rather than one thread per row.
        """
        with self.line.lock:
            self.waiting = True
            self.line.ensure_ticker()

    def stop_clock(self):
        """Stop this row's clock; whether the ticker had painted it at all."""
        with self.line.lock:
            self.waiting = False
            painted, self.clock = self.clock, False
            return painted

    def clock_text(self):
        """`ttft: 0:00:03.4  api.groq.com`: what a running clock shows."""
        tail = f"  {self.extra}" if self.extra else ""
        return f"{self.label}: {pv_clock(self.elapsed(), tenths=True)}{tail}"

    def first_token(self):
        """The first token arrived: freeze this row with its final time.

        `extra` -- something learned while the wait was running, like the
        upstream a router chose -- is appended to the clock, so one row
        carries both the number and what explains it.  A row the ticker never
        painted stays unpainted, because the row exists to explain a stall,
        and a call whose clock never showed drops the attribution with it.
        """
        if self.stop_clock():
            self.paint(self.clock_text())

    def status(self, msg):
        """A job status: end the clock like a token does, then show the status.

        An adaptor whose call() polls says what it is waiting for here, so a
        long job's progress is one row and not a line of its own; the first
        status supersedes the bare clock exactly as the first token would.
        """
        if self.stop_clock():
            self.paint(self.clock_text())
        self.paint(msg)

    def paint(self, text):
        """Show text on this row, and repaint the block around it."""
        self.text = text
        self.line.repaint()


def pv_waiter(err=None, env=None):
    """Tick a bare clock until the first token arrives; return its stopper.

    A caller that already owns a Line drives it with Line.wait and
    Line.first_token directly; this is the one-line way to ask for just a
    clock, and it is what the meter in client.py used to be.
    """
    line = Line(err, env)
    line.wait()
    return line.first_token


def pv_update(row, text=None, final=False):
    """Refresh one meter row: `pv -N <row.name> -btr` over the bytes it saw.

    text adds its bytes to the row's total, making it a meter on the first
    call; final freezes it, so a finished meter stays where it stands above
    whatever comes next.  A stream that never arrives therefore shows nothing
    at all, and repaints are capped at ten a second so a fast stream is not
    spent on escape codes.  The row is what carries the stream's identity,
    the total and the last time on the screen, so two meters and four clocks
    are seven rows of one block and not seven writers of one line.
    """
    now = time.time_ns()
    if text is not None:
        row.bytes += len(text if isinstance(text, bytes) else text.encode())
        row.t0 = row.t0 or now
    if not row.t0:
        return
    if not final and now - row.painted < 10 ** 8:
        return
    row.painted = now
    row.paint(pv_line(row.name, row.bytes, (now - row.t0) / 1e9))


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
