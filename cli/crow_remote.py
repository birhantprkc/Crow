"""The phone's way into a running window: /remote, a LAN-only mirror (#249).

WHAT THIS IS. A small HTTP server on ONE LAN address that serves the window's
own page to a paired phone, forwards the page's `pywebview.api.<name>(...)`
calls to the same `Api` the desktop uses, and streams the same pushes back as
Server-Sent Events. The phone is a second view of one session, never a second
session: there is no policy in here, only transport, pairing and routing.

A HELPER AND NOT A SURFACE, like crow_voice and crow_platform. It never
imports crow_core and it decides nothing a user reads: the wording of /remote
lives in the core, the Api that answers every call lives in the window, and
what reaches this module are callables. Its own status lines (listening,
paired, revoked, lockout) go out through `log`, which the caller points at
crow.log -- never into the chat.

STANDARD LIBRARY ONLY. `ThreadingHTTPServer` because Crow has no asyncio loop
and an HTTP handler thread calling an `Api` method is exactly what pywebview
already does for every js_api call. SSE and not WebSocket because the standard
library has no WebSocket server and iOS kills either one in the background
just the same. The QR encoder is in here too (decision 9 A): a runtime
dependency for 200 lines of Reed-Solomon would change both installers.

SECURITY, stated once (decision 8): plain HTTP on the home LAN. What stands
between a neighbour and a shell at `yolo` is a single-use 120 s pairing token
carried only in the QR, a desktop Allow/Deny for every new device, an HttpOnly
SameSite=Strict cookie whose sha256 is all that is stored, and a Host/Origin
check on every route that does something. The bind is one LAN address, never
0.0.0.0, so the model ports' 127.0.0.1-only rule is not weakened by this file.

ON THE ROAD (#249 stage 5): Tailscale, and Crow never runs it. `tailscale
serve --https=443 http://127.0.0.1:<port>` is a persistent config that
terminates TLS for `<machine>.<tailnet>.ts.net` with a certificate tailscaled
renews itself, and forwards to the loopback. So when the tailnet is up this
server ALSO binds 127.0.0.1:<port> -- the proxy target, nothing else -- where
only the ts.net name passes the Host check and only its https origin passes the
Origin check. Plain HTTP never listens on 0.0.0.0 or on the 100.x address.
Setting the proxy up needs root or a Tailscale operator, so the dialog shows the
one command and `tailscale_state` only reads (`status --json`, `serve status
--json`), without sudo.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlsplit

DESKTOP = "desktop"

# WHO IS CALLING, for the duration of one Api call. The /api dispatcher sets it
# to the device id; pywebview's own calls never touch it and so read DESKTOP.
# A ContextVar and not a thread-local because it is what the Api side reads
# per call, and a value set by a handler thread must not leak into the next
# request that thread serves -- reset() in a finally guarantees that.
CLIENT: contextvars.ContextVar[str] = contextvars.ContextVar("crow_remote_client",
                                                            default=DESKTOP)

COOKIE = "crow_remote"
# 400 days, the RFC 6265bis cap: a browser clamps anything longer to this, so
# asking for more would only make the number here a lie.
COOKIE_MAX_AGE = 400 * 24 * 3600

PAIR_TTL = 120.0             # the QR token's life, decision 7
PAIR_MAX_FAILS = 5           # failed pairings before the lock
PAIR_LOCK = 600.0            # ... and how long the lock holds
# NO PAIRING REQUEST IS EVER HELD OPEN WHILE A PERSON DECIDES. /pair answers
# 202 with a pending id at once and /pair/wait is a short poll: it holds at
# most PAIR_POLL seconds. Measured 2026-09-24 on an iPhone (Chrome for iOS,
# WebKit): a /pair held open for the desktop's Allow was given up by the phone
# after about 6 s as a network error, while the desktop still showed the bar.
PAIR_POLL = 1.0
# A pending pairing lives as long as the desktop's question plus this much
# room for the phone's next poll to collect the answer.
PAIR_CLAIM = 15.0

RING = 5000                  # events kept per device for a resume
HEARTBEAT = 15.0             # an SSE comment this often keeps NATs and proxies awake
RETRY_MS = 2000              # EventSource reconnect delay the page is told
# ONE merged text/think event per this many seconds and phone stream, at most.
# 4/s is the ticket's number: a phone repainting on every token of a 60 tok/s
# stream burns its battery and gains nothing a person can read.
COALESCE = 0.25

MAX_BODY = 8 * 1024 * 1024          # /pair and /api: a prompt, not a file
MAX_UPLOAD = 20 * 1024 * 1024       # /upload: one phone photo, with room
_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
              "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif"}

_MERGEABLE = ("text", "think")

# THE HOME-SCREEN TILE: iOS's apple-touch-icon (180 pt @3x) and the manifest's
# large one. Both drawn by the owner's `icon(size)`, once, then kept.
ICON_ROUTES = {"/apple-touch-icon.png": 180, "/icon-512.png": 512}

_SECURITY_HEADERS = (("Referrer-Policy", "no-referrer"),
                     ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"))


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def device_name(user_agent: str) -> str:
    """"iPhone (Safari)" and the like, for the desktop's Allow/Deny question.

    Only as precise as the question needs: a person recognises their own phone
    by kind and browser. iOS browsers are all WebKit and say "Safari" in their
    UA, so the iOS brand tokens (CriOS, FxiOS, EdgiOS) are looked at first.
    """
    ua = user_agent or ""
    if "iPhone" in ua:
        kind = "iPhone"
    elif "iPad" in ua:
        kind = "iPad"
    elif "Android" in ua:
        kind = "Android"
    elif "Macintosh" in ua:
        kind = "Mac"
    elif "Windows" in ua:
        kind = "Windows"
    elif "Linux" in ua:
        kind = "Linux"
    else:
        kind = "Device"
    for token, browser in (("CriOS", "Chrome"), ("FxiOS", "Firefox"),
                           ("EdgiOS", "Edge"), ("Edg/", "Edge"),
                           ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                           ("Safari/", "Safari")):
        if token in ua:
            return "%s (%s)" % (kind, browser)
    return kind


class DeviceStore:
    """Paired devices, persisted via two callables so this module never imports crow_core.

    The caller wires `load`/`save` to secrets.json. A record is
    {"id","name","created","last_seen","token_sha256"}: the cookie itself is
    never stored, so a copy of secrets.json does not let anyone in.
    """

    def __init__(self, load: Callable[[], list[dict]], save: Callable[[list[dict]], None]):
        self._load = load
        self._save = save
        self._lock = threading.Lock()
        try:
            loaded = load() or []
        except Exception:                  # noqa: BLE001 - an unreadable store pairs afresh
            loaded = []
        self._records = [dict(r) for r in loaded
                         if isinstance(r, dict) and r.get("id") and r.get("token_sha256")]

    def records(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    def by_hash(self, digest: str) -> "dict | None":
        with self._lock:
            for r in self._records:
                if secrets.compare_digest(r["token_sha256"], digest):
                    return dict(r)
        return None

    def add(self, record: dict) -> None:
        with self._lock:
            self._records.append(dict(record))
            self._persist()

    def remove(self, device_id: str) -> bool:
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r["id"] != device_id]
            if len(self._records) == before:
                return False
            self._persist()
            return True

    def touch(self, device_id: str, when: float) -> None:
        with self._lock:
            for r in self._records:
                if r["id"] == device_id:
                    r["last_seen"] = when
            self._persist()

    def _persist(self) -> None:
        self._save([dict(r) for r in self._records])


class _Channel:
    """One device's outgoing events: a ring buffer, a seq, and a pending delta.

    SEQ STARTS AT WALL-CLOCK MICROSECONDS, not at 0. The page hands back the
    last id it saw; after a Crow restart a counter starting at 0 would soon
    reach that old number again, and an id from the previous run would look
    like a position in this run's buffer -- a silent wrong replay. A base taken
    from the clock is above every id the previous run could have handed out,
    so an old id falls outside and the phone gets a snapshot instead.
    """

    def __init__(self):
        self.seq = time.time_ns() // 1000
        self.ring: deque = deque(maxlen=RING)       # (seq, json text)
        self.pending: "dict | None" = None           # a text/think delta being merged
        self.pending_at = 0.0
        self.streams: list = []                      # open stream sockets
        self.closed = False

    def _append(self, message: dict) -> None:
        self.seq += 1
        self.ring.append((self.seq, json.dumps(message, ensure_ascii=False,
                                               separators=(",", ":"))))

    def seal(self) -> None:
        if self.pending is not None:
            message, self.pending = self.pending, None
            self._append(message)

    def seal_due(self, now: float) -> None:
        if self.pending is not None and now - self.pending_at >= COALESCE:
            self.seal()

    def put(self, message: dict, now: float) -> None:
        """Merge a text/think delta into the pending one, or seal and append.

        MERGED ONLY WHILE NOTHING ELSE INTERVENES and every key but the text
        is equal: a `bg` stamp or a different kind between two deltas is a
        boundary the page draws, and merging across it would reorder the view.
        """
        if message.get("k") in _MERGEABLE and isinstance(message.get("t"), str):
            p = self.pending
            if (p is not None and now - self.pending_at < COALESCE
                    and {k: v for k, v in p.items() if k != "t"}
                    == {k: v for k, v in message.items() if k != "t"}):
                p["t"] += message["t"]
                return
            self.seal()
            self.pending = dict(message)
            self.pending_at = now
            return
        self.seal()
        self._append(message)

    def inside(self, last: int) -> bool:
        """Whether a resume from `last` can be answered from the buffer exactly."""
        oldest = self.ring[0][0] if self.ring else self.seq + 1
        return oldest - 1 <= last <= self.seq

    def after(self, last: int) -> list:
        return [(s, text) for s, text in self.ring if s > last]


class Remote:
    """The server: pairing, devices, the /api dispatcher and the event streams.

    Every piece of shared state sits behind one lock and one condition. publish
    only appends under that lock and notifies -- it never touches a socket -- so
    the GUI's worker thread cannot be stalled by a phone on bad Wi-Fi.
    """

    def __init__(self, *, host: str, port: int,
                 page: Callable[[], str],
                 call: Callable[[str, list], object],
                 allowed: frozenset,
                 snapshot: Callable[[str], list],
                 confirm: Callable[[str], bool],
                 store: DeviceStore, upload_dir: str,
                 confirm_ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = lambda s: None,
                 icon: "Callable[[int], bytes | None] | None" = None,
                 tailnet: str = ""):
        self.host = host
        self.port = port
        # #249 stage 5: the ts.net name `tailscale serve` answers for, or "".
        # Set, it adds the loopback listener its proxy forwards to.
        self.tailnet = (tailnet or "").strip().rstrip(".").lower()
        self._page = page
        self._call = call
        self._allowed = frozenset(allowed)
        self._snapshot = snapshot
        self._confirm = confirm
        self._confirm_ttl = float(confirm_ttl)
        # pending id -> {"name", "state": wait|allow|deny|timeout, "until"}
        self._pending: dict[str, dict] = {}
        self._store = store
        self._upload_dir = upload_dir
        self._clock = clock
        self._log = log
        self._icon = icon
        self._icons: dict[int, "bytes | None"] = {}
        self._cond = threading.Condition(threading.RLock())
        self._chans: dict[str, _Channel] = {r["id"]: _Channel() for r in store.records()}
        self._token: "tuple[str, float] | None" = None
        self._fails = 0
        self._locked_until = 0.0
        self._server: "ThreadingHTTPServer | None" = None
        self._thread: "threading.Thread | None" = None
        self._tail_server: "ThreadingHTTPServer | None" = None
        self._tail_thread: "threading.Thread | None" = None
        self._gen = 0

    # ------------------------------------------------------------ lifecycle --

    @property
    def url(self) -> str:
        return "http://%s:%d/" % (self.host, self.port)

    @property
    def tailnet_url(self) -> str:
        """`https://<machine>.<tailnet>.ts.net/` while the loopback listener
        for `tailscale serve` runs, else ""."""
        return ("https://%s/" % self.tailnet) if self._tail_server is not None else ""

    def start(self) -> None:
        """Bind host:port and serve on daemon threads. OSError if the bind fails.

        0.0.0.0 IS REFUSED, not corrected: it would put a shell-capable server on
        every interface including a VPN or a public one, and a caller that asked
        for it has a bug the refusal should surface (decision 10).
        """
        if self.host in ("", "0.0.0.0", "::", "[::]"):
            raise ValueError("remote: refusing to bind every interface (%r)" % self.host)
        if self._server is not None:
            return
        remote = self

        class Handler(_Handler):
            owner = remote

        server = _Server((self.host, self.port), Handler)
        self.port = server.server_address[1]
        with self._cond:
            self._gen += 1
            self._server = server
        # poll 0.1 s: /remote off waits for the serve loop to notice, and half a
        # second of a frozen window per toggle is noticeable.
        self._thread = threading.Thread(target=server.serve_forever, args=(0.1,),
                                        name="crow-remote", daemon=True)
        self._thread.start()
        self._log("remote: listening on %s" % self.url)
        if self.tailnet and self.host != "127.0.0.1":
            self._start_tailnet(remote)

    def _start_tailnet(self, remote: "Remote") -> None:
        """The loopback listener `tailscale serve` forwards to (#249 stage 5).

        A FAILED BIND HERE IS NOT A FAILED START: the LAN mirror is what the
        person asked for, the HTTPS address is extra. It is said in crow.log
        and `tailnet_url` stays "", so the dialog does not offer a dead link.
        """
        class TailHandler(_Handler):
            owner = remote
            via_tailnet = True

        try:
            server = _Server(("127.0.0.1", self.port), TailHandler)
        except OSError as exc:
            self._log("remote: no loopback listener for https://%s/ on 127.0.0.1:%d: %s"
                      % (self.tailnet, self.port, exc))
            return
        with self._cond:
            self._tail_server = server
        self._tail_thread = threading.Thread(target=server.serve_forever, args=(0.1,),
                                             name="crow-remote-tailnet", daemon=True)
        self._tail_thread.start()
        self._log("remote: listening on 127.0.0.1:%d for https://%s/ (tailscale serve)"
                  % (self.port, self.tailnet))

    def stop(self) -> None:
        """Close the listener and every open stream. Devices stay paired."""
        with self._cond:
            server, self._server = self._server, None
            tail, self._tail_server = self._tail_server, None
            self._gen += 1
            socks = [s for ch in self._chans.values() for s in ch.streams]
            self._cond.notify_all()
        if server is None:
            return
        for srv in (server, tail):
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        for s in socks:
            _hang_up(s)
        for thread in (self._thread, self._tail_thread):
            if thread is not None:
                thread.join(timeout=5)
        self._log("remote: stopped")

    def running(self) -> bool:
        return self._server is not None

    # -------------------------------------------------------------- pairing --

    def icon_bytes(self, size: int) -> "bytes | None":
        """The tile at `size` px, drawn once by `icon` and kept; None when
        there is no drawing. NEVER RAISES: a missing icon is a 404, not a 500."""
        with self._cond:
            if size in self._icons:
                return self._icons[size]
        data = None
        if self._icon is not None:
            try:
                data = self._icon(size) or None
            except Exception:              # noqa: BLE001 - see the docstring
                data = None
        with self._cond:
            self._icons[size] = data
        return data

    def new_pairing(self) -> str:
        """A fresh single-use token for the QR; the previous one stops working."""
        token = secrets.token_urlsafe(16)
        with self._cond:
            self._token = (token, self._clock() + PAIR_TTL)
        return self.url + "#t=" + token

    def _pair_check(self, token: str) -> int:
        """200 if the token is good (and now spent), 401 if not, 429 while locked."""
        with self._cond:
            now = self._clock()
            if now < self._locked_until:
                return 429
            good = (self._token is not None and isinstance(token, str)
                    and secrets.compare_digest(self._token[0], token)
                    and now <= self._token[1])
            if good:
                self._token = None
                self._fails = 0
                return 200
            self._fails += 1
            if self._fails >= PAIR_MAX_FAILS:
                self._fails = 0
                self._locked_until = now + PAIR_LOCK
                self._token = None
                self._log("remote: pairing locked for %d min after %d failed attempts"
                          % (PAIR_LOCK // 60, PAIR_MAX_FAILS))
            return 401

    def _pair_begin(self, name: str) -> str:
        """Register a pending pairing for a spent token and ask the desktop on
        a thread of its own. The pending id is what /pair/wait polls with."""
        pid = secrets.token_urlsafe(16)
        with self._cond:
            self._pending_purge()
            self._pending[pid] = {"name": name, "state": "wait",
                                  "until": self._clock() + self._confirm_ttl + PAIR_CLAIM}
        threading.Thread(target=self._pair_decide, args=(pid, name),
                         name="crow-remote-confirm", daemon=True).start()
        return pid

    def _pair_decide(self, pid: str, name: str) -> None:
        """`confirm` returns True (allow), False (deny) or None (nobody
        answered in time). A broken dialog is a deny."""
        try:
            said = self._confirm(name)
        except Exception:                  # noqa: BLE001 - a broken dialog is a Deny
            said = False
        state = "timeout" if said is None else ("allow" if said else "deny")
        with self._cond:
            entry = self._pending.get(pid)
            if entry is not None and entry["state"] == "wait":
                entry["state"] = state
            self._cond.notify_all()

    def _pending_purge(self) -> None:
        now = self._clock()
        for pid in [p for p, e in self._pending.items() if now > e["until"]]:
            del self._pending[pid]

    def _pair_wait(self, pid) -> "tuple[int, dict | None, str]":
        """202 while the desktop decides, 200 with the new device and its
        cookie once allowed, 403 denied, 410 timed out or unknown. Holds at
        most PAIR_POLL seconds; a resolved id is spent on its first answer."""
        end = time.monotonic() + PAIR_POLL
        with self._cond:
            while True:
                self._pending_purge()
                entry = self._pending.get(pid) if isinstance(pid, str) else None
                if entry is None:
                    return 410, None, ""
                left = end - time.monotonic()
                if entry["state"] != "wait" or left <= 0:
                    break
                self._cond.wait(left)
            if entry["state"] == "wait":
                return 202, None, ""
            del self._pending[pid]
            if entry["state"] == "deny":
                return 403, None, ""
            if entry["state"] != "allow":
                return 410, None, ""
            record, cookie = self._add_device(entry["name"])
            return 200, record, cookie

    def _add_device(self, name: str) -> tuple[dict, str]:
        cookie = secrets.token_urlsafe(16)           # 128 bits
        with self._cond:
            taken = {r["name"] for r in self._store.records()}
            unique, n = name, 2
            while unique in taken:
                unique, n = "%s %d" % (name, n), n + 1
            now = time.time()
            record = {"id": "d-" + secrets.token_hex(6), "name": unique,
                      "created": now, "last_seen": now, "token_sha256": _sha256(cookie)}
            self._store.add(record)
            self._chans[record["id"]] = _Channel()
        self._log("remote: paired %s (%s)" % (unique, record["id"]))
        return record, cookie

    # -------------------------------------------------------------- devices --

    def devices(self) -> list[dict]:
        with self._cond:
            out = []
            for r in self._store.records():
                r.pop("token_sha256", None)
                ch = self._chans.get(r["id"])
                r["online"] = bool(ch and ch.streams)
                out.append(r)
            return out

    def device_ids(self) -> list[str]:
        return [r["id"] for r in self._store.records()]

    def forget(self, name_or_id: str) -> bool:
        """Revoke one device: its stream ends now and its cookie is 401 from now on.

        Matched by id, then by exact name, then by a unique name prefix, all
        case-insensitive -- `/remote forget iphone` is how a person types it.
        An ambiguous prefix forgets nothing rather than guessing.
        """
        want = (name_or_id or "").strip().casefold()
        if not want:
            return False
        records = self._store.records()
        hit = ([r for r in records if r["id"].casefold() == want]
               or [r for r in records if r["name"].casefold() == want]
               or [r for r in records if r["name"].casefold().startswith(want)])
        if len(hit) != 1:
            return False
        record = hit[0]
        self._store.remove(record["id"])
        with self._cond:
            ch = self._chans.pop(record["id"], None)
            socks = list(ch.streams) if ch else []
            if ch:
                ch.closed = True
            self._cond.notify_all()
        for s in socks:
            _hang_up(s)
        self._log("remote: revoked %s (%s)" % (record["name"], record["id"]))
        return True

    # -------------------------------------------------------------- publish --

    def publish(self, message: dict, to: "Iterable[str] | None" = None) -> None:
        """Queue one push for every paired device, or for the ids in `to`.

        Thread-safe and non-blocking. Offline devices are buffered as well, so
        a phone that was locked resumes exactly where it stopped.
        """
        with self._cond:
            now = self._clock()
            targets = self._chans.keys() if to is None else [i for i in to if i in self._chans]
            for dev in list(targets):
                self._chans[dev].put(message, now)
            self._cond.notify_all()

    # --------------------------------------------------------------- stream --

    def _serve_events(self, handler: "_Handler", dev: str, cookie: str,
                      last: "int | None") -> None:
        with self._cond:
            ch = self._chans.get(dev)
            gen = self._gen
            if ch is None:
                handler.plain(401, "not paired")
                return
            ch.seal()
            replay = last is not None and ch.inside(last)
            sent = last if replay else ch.seq
        self._store.touch(dev, time.time())
        h = handler
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Set-Cookie", h.cookie_header(cookie))
        h.send_header("Connection", "close")
        h.security_headers()
        h.end_headers()
        h.close_connection = True
        sock = h.connection
        with self._cond:
            ch.streams.append(sock)
        try:
            out = ["retry: %d\n\n" % RETRY_MS]
            if not replay:
                # Captured BEFORE the snapshot is built: a push that lands while
                # it is being built may then arrive twice rather than never. The
                # Api side can make that window empty by building the snapshot
                # under the lock its own push takes.
                items = self._snapshot(dev)
                out.append(_event(sent, json.dumps({"k": "snapshot", "items": items},
                                                   ensure_ascii=False,
                                                   separators=(",", ":"))))
            h.wfile.write("".join(out).encode("utf-8"))
            h.wfile.flush()
            wrote = time.monotonic()
            while True:
                with self._cond:
                    while True:
                        if ch.closed or self._gen != gen:
                            return
                        ch.seal_due(self._clock())
                        if ch.ring and ch.ring[-1][0] > sent:
                            break
                        idle = HEARTBEAT - (time.monotonic() - wrote)
                        if idle <= 0:
                            break
                        self._cond.wait(min(idle, COALESCE) if ch.pending else idle)
                    if ch.ring and sent < ch.ring[0][0] - 1:
                        return                      # fell off the ring: reconnect -> snapshot
                    batch = ch.after(sent)
                if batch:
                    h.wfile.write("".join(_event(s, t) for s, t in batch).encode("utf-8"))
                    sent = batch[-1][0]
                else:
                    h.wfile.write(b": hb\n\n")
                h.wfile.flush()
                wrote = time.monotonic()
        except (OSError, ValueError):
            return
        finally:
            with self._cond:
                if sock in ch.streams:
                    ch.streams.remove(sock)


def _event(seq: int, data: str) -> str:
    return "id: %d\ndata: %s\n\n" % (seq, data)


def _cookie_header(value: str, secure: bool = False) -> str:
    """`Secure` on the https origin only: on the LAN's plain http a Secure
    cookie would never be sent back, and the phone could never stay paired.
    Two origins, two cookies -- a phone paired on the LAN pairs once more on
    the ts.net name (#249 stage 5)."""
    return "%s=%s; HttpOnly;%s SameSite=Strict; Path=/; Max-Age=%d" % (
        COOKIE, value, " Secure;" if secure else "", COOKIE_MAX_AGE)


def _hang_up(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # A fixed port (remote_port) has to be bindable again right after /remote
    # off, not after TIME_WAIT runs out -- the phone's bookmark carries the port.
    # NOT on Windows, where SO_REUSEADDR lets a second process bind the same
    # port on top of this one; Windows has no TIME_WAIT bind refusal to fix.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # A phone that walks out of Wi-Fi mid-response is the normal case here,
        # not an error worth a traceback on the window's stderr.
        pass


class _Handler(BaseHTTPRequestHandler):
    owner: Remote
    # True on the loopback listener `tailscale serve` forwards to (#249 stage 5).
    via_tailnet = False
    protocol_version = "HTTP/1.1"
    server_version = "Crow"
    sys_version = ""

    def log_message(self, format, *args):          # noqa: A002 - the base class's name
        pass                                       # a phone's requests are not crow.log's business

    # ---------------------------------------------------------- responses --

    def security_headers(self) -> None:
        for k, v in _SECURITY_HEADERS:
            self.send_header(k, v)

    def body(self, code: int, data: bytes, ctype: str, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if code >= 400:
            # A refused request may leave its body unread, and on a kept-alive
            # connection that body would be parsed as the next request line.
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in extra:
            self.send_header(k, v)
        self.security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def cookie_header(self, value: str) -> str:
        return _cookie_header(value, secure=self.via_tailnet)

    def json(self, code: int, value, extra=()) -> None:
        self.body(code, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                  "application/json; charset=utf-8", extra)

    def plain(self, code: int, text: str) -> None:
        self.json(code, {"error": text})

    # ------------------------------------------------------------- guards --

    def _host_ok(self) -> bool:
        """The LAN listener: exactly `<lan ip>:<port>`, as before stage 5.

        THE LOOPBACK LISTENER: only the ts.net name, which `tailscale serve`
        passes through unchanged (with or without `:443`). A bare
        `127.0.0.1:<port>` or `localhost:<port>` stays a 421, as it was while
        nothing listened on the loopback at all: a local browser tab is not a
        paired phone, and http://127.0.0.1 counts as a secure context, so
        letting it in would open a third origin nobody chose to pair on -- and
        a DNS-rebinding page in the desktop's browser would reach this port
        under its own name, which fails this check just the same.
        """
        o = self.owner
        host = (self.headers.get("Host") or "").lower()
        if self.via_tailnet:
            return bool(o.tailnet) and host in (o.tailnet, o.tailnet + ":443")
        return host == ("%s:%d" % (o.host, o.port)).lower()

    def _origin_ok(self) -> bool:
        o = self.owner
        if self.via_tailnet:
            return bool(o.tailnet) and self.headers.get("Origin") == "https://" + o.tailnet
        return self.headers.get("Origin") == "http://%s:%d" % (o.host, o.port)

    def _cookie(self) -> "str | None":
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = cookies.SimpleCookie()
            jar.load(raw)
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _device(self) -> "tuple[str, str] | None":
        value = self._cookie()
        if not value:
            return None
        record = self.owner._store.by_hash(_sha256(value))
        if record is None or record["id"] not in self.owner._chans:
            return None
        return record["id"], value

    def _read_body(self, limit: int) -> "bytes | None":
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self.plain(411, "length required")
            return None
        if length < 0 or length > limit:
            self.plain(413, "too large")
            return None
        return self.rfile.read(length)

    # ------------------------------------------------------------- routes --

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()
            return
        if not self._host_ok():
            return self.plain(421, "misdirected request")
        if path.path == "/":
            return self.body(200, self.owner._page().encode("utf-8"),
                             "text/html; charset=utf-8")
        if path.path == "/remote.webmanifest":
            return self.body(200, json.dumps({
                "name": "Crow", "short_name": "Crow", "start_url": "/",
                "display": "standalone", "background_color": "#000000",
                "icons": [{"src": route, "sizes": "%dx%d" % (px, px),
                           "type": "image/png"}
                          for route, px in ICON_ROUTES.items()]}).encode("utf-8"),
                "application/manifest+json")
        if path.path in ICON_ROUTES:
            # No cookie, like `/` and the manifest: iOS fetches the tile
            # without the page's credentials when it is added.
            data = self.owner.icon_bytes(ICON_ROUTES[path.path])
            if not data:
                return self.plain(404, "not found")
            return self.body(200, data, "image/png")
        if path.path == "/me":
            # The page cannot read its HttpOnly cookie; this is how it asks
            # whether the cookie still pairs it, before it shows "expired".
            dev = self._device()
            if dev is None:
                return self.plain(401, "not paired")
            return self._paired(dev)
        if path.path == "/events":
            dev = self._device()
            if dev is None:
                return self.plain(401, "not paired")
            raw = self.headers.get("Last-Event-ID")
            if raw is None:
                raw = (parse_qs(path.query).get("last") or [None])[0]
            try:
                last = int(raw) if raw not in (None, "") else None
            except ValueError:
                last = None
            return self.owner._serve_events(self, dev[0], dev[1], last)
        return self.plain(404, "not found")

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._host_ok():
            return self.plain(421, "misdirected request")
        if not self._origin_ok():
            return self.plain(403, "foreign origin")
        if path == "/pair":
            return self._pair()
        if path == "/pair/wait":
            return self._pair_wait()
        dev = self._device()
        if dev is None:
            return self.plain(401, "not paired")
        if path == "/upload":
            return self._upload()
        if path.startswith("/api/"):
            return self._api(path[len("/api/"):], dev[0])
        return self.plain(404, "not found")

    def _pair(self):
        raw = self._read_body(MAX_BODY)
        if raw is None:
            return
        # A PAIRED PHONE IS NEVER SENT BACK TO PAIRING BY A STALE CODE (iPhone,
        # 2026-09-24): Chrome's autocomplete, a bookmark or a home-screen icon
        # can still carry the first QR's #t=, spent long ago. A valid cookie
        # answers before the token is looked at -- not checked, not consumed,
        # not counted toward the lockout, and no second device.
        dev = self._device()
        if dev is not None:
            return self._paired(dev)
        try:
            token = json.loads(raw or b"{}").get("t")
        except (ValueError, AttributeError):
            token = None
        code = self.owner._pair_check(token)
        if code == 429:
            return self.plain(429, "pairing locked")
        if code != 200:
            return self.plain(401, "bad or expired pairing code")
        pid = self.owner._pair_begin(device_name(self.headers.get("User-Agent", "")))
        self.json(202, {"p": pid})

    def _paired(self, dev: "tuple[str, str]"):
        """200 {id, name, paired} for a phone that holds a valid cookie, which
        is renewed on the way -- /pair and /me both answer this way."""
        record = self.owner._store.by_hash(_sha256(dev[1])) or {}
        self.owner._store.touch(dev[0], time.time())
        self.json(200, {"id": dev[0], "name": record.get("name", ""), "paired": True},
                  (("Set-Cookie", self.cookie_header(dev[1])),))

    def _pair_wait(self):
        raw = self._read_body(MAX_BODY)
        if raw is None:
            return
        try:
            pid = json.loads(raw or b"{}").get("p")
        except (ValueError, AttributeError):
            pid = None
        code, record, cookie = self.owner._pair_wait(pid)
        if code == 202:
            return self.json(202, {"p": pid})
        if code == 403:
            return self.plain(403, "denied on the desktop")
        if code != 200:
            return self.plain(410, "no such pairing, or it timed out")
        self.json(200, {"id": record["id"], "name": record["name"]},
                  (("Set-Cookie", self.cookie_header(cookie)),))

    def _upload(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return self.plain(415, "images only")
        raw = self._read_body(MAX_UPLOAD)
        if raw is None:
            return
        os.makedirs(self.owner._upload_dir, exist_ok=True)
        fd, target = tempfile.mkstemp(prefix="remote-", suffix=_IMAGE_EXT.get(ctype, ".img"),
                                      dir=self.owner._upload_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        self.json(200, {"path": target})

    def _api(self, name: str, dev: str):
        if name not in self.owner._allowed:
            return self.plain(404, "not found")
        raw = self._read_body(MAX_BODY)
        if raw is None:
            return
        try:
            args = json.loads(raw or b"[]")
        except ValueError:
            args = None
        if not isinstance(args, list):
            return self.plain(400, "arguments must be a JSON array")
        token = CLIENT.set(dev)
        try:
            result = self.owner._call(name, args)
            data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        except Exception as exc:           # noqa: BLE001 - the page gets the reason
            return self.plain(500, str(exc) or type(exc).__name__)
        finally:
            CLIENT.reset(token)
        self.body(200, data, "application/json; charset=utf-8")


# ================================================================ TAILNET ==
#
# #249 stage 5: WHERE THE HTTPS ADDRESS STANDS, read and never changed. Every
# call is `tailscale ... --json` without sudo, answered in well under a second
# on robin's PC (tailscale 1.102.3, 2026-09-24); a hung daemon costs at most
# TAILSCALE_TIMEOUT per call. The states, in the order a person meets them:
#
#   missing        no `tailscale` on PATH
#   down           installed, but not running or not logged in (no DNSName)
#   https-off      HTTPS certificates are off in the admin console (no CertDomains)
#   serve-missing  no `tailscale serve` for <name>:443 -> 127.0.0.1:<port>
#   funnel         Funnel is on for <name>:443: the address would be PUBLIC on
#                  the internet, so Crow does not listen for it at all
#   ready          the proxy points here; https://<name>/ works
#
# `bindable` states get the loopback listener, so the command in the dialog
# works the moment it is run, without restarting the mirror.

TAILSCALE_TIMEOUT = 3.0
TAILSCALE_ADMIN_DNS = "https://login.tailscale.com/admin/dns"
TAILNET_BINDABLE = frozenset({"https-off", "serve-missing", "ready"})


def tailscale_serve_command(port: int) -> str:
    """The one-time command that points <name>:443 at this mirror. Persistent
    (it survives reboots, --bg) and the certificate renews itself. Root or a
    Tailscale operator runs it, never Crow."""
    return "%stailscale serve --bg --https=443 http://127.0.0.1:%d" % (
        "" if sys.platform == "win32" else "sudo ", int(port))


def _tailscale_json(run, argv: list) -> "dict | None":
    try:
        code, out = run(argv)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if code != 0:
        return None
    try:
        found = json.loads(out or "{}")
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _tailscale_run(argv: list) -> "tuple[int, str]":
    done = subprocess.run(argv, capture_output=True, text=True,
                          timeout=TAILSCALE_TIMEOUT, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout


def tailscale_state(port: int, run: "Callable[[list], tuple[int, str]] | None" = None,
                    which: Callable[[str], "str | None"] = shutil.which) -> dict:
    """{"state", "name", "ip", "command"} -- see the table above. NEVER RAISES:
    a probe that fails reads as the earliest state it could prove.

    `run(argv) -> (returncode, stdout)` and `which` are the seams the suite
    fills with fakes; nothing in the suite runs the real CLI."""
    run = run or _tailscale_run
    out = {"state": "missing", "name": "", "ip": "",
           "command": tailscale_serve_command(port)}
    exe = which("tailscale")
    if not exe:
        return out
    out["state"] = "down"
    status = _tailscale_json(run, [exe, "status", "--json"])
    if status is None:
        return out
    me = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    name = str(me.get("DNSName") or "").strip().rstrip(".").lower()
    ips = [ip for ip in (me.get("TailscaleIPs") or status.get("TailscaleIPs") or [])
           if isinstance(ip, str) and "." in ip]
    if status.get("BackendState") != "Running" or not name:
        return out
    out.update(name=name, ip=ips[0] if ips else "")
    certs = [str(c).rstrip(".").lower() for c in (status.get("CertDomains") or [])]
    if name not in certs:
        out["state"] = "https-off"
        return out
    serve = _tailscale_json(run, [exe, "serve", "status", "--json"]) or {}
    key = name + ":443"
    if (serve.get("AllowFunnel") or {}).get(key):
        out["state"] = "funnel"
        return out
    handlers = ((serve.get("Web") or {}).get(key) or {}).get("Handlers") or {}
    proxy = str((handlers.get("/") or {}).get("Proxy") or "").rstrip("/").lower()
    targets = {"%s%s:%d" % (scheme, host, int(port))
               for scheme in ("http://", "")
               for host in ("127.0.0.1", "localhost")}
    out["state"] = "ready" if proxy in targets else "serve-missing"
    return out


# ======================================================================= QR ==
#
# A QR encoder for exactly what the pairing URL needs: byte mode, error
# correction level M, versions 1-6 (up to 106 bytes -- the pairing URL is
# about 55). Written after ISO/IEC 18004 and checked module-for-module against
# segno in cli/test_crow_remote.py. Level M because the QR sits on a screen,
# not on a crumpled label: L would shave one version off and a glare spot on a
# glossy laptop panel is exactly the damage M exists for.

# versions 1-6, level M: data codewords, EC codewords per block, blocks
_DATA_CW = (0, 16, 28, 44, 64, 86, 108)
_EC_PER_BLOCK = (0, 10, 16, 26, 18, 24, 16)
_BLOCKS = (0, 1, 1, 1, 2, 2, 4)
_FORMAT_M = 0b00                      # the level's two format bits


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data: bytes) -> tuple[int, list[int]]:
    """Pick the smallest version, then build the interleaved codeword sequence."""
    for version in range(1, 7):
        if 4 + 8 + 8 * len(data) <= _DATA_CW[version] * 8:
            break
    else:
        raise ValueError("QR: %d bytes do not fit version 6-M" % len(data))
    capacity = _DATA_CW[version] * 8
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)                     # byte mode
    put(len(data), 8)                  # count: 8 bits for versions 1-9
    for b in data:
        put(b, 8)
    put(0, min(4, capacity - len(bits)))
    put(0, (-len(bits)) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < _DATA_CW[version]:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    n, size = _BLOCKS[version], _DATA_CW[version] // _BLOCKS[version]
    divisor = _rs_divisor(_EC_PER_BLOCK[version])
    blocks = [words[i * size:(i + 1) * size] for i in range(n)]
    ecs = [_rs_remainder(b, divisor) for b in blocks]
    out = [b[i] for i in range(size) for b in blocks]
    out += [e[i] for i in range(_EC_PER_BLOCK[version]) for e in ecs]
    return version, out


def _format_bits(mask: int) -> int:
    data = _FORMAT_M << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Symbol:
    def __init__(self, version: int):
        self.size = size = version * 4 + 17
        self.dark = [[False] * size for _ in range(size)]
        self.fixed = [[False] * size for _ in range(size)]
        for i in range(size):                                # timing
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        if version >= 2:                                     # the one alignment pattern
            c = size - 7
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self.set(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(None)                               # reserve the format area

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def draw_format(self, mask: "int | None") -> None:
        """The format bits for `mask`; None draws the whole area light, dark module included."""
        bits = 0 if mask is None else _format_bits(mask)
        bit = lambda i: (bits >> i) & 1 == 1                 # noqa: E731
        size = self.size
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit(i))
        self.set(8, size - 8, mask is not None)              # the dark module

    def place(self, words: list[int]) -> None:
        size, i, total = self.size, 0, len(words) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = (right + 1) & 2 == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        test = _MASKS[mask]
        out = [row[:] for row in self.dark]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(x, y):
                    out[y][x] = not out[y][x]
        return out


def _penalty(m: list[list[bool]]) -> int:
    """ISO 18004 section 7.8.3: the four rules, N1=3 N2=3 N3=40 N4=10.

    Rule 3 (finder-like 1:1:3:1:1 with four light modules on one side) counts
    modules beyond the symbol edge as light, since the quiet zone is light, and
    a counted match resumes the search behind itself.

    SCORED BEFORE THE FORMAT BITS ARE DRAWN (that area and the dark module are
    light while scoring), which is how segno reads 7.8 -- masking and its
    evaluation cover the encoding region, format information comes after.
    Other encoders score with the format bits in place; both give a valid
    symbol, and choosing segno's reading is what lets the test compare the two
    module for module instead of only up to the mask.
    """
    size = len(m)
    score = 0
    lines = ["".join("1" if v else "0" for v in row) for row in m]
    lines += ["".join("1" if m[y][x] else "0" for y in range(size)) for x in range(size)]
    for line in lines:
        run, prev = 0, None
        for c in line:
            if c == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, prev = 1, c
        if run >= 5:
            score += 3 + run - 5
        padded = "0000" + line + "0000"
        at = padded.find("1011101")
        while at >= 0:
            if padded[at - 4:at] == "0000" or padded[at + 7:at + 11] == "0000":
                score += 40
                at = padded.find("1011101", at + 7)
            else:
                at = padded.find("1011101", at + 4)
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def qr_matrix(text: str, mask: "int | None" = None) -> list[list[bool]]:
    """The QR symbol for `text` (UTF-8, byte mode, level M), True = dark.

    No quiet zone; qr_svg adds it. `mask` forces one of the eight masks, which
    only the segno comparison needs; otherwise the lowest penalty wins, ties to
    the lower number.
    """
    version, words = _codewords(text.encode("utf-8"))
    sym = _Symbol(version)
    sym.place(words)
    if mask is None:
        mask = min(range(8), key=lambda m: (_penalty(sym.masked(m)), m))
    sym.draw_format(mask)
    return sym.masked(mask)


def qr_svg(text: str, quiet: int = 4) -> str:
    """The QR as a standalone SVG: black on white, quiet zone included.

    EXPLICIT COLOURS, not currentColor: on the dark theme currentColor would
    draw a light-on-dark code, and not every camera reads inverted QR codes.
    One path of row runs keeps it small and crisp at any scale.
    """
    m = qr_matrix(text)
    size = len(m) + 2 * quiet
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append("M%d %dh%dv1h-%dz" % (start + quiet, y + quiet,
                                                   x - start, x - start))
            else:
                x += 1
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'shape-rendering="crispEdges" role="img" aria-label="QR code">'
            '<rect width="%d" height="%d" fill="#fff"/>'
            '<path fill="#000" d="%s"/></svg>' % (size, size, size, size, "".join(parts)))


# ================================================================ THE ICON ==
#
# THE HOME-SCREEN TILE (robin's iPhone, 2026-09-24: "Add to Home Screen" drew a
# generic tile with a "1"). iOS reads `apple-touch-icon` and fills any
# transparency with black, so the tile is the window's own bird (cli/icons/,
# RGBA) composited onto an opaque ground and scaled here. Standard library
# only, like the QR above: a PNG reader for what cli/icons holds (8-bit RGB or
# RGBA, not interlaced) and a writer for opaque RGB.

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    import zlib
    return (len(data).to_bytes(4, "big") + kind + data
            + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big"))


def png_rgb(width: int, height: int, rgb: "bytes | bytearray") -> bytes:
    """An opaque 8-bit RGB PNG (colour type 2) -- no alpha channel at all."""
    import zlib
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride])
                   for y in range(height))
    head = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes((8, 2, 0, 0, 0)))
    return (_PNG_SIG + _png_chunk(b"IHDR", head)
            + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b""))


def png_rgba(data: bytes) -> "tuple[int, int, bytearray]":
    """(width, height, RGBA bytes) of an 8-bit RGB/RGBA non-interlaced PNG.
    ValueError for anything else -- the caller serves no icon then."""
    import zlib
    if not data.startswith(_PNG_SIG):
        raise ValueError("not a PNG")
    pos, idat, head = 8, [], None
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + size]
        if kind == b"IHDR":
            head = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + size
    if head is None or len(head) != 13:
        raise ValueError("no IHDR")
    width, height = int.from_bytes(head[0:4], "big"), int.from_bytes(head[4:8], "big")
    depth, ctype, interlace = head[8], head[9], head[12]
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError("unsupported PNG layout")
    bpp = 4 if ctype == 6 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    prev = bytearray(stride)
    out = bytearray(width * height * 4)
    for y in range(height):
        base = y * (stride + 1)
        kind = raw[base]
        line = bytearray(raw[base + 1:base + 1 + stride])
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif kind != 0:
            raise ValueError("bad PNG filter")
        prev = line
        if bpp == 4:
            out[y * width * 4:(y + 1) * width * 4] = line
        else:
            row = out[y * width * 4:(y + 1) * width * 4]
            row[0::4], row[1::4], row[2::4] = line[0::3], line[1::3], line[2::3]
            row[3::4] = b"\xff" * width
            out[y * width * 4:(y + 1) * width * 4] = row
    return width, height, out


