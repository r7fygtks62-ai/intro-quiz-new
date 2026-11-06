#!/usr/bin/env python3
"""イントロクイズ用 早押しサーバー（部屋ごとにパスワードで分けられる版・標準ライブラリのみ）

使い方:  python quiz_server.py
  出題者:  http://localhost:8000/host     （部屋を作る）
  回答者:  http://（このPCのアドレス）:8000/  （部屋のパスワードを入れる or QRコード）
公開サーバー(Render等)では環境変数 HOST_KEY を設定すると、部屋を作るのに合言葉が必要になります。
"""
import json, mimetypes, os, queue, random, secrets, socket, sys, threading, time, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".ico": "image/x-icon", ".m4a": "audio/mp4", ".wav": "audio/wav"}

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
HOST_KEY = os.environ.get("HOST_KEY", "")   # 部屋を作るための合言葉（空なら誰でも作れる）
MAX_ROOMS = 60
ROOM_IDLE_SEC = 8 * 3600
PRESENCE_TTL = 20

def load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)

TEAMS = load("teams.json")
try: SONGS = load("songs.json")
except Exception: SONGS = []
MEDIA = {}            # 「.song」ファイルから読み込んだ音声・画像（メモリ上）

def load_packed():
    import base64, glob
    pats = [os.path.join(ROOT, "*.song"), os.path.join(ROOT, "public", "*.song"), os.path.join(ROOT, "songs", "*.song")]
    seen = set()
    for fp in sorted(sum((glob.glob(p) for p in pats), [])):
        try:
            with open(fp, encoding="utf-8") as f: d = json.load(f)
            key = (d["title"], d.get("artist", ""))
            if key in seen: continue
            seen.add(key)
            n = len(SONGS)
            base = f"/m/{n}/"
            MEDIA[base + "intro.mp3"] = (base64.b64decode(d["intro"]), "audio/mpeg")
            MEDIA[base + "chorus.mp3"] = (base64.b64decode(d["chorus"]), "audio/mpeg")
            MEDIA[base + "cover.jpg"] = (base64.b64decode(d["cover"]), "image/jpeg")
            SONGS.append({"title": d["title"], "artist": d.get("artist", ""), "intro": base + "intro.mp3",
                          "chorus": base + "chorus.mp3", "cover": base + "cover.jpg"})
        except Exception as e:
            print("曲ファイルを読めませんでした:", fp, e)
load_packed()
ALL_IDS = [t["id"] for t in TEAMS]

lock = threading.RLock()
rooms = {}            # 部屋番号 -> Room
START_DELAY = 1.2     # 「デデン」の間、押せない秒数
PENALTY = 0.5         # お手付き（不正解）の減点
fails = {}            # IP -> [失敗した時刻...]

def norm(s):
    return unicodedata.normalize("NFKC", str(s or "")).strip().casefold()

