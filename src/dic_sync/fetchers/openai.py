"""The OpenAI-shaped listing: GET {base}/models, {"data": [{"id": ...}]}.

Groq, Cerebras, Together, vLLM, llama.cpp, ollama, OpenAI itself, and
Anthropic's own /v1/models all answer this shape.  A provider whose
catalog is one GET under one path is this module's fetcher and no new
code; a provider whose catalog is a different shape is a new module.

No price is fetched: an OpenAI-shaped listing does not report one, and
a table written from a guess is a bill dic never paid.  The published
price pages are scripts/prices.sh's job, and its output is a file the
user edits and dic reads.
"""
from dic_sync import http
from dic_sync.ids import chain


def fetch(provider, entry, key, env, progress=None):  # noqa: ARG001
    """Every model one OpenAI-shaped catalog lists, as config entries.

    The path defaults to /models, which every provider of this shape
    serves; a catalog that lives elsewhere -- a provider whose own API
    is under a longer prefix -- names its path in the recipe, and a
    catalog that shares a host with another gets a prefix so the two
    do not collide in the config namespace.

    The api_base is the base of the call and not always the base of the
    catalog, so a recipe may override it with `catalog_base`; fal is the
    one that does.
    """
    recipe = entry["sync"]
    base = (recipe.get("catalog_base") or entry["api_base"]).rstrip("/")
    catalogs = recipe.get("catalogs") or [{"path": "/models"}]
    headers = http.auth(entry, key)
    out = {}
    for catalog in catalogs:
        body = http.get(base + catalog["path"], headers)
        for model in body.get("data") or []:
            slug = model["id"]
            prefix = catalog.get("prefix") or provider
            out[chain(prefix, slug)] = {"model_name": slug}
        if progress is not None:
            progress("listing", len(out))
    return out
