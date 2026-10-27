"""One GET or POST, JSON back: every failure a RuntimeError with a message.

dic-sync is not on a latency path and pays no price for urllib, so it
does not borrow dic.net's own connection and its dns progress row.  It
does borrow dic.store.http_url's refusal of a non-http scheme, because
a URL that arrived from a provider's own response is still a URL some
response chose, and file: is a URL urllib would honor.

The one thing urllib gives back that dic's own transport does not is an
error body, so it is read and folded into the message: a 401 that names
the missing variable is the difference between a five-minute fix and an
hour of guessing.
"""
import json, urllib.error, urllib.request

from dic.store import http_url

TIMEOUT_S = 30


def auth(entry, key):
    """The headers this provider's own catalog endpoint wants.

    The wire protocol decides: Anthropic's catalog wants a key header
    and a version, fal wants `Key` in front of its token, and every
    OpenAI-shaped listing wants `Bearer`.  The protocol is the entry's
    api_type and not the vendor's name, which is what makes a provider
    dic has never heard of work without a line of code.

    >>> auth({"api_type": "anthropic-messages"}, "k")["x-api-key"]
    'k'
    >>> auth({"api_type": "fal"}, "k")["authorization"]
    'Key k'
    >>> auth({}, "k")["authorization"]
    'Bearer k'
    """
    api_type = entry.get("api_type") or "openai-chat"
    if api_type == "anthropic-messages":
        return {"x-api-key": key, "anthropic-version": "2023-06-01",
                "accept": "application/json"}
    if api_type == "fal":
        return {"authorization": "Key " + key, "accept": "application/json"}
    return {"authorization": "Bearer " + key, "accept": "application/json"}


def get(url, headers, timeout=TIMEOUT_S):
    """GET url with headers and decode the body as JSON."""
    return _open(urllib.request.Request(http_url(url), headers=headers),  # noqa: S310
                 timeout)

def post(url, body, headers, timeout=TIMEOUT_S):
    """POST a JSON body and decode the reply as JSON."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310
        http_url(url), data=data,
        headers={**headers, "content-type": "application/json"})
    return _open(request, timeout)


def _open(request, timeout):
    """One request, one decoded body, one message on any failure."""
    try:
        with urllib.request.urlopen(  # noqa: S310
                request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"{e.code} {request.full_url}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{request.full_url}: {e}") from None
