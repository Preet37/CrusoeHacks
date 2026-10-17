"""End-to-end text test: walks the RUNNING.md section 3 script against a live server.

    cd server && .venv/bin/python scripts/e2e_text.py [--url ws://localhost:8002] [--room e2e-<ts>] [--only 1,3,6]

Connects as speaker "Preet", sends each line as user_text (interim then final), records
every server message with timestamps, asserts per step and prints PASS/FAIL + timing.
Exit 0 only if steps 1-5 and 10-12 pass (6-8 depend on the mind and are informational;
9 is reported but not gating).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

SPEAKER = "Preet"
DEFAULT_REPORT = ("/private/tmp/claude-501/-Users-preet-Desktop-hackathons/"
                  "2233d776-618a-4b78-b73a-48be9dd69e87/scratchpad/e2e_report.json")
GATING = {1, 2, 3, 4, 5, 10, 11, 12}

RE_WEB = re.compile(r"web|front|client|browser|\bui\b|\bapp\b", re.I)
RE_AUTH = re.compile(r"auth", re.I)
RE_PG = re.compile(r"postgres|\bpg\b|database|\bdb\b|sql", re.I)
RE_REDIS = re.compile(r"redis", re.I)


def now_ms() -> int:
    return int(time.time() * 1000)


class Client:
    def __init__(self, url: str, room: str) -> None:
        self.base = url.rstrip("/")
        self.room = room
        self.msgs: list[dict] = []   # each: {"t": seconds since start, "msg": {...}}
        self.sent: list[dict] = []
        self.t0 = time.monotonic()
        self.status = "?"
        self.ws = None
        self._rx: asyncio.Task | None = None

    def t(self) -> float:
        return time.monotonic() - self.t0

    async def connect(self) -> None:
        q = urllib.parse.urlencode({"room": self.room, "user": SPEAKER})
        self.ws = await websockets.connect(f"{self.base}/ws?{q}", max_size=None, ping_interval=20)
        self._rx = asyncio.create_task(self._recv())
        await self.send({"type": "hello", "protocol": 1, "user": SPEAKER, "room": self.room})

    async def _recv(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get("type") == "status":
                    self.status = m.get("state")
                self.msgs.append({"t": round(self.t(), 3), "msg": m})
        except websockets.ConnectionClosed:
            pass

    async def send(self, m: dict) -> None:
        self.sent.append({"t": round(self.t(), 3), "msg": m})
        await self.ws.send(json.dumps(m))

    async def say(self, text: str) -> float:
        """Interim (first half) then final. Returns the time the final was sent."""
        words = text.split()
        interim = " ".join(words[: max(1, len(words) // 2)])
        await self.send({"type": "user_text", "speaker": SPEAKER, "text": interim, "final": False, "ts": now_ms()})
        await asyncio.sleep(0.3)
        await self.send({"type": "user_text", "speaker": SPEAKER, "text": text, "final": True, "ts": now_ms()})
        return self.t()

    def since(self, t: float, typ: str | None = None, **kv) -> list[dict]:
        out = []
        for e in self.msgs:
            if e["t"] < t:
                continue
            m = e["msg"]
            if typ and m.get("type") != typ:
                continue
            if any(m.get(k) != v for k, v in kv.items()):
                continue
            out.append(e)
        return out

    async def wait(self, pred, timeout: float, poll: float = 0.05):
        """Poll pred() until truthy or timeout. Returns pred's value (or None)."""
        end = time.monotonic() + timeout
        while True:
            v = pred()
            if v:
                return v
            if time.monotonic() >= end:
                return None
            await asyncio.sleep(poll)

    async def wait_listening(self, timeout: float = 25.0, quiet: float = 1.5) -> bool:
        """Wait until status is listening and no audio arrived for `quiet` seconds."""
        def ok():
            if self.status not in ("listening", "hand_raised"):
                return False
            last_audio = [e for e in self.msgs if e["msg"].get("type") in ("audio", "status")
                          and (e["msg"].get("type") == "audio" or e["msg"].get("state") != "listening")]
            return not last_audio or self.t() - last_audio[-1]["t"] >= quiet
        return bool(await self.wait(ok, timeout))

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self._rx:
            self._rx.cancel()