def touch_icon(source: bytes, size: int, background: str,
               margin: float = 0.12) -> bytes:
    """`source` (an RGBA PNG) scaled into a `size` x `size` opaque PNG on
    `background` ("#rrggbb"), with `margin` of the edge left clear so iOS's
    rounded corners never cut the drawing. Box-averaged in premultiplied
    alpha, so the edges blend into the ground instead of fringing."""
    sw, sh, px = png_rgba(source)
    bg = tuple(int(background.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    inner = max(1, round(size * (1 - 2 * margin)))
    off = (size - inner) // 2
    rgb = bytearray(bytes(bg) * (size * size))
    for ty in range(inner):
        y0 = ty * sh // inner
        y1 = max(y0 + 1, (ty + 1) * sh // inner)
        for tx in range(inner):
            x0 = tx * sw // inner
            x1 = max(x0 + 1, (tx + 1) * sw // inner)
            r = g = b = a = 0
            for sy in range(y0, y1):
                row = sy * sw * 4
                for sx in range(x0, x1):
                    i = row + sx * 4
                    alpha = px[i + 3]
                    r += px[i] * alpha
                    g += px[i + 1] * alpha
                    b += px[i + 2] * alpha
                    a += alpha
            n = (y1 - y0) * (x1 - x0)
            cover = a / (255 * n)
            o = ((ty + off) * size + tx + off) * 3
            for k, (acc, ground) in enumerate(((r, bg[0]), (g, bg[1]), (b, bg[2]))):
                rgb[o + k] = min(255, round(acc / (255 * n) + ground * (1 - cover)))
    return png_rgb(size, size, rgb)
