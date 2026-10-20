# dic SPEC

`dic` is a minimalist CLI tool for working with chat LLMs models.

"Dic" is Latin for the command "speak".
The idea is that working with AIs is like working with demons and magic,
and Latin is the traditional language for controlling demons and casting spells.

`dic` is similar to simonw's `llm` tool but with an emphasis on speed and Unix-style composability.

## Code Priorities

`dic` is designed for expert CLI users, and so it prioritizes speed.
The system must have minimal latency (i.e. minimizing time-to-first-token displayed),
and the program must terminate "instantly" after receiving the last token.
Some low-level methods of achieving this speed include:
1. importing as few libraries as possible,
1. having as little boilerplate code as possible,
1. streaming the output to stdout.

In particular, `dic` must never import a vendor SDK (`openai`, `anthropic`, etc.);
these packages cost hundreds of milliseconds at import time,
which is more than the time-to-first-token of most APIs.
All API access is plain HTTP against the documented JSON endpoints.

## Code

Program architecture should be simple and not overengineered.
Intro data structures students should find the architecture to be the "obvious" way they would have done things,
with as few as possible files/classes/functions.
Code should be succinct and low token but human friendly.
Include useful variable names and doctests,
but do not go overboard on verbosity.

`dic` is a package -- `dic.dic`, `dic.config`, `dic.store`, `dic.adaptors.*` --
so that it can later be imported as a library, and its modules therefore import
each other by their full package name.
`python dic/dic.py` still works: the entry point puts the checkout root on
`sys.path` when `__package__` is None.

## Options

`dic` supports the following options.
Whenever possible, names and semantics remain the same as simonw's `llm`.

The command line is a tree of commands: the call is the default, and every
other command answers from the local state -- the database, or a session's
ref -- and never calls a model.  Each command takes the options of its own
row, plus the globals `-v`, `-q` and `--models-file`, which every command
accepts.
The command name must be `argv[0]`: a word that is not a command name is the
call's prompt.

| command | → library function | options |
|---|---|---|
| `dic [PROMPT]` | `client.dic` | `-m/--model` `-s/--system` `--prepend-prompt*` `-a/--attachment*` `-o/--option*` `--cache=off\|TTL` `--tools*` `-x/--extract` `-c/--continue` `--mid REF` `--record=bps` `--pv-thinking/--no-…` `--pv-response/--no-…` `--clipboard/--no-…` `--path FILE` `-f/--force` `--mime-type TYPE` `--print=curl\|json` |
| `dic log` | `commands.log` | `-n N` `--all` `--session [NAME]` `--from REF` `--graph` `--oneline` `--format FMT` `--decorate` `--grep TEXT` `-S TEXT` `--stat` `--full` `--lines N` `--no-pager` |
| `dic stats` | `commands.stats` | `-m/--model PAT` `--upstreams` |
| `dic models` | `commands.models` | `--aliases` |
| `dic cost` | `commands.cost` | `--session NAME` `--tree` `--of REF` |
| `dic show` | `commands.show` | `REF` |
| `dic bps` | `bps.bps` | `-m/--model PAT` `--upstream PAT` `--format=csv\|svg` `--where SQL` |
| `dic rev-parse` | `commands.rev_parse` | `[NAME]` |
| `dic branch` | `commands.branch` | `[NAME [START]]` `-d` `-D` |
| `dic update-ref` | `commands.update_ref` | `NAME [NEW [OLD]]` `-d` |

| short form | long form      | meaning |
| ---------- | -------------- | ------- |
| `-m`       | `--model`      | which model to use |
| `-s`       | `--system`     | system prompt |
|            | `--prepend-prompt` | prepend TEXT to the prompt; repeatable |
| `-a`       | `--attachment` | attach the file |
| `-x`       | `--extract`    | extracts first fenced code block |
|            | `--path`       | write the answer to this file, atomically |
| `-f`       | `--force`      | overwrite the file named by `--path` |
|            | `--mime-type`  | the mime type of the answer |
|            | `--cache`      | cache the prompt: `off` (the default), or a TTL the model prices |
| `-c`       | `--continue`   | continue the previous conversation in this session |
|            | `--mid`        | continue the conversation from the given message id |
|            | `--pv-thinking` | show the reasoning stream as a one-line `pv`-style meter (the default) |
|            | `--no-pv-thinking` | stream the reasoning text itself instead |
|            | `--pv-response` | meter the answer on stderr too; the default when stdout is not a terminal |
|            | `--no-pv-response` | do not meter the answer |
|            | `--record`     | record per-round byte-rate samples for plotting (`bps`) |
|            | `--clipboard`  | copy the answer to the terminal's clipboard (the default on a terminal) |
|            | `--tools`      | offer an importable python function as a tool; repeatable |
|            | `--print`      | print the request instead of sending it: `curl` or `json` |
| `-v`       | `--verbose`    | raise stderr verbosity; repeatable |
| `-q`       | `--quiet`      | print nothing to stderr but errors |

The prompt is one argument.  A stdin that is not a terminal is appended after
a blank line, and each `--prepend-prompt TEXT` is prepended above both, in
the order given.  An option `dic` does not know is an error and not a prompt
word: a wrapper that has instructions of its own passes them with
`--prepend-prompt`, so every part of the prompt is named and a misspelled
flag is never sent silently to a model as prose.

Two environment variables supply defaults for the two flags a user tends to want
set the same way every time:

| variable     | default for |
| ------------ | ----------- |
| `DIC_MODEL`  | `-m`        |
| `DIC_SYSTEM` | `-s`        |
| `DIC_VERBOSITY` | `-v`/`-q` |

These are environment variables rather than a second config file because a config
file would add a stat and a parse to the latency path for something a shell startup
file already does, and because the environment is inherited by subshells and
overridable for a single command: `DIC_MODEL=gpt dic ...`.

`DIC_COST_BUDGET` is not a default for any flag: it is a ceiling, checked
before every request, on the same subtree `--cost-session` prints.  A harness
that has spent its budget stops rather than asks.

If `DIC_MODEL` names a model that is not configured, this is an error;
a stale export must never silently fall back to some other model.