def twin_lines(c: Client, t: float) -> list[tuple[float, str, str]]:
    """(time, kind, text) of Twin speech since t: audio chunk texts and Twin transcripts."""
    out = []
    for e in c.msgs:
        if e["t"] < t:
            continue
        m = e["msg"]
        if m.get("type") == "audio" and m.get("text"):
            out.append((e["t"], "audio", m["text"]))
        elif m.get("type") == "transcript" and m.get("speaker") != SPEAKER:
            out.append((e["t"], "transcript", m.get("text", "")))
    return out


def draws(c: Client, t: float, op: str | None = None) -> list[dict]:
    return [e for e in c.since(t, "draw") if op is None or e["msg"].get("op") == op]


def draw_summary(c: Client, t: float) -> str:
    ops = [f"{e['msg'].get('op')}:{e['msg'].get('id')}" for e in draws(c, t)]
    return ", ".join(ops) if ops else "no draw ops"


def first_new_say(c: Client, t: float, exclude: set[str] = frozenset()):
    for e in c.since(t, "audio"):
        sid = e["msg"].get("say_id")
        if sid not in exclude:
            return e
    return None


def say_ids_before(c: Client, t: float) -> set[str]:
    return {e["msg"].get("say_id") for e in c.msgs if e["t"] < t and e["msg"].get("type") == "audio"}


# ---------------------------------------------------------------- steps

class Ctx:
    def __init__(self, c: Client, http_base: str) -> None:
        self.c = c
        self.http = http_base
        self.step1_last_draw: float | None = None
        self.step1_start: float = 0.0
        self.labels: list[str] = []


async def step1(x: Ctx):
    c = x.c
    t0 = await c.say("So our web app talks to an auth service, and auth stores users in Postgres.")
    x.step1_start = t0

    def done():
        boxes = draws(c, t0, "add_box")
        conns = draws(c, t0, "connect")
        labels = [b["msg"].get("label") or b["msg"].get("id") or "" for b in boxes]
        have = (any(RE_WEB.search(l) for l in labels) and any(RE_AUTH.search(l) for l in labels)
                and any(RE_PG.search(l) for l in labels))
        return len(boxes) >= 3 and len(conns) >= 2 and have
    await c.wait(done, 25)
    await asyncio.sleep(1.0)  # let trailing ops land
    ds = [e for e in draws(c, t0) if e["t"] - t0 <= 26]
    boxes = [e for e in ds if e["msg"].get("op") == "add_box"]
    conns = [e for e in ds if e["msg"].get("op") == "connect"]
    labels = [b["msg"].get("label") or "" for b in boxes]
    x.labels = labels
    if ds:
        x.step1_last_draw = max(e["t"] for e in ds)
    times = sorted({round(e["t"] - t0, 2) for e in ds})
    progressive = len(times) >= 2 and (times[-1] - times[0]) > 0.0
    have = {"web": any(RE_WEB.search(l) for l in labels), "auth": any(RE_AUTH.search(l) for l in labels),
            "postgres": any(RE_PG.search(l) for l in labels)}
    ok = len(boxes) >= 3 and len(conns) >= 2 and all(have.values())
    first = round(ds[0]["t"] - t0, 2) if ds else None
    last = round(ds[-1]["t"] - t0, 2) if ds else None
    detail = (f"{len(boxes)} add_box {labels}, {len(conns)} connect; labels ok={have}; "
              f"first draw +{first}s, last +{last}s; progressive={progressive} (op times {times[:12]})")
    return ok, {"first_draw_s": first, "last_draw_s": last}, detail


async def step2(x: Ctx):
    c = x.c
    # The pause: do nothing. Twin's work update may already have started during step 1.
    anchor = x.step1_last_draw if x.step1_last_draw is not None else c.t()
    words = set()
    for l in x.labels:
        words.update(w.lower() for w in re.findall(r"[A-Za-z]{3,}", l))
    words |= {"web", "auth", "postgres", "database"}
    words -= {"service", "the", "and"}

    def found():
        for t, kind, text in twin_lines(c, x.step1_start):
            if any(w in text.lower() for w in words):
                return (t, kind, text)
        return None
    deadline = anchor + 15 - c.t()
    hit = await c.wait(found, max(0.0, deadline))
    lines = twin_lines(c, x.step1_start)
    if hit:
        dt = round(hit[0] - anchor, 2)
        await c.wait_listening(25)
        return dt <= 15, {"after_last_draw_s": dt}, f"Twin {hit[1]} +{dt}s after last draw: {hit[2]!r}"
    got = "; ".join(f"{k}:{tx!r}" for _, k, tx in lines[:3]) or "no Twin speech"
    await c.wait_listening(25)
    return False, {}, f"no Twin line mentioning {sorted(words)[:8]} within 15s of last draw; got: {got}"