class Room:
    def __init__(self, pw, hostpw, n, cap, shuffle):
        self.pw, self.hostpw = pw, hostpw
        self.n, self.cap, self.shuffle = n, cap, shuffle
        self.teams = ALL_IDS[:n]
        self.presence = {}     # 端末ID -> {"team","t"}
        self.clients = []      # {"q","host"}
        self.touched = time.monotonic()
        self.new_game()

    def new_game(self):
        idx = list(range(len(SONGS)))
        if self.shuffle: random.shuffle(idx)
        self.songs = idx
        self.rounds = {}                       # 何問目 -> {"events":[{team,type,d}], "done":bool}
        self.adj = {t: 0.0 for t in self.teams} # 出題者による手動の得点修正
        self.rev = getattr(self, "rev", 0)
        self.run = getattr(self, "run", 0)
        self.pos = 0
        self.clear_round("ready")

    def clear_round(self, phase="ready"):
        self.phase, self.order, self.cur, self.judged, self.wrong, self.winner, self.t0 = phase, [], 0, False, [], None, 0.0
        self.t_open = 0.0

    def scores(self):
        sc = {t: self.adj[t] for t in self.teams}
        for rd in self.rounds.values():
            for e in rd["events"]: sc[e["team"]] += e["d"]
        return sc

    def results(self):
        sc = self.scores(); st = {t: {"correct": 0, "wrong": 0} for t in self.teams}
        hist = []
        for pos in sorted(self.rounds):
            rd = self.rounds[pos]
            for e in rd["events"]: st[e["team"]][e["type"]] += 1
            sg = SONGS[self.songs[pos]] if pos < len(self.songs) else {"title": "", "artist": ""}
            hist.append({"no": pos + 1, "title": sg["title"], "artist": sg["artist"], "events": rd["events"], "skipped": rd.get("skipped", False)})
        return {"teams": self.teams, "scores": sc, "stats": st, "history": hist, "adj": self.adj,
                "phase": self.phase, "pos": self.pos, "total": len(self.songs), "room": self.pw}

    def view(self, host=False):
        counts = {}
        for p in self.presence.values():
            counts[p["team"]] = counts.get(p["team"], 0) + 1
        s = {"rev": self.rev, "run": self.run, "phase": self.phase, "order": self.order, "cur": self.cur,
             "judged": self.judged, "wrong": self.wrong, "winner": self.winner, "pos": self.pos,
             "total": len(self.songs), "teams": self.teams, "cap": self.cap, "joined": counts,
             "delay": int(START_DELAY * 1000)}
        if host and self.pos < len(self.songs): s["song"] = self.songs[self.pos]
        return s

    def broadcast(self):
        self.rev += 1
        self.touched = time.monotonic()
        cache = {}
        for c in list(self.clients):
            k = c["host"]
            if k not in cache: cache[k] = json.dumps(self.view(k), ensure_ascii=False)
            c["q"].put(cache[k])

    def close(self):
        for c in list(self.clients): c["q"].put(json.dumps({"closed": True}))

def get_room(pw):
    return rooms.get(norm(pw))

def host_room(pw, hostpw):
    r = get_room(pw)
    return r if r and norm(hostpw) == norm(r.hostpw) else None

def blocked(ip):
    now = time.time()
    fails[ip] = [t for t in fails.get(ip, []) if now - t < 300]
    return len(fails[ip]) >= 12

def fail(ip):
    fails.setdefault(ip, []).append(time.time())

def create_room(b):
    if HOST_KEY and str(b.get("key", "")) != HOST_KEY:
        return {"ok": False, "reason": "key"}
    if not SONGS: return {"ok": False, "reason": "nosongs"}
    try:
        n = max(2, min(8, int(b.get("n", 8)))); cap = max(1, min(30, int(b.get("cap", 1))))
    except Exception:
        return {"ok": False, "reason": "bad"}
    with lock:
        if len(rooms) >= MAX_ROOMS: return {"ok": False, "reason": "full"}
        for _ in range(200):
            pw = str(random.randint(1000, 9999))
            if pw not in rooms: break
        else:
            return {"ok": False, "reason": "full"}
        hp = secrets.token_urlsafe(12)
        rooms[pw] = Room(pw, hp, n, cap, bool(b.get("shuffle", True)))
    return {"ok": True, "room": pw, "hostpw": hp}

def host_action(r, b):
    a, ph = b.get("action"), r.phase
    with lock:
        if "from" in b and b["from"] != r.pos: return False
        rd = r.rounds.get(r.pos)
        if a == "start" and ph in ("ready", "playing", "buzzed"):
            r.clear_round("playing"); r.t_open = time.monotonic() + START_DELAY; r.t0 = r.t_open; r.run += 1
            r.rounds[r.pos] = {"events": [], "done": False}
        elif a == "wrong" and ph == "buzzed" and r.cur < len(r.order) and rd:
            t = r.order[r.cur]["team"]
            rd["events"].append({"team": t, "type": "wrong", "d": -PENALTY})
            r.wrong.append(t); r.judged = True; r.cur += 1
        elif a == "correct" and ph == "buzzed" and r.cur < len(r.order) and rd:
            r.winner = r.order[r.cur]["team"]
            rd["events"].append({"team": r.winner, "type": "correct", "d": 1}); rd["done"] = True; r.phase = "answer"
        elif a == "reveal" and ph in ("playing", "buzzed"):
            if rd: rd["done"] = True
            r.phase = "answer"
        elif a == "reset" and ph in ("playing", "buzzed", "answer"):
            r.rounds.pop(r.pos, None); r.clear_round("ready")
        elif a == "next" and ph in ("ready", "answer"):
            if ph == "ready": r.rounds[r.pos] = {"events": [], "done": True, "skipped": True}
            if r.pos + 1 >= len(r.songs): r.clear_round("finished")
            else: r.pos += 1; r.clear_round("ready")
        elif a == "prev" and ph == "ready" and r.pos > 0:
            r.pos -= 1; r.clear_round("ready")
        elif a == "finish" and ph != "finished":
            r.clear_round("finished")
        elif a == "restart":
            r.new_game()
        elif a == "adjust" and b.get("team") in r.teams:
            r.adj[b["team"]] += 0.5 if int(b.get("delta", 0)) > 0 else -0.5
        elif a == "close":
            r.close(); rooms.pop(norm(r.pw), None); return True
        else:
            return False
        r.broadcast()
    return True