`DIC_SYSTEM` applies only when starting a *new* conversation.
With `-c` or `--mid` the system prompt is inherited from the conversation,
so that an exported default cannot rewrite the system prompt of a running thread
and leave the `system` column no longer describing it.
The full precedence is `-s`, then the inherited prompt, then `DIC_SYSTEM`,
then the model's own `system` key.

`DIC_MODEL` likewise applies only when starting a *new* conversation.
With `-c` or `--mid` the model is inherited from the message the conversation
continues from, so a thread keeps its provider until the caller names another
one; the full precedence is `-m`, then the inherited `model_id`, then
`DIC_MODEL`, then the first configured entry whose key is exported.

### Tools

`--tools PATH` offers the model a python function, and is repeatable:

    --tools dic.tools.fs:ls      one function
    --tools dic.tools.fs:*       every public function in one module
    --tools pkg.mod.fn           dotted, when the module path is unambiguous

The module path is resolved by importing it, longest first, so the dotted form
means `pkg.mod` plus `fn` when `pkg.mod` is a module and `pkg.mod.fn` is not;
`:` says the same thing without the guess.  A module's `*` is its `__all__`
when it declares one, and otherwise the functions it defines -- never every
name its namespace holds, which is every name it imported.

Nothing is registered and nothing is generated: the tool *is* the function.
Its name is the function's name, its description is the first paragraph of its
docstring, and its parameters are a JSON Schema built from its type
annotations.  A parameter with no annotation, a `*args`, or a type with no JSON
form is an error naming the function, because a parameter the model cannot see
the type of is one it will fill in wrongly; `Annotated[T, "why"]` is how a
parameter is described, because that is a docstring style nobody has to parse.
A result that `json` cannot encode is an error too, and a string is passed
through as itself.

A tool that *fails* is not a failed call: the exception is handed back to the
model as the result, so the model can read it and try again.  A call that keeps
asking for tools is given 16 rounds and then fails, because a loop is a bug and
not a conversation.

### MCP servers

`--mcp WORDS|URL` offers the tools of an MCP server, and is repeatable.  The
value is told from itself: a scheme makes it a URL and anything else makes it a
command, with `--mcp-url` and `--mcp-shell` for the value that looks like the
other one.  There is no file to write and no name to invent: a server is named
after its host or its basename, and a second server of that name takes a
after its host or the command's own name, and a second server of that name takes a

A command is a word list and not a shell line.  It is run by a bash with
A command is a word list and not a shell line, and it is *always* run under
the sandbox dic ships: the child is a bash of dic's own that sources dic's
own dic.sh by absolute path, and the program it starts is
`sandbox -- <words>`.  The jail is therefore not the caller's to forget, and
not the caller's to switch off by redefining a function or exporting a
variable -- a child whose `sandbox` is not a function in that file exits
rather than runs.  The one way to widen it is to say so in the command, whose
leading run is the sandbox's own, and it has two forms.  A leading NAME=value
is *set inside* the jail, so `--mcp 'GH_TOKEN=x mcp-server-github'` is a
server whose own credential is the one it reads, and dic's environment -- an
API key included -- is what the cleared one keeps out.  A leading `-word` is
the sandbox's own argument, so `--mcp '--share-net mcp-fetch'` shares the
network namespace because an `npx` server has to fetch and `--mcp 'mcp-fetch'`
does not.  Writing the target as one quoted string is what keeps those words
apart, the split being dic's and not the shell's.  A
word that is nothing but metacharacters is an error naming the character,
because a `|` is a line whose author read it as one and a program named `|`
does not say so.
What comes back is the same three things `--tools` produces -- a name, a
description and a JSON Schema -- so nothing downstream knows the difference.  A
server's tools are prefixed `mcp__<server>__<tool>`, because the model asks by
name and dic dispatches by name; a name that arrives twice is an error and
never a guess.

A fresh call opens every server it names and asks what it offers.  A call that
opens the servers at once, one per worker, so a fresh call costs the slowest
handshake and not their sum.  A call that continues a conversation re-offers
exactly the tools its row recorded and opens a server on the first call to one
of its tools, so `-c` reaches no server it does not have to, and a server that
has since died is a `tool_result` the model reads rather than a failed call:
dic is a bridge, and what a server said -- an error, a timeout, an `isError` --
is the bridge's cargo.

Both transports are spoken: a command is a child process, one JSON-RPC message
to a line of its stdout, and a URL is a POST whose answer is JSON or a one-way
event stream.  The child is a bash that has sourced dic.sh, which is how the
command reaches the `sandbox` function, and what of the environment it reads
is what the target's leading assignments set, dic's own being cleared; its
own stderr is kept, because a shell that could not run the program says why
there and a broken pipe does not.  A
HTTP secret rides in the URL, because dic stores the spec it was given and
never a credential of its own.  Every wait is bounded, and an expiry names the
method and the number of seconds it waited.

Tools ship in categories, one module each, so `dic.tools.fs:*` is a category
and `dic.tools.fs:ls` is one tool in it.  Importing that module is the cost,
which is why `dic.tool` itself is imported only when `--tools` is given; a tool
whose own heavy imports are inside its body still costs nothing until it is
called.

A tool runs in dic's own process, with dic's own permissions.  dic does not
sandbox it, and does not pretend to know whether it was sandboxed: running
untrusted tools means running dic itself under `bwrap`, so that every tool
inherits the jail rather than being trusted to build one.

### Output

The answer goes to stdout, unless `--path FILE` names a file instead, in which
case the answer is written to `FILE.part` and renamed into place when the call
ends: a reader sees a whole answer or none, and a cancelled call leaves the real
path untouched and its partial bytes at the `.part` the error names.

`-f` overwrites a file that is already there.  Without it, a file that exists
when the call starts is an error *before* the network is touched -- a stale
redirect must never be silently extended -- and a file that appears while the
call is running is reported and left alone.

