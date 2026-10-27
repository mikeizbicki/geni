"""One module per catalog shape.

A fetcher is a function
`fetch(provider, entry, key, env, progress) -> {model_id: config}`: it
takes the abstract entry the recipe lives on, talks to the provider
however that provider talks, and hands back the entries this run
should write.  Nothing generic is done to what comes back -- the
driver merges the dicts and writes the file -- so a fetcher is the
one place a new catalog shape costs code.

`progress(phase, done, total=None)` is where a fetcher says it is
still working: a page listed, a batch priced.  `total` is given only
when the fetcher already knows it -- a cursor walk has no such thing,
a batch list does -- and the driver is what turns the pair into the
one line a person watches.

The names are the ones a `sync` recipe says: `"fetcher": "openai"` is
fetchers.openai.fetch, and a provider whose catalog is a shape dic has
never seen is a new module here and one line in FETCHERS.

    openai        GET {base}/models -> {"data":[{"id": ...}]}
    fal           /models with a cursor, and /models/pricing in batches
    openrouter    three catalogs, three pricing shapes, one provider
"""
from . import fal, openai, openrouter

FETCHERS = {
    "openai": openai.fetch,
    "fal": fal.fetch,
    "openrouter": openrouter.fetch,
}
