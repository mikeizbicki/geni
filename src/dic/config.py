"""Model configuration: json sources, a sqlite cache, one recursive query.

Configuration is plain json -- an object mapping an id to that entry's own
keys -- because `json` is C and `yaml` is a fifty millisecond import, which is
more than the time-to-first-token this program exists to protect.

An id is a `+`-separated chain of names, weakest first: `groq+qwen` is the
entry `qwen` layered onto the entry `groq`, and `thinking-high+groq+qwen`
layers a mixin under both.  Entries that exist only to be layered on
(providers, mixins) set `"abstract": true` and cannot be named with -m; that
is the only difference between a "provider" and a "model", so both files have
one schema and one namespace.

Resolution merges the chain with sqlite's json_patch (RFC 7386: objects merge
recursively, a null deletes), so a model states only what it changes about its
provider.  The parsed entries live in the `config` table of dic.db and are
reparsed only when a source file's mtime or size changes, so the hot path is
one stat per file and one query.
"""
import json, os

from dic.store import config_dir
from dic.tty import DicError

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_PATH = os.path.join(HERE, "models.json")


def providers_path(env):
    """The user's providers, ~/.config/fac/providers.json."""
    return os.path.join(config_dir(env), "providers.json")


def models_path(env):
    """The user's models, ~/.config/fac/models.json."""
    return os.path.join(config_dir(env), "models.json")

# The ancestor chain of an id, folded root-first.  The first CTE is the same
# walk as store.history(): the same query answers "who is my conversational
# past" and "who is my configuration past".  The second linearizes it, the
# third folds it; depth is capped so a hand-written cycle cannot hang dic.
RESOLVE = """
WITH RECURSIVE anc(id, parent, keys, depth) AS (
    SELECT id, parent, keys, 0 FROM config WHERE id = ?
  UNION ALL
    SELECT c.id, c.parent, c.keys, anc.depth + 1
      FROM config c JOIN anc ON c.id = anc.parent WHERE anc.depth < 32),
ord(rn, keys) AS (
    SELECT row_number() OVER (ORDER BY depth DESC), keys FROM anc),
fold(rn, acc) AS (
    SELECT 1, keys FROM ord WHERE rn = 1
  UNION ALL
    SELECT o.rn, json_patch(f.acc, o.keys) FROM fold f JOIN ord o ON o.rn = f.rn + 1)
SELECT acc FROM fold ORDER BY rn DESC LIMIT 1
"""


def parent(model_id):
    """Everything left of the last '+', or None at the root of a chain.

    >>> parent("thinking-high+groq+qwen"), parent("groq")
    ('thinking-high+groq', None)
    """
    return model_id.rsplit("+", 1)[0] if "+" in model_id else None


def merge(base, over):
    """RFC 7386 merge of over into base, matching sqlite's json_patch exactly.

    Used only when two source files define the same id; the resolution of a
    chain is done by sqlite, with these same semantics.

    >>> merge({"options": {"max_tokens": 10, "top_p": 1}},
    ...       {"options": {"max_tokens": 20}})
    {'options': {'max_tokens': 20, 'top_p': 1}}
    >>> merge({"system": "x"}, {"system": None})
    {}
    """
    out = dict(base)
    for key, value in over.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def price_table(cfg):
    """cfg's price rules, with cost_input and cost_output expanded into them.

    A rule is named by the usage name it rates, or by a prefix of one, in
    which case it rates everything under that prefix: 'in' prices every input
    token and 'in.cache_read' prices only the ones a provider served from its
    cache.  The two older keys are kept because every config file in the wild
    states its price that way; a rule already in `price` for the same name
    wins, so a file may restate one rate of one model and nothing else.

    >>> price_table({"cost_input": 3.0, "cost_output": 15.0})["in"]
    {'rate': 3.0}
    >>> price_table({"cost_input": 3.0, "price": {"in": {"rate": 5.0}}})["in"]
    {'rate': 5.0}
    >>> price_table({})
    {}
    """
    table = {name: rule for name, rule in (cfg.get("price") or {}).items() if rule}
    for name, legacy in (("in", "cost_input"), ("out", "cost_output")):
        if cfg.get(legacy) is None:
            continue
        table[name] = {"rate": cfg[legacy], **(table.get(name) or {})}
    return table


def sources(env, models_file=None):
    """The configuration files that exist, weakest first.

    The packaged defaults, then the user's providers and models, then the
    file named by --models-file / $DIC_MODELS_FILE: later files are merged
    over earlier ones rather than replacing them, so a user file may change
    one key of one model.
    """
    paths = (DEFAULTS_PATH, providers_path(env), models_path(env), models_file)
    return [p for p in paths if p and os.path.exists(p)]


def entries(path):
    """One source file as {id: keys}; ids beginning with '#' are comments."""
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        raise DicError(f"cannot read {path}: {e}") from None
    except ValueError as e:
        raise DicError(f"malformed json in {path}: {e}") from None
    if not isinstance(data, dict):
        raise DicError(f"{path}: expected an object mapping model_id to its keys")
    out = {}
    for model_id, keys in data.items():
        if model_id.startswith("#"):
            continue
        if not isinstance(keys, dict):
            raise DicError(f"{path}: {model_id}: expected an object")
        out[model_id] = keys
    return out


