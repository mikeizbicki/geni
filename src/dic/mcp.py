"""MCP: the tools a server offers, as the records a python function already makes.

`--mcp` names a server -- a command to run, or a URL to POST to -- and what
comes back is the same three things `--tools` produces: a name, a description
and a JSON Schema.  So the schema builder, the tool loop and the stored row
need no MCP case at all, and the only new code is the transport, because a
tool that arrives over a pipe arrives later than an import and can fail on
its own.

Two transports, one of each kind:

    stdio   a child process, one JSON-RPC message to a line of its stdout
    http    the streamable transport: one POST, answered by JSON or by a
            one-way event stream

A stdio server is a command, and every command runs under `sandbox`: the
child is a bash that has sourced dic.sh, and what the user typed after
--mcp is that function's argument list.  There is therefore no variable,
no flag and no spelling that runs a server unsandboxed -- a target that
names the jail a second time is a `sandbox: command not found`, and a `|`
in the line is refused by words() rather than run outside it.

A server is opened when its tools are needed and not before.  A fresh call
opens every server it names, because it must know what to offer; a call that
continues a conversation opens one on the first call to one of the tools its
row recorded, and never merely because the conversation was picked up.

Every wait goes on dic.tty's one line: `dns` while the resolver runs, and
`mcp-handshake` while a server is being asked to introduce itself or a
`tools/list` page is arriving.  A `tools/call` paints its own line,
`mcp <server>`, because it is the one request whose deadline is minutes
rather than seconds.  The connection is built through dic.net for that
reason: a stock urlopen resolves inside the same uninterruptible C call
and could paint no dns line at all.

A failure the model can read is not an exception here.  A call that answers
with isError, or that outlives its deadline, raises, and the tool loop
already turns a raise into the result the model reads, with `ok` false in
the row: dic is a bridge between a model and a server, and what the server
said is the bridge's cargo.
"""
import base64, concurrent.futures, http.client, json, os, queue
import shlex, subprocess, threading, time
import urllib.parse
from importlib.resources import files

from dic import net
from dic.store import http_url
from dic.tty import DicError

# The revision this client speaks.  It is sent on every request and never
# branched on: a server that answers with another one is speaking its own
# revision, and how it behaves is its business and not dic's.
VERSION = "2025-06-18"

# A --mcp value is a URL or a command, and the scheme is what tells them
# apart.  --mcp-url and --mcp-shell say which, for the value the guess gets
# wrong, and the kind is stored beside the spec so that a value guessed at
# once is not guessed at again by the call that reconnects it.
KINDS = ("url", "shell")
SCHEMES = ("http", "https")

# Every wait is bounded, because a bridge that hangs is worse than a bridge
# that fails.  A handshake is three round trips to something that was just
# started, so it is seconds, and it governs the whole of the handshake; a
# tool call is the server's own work -- a search, a build, a page fetch --
# so it is minutes.  An expiry names the method it was waiting for, so a
# stalled server says which method stalled.
HANDSHAKE_S = 10
CALL_S = 300