def ping(r, pid, team):
    if team not in r.teams or not pid: return {"ok": False, "reason": "team"}
    with lock:
        old = r.presence.get(pid)
        if not old or old["team"] != team:
            n = sum(1 for p in r.presence.values() if p["team"] == team)
            if n >= r.cap: return {"ok": False, "reason": "full"}
        r.presence[pid] = {"team": team, "t": time.monotonic()}
        if not old or old["team"] != team: r.broadcast()
    return {"ok": True}

def leave(r, pid):
    with lock:
        if r.presence.pop(pid, None) is not None: r.broadcast()

def buzz(r, team):
    if team not in r.teams: return {"ok": False}
    with lock:
        now = time.monotonic()
        if r.phase == "playing" and now < r.t_open: return {"ok": False, "reason": "early"}
        if r.phase == "playing": r.phase = "buzzed"
        elif not (r.phase == "buzzed" and not r.judged): return {"ok": False, "reason": "closed"}
        if any(o["team"] == team for o in r.order): return {"ok": True, "dup": True}
        if not r.order: r.t0 = now
        r.order.append({"team": team, "ms": int((now - r.t0) * 1000)})
        r.broadcast()
        return {"ok": True, "rank": len(r.order)}

def reaper():
    while True:
        time.sleep(3)
        with lock:
            now = time.monotonic()
            for k, r in list(rooms.items()):
                dead = [i for i, p in r.presence.items() if now - p["t"] > PRESENCE_TTL]
                for i in dead: del r.presence[i]
                if dead: r.broadcast()
                if now - r.touched > ROOM_IDLE_SEC and not r.clients:
                    del rooms[k]

