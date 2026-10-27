"""The driver: recipes in, one file out.

Each abstract entry with a `sync` key is a provider whose catalog this
program asks for; each `sync.fetcher` names the module that knows how.
Everything the recipe-driven part does is walk the entries, call the
fetcher, and merge the results -- the shape of a catalog and the shape
of a published price belong to the fetcher, and nothing here is per
provider.

The output is $XDG_STATE_HOME/geni/models.json -- dic.config.synced_path
names the same path -- which dic.config.sources() reads below every
hand-written source, so a user file may change one key of one synced
model and any id it writes shadows the synced one of the same name.

The output carries a `#schema` key.  dic.config.entries() skips every
id beginning with '#', so the key costs a reader loop one iteration and
a reader that does not know its value can refuse the whole file rather
than rate everything at zero, which is what a schema version is for.

The output is never a recipe source for its own run: a `sync` key in
it would be the writer reading its own last output back as
instructions, so recipe_files() excludes it by name and the walk inside
dic.config.sources() is the same otherwise.
"""
import json, os

from dic import config

# The file's own key and the value this build writes.  A later version
# of this program writes another; a reader checks the value and refuses
# one it does not know, which is why the key is at the top level and not
# beside a model.
SCHEMA_VERSION = 1
SCHEMA_KEY = "#schema"


def output_path(env):
    """The file this program writes and dic reads: the synced model list.

    Reached through dic.config so that the path and its "synced" name
    are one thing: a second spelling here would drift from the reader
    the next time the state directory moves.

    >>> output_path({"HOME": "/u"}).endswith("geni/models.json")
    True
    """
    return config.synced_path(env)


def recipe_files(env):
    """The sources a sync recipe may live in: dic's, minus our own output.

    The output file exists after the first run and would otherwise be in
    dic.config.sources() like any other, so the walk excludes it by
    path.  realpath and not the string, because two spellings of the
    same file are one file and a `..` in $DIC_MODELS would make them
    two strings.
    """
    out = os.path.realpath(output_path(env))
    for path in config.sources(env, env.get("DIC_MODELS")):
        if os.path.realpath(path) != out:
            yield path


def recipes(env):
    """Every abstract entry with a `sync` recipe, keyed by its provider id.

    An entry is a recipe when it says so: `abstract` is what makes it a
    thing to inherit from and not a model, and `sync` is what makes it a
    catalog to fetch.  Both together is a provider dic-sync may ask
    about; either alone is a config entry and not this program's.

    The merge is the same key-by-key RFC 7386 merge dic does, so a
    user's providers.json may add a sync key to a shipped provider, or
    change the fetcher of one, without restating the rest of its entry.
    """
    merged = {}
    for path in recipe_files(env):
        for model_id, keys in config.entries(path).items():
            merged[model_id] = config.merge(merged.get(model_id, {}), keys)
    return {model_id: keys for model_id, keys in merged.items()
            if keys.get("abstract") and keys.get("sync")}


def sync(env, report):
    """Fetch every provider's catalog and write the merged list.

    `report(level, text)` is where a line about a provider goes: level
    -1 for a failure, 0 for a count, 1 for a name as it is fetched.
    The caller decides what verbosity means; every failure is caught
    here and named, so one provider that will not answer does not stop
    the others and does not blank the file.

    Returns (path, count): where the file was written and how many
    models it holds.
    """
    from dic_sync.fetchers import FETCHERS

    out = {}
    for provider, entry in sorted(recipes(env).items()):
        recipe = entry["sync"]
        name = recipe.get("fetcher")
        fetch = FETCHERS.get(name)
        if fetch is None:
            report(-1, f"{provider}: unknown fetcher {name!r}")
            continue
        key_name = entry.get("api_key_name")
        key = env.get(key_name) if key_name else None
        if not key:
            report(0, f"{provider}: {key_name} is not set; skipped")
            continue
        try:
            entries = fetch(provider, entry, key, env)
        except Exception as e:  # a fetcher's failure; one provider's problem
            report(-1, f"{provider}: {e}")
            continue
        report(1, f"{provider}: {len(entries)} model(s)")
        out.update(entries)

    path = output_path(env)
    write(path, out)
    return path, len(out)


def write(path, entries):
    """Write the whole list to `path`, atomically: sorted, indented, whole or not.

    Sorted and indented because this is a file a person reads and
    reviews; a `.tmp` unique to this write and a rename because a dic
    reading it while a sync writes it must see one list or the other
    and never half of each.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamped = {SCHEMA_KEY: SCHEMA_VERSION, **entries}
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    with open(tmp, "w") as f:
        json.dump(stamped, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
