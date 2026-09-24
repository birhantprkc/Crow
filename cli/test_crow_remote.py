"""Tests for crow_remote (#249): the LAN server's guards, pairing, streams and QR.

Loopback only: every server here binds 127.0.0.1 on a port the test picks, so
nothing is reachable from the LAN while the suite runs. The clock is injected
wherever the ticket's numbers (120 s token, 10 min lock, 250 ms coalescing)
are asserted, so no test sleeps for them.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crow_platform  # noqa: E402
import crow_remote  # noqa: E402

try:
    import segno                                   # dev-only: pip install segno
    import segno.encoder as _segno_encoder
except ImportError:                                # pragma: no cover - the usual case
    segno = None

UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Harness:
    """One Remote on loopback with in-memory store and recording callables."""

    def __init__(self, test, saved=None, port=None, clock=None):
        self.test = test
        self.saved = saved if saved is not None else {"records": []}
        self.clock = clock or Clock()
        self.logs = []
        self.confirmed = []
        self.allow = True
        self.gate = None                   # an Event: confirm waits for it, like a person
        self.upload_dir = tempfile.mkdtemp(prefix="crow-remote-test-")
        test.addCleanup(shutil.rmtree, self.upload_dir, True)
        self.store = crow_remote.DeviceStore(lambda: list(self.saved["records"]),
                                             self.save)
        self.remote = crow_remote.Remote(
            host="127.0.0.1", port=port or free_port(),
            page=lambda: "<!doctype html><title>Crow</title>",
            call=self.call, allowed=frozenset({"echo", "boom"}),
            snapshot=lambda dev: [{"k": "hello", "dev": dev}],
            confirm=self.confirm, store=self.store, upload_dir=self.upload_dir,
            clock=self.clock, log=self.logs.append)
        self.remote.start()
        test.addCleanup(self.remote.stop)
        self.origin = "http://127.0.0.1:%d" % self.remote.port

    def save(self, records):
        self.saved["records"] = json.loads(json.dumps(records))

    def call(self, name, args):
        if name == "boom":
            raise RuntimeError("it broke")
        return [crow_remote.CLIENT.get(), args]

    def confirm(self, name):
        self.confirmed.append(name)
        if self.gate is not None:
            self.gate.wait(10)
        return self.allow

    def conn(self, timeout=5):
        return http.client.HTTPConnection("127.0.0.1", self.remote.port, timeout=timeout)

    def request(self, method, path, body=None, headers=None, cookie=None, origin=True):
        h = {"User-Agent": UA_IPHONE}
        if origin and method == "POST":
            h["Origin"] = self.origin
        if cookie:
            h["Cookie"] = "%s=%s" % (crow_remote.COOKIE, cookie)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = self.conn()
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def token(self):
        return self.remote.new_pairing().split("#t=", 1)[1]

    def begin(self, token=None):
        """POST /pair: 202 and the pending id, answered at once."""
        r, data = self.request("POST", "/pair", {"t": token or self.token()})
        self.test.assertEqual(r.status, 202, data)
        self.test.assertIsNone(r.getheader("Set-Cookie"))
        return json.loads(data)["p"]

    def wait(self, pid, polls=20):
        """POST /pair/wait until it stops saying 202; the last (response, body)."""
        for _ in range(polls):
            r, data = self.request("POST", "/pair/wait", {"p": pid})
            if r.status != 202:
                return r, data
        return r, data

    def pair(self):
        r, data = self.wait(self.begin())
        self.test.assertEqual(r.status, 200, data)
        cookie = r.getheader("Set-Cookie")
        self.test.assertIn("HttpOnly", cookie)
        self.test.assertIn("SameSite=Strict", cookie)
        self.test.assertIn("Max-Age=34560000", cookie)
        value = cookie.split(";", 1)[0].split("=", 1)[1]
        return json.loads(data)["id"], value

    def open_stream(self, cookie, last=None, query=None):
        c = self.conn()
        h = {"Cookie": "%s=%s" % (crow_remote.COOKIE, cookie)}
        if last is not None:
            h["Last-Event-ID"] = str(last)
        c.request("GET", "/events" + (query or ""), headers=h)
        r = c.getresponse()
        return c, r


def read_events(resp, n):
    """The next n SSE events as (id, data dict); comments and retry skipped."""
    out, eid, data = [], None, None
    while len(out) < n:
        line = resp.fp.readline()
        if not line:
            raise EOFError("stream closed after %d events" % len(out))
        line = line.decode().rstrip("\n")
        if line.startswith("id: "):
            eid = int(line[4:])
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "" and data is not None:
            out.append((eid, data))
            eid, data = None, None
    return out


class RemoteServerTests(unittest.TestCase):

    def setUp(self):
        self.h = Harness(self)

    def test_page_and_headers(self):
        r, data = self.h.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<title>Crow", data)
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.h.request("GET", "/favicon.ico")[0].status, 204)

    def test_wrong_host_is_421(self):
        _, cookie = self.h.pair()
        for method, path in (("GET", "/"), ("GET", "/events"), ("POST", "/api/echo"),
                             ("POST", "/pair"), ("POST", "/upload")):
            r, _ = self.h.request(method, path, body=b"[]", cookie=cookie,
                                  headers={"Host": "evil.example:%d" % self.h.remote.port})
            self.assertEqual(r.status, 421, path)

    def test_foreign_or_missing_origin_is_403(self):
        _, cookie = self.h.pair()
        for origin in ({"Origin": "http://evil.example"}, {}):
            for path in ("/api/echo", "/pair", "/upload"):
                r, _ = self.h.request("POST", path, body=b"[]", cookie=cookie,
                                      headers=origin, origin=False)
                self.assertEqual(r.status, 403, (path, origin))

    def test_no_cookie_is_401(self):
        self.assertEqual(self.h.request("POST", "/api/echo", [])[0].status, 401)
        self.assertEqual(self.h.request("GET", "/events")[0].status, 401)
        self.assertEqual(self.h.request("POST", "/upload", b"x",
                                        {"Content-Type": "image/png"})[0].status, 401)
        r, _ = self.h.request("POST", "/api/echo", [], cookie="made-up")
        self.assertEqual(r.status, 401)

    def test_pairing_asks_the_desktop_and_calls_run_as_the_device(self):
        dev, cookie = self.h.pair()
        self.assertEqual(self.h.confirmed, ["iPhone (Safari)"])
        r, data = self.h.request("POST", "/api/echo", [1, "a"], cookie=cookie)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(data), [dev, [1, "a"]])
        self.assertEqual(crow_remote.CLIENT.get(), crow_remote.DESKTOP)
        r, data = self.h.request("POST", "/api/boom", [], cookie=cookie)
        self.assertEqual(r.status, 500)
        self.assertEqual(json.loads(data), {"error": "it broke"})
        self.assertTrue(any("paired iPhone (Safari)" in s for s in self.h.logs))

    def test_denied_on_the_desktop_pairs_nothing(self):
        self.h.allow = False
        r, _ = self.h.wait(self.h.begin())
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertEqual(self.h.remote.devices(), [])

    def test_token_used_twice_is_401(self):
        token = self.h.token()
        self.assertEqual(self.h.request("POST", "/pair", {"t": token})[0].status, 202)
        self.assertEqual(self.h.request("POST", "/pair", {"t": token})[0].status, 401)

    def test_expired_token_is_401(self):
        token = self.h.token()
        self.h.clock.now += 121
        self.assertEqual(self.h.request("POST", "/pair", {"t": token})[0].status, 401)

    def test_five_failed_pairings_lock_for_ten_minutes(self):
        for _ in range(5):
            self.assertEqual(self.h.request("POST", "/pair", {"t": "wrong"})[0].status, 401)
        r, _ = self.h.request("POST", "/pair", {"t": self.h.token()})
        self.assertEqual(r.status, 429)
        self.assertTrue(any("locked" in s for s in self.h.logs))
        self.h.clock.now += 599
        self.assertEqual(self.h.request("POST", "/pair", {"t": self.h.token()})[0].status, 429)
        self.h.clock.now += 2
        self.assertEqual(self.h.request("POST", "/pair", {"t": self.h.token()})[0].status, 202)

    def test_pair_answers_at_once_and_the_poll_collects_the_allow(self):
        """#249, iPhone 2026-09-24: /pair is never held while a person decides.
        202 with a pending id at once, /pair/wait 202 while the desktop is
        asked, then 200 and the cookie after Allow."""
        self.h.gate = threading.Event()
        started = time.monotonic()
        pid = self.h.begin()
        self.assertLess(time.monotonic() - started, 0.8, "/pair was held open")
        started = time.monotonic()
        r, data = self.h.request("POST", "/pair/wait", {"p": pid})
        took = time.monotonic() - started
        self.assertEqual(r.status, 202, data)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertLess(took, crow_remote.PAIR_POLL + 0.8, "the poll held too long")
        self.assertEqual(self.h.remote.devices(), [])
        self.h.gate.set()
        r, data = self.h.wait(pid)
        self.assertEqual(r.status, 200, data)
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        self.assertEqual(json.loads(data)["name"], "iPhone (Safari)")
        self.assertEqual([d["name"] for d in self.h.remote.devices()], ["iPhone (Safari)"])

    def test_a_pending_id_is_single_use(self):
        pid = self.h.begin()
        self.assertEqual(self.h.wait(pid)[0].status, 200)
        r, _ = self.h.request("POST", "/pair/wait", {"p": pid})
        self.assertEqual(r.status, 410)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertEqual(len(self.h.remote.devices()), 1)

    def test_a_denied_id_is_spent_too(self):
        self.h.allow = False
        pid = self.h.begin()
        self.assertEqual(self.h.wait(pid)[0].status, 403)
        self.assertEqual(self.h.request("POST", "/pair/wait", {"p": pid})[0].status, 410)

    def test_no_answer_from_the_desktop_is_410(self):
        """confirm returns None when nobody answered in time."""
        self.h.allow = None
        r, _ = self.h.wait(self.h.begin())
        self.assertEqual(r.status, 410)
        self.assertEqual(self.h.remote.devices(), [])

    def test_a_pending_pairing_expires_with_the_confirm_timeout(self):
        self.h.gate = threading.Event()
        self.addCleanup(self.h.gate.set)
        pid = self.h.begin()
        self.assertEqual(self.h.request("POST", "/pair/wait", {"p": pid})[0].status, 202)
        self.h.clock.now += 60 + crow_remote.PAIR_CLAIM + 1
        self.assertEqual(self.h.request("POST", "/pair/wait", {"p": pid})[0].status, 410)
        self.h.gate.set()                  # a late Allow finds nothing to let in
        time.sleep(0.1)
        self.assertEqual(self.h.request("POST", "/pair/wait", {"p": pid})[0].status, 410)
        self.assertEqual(self.h.remote.devices(), [])

    def test_unknown_pending_ids_are_410_and_never_lock_pairing(self):
        """NEGATIVE: the lockout counts bad pairing codes, not polls."""
        for bad in ("nope", "", None, 7):
            r, _ = self.h.request("POST", "/pair/wait", {"p": bad})
            self.assertEqual(r.status, 410)
        for _ in range(crow_remote.PAIR_MAX_FAILS):
            self.h.request("POST", "/pair/wait", {"p": "guess"})
        self.assertEqual(self.h.request("POST", "/pair", {"t": self.h.token()})[0].status, 202)
        self.assertFalse(any("locked" in s for s in self.h.logs))

    def test_pair_wait_keeps_the_host_and_origin_guards(self):
        pid = self.h.begin()
        r, _ = self.h.request("POST", "/pair/wait", {"p": pid}, origin=False)
        self.assertEqual(r.status, 403)
        r, _ = self.h.request("POST", "/pair/wait", {"p": pid},
                              headers={"Host": "evil.example:80"})
        self.assertEqual(r.status, 421)

    def test_name_outside_allowed_is_404(self):
        _, cookie = self.h.pair()
        self.assertEqual(self.h.request("POST", "/api/close_window", [],
                                        cookie=cookie)[0].status, 404)
        self.assertEqual(self.h.request("POST", "/api/echo", {"not": "a list"},
                                        cookie=cookie)[0].status, 400)

    def test_upload_writes_an_image_into_the_upload_dir(self):
        _, cookie = self.h.pair()
        r, data = self.h.request("POST", "/upload", b"\x89PNG....",
                                 {"Content-Type": "image/png"}, cookie=cookie)
        self.assertEqual(r.status, 200)
        path = json.loads(data)["path"]
        self.assertEqual(os.path.dirname(path), self.h.upload_dir)
        self.assertTrue(path.endswith(".png"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG....")
        r, _ = self.h.request("POST", "/upload", b"hi", {"Content-Type": "text/plain"},
                              cookie=cookie)
        self.assertEqual(r.status, 415)

    def test_snapshot_first_then_live(self):
        dev, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        self.addCleanup(c.close)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertIn("Max-Age=34560000", r.getheader("Set-Cookie"))
        (sid, snap), = read_events(r, 1)
        self.assertEqual(snap, {"k": "snapshot", "items": [{"k": "hello", "dev": dev}]})
        self.h.remote.publish({"k": "tool", "n": 1})
        (eid, msg), = read_events(r, 1)
        self.assertEqual((eid, msg), (sid + 1, {"k": "tool", "n": 1}))
        self.assertTrue(self.h.remote.devices()[0]["online"])

    def test_last_event_id_inside_replays_exactly_outside_snapshots(self):
        _, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        (sid, _), = read_events(r, 1)
        c.close()
        for i in range(5):
            self.h.remote.publish({"k": "tool", "n": i})
        c, r = self.h.open_stream(cookie, last=sid)
        got = read_events(r, 5)
        c.close()
        self.assertEqual([e for e, _ in got], [sid + 1, sid + 2, sid + 3, sid + 4, sid + 5])
        self.assertEqual([m["n"] for _, m in got], [0, 1, 2, 3, 4])
        # the page's own reconnect carries the position as ?last=
        c, r = self.h.open_stream(cookie, query="?last=%d" % (sid + 3))
        self.assertEqual([e for e, _ in read_events(r, 2)], [sid + 4, sid + 5])
        c.close()
        # a position this buffer never had (an older run, or a made-up one)
        for last in (sid + 99, 12345):
            c, r = self.h.open_stream(cookie, last=last)
            (eid, msg), = read_events(r, 1)
            c.close()
            self.assertEqual(msg["k"], "snapshot")
            self.assertEqual(eid, sid + 5)

    def test_offline_device_is_buffered(self):
        dev, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        (sid, _), = read_events(r, 1)
        c.close()
        self.h.remote.publish({"k": "a"}, to=[dev])
        self.h.remote.publish({"k": "b"}, to=["someone-else"])
        self.h.remote.publish({"k": "c"})
        c, r = self.h.open_stream(cookie, last=sid)
        self.assertEqual([m["k"] for _, m in read_events(r, 2)], ["a", "c"])
        c.close()

    def test_text_deltas_are_coalesced(self):
        _, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        self.addCleanup(c.close)
        read_events(r, 1)
        pub = self.h.remote.publish
        for ch in "hello":                              # frozen clock: one window
            pub({"k": "text", "t": ch})
        pub({"k": "think", "t": "x"})                   # other kind: flushes the text
        pub({"k": "think", "t": "y"})
        pub({"k": "text", "t": "!", "bg": True})        # other keys: a boundary too
        pub({"k": "tool"})
        self.h.clock.now += 0.1
        pub({"k": "text", "t": "a"})
        self.h.clock.now += 0.1
        pub({"k": "text", "t": "b"})
        self.h.clock.now += 0.2                         # 0.3 s after "a": window over
        pub({"k": "text", "t": "c"})
        pub({"k": "idle"})
        got = [m for _, m in read_events(r, 7)]
        self.assertEqual(got, [{"k": "text", "t": "hello"}, {"k": "think", "t": "xy"},
                               {"k": "text", "t": "!", "bg": True}, {"k": "tool"},
                               {"k": "text", "t": "ab"}, {"k": "text", "t": "c"},
                               {"k": "idle"}])

    def test_pending_delta_is_flushed_by_time(self):
        _, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        self.addCleanup(c.close)
        read_events(r, 1)
        self.h.remote.publish({"k": "text", "t": "late"})
        self.h.clock.now += 0.3                          # the stream's own wake-up seals it
        (_, msg), = read_events(r, 1)
        self.assertEqual(msg, {"k": "text", "t": "late"})

    def test_stop_refuses_connections(self):
        self.h.pair()
        port = self.h.remote.port
        self.h.remote.stop()
        self.assertFalse(self.h.remote.running())
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        self.assertEqual(len(self.h.remote.devices()), 1)     # still paired

    def test_stop_ends_open_streams(self):
        _, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        self.addCleanup(c.close)
        read_events(r, 1)
        self.h.remote.stop()
        with self.assertRaises((EOFError, ConnectionError)):
            read_events(r, 1)

    def test_revoked_device_stream_ends_and_cookie_is_401(self):
        dev, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        self.addCleanup(c.close)
        read_events(r, 1)
        self.assertTrue(self.h.remote.forget("iphone"))
        with self.assertRaises((EOFError, ConnectionError)):
            read_events(r, 1)
        self.assertEqual(self.h.request("POST", "/api/echo", [], cookie=cookie)[0].status, 401)
        self.assertEqual(self.h.request("GET", "/events", cookie=cookie)[0].status, 401)
        self.assertEqual(self.h.remote.device_ids(), [])
        self.assertFalse(self.h.remote.forget(dev))
        self.assertTrue(any("revoked" in s for s in self.h.logs))

    def test_forget_matches_id_name_and_unique_prefix_only(self):
        a, _ = self.h.pair()
        b, _ = self.h.pair()
        names = sorted(d["name"] for d in self.h.remote.devices())
        self.assertEqual(names, ["iPhone (Safari)", "iPhone (Safari) 2"])
        self.assertFalse(self.h.remote.forget("iphone (saf"))     # ambiguous
        self.assertTrue(self.h.remote.forget("iPhone (Safari) 2"))
        self.assertTrue(self.h.remote.forget(a))
        self.assertEqual(self.h.remote.devices(), [])

    def test_paired_device_survives_a_restart(self):
        dev, cookie = self.h.pair()
        self.h.remote.stop()
        stored = json.dumps(self.h.saved)
        self.assertNotIn(cookie, stored)
        self.assertIn(crow_remote._sha256(cookie), stored)
        self.assertEqual(set(self.h.saved["records"][0]),
                         {"id", "name", "created", "last_seen", "token_sha256"})
        again = Harness(self, saved=self.h.saved)
        self.assertEqual(again.remote.device_ids(), [dev])
        r, data = again.request("POST", "/api/echo", [], cookie=cookie)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(data)[0], dev)
        self.assertNotIn("token_sha256", again.remote.devices()[0])

    def test_refuses_every_interface(self):
        remote = crow_remote.Remote(host="0.0.0.0", port=free_port(), page=str,
                                    call=lambda n, a: None, allowed=frozenset(),
                                    snapshot=lambda d: [], confirm=lambda n: True,
                                    store=crow_remote.DeviceStore(list, lambda r: None),
                                    upload_dir=self.h.upload_dir)
        with self.assertRaises(ValueError):
            remote.start()
        self.assertFalse(remote.running())

    def test_publish_from_many_threads(self):
        dev, cookie = self.h.pair()
        c, r = self.h.open_stream(cookie)
        self.addCleanup(c.close)
        (sid, _), = read_events(r, 1)

        def burst(k):
            for i in range(50):
                self.h.remote.publish({"k": k, "n": i})

        threads = [threading.Thread(target=burst, args=("t%d" % i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = read_events(r, 200)
        self.assertEqual([e for e, _ in got], list(range(sid + 1, sid + 201)))
        for i in range(4):
            self.assertEqual([m["n"] for _, m in got if m["k"] == "t%d" % i], list(range(50)))


class DeviceNameTests(unittest.TestCase):

    def test_names(self):
        self.assertEqual(crow_remote.device_name(UA_IPHONE), "iPhone (Safari)")
        self.assertEqual(crow_remote.device_name(
            UA_IPHONE.replace("Version/18.0", "CriOS/129.0")), "iPhone (Chrome)")
        self.assertEqual(crow_remote.device_name(
            "Mozilla/5.0 (Linux; Android 14) Chrome/129.0 Mobile Safari/537.36"),
            "Android (Chrome)")
        self.assertEqual(crow_remote.device_name(""), "Device")


@unittest.skipIf(crow_platform.IS_WINDOWS, "Linux listing; Windows is stage 3")
class LanAddressTests(unittest.TestCase):

    IP_JSON = json.dumps([
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1"}]},
        {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.64.1.2"}]},
        {"ifname": "br0", "addr_info": [{"family": "inet", "local": "10.9.0.1"}]},
        {"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.2.102"},
                                          {"family": "inet", "local": "169.254.3.3"}]},
        {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "203.0.113.5"}]},
    ])

    def test_order_and_filters(self):
        sys_net = tempfile.mkdtemp(prefix="crow-sysnet-")
        self.addCleanup(shutil.rmtree, sys_net, True)
        for name in ("wlan0", "eth0"):
            os.makedirs(os.path.join(sys_net, name, "device"))
        os.makedirs(os.path.join(sys_net, "br0"))
        got = crow_platform.lan_addresses(query=lambda: self.IP_JSON, sys_net=sys_net)
        self.assertEqual(got, [("wlan0", "192.168.2.102"), ("br0", "10.9.0.1"),
                               ("eth0", "203.0.113.5")])

    def test_nothing_is_an_empty_list(self):
        self.assertEqual(crow_platform.lan_addresses(query=lambda: ""), [])
        self.assertEqual(crow_platform.lan_addresses(query=lambda: "not json"), [])


# Twenty pairing-shaped URLs over the lengths the QR has to carry (versions 3-5)
# plus the edges of the range: one byte, and the 106 bytes version 6-M holds.
QR_URLS = ["http://192.168.%d.%d:%d/#t=%s" % (i * 13 % 256, i * 7 % 254 + 1, 1024 + i * 3079,
                                               "AbC-_x9" * (3 + i % 3) + "z" * (i % 5))
           for i in range(17)] + ["a", "http://10.0.0.1:8765/", "x" * 106]


class QrTests(unittest.TestCase):

    def test_shape(self):
        self.assertEqual(len(crow_remote.qr_matrix("a")), 21)
        m = crow_remote.qr_matrix("http://192.168.178.120:48123/#t=" + "A" * 22)
        self.assertEqual(len(m), 33)                                  # version 4
        self.assertTrue(all(len(row) == 33 for row in m))
        self.assertEqual(len(crow_remote.qr_matrix("x" * 106)), 41)  # version 6
        with self.assertRaises(ValueError):
            crow_remote.qr_matrix("x" * 107)

    def test_reed_solomon_against_the_textbook_example(self):
        # "HELLO WORLD" 1-M, the worked example every QR tutorial uses.
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(crow_remote._rs_remainder(data, crow_remote._rs_divisor(10)),
                         [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_svg(self):
        svg = crow_remote.qr_svg("hello")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('viewBox="0 0 29 29"', svg)                   # 21 + 2 * 4
        self.assertIn('fill="#fff"', svg)
        self.assertIn('fill="#000"', svg)

    @unittest.skipUnless(segno is not None, "segno not installed (dev-only check)")
    def test_matrices_equal_segno(self):
        # SEGNO 1.6.6 PADS ONE BYTE TOO MANY. Its write_padding_bits adds
        # `8 - length % 8` zero bits, which is 8 when the stream already ends on
        # a codeword boundary -- and in byte mode after a full terminator it
        # always does (4 + 8 + 8n + 4 bits). The spec's number is 0, so segno's
        # symbols carry 0x00 where the first 0xEC pad belongs: valid, readable,
        # and one codeword different. Patched to the spec for this comparison.
        original = _segno_encoder.write_padding_bits
        _segno_encoder.write_padding_bits = (
            lambda buff, version, length: buff.extend([0] * ((-length) % 8)))
        try:
            for url in QR_URLS:
                ref = segno.make_qr(url, error="m", mode="byte", boost_error=False)
                want = [[bool(v) for v in row] for row in ref.matrix_iter(border=0)]
                self.assertEqual(crow_remote.qr_matrix(url), want, url)
        finally:
            _segno_encoder.write_padding_bits = original


if __name__ == "__main__":
    unittest.main()