def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1)); ips.append(s.getsockname()[0]); s.close()
    except Exception: pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips: ips.append(ip)
    except Exception: pass
    return ips

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def send_json(self, obj, code=200, cors=False):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        if cors: self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: b = json.loads(self.rfile.read(n) or b"{}")
        except Exception: b = {}
        path = urlparse(self.path).path
        room = b.get("room", "")
        if path == "/api/create":
            if blocked(self.ip()): return self.send_json({"ok": False, "reason": "wait"}, 429)
            res = create_room(b)
            if res.get("reason") == "key": fail(self.ip())
            return self.send_json(res)
        if path == "/api/host_login":
            if blocked(self.ip()): return self.send_json({"ok": False, "reason": "wait"}, 429)
            with lock: r = host_room(room, b.get("hostpw", ""))
            if not r: fail(self.ip())
            return self.send_json({"ok": bool(r), "n": r.n if r else 0})
        if path == "/api/host":
            with lock:
                r = host_room(room, b.get("hostpw", ""))
                if not r: return self.send_json({"ok": False, "reason": "auth"}, 403)
                return self.send_json({"ok": host_action(r, b)})
        with lock: r = get_room(room)
        if not r: return self.send_json({"ok": False, "reason": "noroom"})
        if path == "/api/buzz": return self.send_json(buzz(r, str(b.get("team", ""))))
        if path == "/api/ping": return self.send_json(ping(r, str(b.get("id", "")), str(b.get("team", ""))))
        if path == "/api/leave":
            leave(r, str(b.get("id", ""))); return self.send_json({"ok": True})
        self.send_json({"ok": False}, 404)

    def do_GET(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        g = lambda k: (q.get(k) or [""])[0]
        if path == "/api/config":
            port = self.server.server_address[1]
            return self.send_json({"teams": TEAMS, "needKey": bool(HOST_KEY),
                                   "urls": [f"http://{ip}:{port}/" for ip in lan_ips()]}, cors=True)
        if path == "/api/room":     # 参加者がパスワードを入れたときの確認
            with lock: r = get_room(g("room"))
            return self.send_json({"ok": bool(r)})
        if path == "/api/songs":    # 曲名などは出題者(または作成キー)にだけ見せる
            with lock: okh = host_room(g("room"), g("hostpw")) is not None
            okk = (g("key") == HOST_KEY) if HOST_KEY else (g("admin") == "1")
            if not okh and not okk:
                return self.send_json({"ok": False}, 403, cors=True)
            return self.send_json({"ok": True, "songs": SONGS}, cors=True)
        if path == "/api/results":
            with lock:
                r = host_room(g("room"), g("hostpw"))
                if not r: return self.send_json({"ok": False}, 403)
                return self.send_json({"ok": True, **r.results()})
        if path == "/api/events":
            return self.events(g("room"), g("hostpw"))
        if path == "/": path = "/index.html"
        if path == "/play": path = "/play.html"
        if path == "/host": path = "/host.html"
        if path == "/results": path = "/results.html"
        self.static(path)

    def events(self, room, hostpw):
        with lock:
            r = get_room(room)
            is_host = bool(r) and norm(hostpw) == norm(r.hostpw)
            if not r: return self.send_json({"closed": True}, 404)
            c = {"q": queue.Queue(), "host": is_host}
            r.clients.append(c)
            c["q"].put(json.dumps(r.view(is_host), ensure_ascii=False))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                try:
                    data = c["q"].get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    if '"closed"' in data: break
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with lock:
                if c in r.clients: r.clients.remove(c)
            self.close_connection = True

    def find_static(self, path):
        rel = os.path.normpath(path.lstrip("/\\"))
        base = os.path.basename(rel)
        top = rel.split(os.sep)[0]
        cands = [os.path.join("public", rel)]
        if top in ("teams", "songs"): cands.append(rel)
        if os.path.splitext(base)[1].lower() in MIME and base != "":
            cands += [os.path.join("public", base), base, os.path.join("songs", base), os.path.join("teams", base)]
        for c in cands:
            fp = os.path.abspath(os.path.join(ROOT, c))
            if os.path.commonpath([ROOT, fp]) == ROOT and os.path.isfile(fp):
                return fp
        return None

    def serve_bytes(self, data, ctype):
        size = len(data); rng = self.headers.get("Range"); start, end, code = 0, size - 1, 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0); end = int(b) if b else size - 1; code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers(); self.wfile.write(data[start:end + 1])

    def static(self, path):
        if path in MEDIA:
            return self.serve_bytes(*MEDIA[path])
        fp = self.find_static(path)
        if fp and (os.path.basename(fp) in ("quiz_server.py", "songs.json") or fp.endswith(".song")): fp = None
        if fp:
            ctype = MIME.get(os.path.splitext(fp)[1].lower()) or mimetypes.guess_type(fp)[0] or "application/octet-stream"
            size = os.path.getsize(fp)
            rng = self.headers.get("Range")
            start, end, code = 0, size - 1, 200
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a or 0); end = int(b) if b else size - 1; code = 206
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start); self.wfile.write(f.read(end - start + 1))
            return
        self.send_json({"error": "not found"}, 404)

class S(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)): return
        super().handle_error(request, client_address)

if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    threading.Thread(target=reaper, daemon=True).start()
    srv = S(("0.0.0.0", PORT), H)
    print("=" * 56)
    print(" イントロクイズ 早押しサーバー 起動中")
    print(f"  出題者(このPC):  http://localhost:{PORT}/host   ← 部屋を作る")
    for ip in lan_ips():
        print(f"  回答者(スマホ):  http://{ip}:{PORT}/")
    print(" 終了は Ctrl+C")
    print("=" * 56)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