`--mime-type` says what the answer will be.  It defaults to the model's `output`
key, and to `text/plain` without one.  An answer that is not `text/*` is bytes
and has no terminal form, so it is an error to ask for one without `--path`, and
one blob is one file: a blob written to `FILE` is numbered `FILE-0.mp4`,
`FILE-1.mp4`, because the stream does not say in advance how many it will carry.

Every output file is also an input.  The row records the files its turn
produced, exactly as it records the files its prompt attached, and `-c` or
`--mid` sends both back to the next model: a conversation may generate a video
and then ask a text model how well it answered the prompt.  A file a turn names
that dic can no longer read is an error, never a turn that quietly loses it.

### Color

Every stream `dic` writes has a meaning and therefore a color:
blue for model output, a faded gray for the model's reasoning,
orange for the cost summary, red for errors.
There is deliberately no uncolored terminal output.
Color is emitted when `$DIC_COLOR` is `always`,
suppressed when it is `never`,
and otherwise used only when the stream is a terminal and `$NO_COLOR` is unset,
so a pipe gets clean text without the caller having to ask.

### Clipboard

The answer is also put in the terminal's clipboard whenever it is printed to
one -- the same condition as colour, because it is the same stream: a terminal
is where the answer is already on the screen, and reaching for the mouse to
select it is the step this deletes.

The mechanism is OSC 52, one escape sequence carrying the answer base64
encoded, and the *terminal emulator* is what performs the copy.  `dic`
therefore needs no subprocess (`xclip`, `pbcopy`), no library, and no idea
whether the session is X11, Wayland, macOS or an ssh connection; a terminal
that does not implement the sequence ignores the bytes, which is the right
failure, because the answer is still on the screen and nothing has broken.
The sequence is written to the same stream the blue answer went to, and only
after the answer is complete, so nothing is ever copied half-written and a
pipe is never handed an escape it did not ask for.

`--clipboard` and `--no-clipboard` force it either way, and `$DIC_CLIPBOARD`
sets the default for a user who wants it off everywhere.

### Progress meters

Reasoning is progress rather than content, and a thinking model can emit thousands
of tokens that nobody reads and that push the answer off the screen.
By default the reasoning text is therefore replaced with a single status line on
stderr in the format of `pv -N thinking -btr`:

```
thinking: 58.0  B 0:00:04 [16.5  B/s]
```

The line is repainted in place, in the same faded gray under the same color rules,
and is terminated by a newline when the first answer token arrives, so the answer
starts on its own line and exactly one thinking line remains at exit.
If the model never emitted reasoning, the line is never written at all.
`--no-pv-thinking` streams the reasoning text itself instead.

`--pv-response` meters the answer the same way and in the same place, but *as well
as* the answer rather than instead of it: the text still goes to stdout untouched.
It is therefore off by default when stdout is a terminal, where the meter and the
answer would overwrite each other, and on by default when stdout is a pipe, where
stderr is the only thing on the screen and the meter is the only sign of life.
`--pv-response` and `--no-pv-response` force it either way.
Both meters are the same code and the same line of the screen: the thinking meter
closes when the first answer token arrives, and the response meter starts below it.

A slow first token looks exactly like a hung program, so while the meter is
enabled and half a second has passed with nothing received, the same line carries
a bare clock and keeps counting, in tenths of a second:

```
ttft: 0:00:03.4
```

It has no byte counter because no bytes have arrived; it is closed with the final
time when the first token does arrive, and a faster call never shows it at all.

The same line is a block.  Each wait gets a row of its own, and the rows are
repainted together: four `--mcp` servers therefore show four `mcp-handshake`
clocks that count in parallel and freeze in place as each one is answered,
rather than one line rewritten four times.  A row is named, so the second
round of a tool loop reuses the first round's rows and a block does not grow
a line per call.  A message written while rows are live -- `report`'s cost
line, `-vv`'s `mcp: name: method` -- moves the block down and takes the line
above it, so a log line never lands on a clock.

The same clock names what it is waiting for.  The name is `ttft` for the request
itself, and `dns` while the host is being resolved, because a resolver that has
stopped answering is a ten-second wait that otherwise looks exactly like a
provider that has stopped answering:

```
dns: 0:00:03.4  api.groq.com
```

The resolver runs on a thread of its own, so that `^C` interrupts it:
`getaddrinfo` is a blocking C call that never returns to the interpreter, and a
signal cannot be handled while it runs.  The label returns to `ttft` once the
addresses are in hand, and the clock behind the label is never restarted, since
a dns wait and the ttft wait that follows it are one wait from the user's side.
The addresses the thread found are handed to the socket, so the lookup is not
done twice.

### Verbosity

stderr is graded, and the grade is one integer resolved once at startup:
`$DIC_VERBOSITY` if set, otherwise `1` when stderr is a terminal and `0` when it
is not, then moved by `-q` (to 0) or `-v` (each repetition one higher).

| level | stderr |
| ----- | ------ |
| 0 | errors only |
| 1 | the cost and `--mid` line |
| 2 | and the timings of this call: overhead, ttft, tok/s, total, and the itemized cost |
| 3 | and the request body and URL before it is sent |

All of it goes through one `report(verbosity, level, msg)` in `tty.py`.

**TODO:**
1. A fresh call fetches `tools/list` from every server it names, every time:
    the `mcp_tools` table that would remember the answer is in the schema and
    unused.  What that table would save is one handshake per server per new
    conversation, which is what makes it a cache and not a requirement.
    Per-server deadlines and the older two-endpoint SSE transport are not
    implemented either, so a server that needs more than `--mcp`'s value is
    not reachable yet.

5. Subagents: an agent that calls other agents is several conversations under
    one session, and every piece dic needs for that is already here.  A session
    name nests -- `DIC_SESSION=parent/scruta-1` is a child of `parent` --
    `--cost-session` sums the subtree, and `-c` reads the pointer of whichever
    session names it, so a child that wants a fresh context simply does not pass
    `-c`.  What is missing is the ergonomics a harness wants: a `--session NAME`
    flag, so a script spawns a child without juggling `export`; and a
    `--subsession SUFFIX` that appends a unique child name (`parent/scruta-<ulid>`)
    and prints the mid it wrote, for the common "fork a fresh context, attributed
    to my own subtree" case.  A `scruta()` shell function built from them is the
    whole of what a subagent is: dic's job is the call, and the orchestration is
    the script's, exactly as `itera` already is.