# The characters every wire grammar accepts in a tool name: OpenAI's
# ^[a-zA-Z0-9_-]{1,64}$ is the strictest of them, and the name dic sends is
# the name dic dispatches on, so it is built to satisfy that one.
SAFE = frozenset("abcdefghijklmnopqrstuvwxyz"
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

# The bytes a --mcp command cannot mean.
#
# After shlex.split a word is an argv element, so none of these is syntax
# any more -- `a | b` is three words and the second is a pipe character.
# That is exactly why they are refused: a caller who wrote a pipe meant a
# pipe, and a word that arrives with shell syntax still in it is a word
# whose author read the line differently than dic did.  Failing names the
# disagreement; passing it through to bwrap hides it.  `=` is not here
# because `FOO=bar` is the assignment sandbox() peels, and `-` is not
# because a bwrap flag is the vocabulary being typed.
SHELL = frozenset("|&;<>()$`\\*?[]{}~!")


def words(target):
    """A --mcp command as the word list sandbox() receives.

    shlex.split is the shell's tokenizer without the shell: quotes group a
    word and nothing else is syntax, so what comes back is a list of
    arguments and not a line.  A metacharacter that survives into one is
    therefore a disagreement about what the line means -- a pipe whose
    author expected a pipe -- and is refused rather than handed to bwrap
    as a program named `|`.

    >>> words("server --port 3")
    ['server', '--port', '3']
    >>> words("'a b' c")
    ['a b', 'c']
    >>> words("a | b")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mcp: '|' in 'a | b': a command is a word list, not a shell line (use a script for pipes and expansions)
    >>> words("")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mcp: an empty command
    """
    if not target.strip():
        raise DicError("--mcp: an empty command")
    out = []
    for word in shlex.split(target):
        bad = sorted(SHELL.intersection(word))
        if bad:
            raise DicError(f"--mcp: {bad[0]!r} in {target!r}: a command is a"
                           " word list, not a shell line (use a script for"
                           " pipes and expansions)")
        out.append(word)
    return out


def command(target):
    """The argv that runs one --mcp command, under sandbox.

    The child is bash, dic.sh is sourced into it, and `sandbox` runs the
    target -- always, and with the target as positional arguments rather
    than as a line.  There is therefore no spelling that skips the jail:
    a target that names the jail a second time is a `sandbox: command not
    found`, and a `|` is a DicError from words().  `"$@"` is never
    re-expanded, so no byte of the target is syntax by the time anything
    reads it.

    A leading NAME=value and a leading --flag are the wrapper's own
    vocabulary: sandbox() peels the assignment into --setenv, and bwrap
    applies a flag after its defaults, so `--share-net -- server` is a
    relaxation the caller typed and not one dic inferred.

    >>> command("server")[0], command("server")[-1]
    ('bash', 'server')
    >>> command("server")[3:5]
    ['--', 'server']
    """
    script = (f"source {shlex.quote(str(files('dic.scripts') / 'dic.sh'))}"
              " 2>/dev/null\n")
    return ["bash", "-c", script + 'sandbox "$@"', "--", *words(target)]


def sanitize(name):
    """A name built only from the characters every wire grammar accepts.

    >>> sanitize("read_file"), sanitize("notion.comments/add")
    ('read_file', 'notion-comments-add')
    """
    return ("".join(c if c in SAFE else "-" for c in name) or "tool")[:64]


def namespace(server, tool):
    """The name one server's tool is offered under.

    A server's tools and a python function's tools share one namespace,
    because the model asks by name and dic dispatches by name; so a server's
    tool is prefixed with the server it came from, and a collision is an
    error rather than a guess.

    >>> namespace("notion", "search")
    'mcp__notion__search'
    """
    return f"mcp__{sanitize(server)}__{sanitize(tool)}"[:64]


def specs(auto=None, urls=None, shells=None):
    """The --mcp, --mcp-url and --mcp-shell values, as [kind, target] pairs.

    The kind is the one fact about a spec that cannot be read back off it, so
    it is stored with it.  A command is validated here and not when its
    first tool is called: a `|` in the line is a disagreement about what
    the line means, and the caller who typed it is the one who can fix it.
    What the pair stores is the string, so the word list a later call uses
    is the one built then.

    >>> specs("npx -y s", "https://x/mcp", "just a command")
    [['shell', 'npx -y s'], ['url', 'https://x/mcp'], ['shell', 'just a command']]
    >>> specs(["a", "b"])
    [['shell', 'a'], ['shell', 'b']]
    >>> specs("a | b")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mcp: '|' in 'a | b': a command is a word list, not a shell line (use a script for pipes and expansions)
    """
    def listed(value):
        return [value] if isinstance(value, str) else list(value or ())

    pairs = ([["url" if urllib.parse.urlparse(v).scheme in SCHEMES else "shell", v]
              for v in listed(auto)]
             + [["url", v] for v in listed(urls)]
             + [["shell", v] for v in listed(shells)])
    for kind, target in pairs:
        if kind == "shell":
            words(target)
    return pairs


def name_of(kind, target):
    """The name a server is offered under, before it is made unique.

    There is no config file and so no name to read, so a server is named
    after what it was given: a URL by its host and a command by its basename,
    which is the name a person would have written anyway.  A `www.` is a
    convention and not a name, so it is left off, and a leading --flag or
    NAME=value is the wrapper's and not the program's, so it is walked past.

    >>> name_of("url", "https://www.mcp.example.com/mcp?a=1")
    'mcp.example.com'
    >>> name_of("url", "http://localhost:8000/sse")
    'localhost'
    >>> name_of("shell", "npx -y @modelcontextprotocol/server-filesystem .")
    'npx'
    >>> name_of("shell", "--share-net -- websearch")
    'websearch'
    """
    if kind == "url":
        parts = urllib.parse.urlsplit(target if "//" in target else "//" + target)
        host = parts.hostname or "server"
        return host[4:] if host.startswith("www.") else host
    for word in words(target):
        head, sep, _ = word.partition("=")
        if word.startswith("-") or (sep and head.isidentifier()):
            continue
        return os.path.basename(word)
    return "server"


def names(pairs):
    """(name, kind, target) for each spec, each name unique and safe.

    Two servers of one name cannot both be offered -- the name is the
    namespace -- so the second and its successors take a number, in the order
    the flags were given.

    >>> names([["url", "https://mcp.example.com/mcp"], ["shell", "npx -y s"]])
    [('mcp-example-com', 'url', 'https://mcp.example.com/mcp'), ('npx', 'shell', 'npx -y s')]
    >>> names([["shell", "npx -y a"], ["shell", "npx -y b"]])
    [('npx', 'shell', 'npx -y a'), ('npx-2', 'shell', 'npx -y b')]
    """
    out, taken = [], set()
    for kind, target in pairs:
        base = sanitize(name_of(kind, target))
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}-{n}"[:64]
        taken.add(name)
        out.append((name, kind, target))
    return out


