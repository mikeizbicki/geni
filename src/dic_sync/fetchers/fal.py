"""fal's catalog and its batched pricing.

Two endpoints on api.fal.ai (which is not the same host as
queue.fal.run, where fal's inference runs -- a recipe says so with
`catalog_base`): GET /models lists every endpoint with a next_cursor,
and GET /models/pricing takes `endpoint_id` as a one-to-fifty list and
returns a unit price for each.

fal's unit is a plural noun -- "seconds" for a video model, "images"
for a still -- and its value is already in dollars, so a price lands
in dic's table unchanged once the noun is made singular.  fal reports
no token axis for these models and no refinement like a resolution
tier, so a rule is `out` and nothing else.
"""
from dic_sync import http, units
from dic_sync.ids import chain

PAGE_LIMIT = 100
PRICE_BATCH = 50


def catalog(base, headers, progress=None):
    """Every endpoint_id the catalog lists, walking next_cursor.

    A listing nobody paginates is a listing that stops at the first
    page, and fal's catalog is large enough that the first page is a
    fraction of it.  The cursor is the loop's condition and not
    `has_more`, because a server that sends one without the other
    still terminates on the cursor and one that sends neither is
    <function catalog>
    """
    ids, cursor = [], None
    while True:
        url = f"{base}/models?limit={PAGE_LIMIT}"
        if cursor:
            url += f"&cursor={cursor}"
        body = http.get(url, headers)
        for model in body.get("models") or []:
            if model.get("endpoint_id"):
                ids.append(model["endpoint_id"])
        if progress is not None:
            progress("listing", len(ids))
        cursor = body.get("next_cursor")
        if not cursor:
            break
    return ids


def pricing(base, headers, ids, progress=None):
    """{endpoint_id: rule} for one catalog, in batches of fifty.

    The endpoint takes `endpoint_id` as a comma-separated query
    parameter and rejects more than fifty, so the walk is by fifty.  A
    model the pricing endpoint does not know keeps no rule and costs
    nothing, which is not a silent zero: it is a model dic stats can
    count and `dic log --format='%c'` prints as $0.  A batch whose
    request failed -- a rate limit that outlived the retry -- is skipped
    the same way, so one 429 costs that batch's prices and not the
    catalog.
    """
    out = {}
    total = len(ids)
    for i in range(0, len(ids), PRICE_BATCH):
        chunk = ids[i:i + PRICE_BATCH]
        url = f"{base}/models/pricing?endpoint_id=" + ",".join(chunk)
        try:
            body = http.get(url, headers)
        except RuntimeError:
            # the pricing endpoint is the rate-limited one: skipping a
            # batch costs its prices and not the whole catalog, and the
            # models it names land anyway, unpriced, which is what a
            # model the pricing endpoint does not know looks like too.
            body = None
        for price in (body or {}).get("prices") or []:
            endpoint = price.get("endpoint_id")
            if not endpoint or price.get("unit_price") is None:
                continue
            out[endpoint] = {"out": {
                "rate": float(price["unit_price"]),
                "unit": units.plural(price.get("unit") or "second")}}
        if progress is not None:
            progress("pricing", min(i + PRICE_BATCH, total), total)
    return out


def fetch(provider, entry, key, env, progress=None):  # noqa: ARG001
    """Every endpoint the catalog lists, each with the price it is sold at.

    The pricing call is a second pass over the whole catalog, because
    the endpoint takes a list of ids and not a wildcard; one catalog of
    a hundred models is two calls, and a catalog of a thousand is
    twenty, all of them here and none of them on dic's path.
    """
    recipe = entry["sync"]
    base = (recipe.get("catalog_base") or entry["api_base"]).rstrip("/")
    headers = http.auth(entry, key)
    out = {}
    for catalog_spec in recipe.get("catalogs") or [{"path": "/models"}]:
        ids = catalog(base, headers, progress)
        prices = pricing(base, headers, ids, progress)
        prefix = catalog_spec.get("prefix") or provider
        for endpoint in ids:
            config = {"model_name": endpoint}
            if endpoint in prices:
                config["price"] = prices[endpoint]
            out[chain(prefix, endpoint)] = config
    return out