2. Many providers allow prompt caching to reduce cost of input tokens.
    It's not clear to me the best way to structure this from the cli or in the various config files.

3. There is no cross-provider standard for listing models or their prices.
    `GET /v1/models` is OpenAI-shaped and served by Groq, Together, vLLM and OpenRouter,
    but only OpenRouter reports `pricing`, and Anthropic reports none.
    A future `dic --sync` should hit each provider's list endpoint and *generate* the
    entries under a provider id, never fetching prices on the latency path.

4. The `stats` view has no notion of a percentile, only averages and maxima,
    because sqlite has no `percentile()` without an extension.
    A median is expressible with a window function over the view and should replace
    `avg` once the query is worth the length.

**OUT OF SCOPE:**

1. The `llm` command provides mechanisms for dynamically building the prompt (for example using "fragments" or "prompt templates").
    `dic` will never implement these features;
    they should instead live in separate programs that can be used to generate interesting prompts,
    and then have those prompts passed into `dic`.

1. The `llm` command provides mechanisms for working with non-chat models (e.g. embedding models).
    These non-chat models should be provided separate stand alone programs.

1. `llm` uses a plugin per provider, which means that a newly released model cannot be used
    until its plugin has been updated, and that plugin load times dominate startup.
    `dic` will never have a per-provider plugin system.
    Adapters are per *wire protocol* (of which there are only a handful) and model names are opaque
    passthrough strings, so a model released today works today.

## Conversation History

A major change between `dic` and `llm` is how conversation history is stored.
In `llm`, conversations are always "linear" and always "global",
but in `dic` conversations can have non-linear tree structures and are local to the current shell session.

The way this works is that all messages sent with `dic` have a "message id" or "mid" which serves as the primary key in a sqlite table "messages" located at `$XDG_STATE_HOME/geni/dic.db` (`~/.local/state/geni/dic.db` by default).
The messages table has the following columns:
- `mid`: a ULID
- `round`: which API call of this user turn the row is.  A call that used tools
  is several requests for one answer, so each is a row of its own and `prev_mid`
  chains them; the cost of a turn is the sum of its rounds.
- `user`: the user prompt
- `system`: the system prompt
- `response`: the plain text of the API response, as streamed to stdout
- `response_raw`: the provider's own JSON content blocks for the assistant turn, stored verbatim
- `prev_mid`: (default NULL) previous `mid` if the conversation is multi-turn; importantly, two messages can share the same `prev_mid`, so the structure forms a tree and not a linked list
- `attachments`: a list of indexes into the attachments table
- `outputs`: the files this turn produced, as a JSON list of
  `{path, mime_type}`; the bytes live on disk exactly as an attachment's do
- `tool_results`: what the tools this round asked for answered, as a JSON list
  of `{id, name, ok, error, content, t_start, t_end}`.  `content` is what the
  next round is sent, so a tool's answer survives `-c`; `ok` is false for a tool
  that raised as much as for one that was never offered, so a failure rate is a
  count of the rows where it is false.
- `model_id`: the `model_id` used to generate the response
- `api_type`: the wire protocol used to generate the response (see "Model configuration")
- `session`: the `DIC_SESSION` this row was written under.  A session name is
  a path, so a subagent that runs under `DIC_SESSION=parent/scruta-1` is a child
  of `parent`, and the cost of a harness run is one query over one index.  This
  is not session state: a pointer is still a tmpfs file, and this is a fact about
  the row, like `model_id`.
- `upstream`: the upstream that actually answered, as the router named it in
  its own `provider` field, or NULL when the response named none.  A fact about
  the row like `session`, and the one that makes a router's choices countable:
  a model served slowly by one upstream and quickly by another is otherwise a
  single average over both.  The wire field is the router's `provider`; the
  column is `upstream`, because a "provider" in dic is a configuration entry
  a model inherits from and the two are not the same thing.
- `status`, `error`: the HTTP status of the call and the server's message when it was not 200
- `usage`: the quantities the API reported, as a JSON object of disjoint dotted
  names: `in` is the input charged at the usual rate, `in.cache_read` the part
  served from a cache, `out.reasoning` the output that was billed but never
  printed.  Disjoint, so that a cost is a plain sum over the names a rule prices,
  and so that no token is charged twice however many rules name it.
- `cost`, `cost_items`, `price_hash`: what the call was billed, the per-rule
  breakdown that produced it, and the price table it was read from.  A price
  table lives in a config file, which is edited; a bill is not, so it is
  *stored* when the call ends and never recomputed.  A call that never
  finished -- a ^C, or a stream that failed after its 200 -- is stored with
  `cost` NULL and not zero, because `--stats` sums that column and a call
  that was cut short is not a free one.  A provider that reports what it
  charged is the ground truth and its dollars are the ones stored; the
  table's estimate is kept beside them in `cost_items`, and `price_hash` is
  then the literal `provider` instead of a table's, so an invoiced row is
  countable apart from an estimated one.
- `tokens_input`, `tokens_output`, `tokens_reasoning`: the three counts a price
  is usually quoted in, as `GENERATED` `VIRTUAL` columns over `usage`.  The same
  rule as `time` below: a value that is a function of another value is not
  stored, and nothing outside sqlite needs to know that these are views.
- `t_start`, `t_connect`, `t_request`, `t_headers`, `t_first`, `t_last`, `t_done`:
  nanoseconds since the epoch at each phase boundary of the call (see "Timing")

There is no `time` column: it is `t_start / 1000000000`, and a value that is a
function of another value is not stored.

There must be an index on `prev_mid`, since reconstructing a conversation walks the tree upwards.

`dic` accepts a `--mid` flag which allows continuing the conversation from any previous message,
and so the `messages` table must contain all information needed to ensure that the conversation can be reconstructed in the future.
This is why both `response` and `response_raw` are stored:
`response` is what a human wants to read, but `response_raw` is what must be sent back to the API,
and it may contain blocks (reasoning blocks, signatures, tool calls) that `dic` does not itself understand.
`dic` must be able to replay these blocks without understanding them.