def content(result):
    """One tools/call result as the text the model reads back.

    Verbatim: `content`, `structuredContent` and `isError` are the server's
    own, and dic normalizes none of them, because a bridge that reformatted a
    result would be a bridge that lost one.  `isError` raises, which is how
    the tool loop records a failure: the message becomes the result the model
    reads and `ok` is false in the row.

    >>> content({"content": [{"type": "text", "text": "hi"}]})
    '{"content": [{"type": "text", "text": "hi"}]}'
    >>> content({"isError": True, "content": [{"text": "boom"}]})
    Traceback (most recent call last):
    ...
    dic.tty.DicError: boom
    """
    result = result or {}
    if result.get("isError"):
        said = [block.get("text") or block.get("type") or "error"
                for block in result.get("content") or []]
        raise DicError("; ".join(said) or "the tool failed")
    return json.dumps(result, ensure_ascii=False)


class Server:
    """One MCP server, opened on the first thing that needs it.

    The transports differ in everything but the conversation: a stdio server
    is a child process that answers on its stdout and sees EOF when dic's end
    of that pipe closes, and an HTTP server is an endpoint that answers a
    POST.  Both speak the same JSON-RPC, so the exchange is written twice and
    the protocol above it once.

    One request is in flight at a time, which is what the tool loop does
    anyway, and it is what makes a synchronous request possible on a stdio
    server: the replies are read by a thread of their own and handed over a
    queue, so a wait has a deadline and a ^C has an eval loop to land in.
    """

    def __init__(self, name, kind, target, say, line=None):
        self.name = name
        self.kind = kind
        self.target = target
        self.say = say
        # a server takes a row of its own, so four handshakes are four rows
        # with four clocks rather than one row rewritten by four writers
        self.line = line.row(name) if line is not None else None
        self.proc = None            # the child, once there is one
        self.url = None             # the endpoint, once there is one
        self.auth = {}              # what the URL's userinfo became
        self.session = None         # Mcp-Session-Id, if the server sent one
        self.inbox = queue.Queue()  # the child's replies, in arrival order
        self.stderr_lines = []      # and what it said on its own stderr
        self.stderr_done = threading.Event()
        self.next_id = 1
        self.opened = False
        self.protocol = VERSION

    # ---------------------------------------------------------------- open

    def open(self):
        """Start the server, say hello, and note the revision it agreed on."""
        if self.opened:
            return
        started = time.monotonic()
        if self.kind == "shell":
            self.spawn()
        else:
            self.endpoint()
        result = self.request("initialize", {
            "protocolVersion": VERSION,
            # nothing to declare: dic reads no roots, samples no model and
            # asks the user nothing, so it is a client of tools and of
            # nothing else
            "capabilities": {},
            "clientInfo": {"name": "dic", "version": VERSION}}, HANDSHAKE_S)
        self.protocol = (result or {}).get("protocolVersion") or VERSION
        self.notify("notifications/initialized")
        self.opened = True
        self.say(3, f"mcp: {self.name}: ready in {time.monotonic() - started:.2f}s")

    def spawn(self):
        """Start the child, once, and read its two streams on threads of its own."""
        if self.proc is not None:
            return
        argv = command(self.target)
        try:
            self.proc = subprocess.Popen(  # noqa: S603
                argv, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except OSError as e:
            raise DicError(f"mcp: {self.name}: cannot start: {e}") from None
        threading.Thread(target=self.read, daemon=True).start()
        threading.Thread(target=self.read_err, daemon=True).start()
        self.say(3, f"mcp: {self.name}: sandbox {self.target}")

    def read(self):
        """Every line the child writes, until it closes its own stdout.

        A server that logs to stdout is a server that logs to the wrong
        stream: the line is not a message, so it is shown at -vv and
        otherwise dropped rather than guessed at.
        """
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                self.say(3, f"mcp: {self.name}: {line[:200]}")
        self.inbox.put(None)        # the server is gone; every wait ends

    def read_err(self):
        """Every line the child writes to its own stderr, kept for the error.

        A command is a shell, and a shell says on its stderr why it could
        not run what it was given; that is the diagnostic a user needs
        when the server is gone, and it is the one thing a broken pipe
        cannot say.  It is drained on a thread of its own, because a pipe
        a child could fill is a pipe the child blocks on.
        """
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                self.stderr_lines.append(line)
        self.stderr_done.set()

    def endpoint(self):
        """The URL to POST to, and the credential its userinfo carries."""
        if self.url is not None:
            return
        url = http_url(self.target if "//" in self.target
                       else "http://" + self.target)
        parts = urllib.parse.urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        self.url = urllib.parse.urlunsplit(parts._replace(netloc=netloc))
        if parts.username:
            # a secret rides in the URL or nowhere: dic stores the spec it was
            # given and never a credential of its own, so there is no second
            # place for one to come from and none to leak from
            pair = (urllib.parse.unquote(parts.username) + ":"
                    + urllib.parse.unquote(parts.password or ""))
            self.auth = {"authorization": "Basic "
                         + base64.b64encode(pair.encode()).decode()}
        self.say(3, f"mcp: {self.name}: {self.url}")

    # ------------------------------------------------------------ exchange

    def request(self, method, params, limit):
        """One JSON-RPC round trip, under the deadline its stage was given.

        The wait is painted on the caller's line: `mcp-handshake` for
        everything that starts a server talking and `mcp <server>` for a
        tools/call, which is the one request that can take minutes.  The
        line closes when the reply arrives, so a handshake that is slow and
        a handshake that never answers read the same way until the deadline.
        """
        message_id = self.next_id
        self.next_id += 1
        body = {"jsonrpc": "2.0", "id": message_id, "method": method,
                "params": params or {}}
        self.say(3, f"mcp: {self.name}: {method}")
        if self.line is not None:
            if method == "tools/call":
                self.line.begin(f"mcp {self.name}",
                                (params or {}).get("name", ""))
            else:
                self.line.begin("mcp-handshake", self.name)
        try:
            deadline = time.monotonic() + limit
            reply = (self.exchange_stdio(body, message_id, deadline)
                     if self.kind == "shell"
                     else self.exchange_http(body, deadline))
        finally:
            if self.line is not None:
                self.line.arrived()
        if reply is None:
            raise DicError(self.describe(
                f"{method}: no answer within {limit}s"))
        if reply.get("error"):
            error = reply["error"]
            raise DicError(f"mcp: {self.name}: {method}:"
                           f" {error.get('message') or error}")
        return reply.get("result")

    def notify(self, method, params=None):
        """A notification: no id, and no reply to wait for.

        An HTTP notification is a POST like any other, so it is painted the
        same way its sibling request is; a stdio one is a write into a pipe
        that is already open and has nothing to wait for to paint.
        """
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if self.kind == "shell":
            self.write(json.dumps(body) + "\n")
            return
        if self.line is not None:
            self.line.begin("mcp-handshake", self.name)
        try:
            self.post(json.dumps(body).encode(), HANDSHAKE_S)
        finally:
            if self.line is not None:
                self.line.arrived()

    def write(self, line):
        """One line to the child, or an error that says it is gone."""
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except OSError as e:
            raise DicError(self.describe(f"the server is gone: {e}")) from None

    def describe(self, what):
        """One failure, with whatever the child said on its stderr beside it.

        A command is a shell, and a shell says on its stderr why it could
        not run what it was given -- ``not found``, a missing module, a
        port already taken -- which is exactly what a broken pipe cannot
        say.  It is waited for, because a child's exit and its stderr
        having been read are not one instant, and it is added only once
        the child has exited, so a server that is merely slow contributes
        nothing to the message about it.
        """
        if (self.kind == "shell" and self.proc is not None
                and self.proc.poll() is not None):
            self.stderr_done.wait(timeout=1)
        said = "; ".join(self.stderr_lines[-3:])
        return f"mcp: {self.name}: {what}" + (f": {said}" if said else "")

    def exchange_stdio(self, body, message_id, deadline):
        """Write the request and wait for its reply, skipping notifications.

        The wait is a timeout on a queue rather than a read on a pipe, because
        the reader thread is what makes it interruptible: a ^C has the main
        thread's eval loop to land in, which a blocking read of a child's
        stdout is exactly what prevents.
        """
        self.write(json.dumps(body) + "\n")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                return None
            if message is None:
                raise DicError(self.describe("the server exited"))
            if message.get("id") == message_id:
                return message

    def exchange_http(self, body, deadline):
        """One POST, and the message it answers with, if it answered in time."""
        data = self.post(json.dumps(body).encode(),
                         max(0.1, deadline - time.monotonic()))
        return self.message(data) if data is not None else None

    def headers(self):
        """The headers every request to this server carries.

        The session id is the server's to name and rides on every request
        after the first, which is how a streamable server keeps one
        conversation's state out of another's way; the user-agent names dic
        and nothing else, because the interpreter's default names
        Python-urllib and a bot check bans it.
        """
        return {"content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "user-agent": f"dic/{VERSION}",
                "mcp-protocol-version": self.protocol,
                **({"mcp-session-id": self.session} if self.session else {}),
                **self.auth}

    def post(self, data, limit, label="mcp-handshake"):
        """POST data and return the body, or None when the deadline passed.

        The connection is built through dic.net, so a resolver that has
        stopped answering paints a `dns:` line and a ^C has an eval loop to
        land in; a stock urlopen resolves inside that same uninterruptible
        C call and neither is possible.
        """
        parts = urllib.parse.urlparse(self.url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn = net.connection(self.url, self.line, label,
                              timeout=max(0.1, limit))
        try:
            conn.request("POST", path, data, self.headers())
            response = conn.getresponse()
            body = response.read().decode("utf-8", "replace")
        except TimeoutError:
            return None
        except (http.client.HTTPException, OSError) as e:
            raise DicError(f"mcp: {self.name}: {e}") from None
        finally:
            conn.close()
        if response.status >= 400:
            raise DicError(f"mcp: {self.name}: {response.status} {body[:300]}")
        if not self.session:
            self.session = response.headers.get("mcp-session-id")
        return body

    def message(self, body):
        """The reply in a response body, whether it is JSON or an event stream.

        The streamable transport may answer a POST with either, per request:
        JSON when the reply is the whole of it, and a one-way event stream
        when the server has something to say first.  Only the message with an
        id is the reply; the rest is the server talking, and is shown rather
        than acted on.
        """
        body = (body or "").strip()
        if not body:
            return None
        if body.startswith("{"):
            try:
                return json.loads(body)
            except ValueError:
                return None
        reply = None
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            try:
                message = json.loads(payload)
            except ValueError:
                continue
            if message.get("id") is not None and reply is None:
                reply = message
            else:
                self.say(3, f"mcp: {self.name}: {payload[:200]}")
        return reply

    # --------------------------------------------------------------- tools

    def list(self):
        """Open the server and ask what it offers, to the end of its cursors."""
        self.open()
        tools, cursor = [], None
        while True:
            result = self.request("tools/list",
                                  {"cursor": cursor} if cursor else {},
                                  HANDSHAKE_S)
            tools.extend((result or {}).get("tools") or [])
            cursor = (result or {}).get("nextCursor")
            if not cursor:
                return tools

    def caller(self, tool):
        """A function the tool loop can call: one tools/call, as text.

        `tool.call` passes a call's arguments as keywords and takes back a
        string, which is the shape of a tools/call exactly, so an MCP tool is
        a python tool whose body happens to be a pipe or a POST.
        """
        def call(**arguments):
            return content(self.request(
                "tools/call", {"name": tool, "arguments": arguments}, CALL_S))
        return call


def resolve(pairs, blob=None, say=None, line=None):
    """(records, mapping) for the servers of one call.

    `blob` is the tools blob of the row this call continues, or None for a
    fresh call.  A fresh call opens every server and asks what it offers,
    because it must know before the model can be told; one that does not
    answer is a DicError and not a shorter list.  The handshakes run at
    once, one worker per server, so a fresh call costs the slowest one and
    not their sum.  A continuation builds its records out of the blob and
    opens nothing, so `-c` reaches a server on the first call to one of its
    tools and never merely because the conversation was picked up.

    `line` is the caller's stderr line, painted as `mcp-handshake` while a
    server is being asked to introduce itself and as `mcp <server>` while a
    tool is running; a library caller that has no line passes none, and every
    wait then goes unpainted exactly as it does today.

    `mapping` is what the next call needs and what the row stores: the
    namespaced name of each tool, back to the server and to the name the
    server itself knows it by.  It is data and not schema, because it is a
    fact about one row's tools and not about the database.
    """
    say = say or (lambda *_: None)
    servers = {name: Server(name, kind, target, say, line)
               for name, kind, target in names(pairs)}
    if blob is not None:
        mapping = blob.get("mcp") or {}
        records = []
        for entry in blob.get("records") or []:
            where = mapping.get(entry["name"])
            if where is None or where[0] not in servers:
                continue
            records.append({"name": entry["name"],
                            "description": entry.get("description") or "",
                            "parameters": entry["parameters"],
                            "fn": servers[where[0]].caller(where[1])})
        return records, mapping

    # A fresh call must know what a server offers before the model can be
    # told, so a server that will not start refuses the call: a model
    # offered some of the tools it was told about is worse than a call
    # that never happened.  The DicError already names the server and why.
    #
    # The handshakes run at once and not one after another.  A handshake is
    # a round trip -- a spawn, or a POST -- and a fresh call makes one per
    # server, so N of them in series is N round trips where one would do;
    # each server has its own child process or endpoint and its own row of
    # the progress block, so nothing here shares state but the reply list,
    # and that is read back on this thread once every worker has finished.
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(servers))) as pool:
        futures = {name: pool.submit(server.list)
                   for name, server in servers.items()}
        listed = {name: future.result() for name, future in futures.items()}

    records, mapping = [], {}
    for name in servers:                # the order the flags were given
        say(2, f"mcp: {name}: {len(listed[name])} tool(s)")
        for tool in listed[name]:
            full = namespace(name, tool["name"])
            if full in mapping:
                raise DicError(f"mcp: {full}: one server's tools name one"
                               " name twice")
            mapping[full] = [name, tool["name"]]
            records.append({
                "name": full,
                "description": tool.get("description") or "",
                "parameters": tool.get("inputSchema") or {"type": "object"},
                "fn": servers[name].caller(tool["name"])})
    return records, mapping
