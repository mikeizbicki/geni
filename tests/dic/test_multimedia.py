"""The protocols that are not one POST and one stream.

test_client.py's fixture answers every POST with SSE, because every protocol
it tests is one request and one stream.  These three are not: openai-images
answers one JSON body, and openai-videos and fal submit a job, poll it over
GET, and download the asset over a second GET -- and fal uploads its
attachments to a second host with a PUT first.  So this file brings its own
handler and its own `models`, and reuses conftest's env, streams and path
fixtures by name, which is what makes it one file and not a second suite.

A failure is the other thing these adaptors do differently: they raise
DicError out of call() instead of stamping a status, so the row they must
still leave behind is what test_a_failure_from_a_call_adaptor is about.
"""
import base64, collections, http.server, json, os, threading, urllib.parse

import pytest

from dic.adaptors import fal
from dic.client import dic
from dic.store import config_dir, db, session_path
from dic.tty import DicError

IMAGE = b"\x89PNG\r\n\x1a\n\x00\x00"
VIDEO = b"\x00\x00\x00\x18ftypmp42"
JOB, VID = "req_1", "video_1"

Hit = collections.namedtuple("Hit", "kind path body")


def poll(server, kind, waiting, done):
    """Answer the first poll with a waiting state and the next with the done one.

    One turn of the poll loop, so the test exercises the repaint a job's
    status makes and not only the happy path that never waits.
    """
    server.polls[kind] = server.polls.get(kind, 0) + 1
    return waiting if server.polls[kind] == 1 else done


class _Handler(http.server.BaseHTTPRequestHandler):
    """A router: SSE is gone, and GET, PUT and mode-shaped POSTs are here."""

    def _send(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self, kind):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = raw          # a multipart create is bytes and stays bytes
        self.server.received.append(Hit(kind, self.path, body))
        return body

    def do_PUT(self):
        self._read("PUT")       # fal's upload: the file, not a form
        self._send(200, {})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        self._read("POST")
        if path == "/storage/upload/initiate":
            return self._send(200, {"upload_url": self.server.api_base + "/upload",
                                    "file_url": self.server.api_base + "/asset"})
        if self.server.status != 200:
            return self._send(self.server.status, self.server.error.encode())
        if self.server.mode == "images":
            return self._send(200, {"data": [
                {"b64_json": base64.b64encode(IMAGE).decode()}]})
        if self.server.mode == "videos":
            return self._send(200, {"id": VID})
        if self.server.mode == "fal":
            return self._send(200, {"request_id": JOB})
        self._send(404, {"error": "no mode"})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        self._read("GET")
        if path == "/asset":
            return self._send(200, IMAGE, "image/png")
        if path.endswith("/content"):
            return self._send(200, VIDEO, "video/mp4")
        if path.endswith("/status"):
            return self._send(200, poll(self.server, "fal",
                                        {"status": "IN_QUEUE"},
                                        {"status": "COMPLETED"}))
        if "/requests/" in path:
            return self._send(200, {"images": [
                {"url": self.server.api_base + "/asset"}]})
        if path.startswith("/videos/"):
            return self._send(200, poll(self.server, "videos",
                                        {"status": "in_progress"},
                                        {"status": "completed",
                                         "seconds": "5"}))
        self._send(404, {"error": "no route"})

    def log_message(self, *args):
        pass                    # the test's assertions are the log


@pytest.fixture
def server():
    """A real HTTP server that plays all three protocols, switched by `mode`.

    `mode` is "images", "videos" or "fal"; `status` and `error` replace the
    answer with a failure, as conftest's `sse` does; `received` is one Hit
    per request -- kind, path and body -- so a test can assert what dic sent
    and not only what came back; `polls` counts the GETs that waited.
    """
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    srv.api_base = f"http://127.0.0.1:{srv.server_port}"
    srv.mode, srv.status, srv.error = "images", 200, "boom"
    srv.received, srv.polls = [], {}
    threading.Thread(target=srv.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def models(env, server):
    """The test's models.json: `fake`, then one entry per non-streaming protocol.

    Each of the others states only its api_type and its output, so they are
    also the proof that -m fake+images inherits api_base, api_key_name and
    nothing else from `fake`.
    """
    directory = config_dir(env)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "models.json"), "w") as f:
        json.dump({
            "fake": {"model_name": "fake-1", "api_base": server.api_base,
                     "api_key_name": "FAKE_API_KEY"},
            "fake+images": {"api_type": "openai-images", "model_name": "gpt-image-1",
                            "output": "image/png",
                            "price": {"out": {"rate": 0.04, "unit": "image"}}},
            "fake+videos": {"api_type": "openai-videos", "model_name": "sora-2",
                            "output": "video/mp4",
                            "price": {"out": {"rate": 0.10, "unit": "second"}}},
            "fake+fal": {"api_type": "fal", "model_name": "fal-ai/nano-banana/edit",
                         "output": "image/png",
                         "price": {"out": {"rate": 0.04, "unit": "image"}}},
            "fake+stub": {"api_type": "stub"},
        }, f)
    env["FAKE_API_KEY"] = "test-key"


