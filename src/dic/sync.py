"""Fetch each provider's model list, and write it as a config source.

`dic sync` asks every configured provider for its models and writes the
result to $XDG_STATE_HOME/geni/models.json, which config.sources() reads
below every hand-written file.  A model released this morning is usable
this morning; a model retired last week stops appearing after a re-sync;
and any id the user writes in their own models.json still shadows the
generated one of the same name.

Only the protocols that serve a list are asked.  openai-chat,
openai-responses and anthropic-messages all answer an OpenAI-shaped
GET /models with a data list of ids, so one request shape covers them
all; fal, openai-images, openai-videos and typesafe name one resource
at a time and have no list to ask.

Prices are not fetched.  What a model costs is not what a list endpoint
reports, and a table written from a guess is a bill dic never paid, so
a synced entry is a name and nothing else and a price stays whatever
the packaged defaults and the user's own files say.
"""
import json, os

from dic import config
from dic.store import http_url
from dic.tty import DicError, report

# The protocols whose models can be listed with one GET /models.  A
# provider whose api_type is not here is skipped, not failed: its models
# are hand-written, which is the only way dic has ever named them.
LISTABLE = ("openai-chat", "openai-responses", "anthropic-messages")

# A listing is a static list and not a call to a model, so a minute is
# long enough for a slow provider and short enough that a hung one is
# something the user sees now rather than later.
TIMEOUT_S = 30


def mangle(provider, slug):
    """A model list's id as a config id: one '+' chain, one safe segment.

    A '/' is a namespace in the model's own name and not a chain
    delimiter, so it becomes a '-'; a '+' would be read as another link
    in the chain and would resolve to the wrong entry.  The provider is
    unchanged: it is the head of the chain and the whole point of the id.

    >>> mangle("groq", "qwen/qwen3.8-27b")
    'groq+qwen-qwen3.8-27b'
    >>> mangle("openai", "gpt-6")
    'openai+gpt-6'
    >>> mangle("openai", "a+b")
    'openai+a-b'
    """
    return f"{provider}+{slug.replace('/', '-').replace('+', '-')}"


def providers(conn):
    """Every configured entry dic can ask for a list, as (id, keys).

    An entry a model inherits from -- what the rest of dic calls a
    provider -- is the only kind with an api_base and a key of its own
    to send them with, and only the ones whose api_type serves a list.
    A hand-written entry that states its own api_base is such an entry
    of its own, so a proxy under a name of its own is asked for under
    that name and not under the provider it copied.
    """
    rows = conn.execute(
        "SELECT id, keys FROM config WHERE abstract = 1").fetchall()
    out = []
    for row in rows:
        keys = json.loads(row["keys"] or "{}")
        if not keys.get("api_base") or not keys.get("api_key_name"):
            continue
        if (keys.get("api_type") or "openai-chat") not in LISTABLE:
            continue
        out.append((row["id"], keys))
    return out


def listed(provider, keys, key, timeout=TIMEOUT_S):
    """The ids one provider's GET /models reports, or a DicError saying why not."""
    import urllib.error, urllib.request

    base = keys["api_base"].rstrip("/")
    url = http_url(base + "/models")
    if (keys.get("api_type") or "openai-chat") == "anthropic-messages":
        headers = {"accept": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
    else:
        headers = {"accept": "application/json",
                   "authorization": "Bearer " + key}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        raise DicError(f"sync: {provider}: {url}: {e}") from None
    return [m["id"] for m in body.get("data") or [] if m.get("id")]


def fetch(conn, env, say):
    """The whole synced file: {id: entry} for every provider that answered.

    A provider whose key is not exported and one whose list endpoint
    fails are both reported and skipped, so an install holding one
    provider's key does not see errors for the rest and one stale key
    cannot blank the file.
    """
    out = {}
    for provider, keys in providers(conn):
        key = env.get(keys["api_key_name"])
        if not key:
            say(f"{provider}: {keys['api_key_name']} is not set; skipped")
            continue
        try:
            ids = listed(provider, keys, key)
        except DicError as e:
            say(str(e))
            continue
        for slug in ids:
            out[mangle(provider, slug)] = {"model_name": slug}
        say(f"{provider}: {len(ids)} model(s)")
    return out


def write(path, entries):
    """Write entries to path atomically: sorted, indented, whole or not at all.

    Sorted and indented, because this is a file a person reads; a `.tmp`
    unique to this write and a rename, because an invocation that reads
    it while a sync writes it must see one list or the other and not
    half of each.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def sync(conn, env):
    """dic sync: rebuild the synced model list from every provider's own.

    It reads no message row and calls no model -- the config cache is
    reparsed on the next invocation, whose stat sees the new file's
    mtime -- so it is a readout like every other command, with the
    network in place of the message tree.
    """
    path = config.synced_path(env)
    entries = fetch(conn, env,
                    lambda text: report(1, 1, f"sync: {text}", env=env))
    write(path, entries)
    return f"{path}: {len(entries)} model(s)\n"