## Timing and statistics

`dic` stores *instants*, never durations: every metric anyone has asked for is the
difference of two of them, and subtraction is sqlite's job.

| column | taken |
| ------ | ----- |
| `t_start`   | the first line of `dic.py`, before every import but `time` |
| `t_connect` | TCP and TLS established |
| `t_request` | the request body has been written |
| `t_headers` | the response headers have arrived |
| `t_first`   | the first event carrying text |
| `t_last`    | the stream closed |
| `t_done`    | the row is written, immediately before exit |

These are the instants of one *round*, and a call that used tools has several.
The first round's `t_start` is the process's, before every import but `time`; a
later round's is where the round before it stopped, so `t_request - t_start` is
that round's own overhead — `dic`'s own for the first round, including the
interpreter, the config query, the history query and whichever adaptor was
imported, and the tools that ran meanwhile for a later one.  Time to first token
is `t_first - t_start`, generation rate is `tokens_output / (t_last - t_first)`,
and end of response is `t_last - t_start`, all within one round; the rate of the
turn as a whole is over the sum of its rounds' streams, which is why the dollars
on the cost line are summed too.

A failed call is stored too, with its `status` and `error` and an empty `response`,
so that an error rate is countable and a provider's failures do not silently vanish.
The session pointer is *not* moved on failure, so a failed row is always a leaf and
is never replayed into a later conversation.

### The stats view

Nothing derived is materialized.  A `stats` view names each duration, in
milliseconds, and each rate:

```sql
CREATE VIEW stats AS SELECT
    mid, model_id, api_type, status, upstream, cost, price_hash,
    substr(model_id, 1, instr(model_id || '+', '+') - 1) AS head,
    t_start / 1000000000 AS time,
    (t_request - t_start) / 1e6 AS ms_overhead,
    (t_first   - t_start) / 1e6 AS ms_ttft,
    ...
  FROM messages;
```

`dic --stats` is then a single `GROUP BY model_id` over that view: frequency of use
(`count(*)`) and runtime performance (`avg(ms_ttft)`, `avg(tok_per_sec)`, ...) are
the same aggregate over the same rows, so they are one query, with averages
restricted to `status = 200`.  `sum(cost)` is the historic total, added up and
never recomputed, with `count(DISTINCT price_hash)` beside it so that a model
whose rows were rated under two different tables is visible rather than averaged.
Python joins the resulting cells with tabs and computes nothing; the output is
therefore already `sort`- and `awk`-shaped.
This query scans `messages` and will grow slower with the table, which is
acceptable: `--stats` is never on the latency path.
A second query over `json_each(tool_results)` is printed below it when a tool
has run: each tool, per model, with how often it was called and how often it
failed, because a tool that fails is a turn the model had to correct and not a
call dic made.

`dic stats --upstreams` is that same aggregate one level deeper: `GROUP BY
model_id, upstream` over the rows a router named an upstream for, filtered by
`-m` the same way `--stats` is.  A provider that answers directly sends no
`provider` field and so has no rows here, which is why it is a query of its own
and not a column of `--stats`: the question is not how a model performed but
which of the routes it was served by did.

Grouping by `head` instead, or filtering by `time`, is a matter of editing the
one query — which is why the view stores the parts rather than the answers.

The `dic` tool also accepts a `-c` flag to continue the previous conversation.
In `llm`, the `-c` flag is global, and so if you use the `llm` tool in two separate terminal bash sessions, they will follow the same conversation.
In `dic`, however, `-c` is local to the current shell session.
The session is identified by the `DIC_SESSION` environment variable,
which the user is expected to set in their shell startup file, e.g.

```bash
export DIC_SESSION=$(uuidgen)
```

If `DIC_SESSION` is unset, the session key is the literal string `global`,
and so `-c` behaves exactly as it does in `llm`.
This makes the mechanism trivially explicit:
subshells, pipelines and command substitutions inherit the variable and therefore the conversation;
two terminals get two conversations;
two terminals that deliberately export the same value share one conversation;
and `DIC_SESSION=foo dic -c ...` is a one-off way to address a named conversation.

The pointer for each session is stored as a single file at
`$XDG_RUNTIME_DIR/geni/<session>` whose contents are the last `mid` written by that session.
The name is percent-escaped before it is used as a filename -- `%` becomes
`%25` and `/` becomes `%2F` -- because a session name is a path and a file
cannot also be a directory: `parent` and `parent/child` are two files at the
same level, and neither is in the way of the other.
This location is a tmpfs, so the pointers are wiped on logout and reboot with no garbage collection,
no liveness checking, and no locking beyond an atomic rename.
The file's mtime serves as the "last used" time for free.
The file is a ref and is read and written as one: `dic rev-parse NAME` is
its value, `dic branch` draws the sessions as the tree their names spell,
`dic branch NAME START` forks one, `dic update-ref NAME NEW [OLD]` moves
one, and `dic branch -d NAME` or `dic update-ref -d NAME` removes one.
OLD is `git update-ref`'s own compare-and-swap -- the move fails unless the
ref still holds it -- and it is what lets a harness merge a forked branch
back without clobbering a parent that moved meanwhile.  No wrapper derives
the name-to-file rule for itself: the escaping lives in one function, and
these verbs are how anything else reaches it.

A move also reports the cost of the conversation the new mid ends, and,
when OLD is given and the calls above it cost anything, the cost of what
the fork added above it.  Both go to stderr in the cost role, before the
compare-and-swap: the calls were made whether or not the parent moved, and
a harness that forked, spent money and merged its ref back is the one place
the two numbers are already in hand.

There is deliberately no `sessions` table in sqlite:
session state is ephemeral, and the message tree in sqlite is immutable and append-only.
What a row stores instead is the *name* of the session that wrote it -- one
column, so that a harness's total spend is one aggregate rather than a walk of
the tmpfs directory, and a nested name is a subtree.
If `XDG_RUNTIME_DIR` is unset, fall back to `/tmp/geni-$UID/`.