STUB = '''"""A stub adaptor: no wire, no network; records prepare and replaces call."""
PATH = "/stub"


def auth(key):
    return {"Authorization": "Bearer " + key}


def prepare(model, key, turns, params):
    turns[-1]["blocks"].append({"type": "text", "text": "prepared"})
    return turns


def build(model, turns, system, params):
    return {"model": model["model_name"], "turns": turns}


def call(model, key, body, line, stamps):
    yield {"text": body["turns"][-1]["blocks"][-1]["text"]}


def parse(event, acc):
    return event["text"], ""


def finish(acc):
    return None
'''


@pytest.fixture
def stub(env):
    """Write ~/.config/geni/adapters/stub.py: the escape hatch, from the inside.

    A file adaptor is the one place a test can see whether prepare ran before
    build and whether call replaced events(), without inventing a wire.
    """
    directory = os.path.join(config_dir(env), "adapters")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "stub.py"), "w") as f:
        f.write(STUB)


def tree(env):
    """The message tree, oldest first."""
    conn = db(env)
    rows = conn.execute("SELECT * FROM messages ORDER BY t_start").fetchall()
    conn.close()
    return rows


def sent(server, kind, prefix=""):
    """The requests of one kind whose path starts with prefix, in order."""
    return [h for h in server.received
            if h.kind == kind and h.path.startswith(prefix)]


def test_an_image_lands_at_a_numbered_path_and_stdout_stays_empty(
        streams, env, server, models, tmp_path):
    server.mode = "images"
    out = tmp_path / "cat.png"

    reply = dic("a cat", model="fake+images", path=str(out),
                env=env, out=streams.out, err=streams.err)

    assert streams.out.getvalue() == ""          # bytes are not a terminal's
    assert (tmp_path / "cat-0.png").read_bytes() == IMAGE
    assert reply.paths == [str(tmp_path / "cat-0.png")]

    request = sent(server, "POST")[0]
    assert (request.path, request.body["prompt"]) == ("/images/generations", "a cat")
    row = tree(env)[0]
    assert row["status"] == 200                  # a transport with no wire status
    assert json.loads(row["outputs"]) == [
        {"path": str(tmp_path / "cat-0.png"), "mime_type": "image/png"}]


def test_a_non_text_answer_refuses_to_run_without_a_path(streams, env, server, models):
    server.mode = "images"

    with pytest.raises(DicError, match="needs --path"):
        dic("a cat", model="fake+images", env=env,
            out=streams.out, err=streams.err)

    assert server.received == []                 # before the network, not after it


def test_a_video_job_polls_then_downloads_the_file(streams, env, server, models,
                                                   tmp_path):
    server.mode = "videos"
    source, out = tmp_path / "still.png", tmp_path / "clip.mp4"
    source.write_bytes(IMAGE)

    dic("a wave", model="fake+videos", attachment=[str(source)], path=str(out),
        env=env, out=streams.out, err=streams.err)

    # a reference image travels as multipart, under the field the service names
    create = sent(server, "POST", "/videos")[0].body
    assert b'name="input_reference"' in create and IMAGE in create
    assert server.polls["videos"] == 2           # one waiting, one done
    assert (tmp_path / "clip-0.mp4").read_bytes() == VIDEO
    row = tree(env)[0]
    assert (row["status"], json.loads(row["usage"])) == (200, {"out": 5.0})


def test_a_fal_job_uploads_its_attachment_then_downloads_the_asset(
        streams, env, server, models, tmp_path, monkeypatch):
    monkeypatch.setattr(fal, "UPLOAD", server.api_base)   # the one host dic hardcodes
    server.mode = "fal"
    source, out = tmp_path / "in.png", tmp_path / "gen.png"
    source.write_bytes(IMAGE)

    dic("paint it blue", model="fake+fal", attachment=[str(source)],
        path=str(out), env=env, out=streams.out, err=streams.err)

    assert sent(server, "PUT", "/upload")[0].body == IMAGE   # prepare ran first
    create = sent(server, "POST", "/fal-ai")[0].body
    assert create["image_urls"] == [server.api_base + "/asset"]
    assert server.polls["fal"] == 2
    assert (tmp_path / "gen-0.png").read_bytes() == IMAGE
    assert tree(env)[0]["status"] == 200


def test_a_failure_from_a_call_adaptor_is_recorded(streams, env, server, models,
                                                   tmp_path):
    server.mode, server.status = "fal", 500

    with pytest.raises(DicError, match="500"):
        dic("a cat", model="fake+fal", path=str(tmp_path / "x.png"),
            env=env, out=streams.out, err=streams.err)

    row = tree(env)[0]
    assert row["status"] == -1 and "boom" in row["error"]
    assert row["response"] == ""
    assert not os.path.exists(session_path(env))     # never continued by -c


def test_prepare_runs_before_build_and_call_replaces_the_transport(
        streams, env, models, stub):
    reply = dic("hi", model="fake+stub", env=env,
                out=streams.out, err=streams.err)

    # the stub's build only sees "prepared" if prepare rewrote the turns first,
    # and its call only runs at all if it replaced events()
    assert streams.out.getvalue() == "prepared\n"
    assert (reply.status, reply.mid is not None) == (200, True)
