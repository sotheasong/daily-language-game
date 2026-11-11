#!/usr/bin/env python3
"""The review page. Hear the five clips in order, then approve or reject.

The gate exists so no session goes live unheard, and a terminal table cannot
play audio or show where a pin lands, so a CLI review tool would be approval
theatre. This is a local page instead: it plays each round exactly as a player
gets it, audio first and nothing else, then reveals the answer with its
density map on demand.

Nothing here is the player's front end, which is a separate static site. This
is a curator's tool, so it is the standard library plus the Pillow rendering
that tools/preview.py already does, and it writes straight back to
data/schedule.json and data/rejections.json.

    python3 tools/review.py
    python3 tools/review.py --port 8111 --today 2026-12-01

Approving writes a hash of what you just saw. Change a clip afterwards and
the day reverts to draft on its own, so re-pulling a corpus cannot quietly
promote audio nobody listened to.
"""

import argparse
import datetime
import io
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

from pipeline import approvals, schedule  # noqa: E402

CLIP_DIR = os.path.join(REPO, ".cache", "clips")
# Rendering is optional. The maps need Pillow and the two build artifacts,
# and the page is still usable for listening without them.
try:
    import preview
    HAVE_RENDER, RENDER_ERROR = True, None
except Exception as exc:  # pragma: no cover - depends on the local install
    # Say which import failed. Reporting only "maps are off" sent me looking
    # at the two data files when the cause was Pillow not being installed.
    HAVE_RENDER, RENDER_ERROR = False, f"{type(exc).__name__}: {exc}"

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review</title>
<style>
  :root {
    --bg: #faf8f5; --fg: #1a1714; --muted: #6b635a; --line: #e0d9d0;
    --card: #fff; --accent: #8c1d10; --ok: #1d6b3f; --warn: #a8660c;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #16140f; --fg: #f0ebe4; --muted: #9a9086; --line: #332e27;
      --card: #201d17; --accent: #e8893f; --ok: #5aa87a; --warn: #d9a04c;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 24px 16px 80px; }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  .runway { color: var(--muted); font-size: .9rem; margin-bottom: 20px; }
  .runway b.fine { color: var(--ok); } .runway b.warn { color: var(--warn); }
  .runway b.fail, .runway b.dry { color: var(--accent); }
  .day { background: var(--card); border: 1px solid var(--line);
         border-radius: 10px; margin-bottom: 18px; overflow: hidden; }
  .day > header { display: flex; align-items: baseline; gap: 10px;
                  padding: 12px 16px; border-bottom: 1px solid var(--line); }
  .day h2 { font-size: 1rem; margin: 0; font-variant-numeric: tabular-nums; }
  .pill { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em;
          padding: 2px 8px; border-radius: 99px; border: 1px solid var(--line);
          color: var(--muted); }
  .pill.approved { color: var(--ok); border-color: var(--ok); }
  .pill.rejected { color: var(--accent); border-color: var(--accent); }
  .round { display: grid; grid-template-columns: 1.6rem 1fr; gap: 10px;
           padding: 12px 16px; border-bottom: 1px solid var(--line); }
  .round:last-of-type { border-bottom: 0; }
  .n { color: var(--muted); font-variant-numeric: tabular-nums; }
  audio { width: 100%; height: 34px; margin: 2px 0 6px; }
  .answer { font-size: .9rem; color: var(--muted); }
  .answer button { font: inherit; background: none; border: 0; padding: 0;
                   color: var(--accent); cursor: pointer;
                   text-decoration: underline; }
  .answer .name { color: var(--fg); font-weight: 600; }
  .answer img { display: block; max-width: 100%; height: auto; margin-top: 8px;
                border: 1px solid var(--line); border-radius: 6px; }
  .acts { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px;
          background: color-mix(in srgb, var(--card) 85%, var(--bg)); }
  button.act { font: inherit; padding: 6px 14px; border-radius: 7px;
               border: 1px solid var(--line); background: var(--card);
               color: var(--fg); cursor: pointer; }
  button.act.yes { border-color: var(--ok); color: var(--ok); }
  button.act.no { border-color: var(--accent); color: var(--accent); }
  button.act:disabled { opacity: .45; cursor: default; }
  .note { color: var(--muted); font-size: .85rem; padding: 0 16px 12px; }
  .empty { color: var(--muted); padding: 40px 0; text-align: center; }
  @media (max-width: 520px) { .round { grid-template-columns: 1fr; } .n { display: none; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>Review</h1>
  <div class="runway" id="runway"></div>
  <div id="days"></div>
</div>
<script>
const state = { days: [], runway: 0, state: "dry", render: false };

async function load() {
  const r = await fetch("/api/schedule");
  Object.assign(state, await r.json());
  draw();
}

function draw() {
  document.getElementById("runway").innerHTML =
    `<b class="${state.state}">${state.runway} approved days</b> remaining` +
    (state.state === "dry" ? " &middot; the endpoint would serve the fallback"
     : state.state === "fail" ? " &middot; a build would fail here" : "");

  const host = document.getElementById("days");
  if (!state.days.length) {
    host.innerHTML = `<p class="empty">Nothing scheduled from today.<br>
      Generate drafts with <code>python3 -m pipeline.schedule</code>.</p>`;
    return;
  }
  host.innerHTML = "";
  for (const day of state.days) host.append(dayCard(day));
}

function dayCard(day) {
  const el = document.createElement("section");
  el.className = "day";
  const head = document.createElement("header");
  head.innerHTML = `<h2>${day.date}</h2>
    <span class="pill ${day.status}">${day.status}</span>`;
  el.append(head);

  day.rounds.forEach((round, i) => el.append(roundRow(day, round, i)));

  const acts = document.createElement("div");
  acts.className = "acts";
  const yes = document.createElement("button");
  yes.className = "act yes";
  yes.textContent = day.status === "approved" ? "Approved" : "Approve this day";
  yes.disabled = day.status === "approved";
  yes.onclick = () => post("/api/approve", { date: day.date });
  const no = document.createElement("button");
  no.className = "act no";
  no.textContent = "Reject this day";
  no.onclick = () => {
    const reason = prompt("Why is this day rejected?");
    if (reason && reason.trim()) post("/api/reject", { date: day.date, reason });
  };
  acts.append(yes, no);
  el.append(acts);

  if (day.reason) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "Rejected: " + day.reason;
    el.append(note);
  }
  return el;
}

function roundRow(day, round, i) {
  const el = document.createElement("div");
  el.className = "round";
  const n = document.createElement("div");
  n.className = "n";
  n.textContent = i + 1;

  const body = document.createElement("div");
  const audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = "/clip/" + round.clip + ".opus";
  body.append(audio);

  const answer = document.createElement("div");
  answer.className = "answer";
  const reveal = document.createElement("button");
  reveal.textContent = "Reveal answer";
  reveal.onclick = () => {
    answer.innerHTML = `<span class="name">${round.name}</span>
      &middot; tier ${round.tier} &middot; <code>${round.clip}</code>`;
    if (state.render) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "Where " + round.name + " is spoken";
      img.src = "/map/" + encodeURIComponent(round.name) + ".png";
      answer.append(img);
    }
    const bad = document.createElement("button");
    bad.textContent = "Reject this clip for good";
    bad.style.marginLeft = ".5rem";
    bad.onclick = () => {
      const reason = prompt("What is wrong with this clip?\\n" +
        "The reasons become the record of what a bad clip looks like.");
      if (reason && reason.trim())
        post("/api/reject-clip", { clip: round.clip, reason });
    };
    answer.append(document.createElement("br"), bad);
  };
  answer.append(reveal);
  body.append(answer);

  el.append(n, body);
  return el;
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body) });
  if (!r.ok) { alert(await r.text()); return; }
  await load();
}