async def step3(x: Ctx):
    c = x.c
    await c.wait_listening(20)
    t0 = await c.say("Actually auth also uses Redis for sessions.")

    def done():
        rb = [e for e in draws(c, t0, "add_box") if RE_REDIS.search((e["msg"].get("label") or "") + (e["msg"].get("id") or ""))]
        if not rb:
            return None
        rid = {e["msg"]["id"] for e in rb}
        cn = [e for e in draws(c, t0, "connect") if e["msg"].get("from_id") in rid or e["msg"].get("to_id") in rid]
        return (rb, cn) if cn else None
    hit = await c.wait(done, 25)
    await asyncio.sleep(2.0)
    dup = [e["msg"] for e in draws(c, t0, "add_box")
           if RE_AUTH.search(e["msg"].get("label") or "") and not RE_REDIS.search(e["msg"].get("label") or "")]
    if not hit:
        return False, {}, f"no redis add_box + connect within 25s; got: {draw_summary(c, t0)}"
    dt = round(max(hit[0][0]["t"], hit[1][0]["t"]) - t0, 2)
    ok = not dup
    detail = f"redis box+connect at +{dt}s; ops: {draw_summary(c, t0)}"
    if dup:
        detail += f"; DUPLICATE auth add_box: {[(d.get('id'), d.get('label')) for d in dup]}"
    await c.wait_listening(25)
    return ok, {"redis_s": dt}, detail


async def step4(x: Ctx):
    c = x.c
    await c.wait_listening(20)
    t_before = c.t()
    old = say_ids_before(c, t_before)
    t0 = await c.say("Twin, does that look right?")
    fa = await c.wait(lambda: first_new_say(c, t0, old), 8)
    tr = await c.wait(lambda: [e for e in c.since(t0, "transcript") if e["msg"].get("speaker") != SPEAKER], 20)
    if not fa:
        return False, {}, f"no new Twin audio within 8s; transcript={bool(tr)}; status={c.status}"
    sid = fa["msg"]["say_id"]
    lastm = await c.wait(lambda: [e for e in c.since(t0, "audio", say_id=sid) if e["msg"].get("last")], 20)
    chunks = c.since(t0, "audio", say_id=sid)
    seqs = [e["msg"].get("seq") for e in chunks]
    seq_ok = seqs == list(range(len(seqs)))
    lat = round(fa["t"] - t0, 3)
    tr_lat = round(tr[0]["t"] - t0, 2) if tr else None
    text = tr[0]["msg"].get("text") if tr else " ".join(e["msg"].get("text") or "" for e in chunks)
    nulls = sum(1 for e in chunks if e["msg"].get("data") is None)
    ok = bool(lastm) and seq_ok and tr_lat is not None and tr_lat <= 8
    detail = (f"say_id={sid} final->first audio {lat*1000:.0f}ms, transcript +{tr_lat}s, seqs={seqs}, "
              f"last={bool(lastm)}, null-data chunks={nulls}; said {text!r}")
    await c.wait_listening(25)
    return ok, {"final_to_first_audio_ms": round(lat * 1000), "transcript_s": tr_lat}, detail


async def step5(x: Ctx):
    c = x.c
    await c.wait_listening(20)
    old = say_ids_before(c, c.t())
    t0 = await c.say("Twin, explain in detail how sessions differ from JWTs.")

    def trigger():
        a = first_new_say(c, t0, old)
        if a:
            return ("audio", a["msg"]["say_id"])
        st = c.since(t0, "status")
        if any(e["msg"].get("state") == "speaking" for e in st):
            return ("speaking", None)
        return None
    trig = await c.wait(trigger, 12, poll=0.01)
    if not trig:
        return False, {}, f"Twin never started (no audio/speaking within 12s); status={c.status}"
    tb = c.t()
    await c.send({"type": "barge_in", "speaker": SPEAKER, "ts": now_ms()})
    sid = trig[1]
    stop = await c.wait(lambda: c.since(tb, "stop_audio"), 3, poll=0.01)
    if not stop:
        return False, {}, f"no stop_audio within 3s of barge_in (trigger={trig[0]})"
    st = stop[0]
    stop_lat = round(st["t"] - tb, 3)
    if sid is None:
        sid = st["msg"].get("say_id")
    await asyncio.sleep(3.0)
    after = [e for e in c.since(st["t"], "audio") if e["msg"].get("say_id") == sid and e["t"] > st["t"]]
    mismatch = st["msg"].get("say_id") not in (None, sid)
    ok = stop_lat <= 1.0 and not after and not mismatch
    detail = (f"barge_in on {trig[0]} (say_id={sid}) at +{tb - t0:.2f}s; stop_audio(say_id={st['msg'].get('say_id')}) "
              f"after {stop_lat*1000:.0f}ms; audio for {sid} after stop: {len(after)}")
    await c.wait_listening(15)
    return ok, {"stop_latency_ms": round(stop_lat * 1000)}, detail