If `-c` is passed we assume that `--mid` is not passed,
and the correct behavior is to first do a lookup to find the appropriate `--mid` value and then proceed as if that value was passed.
That is, `-c` is pure sugar for `--mid $(cat $XDG_RUNTIME_DIR/fac/dic/$DIC_SESSION)`.
If both are passed, this is an error: `-c` and `--mid` name two answers to
one question and neither silently wins.
If `-c` is passed and the session pointer does not exist, this is an error and `dic` exits nonzero;
it must never silently start a new conversation or continue somebody else's.

A third table `attachments` names the files attached with `-a`.
It has a ULID as primary key, a `path`, the file's `hash`, and its `mime-type`.
The file is the truth and is never copied: the bytes stay where the user put
them and are re-encoded for whichever provider is next, so the same attachment
serves a video model and a text model without existing twice.
An attachment and an output are the same kind of thing -- both are files a turn
names -- so both are recorded the same way and both are read back from disk when
the conversation is rebuilt.
A file a turn names that dic can no longer read is an error, never a turn that
quietly loses it.

A fourth table `record` holds the byte-rate samples one round's streams
produced, so that a plot of B/s over the generation is a query and not only
something watched on a terminal.  It has one row per round that was
recorded, keyed by `mid`, and none for a round that was not, so an install
that does not record carries no bytes and no reader of the message tree pays
for the indirection.
`samples` is a JSON list of `[t_ms, thinking, response]` on a 100ms grid: how
many bytes of reasoning and how many bytes of answer had arrived by each
point, in milliseconds since the round's request went out.  Recording is off
by default -- `--record=bps` / `DIC_RECORD` -- because it is a diagnostic and not
a fact a conversation needs, and a round it skips is a missing row rather
than a null.

## Reading a log

`dic log` is the readout a person reads and the one a completion picker is
built from, and it is git's own log over a message tree.

A row is the mid, a tab, and then `--format` expanded, so column one is the
full mid and column two is what a person reads.  A picker therefore hides the
first with `--with-nth=2..`, previews a row with `dic show {1}`, and reads
back the mid it was given without a second query.  `--graph` draws the forest
at the head of column two and not of the line, so the mid stays column one
however the forest looks, and the lines a merge draws carry no mid and are
therefore not pickable.

`--format FMT` is git's %-syntax over a row:

| specifier | value |
| --------- | ----- |
| `%H` `%h` | the mid, and its ten-character prefix |
| `%s` `%b` | the prompt, and the first line of the answer |
| `%a` | how long ago, as `5m ago` |
| `%m` `%u` `%S` | the model, the upstream a router chose, the session |
| `%c` `%t` | the dollars the call was billed, and its token count |
| `%D` | the decorations, which `--decorate` turns on |
| `%n` `%xNN` `%%` | a newline, one byte named by its hex, a literal percent |

Anything else is an error, because a specifier that is dropped is a log that
reads as if nothing happened.  The default is `%h%D %a %m %s`.

`--oneline` is `--format='%h %s'` under the name every git user already
knows: the short mid and the prompt, with `--decorate`'s mark inline when
there is one.  Naming both it and `--format` at once is an error, because
they are two answers to what a row prints.

`--decorate` makes `%D` ` (HEAD)` on the message this session's pointer holds
-- the one `-c` would resume -- and ` (session:NAME)` on a row another shell
wrote, so a row read out of `--all` is attributable and the conversation a
bare `-c` would continue is visible in the list rather than only in the
picker.  `%D` writes its own leading space and nothing at all when it has
nothing to say, which is git's convention and what lets a format put it
beside its neighbour.

`--grep TEXT` keeps the rows whose prompt contains TEXT, and `-S TEXT` keeps
the rows whose prompt or answer contains it, which is the one that answers
whether a string ever reached the model.  Both are bound values, neither is
SQL, and both narrow whatever scope `--all` or `--session` chose rather than
replacing it.

### The long format

A `dic log` with no `--format` and no `--oneline` prints git log's own long
shape: a header block, a blank line, then the turn's parts.

    mid 01M50TPQ2J6PHW2KK5D8KKMSTS (HEAD)
    Model:    openrouter+deepseek
    Date:     Tue Jun 02 14:23:04 2026

    Prompt:
        $ accio src/dic scripts

    Response:
        You are a coding agent.

A turn that called tools is one record, not several: the header block is the
round-0 row's, which is the one carrying the prompt, and the rounds after it
are a block each, numbered and named for the tools they called, because the
rounds are one answer and a log that showed them apart would say a
conversation had more turns than it did.  A round whose own head was filtered
out of the query -- a `--grep` that matched the answer and not the prompt --
is shown as a record of its own rather than dropped.

The prompt and every answer keep their first and last ten lines, with
`<... N lines elided ...>` between, because either can be a pasted file or a
model's essay and a log is for finding a turn and not for reading it.
`--full` prints the whole of both and `--lines N` retunes the ends.  `--stat`
adds the dollars and tokens the turn was billed, summed over its rounds, so
the default header stays three lines.

Output that is longer than a screen is piped through `$DIC_PAGER`, then
`$PAGER`, then `less -FRX`, but only when stdout is a terminal, so a pipe is
never handed a pager it did not ask for.  `--no-pager` prints straight
through.  `dic show` renders with the same code, plus the details a log of
many rows leaves out -- the system prompt, the files the turn named, the
tools it called, the wire protocol -- so a row's preview and its record
cannot disagree.

## Model configuration

Models are configured in JSON, not YAML.
`json` is a C extension already in the interpreter; `import yaml` alone costs tens of
milliseconds, which is more than the time-to-first-token `dic` exists to protect.
`tomllib` is pure Python and its array-of-tables syntax is painful at a thousand models.

There are four sources, weakest first:

1. `dic/models.json`, the defaults shipped with the package,
2. `~/.config/geni/providers.json`, the user's providers,
3. `~/.config/geni/models.json`, the user's models,
4. `$DIC_MODELS`, if it names a file.