def sync(conn, env, models_file=None):
    """Reparse the sources into the config cache, but only if one changed.

    A stat is ~20us and a parse is milliseconds, so the common invocation
    pays four stats and nothing else.
    """
    stamps = []
    for path in sources(env, models_file):
        st = os.stat(path)
        stamps.append((path, st.st_mtime_ns, st.st_size))
    cached = [tuple(r) for r in conn.execute(
        "SELECT path, mtime, size FROM config_meta ORDER BY path")]
    if cached == sorted(stamps):
        return

    combined, origin = {}, {}
    for path, _, _ in stamps:
        for model_id, keys in entries(path).items():
            combined[model_id] = merge(combined.get(model_id, {}), keys)
            origin[model_id] = path

    rows = []
    for pos, (model_id, keys) in enumerate(combined.items()):
        keys = dict(keys)
        abstract = 1 if keys.pop("abstract", False) else 0
        alias = keys.pop("alias", None)
        rows.append((model_id, parent(model_id), json.dumps(keys), abstract,
                     alias, pos, origin[model_id]))
    with conn:
        conn.execute("DELETE FROM config")
        conn.execute("DELETE FROM config_meta")
        conn.executemany("INSERT INTO config VALUES (?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO config_meta VALUES (?,?,?)", stamps)


def defined(conn, model_id):
    """Whether model_id is a row in the config table."""
    return conn.execute("SELECT 1 FROM config WHERE id=?", (model_id,)).fetchone() is not None


def canonical(conn, name):
    """name itself, or the id of the entry whose `alias` is name.

    So `dic -m qwen` and the shell alias `qwen` name the same model without
    either being written down twice.
    """
    if defined(conn, name):
        return name
    r = conn.execute("SELECT id FROM config WHERE alias=?", (name,)).fetchone()
    return r["id"] if r else name


def materialize(conn, model_id):
    """Give an ad-hoc chain like `thinking-high+groq+qwen` real rows.

    Every prefix becomes a row whose own keys are those of the longest defined
    suffix of that prefix, so a combination nobody wrote down still means what
    it reads as: the mixin, then the provider, then the model.  Once inserted
    it is an ordinary entry and resolves by the ordinary query.
    """
    if defined(conn, model_id):
        return
    names = model_id.split("+")
    if len(names) == 1 or not defined(conn, names[0]):
        raise DicError(f"unknown model: {model_id} (try `dic --models`)")
    rows = []
    for i in range(1, len(names) + 1):
        prefix = "+".join(names[:i])
        if defined(conn, prefix):
            continue
        keys = "{}"
        for j in range(1, i):          # the longest defined suffix of the prefix
            row = conn.execute("SELECT keys FROM config WHERE id=?",
                               ("+".join(names[j:i]),)).fetchone()
            if row:
                keys = row["keys"]
                break
        rows.append((prefix, parent(prefix), keys, 0, None, None, "ad-hoc"))
    if rows:
        with conn:
            conn.executemany("INSERT OR IGNORE INTO config VALUES (?,?,?,?,?,?,?)", rows)


def resolved_keys(conn, model_id):
    """The folded keys of model_id's chain, or {} if there is no such chain."""
    row = conn.execute(RESOLVE, (model_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def resolve(conn, model_id):
    """The fully merged configuration of model_id, with model_id itself added.

    The resolved id is what gets stored in the messages table, so a later
    --mid can say which entry produced a turn even after the files change.
    """
    model_id = canonical(conn, model_id)
    materialize(conn, model_id)
    row = conn.execute("SELECT abstract FROM config WHERE id=?", (model_id,)).fetchone()
    if row and row["abstract"]:
        raise DicError(f"{model_id} is abstract (a provider or mixin), not a model")
    cfg = resolved_keys(conn, model_id)
    if not cfg.get("model_name"):
        raise DicError(f"{model_id}: no model_name configured (try `dic --models`)")
    cfg["price"] = price_table(cfg)
    cfg["model_id"] = model_id
    return cfg


def model_ids(conn):
    """Every id that -m accepts, in configuration order."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM config WHERE abstract=0 AND pos IS NOT NULL ORDER BY pos")]


def default_id(conn, env):
    """The model used when neither -m nor $DIC_MODEL says otherwise.

    The first configured entry whose api_key_name is actually exported, so an
    install holding one provider's key needs no configuration at all; failing
    that the first entry, which then fails naming the key to export.
    """
    first = None
    for model_id in model_ids(conn):
        first = first or model_id
        if env.get(resolved_keys(conn, model_id).get("api_key_name") or ""):
            return model_id
    if not first:
        raise DicError(f"no models configured: write {models_path(env)}")
    return first


def aliases(conn):
    """Shell alias definitions for every entry that names one.

    `dic.sh` evals this, so the shell name and the model name come from the
    same table and cannot drift apart.
    """
    return "".join(f"alias {r['alias']}='dic -m {r['id']}'\n"
                   for r in conn.execute(
                       "SELECT id, alias FROM config"
                       " WHERE alias IS NOT NULL AND abstract=0 ORDER BY pos"))