async def _wait_op(x: Ctx, text: str, op: str, timeout: float = 30):
    c = x.c
    await c.wait_listening(20)
    t0 = await c.say(text)
    hit = await c.wait(lambda: draws(c, t0, op), timeout)
    if hit:
        dt = round(hit[0]["t"] - t0, 2)
        m = hit[0]["msg"]
        extra = {k: (str(m.get(k))[:80]) for k in ("chart", "title", "labels", "series", "code") if k in m}
        await c.wait_listening(20)
        return True, {f"{op}_s": dt}, f"{op} id={m.get('id')} at +{dt}s {extra}"
    speech = "; ".join(tx for _, k, tx in twin_lines(c, t0) if k == "transcript")[:200]
    return False, {}, f"no {op} within {timeout:.0f}s; draws: {draw_summary(c, t0)}; Twin said: {speech!r}"


async def step6(x: Ctx):
    return await _wait_op(x, "Twin, walk me through the login sequence.", "add_mermaid")


async def step7(x: Ctx):
    return await _wait_op(x, "Our p95 latency went from 420 to 180 milliseconds after the cache.", "add_chart")


async def step8(x: Ctx):
    return await _wait_op(x, "What would the new login screen look like?", "add_mock")


async def step9(x: Ctx):
    c = x.c
    await c.wait_listening(20)
    t0 = await c.say("OK, different topic: let's talk about billing.")

    def done():
        tc = [e for e in c.since(t0, "topic") if e["msg"].get("changed")]
        fr = draws(c, t0, "frame")
        fo = draws(c, t0, "focus")
        if tc or (fr and fo):
            return tc, fr, fo
        return None
    hit = await c.wait(done, 30)
    if not hit:
        tops = [e["msg"] for e in c.since(t0, "topic")]
        return False, {}, f"no topic changed=true nor frame+focus in 30s; topics={tops}; draws: {draw_summary(c, t0)}"
    await asyncio.sleep(1.5)
    tc = [e for e in c.since(t0, "topic") if e["msg"].get("changed")]
    fr, fo = draws(c, t0, "frame"), draws(c, t0, "focus")
    first = min(e["t"] for e in (tc + fr + fo)) - t0
    detail = (f"topic changed={[(e['msg'].get('topic_id'), e['msg'].get('title')) for e in tc]}, "
              f"frame={[e['msg'].get('id') for e in fr]}, focus={[e['msg'].get('id') for e in fo]}; first +{first:.2f}s")
    await c.wait_listening(20)
    return True, {"topic_s": round(first, 2)}, detail


async def step10(x: Ctx):
    c = x.c
    await c.wait_listening(20)
    t0 = await c.say("Not now, Twin.")
    hit = await c.wait(lambda: c.since(t0, "status", state="hand_raised"), 2)
    if hit:
        dt = round(hit[0]["t"] - t0, 3)
        return True, {"hand_raised_ms": round(dt * 1000)}, f"hand_raised after {dt*1000:.0f}ms"
    return False, {}, f"no hand_raised within 2s; status={c.status}; statuses={[e['msg'].get('state') for e in c.since(t0, 'status')]}"


async def step11(x: Ctx):
    c = x.c
    t0 = c.t()
    url = f"{x.http}/simulate/calendar_join?" + urllib.parse.urlencode({"room": c.room, "meeting": "Daily standup"})

    def post():
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode() or "{}")
    try:
        resp = await asyncio.to_thread(post)
    except Exception as e:  # noqa: BLE001
        return False, {}, f"POST failed: {e}"
    if not resp.get("ok"):
        return False, {}, f"POST returned {resp}"
    br = await c.wait(lambda: c.since(t0, "brief"), 15)
    tr = await c.wait(lambda: [e for e in c.since(t0, "transcript") if e["msg"].get("speaker") != SPEAKER], 15)
    if not br or not tr:
        aud = twin_lines(c, t0)
        return False, {}, f"brief={bool(br)} transcript={bool(tr)} within 15s; audio texts={[a[2] for a in aud][:2]}"
    b, t = round(br[0]["t"] - t0, 2), round(tr[0]["t"] - t0, 2)
    preds = br[0]["msg"].get("predictions")
    await c.wait_listening(15)
    return t <= 15, {"brief_s": b, "greet_s": t}, f"brief +{b}s ({preds}); Twin said +{t}s: {tr[0]['msg'].get('text')!r}"