Each file is one JSON *object* mapping a model id to that entry's own keys,
so lookup is a dict lookup and not a scan.
Ids beginning with `#` are ignored, which is where comments go.
Entries with the same id in two files are merged key by key,
later files winning, so a user file may change one price without restating a model.

There is no separate schema for providers.
`providers.json` and `models.json` differ only in which entries people tend to put in them.

### Inheritance

A model id is a `+`-separated chain of names, weakest first:

```json
{
  "groq":      {"abstract": true,
                "api_base": "https://api.groq.com/openai/v1",
                "api_key_name": "GROQ_API_KEY"},
  "groq+qwen": {"model_name": "qwen/qwen3.8-27b", "alias": "qwen",
                "options": {"max_tokens": 200}},
  "thinking-high": {"abstract": true,
                    "options": {"thinking": {"type": "enabled",
                                             "budget_tokens": 8000}}}
}
```

`dic -m groq+qwen` resolves the chain `groq`, then `groq+qwen`,
merging each entry into the accumulated result with RFC 7386 semantics:
objects merge recursively and a `null` deletes a key.
A model therefore states only what it changes about its provider,
and `-o` still overrides individual options on top of that.

Entries that exist only to be inherited from — providers, and mixins such as
`thinking-high` — set `"abstract": true`.
They cannot be named with `-m`, and that is the *only* difference between a provider
and a model: one schema, one namespace, one table.

Chains nobody wrote down also work.
`dic -m thinking-high+groq+qwen` materializes each prefix of the chain,
giving each the keys of the longest *defined* suffix of that prefix,
so the combination means exactly what it reads as: the mixin, then the provider, then the model.

