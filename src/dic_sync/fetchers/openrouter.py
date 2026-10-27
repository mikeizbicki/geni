"""Three catalogs, three pricing shapes, one provider.

OpenRouter publishes its chat, image and video models at three
different paths and prices them three different ways:

  /models           pricing inline on the row, quoted per TOKEN as a
                    decimal string, with `overrides` for a long-context
                    threshold
  /images/models    pricing only at the per-model /endpoints
                    sub-resource, keyed by `billable`
                    (`output_image`, `input_image`, ...)
  /videos/models    pricing inline as `pricing_skus`, whose keys carry
                    the resolution tier and whose values are
                    cent-strings

Only the chat catalog's pricing is translated.  Image pricing costs
one request per model and a sync would then be O(models) for one
provider; video pricing names a resolution the video adaptor does not
yet report in usage, so a tiered rule would match nothing and a flat
rule would be wrong for the tiers.  Both are named in a comment and
left out, which is a smaller lie than a price that does not match what
was bought.

The three paths and the two extra protocols live in the recipe, so the
fetcher is the loop and nothing per-catalog: a recipe that names a
fourth path, or renames one, or says that the third does have pricing
after all, is a change to the config file and not to this module.
"""
from dic_sync import http, units
from dic_sync.ids import chain

# The TTL a wire `input_cache_write` is billed at.  OpenRouter's own
# caching docs do not publish separate rates for a five-minute write and
# an hour, so a row that names one write price names one TTL and the
# constant here is the one.  A models.json that changes it changes it
# for every entry this fetcher writes, and dic reads whatever is there.
CACHE_WRITE_TTL = "5m"


# The routing keys a recipe catalog carries: everything else is a fact
# about the models it names -- api_type, output, and any future config
# key dic learns -- and is merged into their entries verbatim.
ROUTING = ("path", "prefix", "pricing")


def catalog_entry(slug, spec):
    """One model's config entry from its catalog spec: name and facts.

    Everything the spec says besides the routing keys is a fact about
    the models the spec listed, so it lands in each of them: a rule
    naming /images/models with an api_type of openai-images is how a
    model that arrived under the provider's own id inherits the
    protocol the catalog speaks and not the chat one.

    >>> catalog_entry("google/gemini-3", {"path": "/models",
    ...                                   "pricing": "chat"})
    {'model_name': 'google/gemini-3'}
    >>> catalog_entry("openai/gpt-image-2", {"path": "/images/models",
    ...                                      "api_type": "openai-images",
    ...                                      "output": "image/png"})
    {'model_name': 'openai/gpt-image-2', 'api_type': 'openai-images', 'output': 'image/png'}
    """
    entry = {"model_name": slug}
    for key, value in spec.items():
        if key not in ROUTING:
            entry[key] = value
    return entry


def chat_pricing(model):
    """One chat model's pricing as a dic rule table, or {} for none.

    A rate the wire does not publish is not written, and a rate the wire
    publishes as 0.0 is: a free tier is a price like any other, and
    dropping it would rewrite the catalog.

    >>> chat_pricing({"pricing": {
    ...     "prompt": "0.000001", "completion": "0.000005"}})
    {'in': {'rate': 1.0}, 'out': {'rate': 5.0}}
    >>> chat_pricing({"pricing": {
    ...     "prompt": "0.000001", "completion": "0.000005",
    ...     "overrides": [{"min_prompt_tokens": 272000,
    ...                    "prompt": "0.000002",
    ...                    "completion": "0.0000075"}]}})
    {'in': {'rate': 1.0}, 'out': {'rate': 5.0}, 'in.long': {'key': 'in', 'rate': 2.0, 'when': 'in_total > 272000'}, 'out.long': {'key': 'out', 'rate': 7.5, 'when': 'in_total > 272000'}}
    >>> chat_pricing({"pricing": {}})
    {}
    """
    pricing = model.get("pricing") or {}
    out = {}
    if pricing.get("prompt") is not None:
        out["in"] = {"rate": units.per_token(pricing["prompt"])}
    if pricing.get("completion") is not None:
        out["out"] = {"rate": units.per_token(pricing["completion"])}
    if pricing.get("input_cache_read") is not None:
        out["in.cache_read"] = {
            "rate": units.per_token(pricing["input_cache_read"])}
    if pricing.get("input_cache_write") is not None:
        out[f"in.cache_write.{CACHE_WRITE_TTL}"] = {
            "rate": units.per_token(pricing["input_cache_write"])}
    for override in pricing.get("overrides") or []:
        boundary = override.get("min_prompt_tokens")
        if boundary is None:
            continue
        condition = f"in_total > {boundary}"
        if override.get("prompt") is not None:
            out["in.long"] = {"key": "in",
                              "rate": units.per_token(override["prompt"]),
                              "when": condition}
        if override.get("completion") is not None:
            out["out.long"] = {"key": "out",
                               "rate": units.per_token(override["completion"]),
                               "when": condition}
    return out


def fetch(provider, entry, key, env, progress=None):  # noqa: ARG001
    """The catalogs one OpenRouter account serves, as config entries.

    The recipe names the paths and their pricing shapes; the fetcher is
    the loop that visits them and nothing per-catalog, so a fourth
    catalog, or one whose prices dic learns to translate, is a change
    to the config file.
    """
    base = entry["api_base"].rstrip("/")
    recipe = entry["sync"]
    headers = http.auth(entry, key)
    catalogs = recipe.get("catalogs") or [{"path": "/models",
                                           "pricing": "chat"}]
    out = {}
    for catalog in catalogs:
        path = catalog["path"]
        prefix = catalog.get("prefix") or provider
        before = len(out)
        for model in http.get(base + path, headers).get("data") or []:
            slug = model["id"]
            config = catalog_entry(slug, catalog)
            if catalog.get("pricing") == "chat":
                price = chat_pricing(model)
                if price:
                    config["price"] = price
            out[chain(prefix, slug)] = config
        if progress is not None:
            progress(path.strip("/"), len(out) - before)
    return out