async def step12(x: Ctx):
    c = x.c
    evs = [e["msg"] for e in c.msgs if e["msg"].get("type") == "agent_event"]
    parent = {}
    for m in evs:
        if m.get("event_id"):
            parent.setdefault(m["event_id"], m.get("parent_id"))

    def depth(eid, seen=()):
        p = parent.get(eid)
        if not p or eid in seen:
            return 1
        return 1 + depth(p, seen + (eid,))
    maxd = max((depth(e) for e in parent), default=0)
    agents = sorted({m.get("agent") for m in evs if m.get("agent")})
    states = {}
    for m in evs:
        states[m.get("state")] = states.get(m.get("state"), 0) + 1
    return maxd >= 2, {"max_depth": maxd, "events": len(evs)}, f"{len(evs)} agent_event, max depth {maxd}, agents={agents}, states={states}"


STEPS = {1: ("system description -> boxes+arrows", step1), 2: ("pause -> work update", step2),
         3: ("Redis, no dup auth", step3), 4: ("addressed question -> audio", step4),
         5: ("barge-in", step5), 6: ("mermaid sequence", step6), 7: ("latency chart", step7),
         8: ("login mock", step8), 9: ("topic change", step9), 10: ("not now -> hand_raised", step10),
         11: ("calendar join brief+greet", step11), 12: ("agent_event nesting", step12)}


def scrub(m: dict) -> dict:
    if m.get("type") == "audio" and m.get("data"):
        m = dict(m)
        m["data"] = f"<{len(m['data'])} b64 chars>"
    return m


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8002")
    ap.add_argument("--room", default=f"e2e-{int(time.time())}")
    ap.add_argument("--only", default="")
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--strict", action="store_true", help="exit 1 if ANY selected step fails (not just gating steps)")
    a = ap.parse_args()
    only = [int(s) for s in a.only.split(",") if s.strip()] or sorted(STEPS)
    http = re.sub(r"^ws", "http", a.url.rstrip("/"))

    c = Client(a.url, a.room)
    print(f"room={a.room} url={a.url}")
    await c.connect()
    await c.wait(lambda: c.status != "?", 5)
    x = Ctx(c, http)
    results = []
    for n in only:
        name, fn = STEPS[n]
        ts = c.t()
        try:
            ok, timing, detail = await fn(x)
        except Exception as e:  # noqa: BLE001
            ok, timing, detail = False, {}, f"script exception: {type(e).__name__}: {e}"
        dur = round(c.t() - ts, 1)
        results.append({"step": n, "name": name, "pass": bool(ok), "timing": timing, "detail": detail, "dur_s": dur})
        print(f"[{'PASS' if ok else 'FAIL'}] {n:>2} {name} ({dur}s): {detail}", flush=True)
    await c.close()

    print("\n step | result | name                          | timing")
    print("------+--------+-------------------------------+------------------------------")
    for r in results:
        print(f" {r['step']:>4} | {'PASS' if r['pass'] else 'FAIL':<6} | {r['name']:<29} | {r['timing']}")
    gating = results if a.strict else [r for r in results if r["step"] in GATING]
    code = 0 if all(r["pass"] for r in gating) else 1
    au = [e["msg"] for e in c.msgs if e["msg"].get("type") == "audio"]
    nulls = sum(1 for m in au if m.get("data") is None)
    if au and nulls:
        print(f"\nWARN: {nulls}/{len(au)} audio chunks had data=null (server TTS failed; browser falls back to speechSynthesis)")
    print(f"\n{'strict: all selected' if a.strict else 'gating steps (1-5,10-12)'} present: {[r['step'] for r in gating]} -> exit {code}; total {c.t():.0f}s")

    rep = {"room": a.room, "url": a.url, "started_ms": now_ms() - int(c.t() * 1000), "results": results,
           "sent": c.sent, "received": [{"t": e["t"], "msg": scrub(e["msg"])} for e in c.msgs]}
    p = Path(a.report)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, indent=1, default=str))
    print(f"report: {p}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