The delimiter is `+` rather than `/`, because `/` already appears inside model names
(`qwen/qwen3.8-27b`, OpenRouter's `anthropic/claude-...`);
rather than `[]`, because those are glob metacharacters and `dic -m [groq]qwen`
is an error under zsh's default `nomatch`;
and rather than a space, because that would require quoting on every invocation.
Composition is associative and rightmost-wins, so a linear chain loses nothing
that a nested notation would have bought: a named intermediate is just another entry.

### Keys

After inheritance an entry must have:
1. `model_name`: the name of the model in the API
2. `api_key_name`: the name of the environment variable that stores the API key (semantics differ from `llm`)
3. `api_base`: the API endpoint, without the protocol-specific path suffix

Optional keys are:
1. `api_type`: which wire protocol to speak; defaults to `openai-chat`
2. `options`: a mapping merged verbatim into the JSON request body
3. `headers`: a mapping merged verbatim into the HTTP request headers
4. `price`: the rules that rate this model's `usage` (see "Prices").
   `cost_input` and `cost_output` are the two rules every file in the wild
   states, kept as shorthand for an `in` and an `out` rule at the same rate;
   a rule in `price` for the same name wins over them.
5. `system`: a default system prompt for this model, used when the conversation is
   new and neither `-s` nor `DIC_SYSTEM` is given.
   Unlike `DIC_SYSTEM` this is per-model, which is what a model-specific house style needs.
6. `alias`: the shell alias and `-m` shorthand for this entry.
7. `abstract`: this entry may only be inherited from.
8. `output`: the mime type of the answer, so that a non-text model can demand
   `--path` before the call is made; defaults to `text/plain`.

The `options` and `headers` keys are the primary extensibility mechanism.
Most new provider features are new JSON fields or new beta headers,
so these can be used before `dic` knows anything about them.
`dic` must not validate them: unknown keys are forwarded to the API and the API is allowed to reject them.
Client-side validation is what makes tools obsolete on release day.

`-o` is transparent and dic never reads it back: what a model was *asked*
for is the provider's business and what it *charged* is in the response,
which is where a price is read from.  The two keys dic owns instead of
forwarding are `tools`, which `--tools` writes, and the cache breakpoint
`--cache` builds (see "Prompt caching").

### Prices

How much a call cost is a function of two things, and `dic` keeps them apart:
the *quantities* in `usage`, and the *table* that turns them into dollars.
Quantities can be read again forever; tables are edited, so the dollars a table
produced are stored with the row and never recomputed.  There is no `--reprice`.

A price table is one object of named rules:

```json
"price": {
  "in":            {"rate": 3.00},
  "in.cache_read": {"rate": 0.30},
  "in.long":       {"rate": 6.00, "key": "in", "when": "in_total > 200000"},
  "out":           {"rate": 15.00},
  "out.batch":     {"rate": 7.50, "key": "out", "tier": "batch"}
}
```

A rule is named by the `usage` name it rates, or by a prefix of one, in which
case it rates everything under that prefix: `in` prices every input token and
`in.cache_read` prices only the ones served from a cache.  A rule may instead
say `key`, naming the usage name it rates itself, which is what a rule that only
prices *part* of a request's tokens needs -- the second rate for a prompt past
the long context threshold states the same key as the first and distinguishes
itself with `when`.

`unit` says what one of the quantity is.  It defaults to `token`, which is
quoted per million, like `cost_input` always was; any other unit is quoted per
one, so an image at `$0.04` is one rule and nothing else in the table changes.

Batching, flex and priority tiers, and the long context threshold are all
properties of the *request* and not of a token in it, so they are matched
against request facts -- `tier`, and the totals `in_total` and `out_total` --
and not against the name.  `when` is a python expression over those facts,
evaluated with no builtins; it is a config file, exactly as trusted as the
adapters escape hatch and no more.

The most specific matching rule wins.  Two rules that match equally well at two
different rates are an error, because a silently picked price is a wrong invoice.

### Prompt caching

Caching is a purchase, so it is off unless it can be priced.  A call carries a
provider cache breakpoint only when `--cache=TTL` names a TTL the model's own
price table rates -- `in.cache_write.5m`, `in.cache_write.1h` -- and `off` is
the default for every model and the only value for a model that rates none.
Turning caching off is therefore not a switch but the absence of a cache rule,
which is why `openrouter+*` and every other provider whose cache behavior dic
cannot rate are never sent a `cache_control` block at all.

`--cache=off|5m|1h`, or `$DIC_CACHE`, chooses per call.  A TTL the model does
not price is an error naming the ones it does, never a silent miss: a cache the
user believes is warm is worse than no cache.

The TTL is a property of the *write*, not of the read, so a read is priced by
`in.cache_read` however the entry was written.  Where a provider reports the
write side split by TTL, each TTL is a usage name of its own and is billed at
its own rate; where it reports one total, that total is billed at the TTL the
call asked for, because that is the TTL the provider charged.

`dic` marks the system prompt and the last turn of the conversation, the two
prefixes a continuing call reuses, and copies what it marks: what is stored in
`response_raw` is what the provider sent, and a breakpoint belongs to one
request and never to the record.  A provider's opaque reasoning block takes no
breakpoint, so one is placed on the last block that does.

### The config cache

Parsing configuration on every invocation is a cost paid by every invocation,
so the parsed entries live in `dic.db` beside the messages:

```sql
CREATE TABLE config(id TEXT PRIMARY KEY, parent TEXT, keys TEXT,
                    abstract INTEGER, alias TEXT, pos INTEGER, source TEXT);
CREATE TABLE config_meta(path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER);
```

`keys` is the entry's *own* JSON only; `parent` is everything left of the last `+`,
`NULL` at the root.
Startup stats each source file (~20 µs each) and reparses only when an mtime or size
no longer matches `config_meta`, in which case both tables are rewritten in one transaction.

Resolution is then a single round trip: a recursive CTE walks the ancestor chain —
the same shape of query as `history()`, which walks a conversation's ancestor chain,
so *the same query answers "who is my configuration past" and "who is my conversational past"* —
linearizes it root-first, and folds it with sqlite's `json_patch`,
which is RFC 7386 merge for free.
Depth is capped so a hand-written cycle cannot hang `dic`.

### Defaults, aliases and listing

When `-m` is not given, the model is `$DIC_MODEL` if it is set.
Otherwise `dic` uses the first configured entry whose `api_key_name` is actually set
in the environment, so an install holding only one provider's key needs no configuration at all;
failing even that, the first entry, which then fails with a message naming the key to export.
An id that is not configured is an error, never a silent fallback.

`dic --models` is one tab-separated table with a header: the id, the
environment variable its `api_key_name` resolves to, and whether that
variable is set.  A model whose key is missing is marked rather than
hidden, so a user chasing a model that will not run sees the export it
names, and the id stays in column one, so `tail -n +2 | cut -f1` is what
shell completion reads.  `dic --aliases` prints
`alias qwen='dic -m groq+qwen'` for every entry with an `alias` key,
which `dic.sh` evals.
Shell names and model names therefore come from one table and cannot drift apart,
and the same list is what shell completion should use.

The fully resolved id is what is stored in the `model_id` column,
so a conversation still says which entry produced each turn after the files are edited.

### Wire protocols

`api_type` selects one of a small number of built-in adapters.
Adapters correspond to protocols, not vendors, and each is a few dozen lines that
builds a request body and parses a stream of server-sent events.

| `api_type`           | notes |
| -------------------- | ----- |
| `openai-chat`        | the default; also OpenRouter, Groq, Together, vLLM, llama.cpp, ollama, ... |
| `openai-responses`   | OpenAI's newer endpoint |
| `anthropic-messages` | Anthropic's native endpoint |
| `openai-images`      | OpenAI's /images/generations and /images/edits |
| `openai-videos`      | OpenAI's /videos: create, poll, download |
| `fal`                | fal.ai's queue API: submit, poll, fetch |

Note that Anthropic also offers an OpenAI-compatible shim at the same `api_base`,
which can be used with `api_type: openai-chat`,
but it silently ignores unsupported fields and does not expose reasoning or tool-use blocks faithfully,
so `anthropic-messages` is preferred.

As a final escape hatch, if `api_type` names a file `~/.config/geni/adapters/<api_type>.py`,
that file is imported and used as the adapter.
It is loaded by path, but it imports `dic`'s own modules by package name --
`from dic.store import data_url` -- exactly as the built-in adapters do.
This import happens only when a model that references it is actually selected,
so it costs nothing at startup for everyone else.
An adaptor may define `prepare(model, key, turns, params)` and
`call(model, key, body, line, stamps)` on top of the five
names above.  `prepare` runs before `build` and may rewrite the turns --
`fal` uploads the newest turn's attachments there, because its models read a
URL and not a data: URL.  `call` replaces the transport and yields the
events `parse` already knows, which is what a protocol that polls and then
answers with a file rather than a stream of tokens needs.  It repaints
`line`, the one line dic keeps on stderr for progress, so a queued job's
status reads like the ttft clock a slow token starts instead of scrolling a
line of its own.


### Switching models mid-conversation

Because `--mid` can point at any message, a conversation may span several providers.
To make this work, `dic` has a provider-neutral intermediate representation of a conversation turn:
a list of `{role, blocks}`, where each block is one of
`text`, `image` (raw bytes plus mime type), `tool_call`, `tool_result`, or `thinking`.
Each adapter implements a conversion to and from this representation.

When building a request, `dic` walks the ancestor chain and for each historical turn:

1. if the stored `api_type` equals the target `api_type`, the stored `response_raw` blocks are replayed
   verbatim; this preserves full fidelity, provider signatures, and any prompt-cache prefix;
2. otherwise the turn is converted through the intermediate representation.

Text, system prompts, attachments and tool calls survive conversion, and a
file a turn names is read from disk here: if it is gone the call fails, because
a turn that silently loses its video is worse than a turn that refuses to be
sent.
Provider-specific opaque blocks do not:
Anthropic `thinking` blocks carry signatures that are only valid for the model that produced them,
and OpenAI reasoning items are encrypted.
On a cross-provider transition these blocks are dropped entirely, never partially;
they remain in the database for display and audit.
Prompt caching is also lost at the point of transition, by definition.

Conversion must additionally normalize the history into the form the target requires:
merge consecutive same-role turns, drop empty text blocks,
and move the system prompt between a top-level parameter and a leading message as appropriate.
A single-provider conversation never touches this code path.

## shell integration

The script `dic.sh` contains useful aliases and defaults.
