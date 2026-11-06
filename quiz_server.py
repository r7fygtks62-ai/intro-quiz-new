#!/usr/bin/env python3
"""イントロクイズ用 早押しサーバー（標準ライブラリのみ・追加インストール不要）

使い方:  python server.py   （Windowsは start.bat をダブルクリック）
出題者:  http://localhost:8000/host
回答者:  表示されるURL（または出題者画面のQRコード）をスマホで開く
"""
import json, mimetypes, os, queue, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Windowsのレジストリ設定に左右されないよう拡張子を固定
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".ico": "image/x-icon", ".m4a": "audio/mp4", ".wav": "audio/wav"}

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
HOST_KEY = os.environ.get("HOST_KEY", "")   # 公開サーバーで出題者操作を守る合言葉（空なら無し）

def load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)

TEAMS = load("teams.json")
SONGS = load("songs.json")
TEAM_IDS = [t["id"] for t in TEAMS]

lock = threading.Lock()
clients = []          # [{"q": Queue, "team": str|None, "role": str}]
state = {
    "rev": 0,
    "run": 0,           # スタートを押した回数（音声の頭出し用）
    "song": 0,
    "phase": "ready",   # ready / playing / buzzed / answer
    "order": [],        # [{"team": "A", "ms": 0}]  押した順（サーバー受信順）
    "cur": 0,           # order の何番目が回答中か
    "judged": False,    # 不正解が1回でも押されたら順位を確定
    "wrong": [],        # 不正解になったチーム
    "t0": 0.0,
}

def public_state():
    counts = {}
    for c in clients:
        if c["team"]:
            counts[c["team"]] = counts.get(c["team"], 0) + 1
    s = {k: v for k, v in state.items() if k != "t0"}
    s["joined"] = counts
    return s

def broadcast():
    state["rev"] += 1
    data = json.dumps(public_state(), ensure_ascii=False)
    for c in list(clients):
        c["q"].put(data)

def host_action(body):
    a = body.get("action")
    with lock:
        ph = state["phase"]
        if a == "start" and ph in ("ready", "playing", "buzzed", "answer"):
            state.update(phase="playing", order=[], cur=0, judged=False, wrong=[], t0=time.monotonic(), run=state["run"] + 1)
        elif a == "wrong" and ph == "buzzed" and state["cur"] < len(state["order"]):
            state["wrong"].append(state["order"][state["cur"]]["team"])
            state["judged"] = True
            state["cur"] += 1
        elif a in ("correct", "reveal") and ph in ("playing", "buzzed"):
            state["phase"] = "answer"
        elif a == "next":
            state.update(song=(state["song"] + 1) % len(SONGS), phase="ready", order=[], cur=0, judged=False, wrong=[])
        elif a == "prev":
            state.update(song=(state["song"] - 1) % len(SONGS), phase="ready", order=[], cur=0, judged=False, wrong=[])
        elif a == "goto":
            i = int(body.get("i", 0))
            if 0 <= i < len(SONGS):
                state.update(song=i, phase="ready", order=[], cur=0, judged=False, wrong=[])
        elif a == "reset":
            state.update(phase="ready", order=[], cur=0, judged=False, wrong=[])
        else:
            return False
        broadcast()
    return True

def buzz(team):
    if team not in TEAM_IDS:
        return {"ok": False}
    with lock:
        now = time.monotonic()
        ph = state["phase"]
        if ph == "playing":
            state["phase"] = "buzzed"
        elif not (ph == "buzzed" and not state["judged"]):
            return {"ok": False, "reason": "closed"}
        if any(o["team"] == team for o in state["order"]):
            return {"ok": True, "dup": True}
        if not state["order"]:
            state["t0"] = now
        state["order"].append({"team": team, "ms": int((now - state["t0"]) * 1000)})
        broadcast()
        return {"ok": True, "rank": len(state["order"])}

def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

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
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        path = urlparse(self.path).path
        if path == "/api/buzz":
            self.send_json(buzz(str(body.get("team", ""))))
        elif path == "/api/host":
            if HOST_KEY and self.headers.get("X-Host-Key", "") != HOST_KEY:
                return self.send_json({"ok": False, "reason": "key"}, 403)
            self.send_json({"ok": host_action(body)})
        else:
            self.send_json({"ok": False}, 404)

    def do_GET(self):
        u = urlparse(self.path); path = u.path
        if path == "/api/config":
            port = self.server.server_address[1]
            return self.send_json({"teams": TEAMS, "songs": SONGS, "needKey": bool(HOST_KEY),
                                   "urls": [f"http://{ip}:{port}/" for ip in lan_ips()]}, cors=True)
        if path == "/api/state":
            with lock: return self.send_json(public_state())
        if path == "/api/events":
            return self.events(parse_qs(u.query))
        if path in ("/", "/play"): path = "/play.html"
        if path == "/host": path = "/host.html"
        self.static(path)

    def events(self, q):
        team = (q.get("team") or [None])[0]
        c = {"q": queue.Queue(), "team": team if team in TEAM_IDS else None}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        with lock:
            clients.append(c)
            c["q"].put(json.dumps(public_state(), ensure_ascii=False))
            if c["team"]: broadcast()
        try:
            while True:
                try:
                    data = c["q"].get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with lock:
                if c in clients: clients.remove(c)
                broadcast()
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

    def static(self, path):
        fp = self.find_static(path)
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
        # スマホが画面を閉じた・切り替えたときの切断エラーは無視する
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)

if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    srv = S(("0.0.0.0", PORT), H)
    print("=" * 56)
    print(" イントロクイズ 早押しサーバー 起動中")
    print(f"  出題者(このPC):  http://localhost:{PORT}/host")
    for ip in lan_ips():
        print(f"  回答者(スマホ):  http://{ip}:{PORT}/")
    print(" 終了は Ctrl+C")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