load();
</script>
</body>
</html>
"""


class Review(BaseHTTPRequestHandler):
    server_version = "review/1.0"

    # Quieter than the default, which logs every audio range request.
    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write(f"  {self.command} {self.path}\n")

    # -- helpers ---------------------------------------------------------

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/schedule":
            return self._schedule()
        if path.startswith("/clip/"):
            return self._clip(path[len("/clip/"):])
        if path.startswith("/map/"):
            return self._map(urllib.parse.unquote(path[len("/map/"):]))
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._json_body()
        except ValueError:
            return self._send(400, "not JSON", "text/plain")
        try:
            if path == "/api/approve":
                return self._decide(body["date"], "approve")
            if path == "/api/reject":
                return self._decide(body["date"], "reject", body.get("reason"))
            if path == "/api/reject-clip":
                return self._reject_clip(body["clip"], body.get("reason"))
        except (KeyError, ValueError) as exc:
            return self._send(400, str(exc), "text/plain")
        return self._send(404, "not found", "text/plain")

    def _schedule(self):
        doc = approvals.load_schedule(self.server.schedule_path)
        days = []
        for day in doc.get("days", []):
            if day["date"] < self.server.today:
                continue
            out = dict(day, status=approvals.effective_status(day))
            days.append(out)
        payload = {
            "days": days,
            "runway": approvals.runway(doc.get("days", []), self.server.today),
            "state": approvals.runway_state(
                approvals.runway(doc.get("days", []), self.server.today)),
            "render": HAVE_RENDER,
        }
        return self._send(200, json.dumps(payload))

    def _clip(self, name):
        # Join and then check, so a crafted name cannot climb out of the
        # clip directory even though this only ever listens on localhost.
        path = os.path.normpath(os.path.join(CLIP_DIR, name))
        if not path.startswith(CLIP_DIR + os.sep) or not os.path.isfile(path):
            return self._send(404, "no such clip", "text/plain")
        with open(path, "rb") as fh:
            blob = fh.read()
        return self._send(200, blob, "audio/ogg")

    def _map(self, name):
        name = name[:-4] if name.endswith(".png") else name
        if not HAVE_RENDER:
            return self._send(404, "rendering unavailable", "text/plain")
        try:
            image = self.server.render(name)
        except KeyError:
            return self._send(404, f"no footprint for {name}", "text/plain")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return self._send(200, buf.getvalue(), "image/png")

    def _decide(self, date, action, reason=None):
        doc = approvals.load_schedule(self.server.schedule_path)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds")
        found = False
        for i, day in enumerate(doc.get("days", [])):
            if day["date"] != date:
                continue
            doc["days"][i] = (approvals.approve(day, self.server.who, now)
                              if action == "approve"
                              else approvals.reject(day, self.server.who, now,
                                                    reason or ""))
            found = True
            break
        if not found:
            raise KeyError(f"no day on {date}")
        approvals.save_schedule(doc, self.server.schedule_path)
        sys.stderr.write(f"  {action}d {date}\n")
        return self._send(200, json.dumps({"ok": True}))

    def _reject_clip(self, clip, reason):
        rejections = approvals.load_rejections(self.server.rejections_path)
        rejections = approvals.reject_clip(
            rejections, clip, reason or "", datetime.date.today().isoformat())
        approvals.save_rejections(rejections, self.server.rejections_path)
        sys.stderr.write(f"  rejected clip {clip}: {reason}\n")
        return self._send(200, json.dumps({"ok": True}))


def make_renderer(size):
    """Render one entry's footprint and density, reusing tools/preview.py.

    Cached, because the basemap is tens of megabytes of GeoJSON and a session
    asks for five maps.
    """
    path = (preview.BASEMAP if os.path.exists(preview.BASEMAP)
            else preview.FALLBACK_BASEMAP)
    with open(path, encoding="utf-8") as fh:
        basemap = json.load(fh)
    with open(preview.FOOTPRINTS, encoding="utf-8") as fh:
        feats = {f["properties"]["entry"]: f for f in json.load(fh)["features"]}
    dens = {}
    if os.path.exists(preview.DENSITY):
        with open(preview.DENSITY, encoding="utf-8") as fh:
            dens = {e["entry"]: e for e in json.load(fh)}

    panel_size = (size, int(size * preview.PANEL[1] / preview.PANEL[0]))
    cache = {}

    def render(name):
        if name not in cache:
            if name not in feats:
                raise KeyError(name)
            cache[name] = preview.panel(name, feats[name], basemap,
                                        dens.get(name), True, panel_size)
        return cache[name]
    return render


def main():
    ap = argparse.ArgumentParser(
        description="Review draft sessions and approve them.")
    ap.add_argument("--port", type=int, default=8110)
    ap.add_argument("--schedule", default=approvals.DEFAULT_SCHEDULE)
    ap.add_argument("--rejections", default=approvals.DEFAULT_REJECTIONS)
    ap.add_argument("--who", default=os.environ.get("USER", "unknown"),
                    help="recorded as approved_by")
    ap.add_argument("--today", default=None,
                    help="YYYY-MM-DD; days before this are hidden")
    ap.add_argument("--map-size", type=int, default=520)
    args = ap.parse_args()

    if not os.path.exists(args.schedule):
        sys.stderr.write(
            f"no schedule at {args.schedule}. Generate drafts first:\n"
            f"    python3 -m pipeline.schedule --days 30\n")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Review)
    server.schedule_path = args.schedule
    server.rejections_path = args.rejections
    server.who = args.who
    server.today = args.today or datetime.date.today().isoformat()
    server.render = make_renderer(args.map_size) if HAVE_RENDER else None

    doc = approvals.load_schedule(args.schedule)
    for line in approvals.report(doc, approvals.load_rejections(args.rejections),
                                 server.today):
        sys.stderr.write(line + "\n")
    if not HAVE_RENDER:
        sys.stderr.write(
            f"  maps are off ({RENDER_ERROR}). They need Pillow plus\n"
            f"  data/footprints.geojson and data/density.json; listening\n"
            f"  works without them.\n")
    sys.stderr.write(
        f"\n  http://127.0.0.1:{args.port}  approving as {server.who!r}\n"
        f"  approvals go to {args.schedule}\n  ctrl-c to stop\n\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
