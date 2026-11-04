# Mr Ai Xenon - Free Fire Like API (Flask) - HARDENED
# Required env: ADMIN_KEY (12+ chars, A-Z 0-9 _ -), FIREBASE_SECRET (unless USE_LOCAL=1)
from dotenv import load_dotenv
load_dotenv()
import os, re, json, time, hmac, hashlib, secrets, string, threading, socket, ipaddress, random
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, parse_qs, unquote
import requests
from flask import Flask, request, jsonify, make_response, Response

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # tiny bodies only

KEY_RE = re.compile(r"^[A-Z0-9_-]{3,64}$")
ROOT = "xenon_web"
SERVERS = {"bd"}  # allowed server_name values (whitelist)

# ------------------------------------------------------------------ config (no weak defaults)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip().upper()
if len(ADMIN_KEY) < 12 or not KEY_RE.match(ADMIN_KEY):
    raise RuntimeError("Set env ADMIN_KEY: 12-64 chars, only A-Z 0-9 _ -  (use a long random one)")
DEV = os.environ.get("DEV_TAG", "@Jihad_69xi")
FB_URL = os.environ.get("FIREBASE_URL", "https://mr-ai-xenon-default-rtdb.firebaseio.com").rstrip("/")
FB_SECRET = os.environ.get("FIREBASE_SECRET", "")
USE_LOCAL = os.environ.get("USE_LOCAL", "0") == "1"
if not USE_LOCAL and not FB_SECRET:
    raise RuntimeError("Set env FIREBASE_SECRET and lock Firebase rules (.read/.write = false)")
UP_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "240"))
ALLOW_PRIVATE_UPSTREAM = os.environ.get("ALLOW_PRIVATE_UPSTREAM", "0") == "1"
TOKEN_TTL = 2 * 3600
TOKEN_SECRET = hashlib.sha256(("tok|" + ADMIN_KEY + "|" + FB_SECRET).encode()).digest()


# ------------------------------------------------------------------ vercel path fix (whitelisted)
def _allowed_path(p):
    return p == "/" or p in ("/like", "/limit", "/health") or p.startswith("/api/admin/") or p == "/api/login"


class FixVercelPath:
    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        p = environ.get("PATH_INFO", "")
        if p in ("/api/index", "/api/index.py"):
            orig = None
            qp = parse_qs(environ.get("QUERY_STRING", "")).get("_p")
            if qp and _allowed_path(qp[0]):
                orig = qp[0]
            m = environ.get("HTTP_X_NOW_ROUTE_MATCHES", "")
            if orig is None and m:
                g = parse_qs(m, keep_blank_values=True).get("1")
                if g is not None:
                    c = "/" + unquote(g[0]).lstrip("/")
                    if _allowed_path(c):
                        orig = c
            if orig is None:
                q = parse_qs(environ.get("QUERY_STRING", ""))
                orig = "/like" if "uid" in q else ("/limit" if "key" in q else "/")
            environ["PATH_INFO"] = orig
        return self.wsgi(environ, start_response)


app.wsgi_app = FixVercelPath(app.wsgi_app)


# ------------------------------------------------------------------ storage
class FirebaseStore:
    def _u(self, path):
        return f"{FB_URL}/{path}.json"

    def _p(self):
        return {"auth": FB_SECRET} if FB_SECRET else {}

    def get(self, path):
        r = requests.get(self._u(path), params=self._p(), timeout=12)
        r.raise_for_status()
        return r.json()

    def get_etag(self, path):
        r = requests.get(self._u(path), params=self._p(), headers={"X-Firebase-ETag": "true"}, timeout=12)
        r.raise_for_status()
        return r.json(), r.headers.get("ETag")

    def put_if(self, path, val, etag):
        """Atomic compare-and-set. False if someone else changed it first."""
        if not etag:
            raise requests.RequestException("no etag")
        r = requests.put(self._u(path), params=self._p(), json=val, headers={"if-match": etag}, timeout=12)
        if r.status_code == 412:
            return False
        r.raise_for_status()
        return True

    def put(self, path, val):
        requests.put(self._u(path), params=self._p(), json=val, timeout=12).raise_for_status()

    def patch(self, path, val):
        requests.patch(self._u(path), params=self._p(), json=val, timeout=12).raise_for_status()

    def delete(self, path):
        requests.delete(self._u(path), params=self._p(), timeout=12).raise_for_status()

    def incr(self, path, field, n=1):
        self.patch(path, {field: {".sv": {"increment": n}}})


class LocalStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.file = os.environ.get("DATA_FILE", os.path.join(BASE, "data.json"))
        try:
            with open(self.file) as f:
                self.d = json.load(f)
        except Exception:
            self.d = {}

    def _save(self):
        try:
            with open(self.file, "w") as f:
                json.dump(self.d, f)
        except Exception:
            pass

    def _parts(self, path):
        return [p for p in path.split("/") if p]

    def get(self, path):
        with self.lock:
            n = self.d
            for p in self._parts(path):
                if not isinstance(n, dict) or p not in n:
                    return None
                n = n[p]
            return json.loads(json.dumps(n))

    def get_etag(self, path):
        with self.lock:
            v = self.get(path)
            return v, json.dumps(v, sort_keys=True)

    def put_if(self, path, val, etag):
        with self.lock:
            if json.dumps(self.get(path), sort_keys=True) != etag:
                return False
            self.put(path, val)
            return True

    def put(self, path, val):
        with self.lock:
            parts, n = self._parts(path), self.d
            for p in parts[:-1]:
                n = n.setdefault(p, {})
            n[parts[-1]] = val
            self._save()

    def patch(self, path, val):
        with self.lock:
            cur = self.get(path) or {}
            cur.update(val)
            self.put(path, cur)

    def delete(self, path):
        with self.lock:
            parts, n = self._parts(path), self.d
            for p in parts[:-1]:
                n = n.get(p, {})
            n.pop(parts[-1], None)
            self._save()

    def incr(self, path, field, n=1):
        with self.lock:
            cur = self.get(path) or {}
            cur[field] = int(cur.get(field, 0) or 0) + n
            self.put(path, cur)


store = LocalStore() if USE_LOCAL else FirebaseStore()


# ------------------------------------------------------------------ security helpers
def client_ip():
    ip = request.headers.get("X-Vercel-Forwarded-For") or ""
    if not ip:
        xff = request.headers.get("X-Forwarded-For", "")
        ip = xff.split(",")[-1].strip() if xff else ""   # rightmost = added by our own proxy
    return ip or request.remote_addr or "?"


_hits = defaultdict(deque)
_rl_lock = threading.Lock()


def rl_over(bucket, limit, window):
    now, k = time.time(), (bucket, client_ip())
    with _rl_lock:
        q = _hits[k]
        while q and q[0] <= now - window:
            q.popleft()
        if len(_hits) > 20000:                      # memory guard
            for kk in [x for x, qq in _hits.items() if not qq][:5000]:
                _hits.pop(kk, None)
        return len(q) >= limit


def rl_add(bucket):
    with _rl_lock:
        _hits[(bucket, client_ip())].append(time.time())


def too_many():
    return jsonify(ok=False, error="Too many requests. Try again later."), 429


def make_token():
    exp = str(int(time.time()) + TOKEN_TTL)
    return exp + "." + hmac.new(TOKEN_SECRET, exp.encode(), hashlib.sha256).hexdigest()


def check_token(t):
    try:
        exp, sig = (t or "").split(".", 1)
        if int(exp) < time.time():
            return False
        good = hmac.new(TOKEN_SECRET, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode(), good.encode())
    except Exception:
        return False


def norm(k):
    return (k if isinstance(k, str) else "").strip().upper()


def is_admin(k):
    return hmac.compare_digest(norm(k).encode(), ADMIN_KEY.encode())


def body():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def need_admin(fn):
    def w(*a, **kw):
        if not check_token(request.headers.get("X-Admin-Token", "")):
            return jsonify(ok=False, error="Unauthorized"), 401
        try:
            return fn(*a, **kw)
        except requests.RequestException:
            return jsonify(ok=False, error="Database error"), 500
    w.__name__ = fn.__name__
    return w


def url_problem(url):
    """Block non-http(s) and private/internal hosts (SSRF)."""
    try:
        sp = urlsplit(re.sub(r"[{}]", "", url))
        if sp.scheme not in ("http", "https") or not sp.hostname:
            return "Invalid URL"
        if ALLOW_PRIVATE_UPSTREAM:
            return None
        for i in socket.getaddrinfo(sp.hostname, None):
            ip = ipaddress.ip_address(i[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return "Private/internal address not allowed"
    except OSError:
        return "Host not resolvable"
    except Exception:
        return "Invalid URL"
    return None


# ------------------------------------------------------------------ data helpers
def get_key(k):
    v = store.get(f"{ROOT}/keys/{k}")
    return v if isinstance(v, dict) else None


def main_url():
    v = store.get(f"{ROOT}/config/main_url")
    return v if isinstance(v, str) else ""


def to_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def reserve(k):
    """Atomically take 1 use BEFORE calling upstream (stops parallel-request limit bypass).
    Returns (error_or_None, original_record)."""
    path = f"{ROOT}/keys/{k}"
    for _ in range(8):
        v, etag = store.get_etag(path)
        if not isinstance(v, dict):
            return "INVALID KEY", None
        if v.get("active", True) is False:
            return "KEY DISABLED", None
        if to_int(v.get("used")) >= to_int(v.get("limit")):
            return "LIMIT FINISHED - contact owner for more limit", None
        nv = dict(v)
        nv["used"] = to_int(v.get("used")) + 1
        if store.put_if(path, nv, etag):
            return None, v
        time.sleep(random.uniform(0.03, 0.15))
    return "SERVER BUSY - try again", None


def refund(k):
    try:
        store.incr(f"{ROOT}/keys/{k}", "used", -1)
    except Exception:
        app.logger.warning("refund failed")


UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
      "Accept": "application/json, text/plain, */*"}


def build_upstream(url, uid, server):
    url = (url or "").strip().strip('"').strip("'").replace(" ", "")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    low = url.lower()
    if any(p in low for p in ("{uid}", "{server}", "{server_name}", "{region}")):
        u = re.sub(r"\{uid\}", uid, url, flags=re.I)
        u = re.sub(r"\{(server_name|server|region)\}", server, u, flags=re.I)
        params = None
    else:
        sp = urlsplit(url)
        keep = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
                if k.lower() not in ("uid", "server_name")]
        u = urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(keep), ""))
        params = {"uid": uid, "server_name": server}
    last = None
    for noproxy in (False, True):          # TLS verification is ALWAYS on
        for _ in range(2):
            try:
                ses = requests.Session()
                if noproxy:
                    ses.trust_env = False
                return ses.get(u, params=params, headers=UA, timeout=(10, UP_TIMEOUT), allow_redirects=True)
            except requests.ReadTimeout:
                raise
            except requests.RequestException as e:
                last = e
                time.sleep(0.4)
    raise last


def parse_json(r):
    try:
        return r.json()
    except ValueError:
        t = (r.text or "").strip().lstrip("\ufeff")
        i = min([x for x in (t.find("{"), t.find("[")) if x >= 0], default=-1)
        if i < 0:
            raise
        return json.JSONDecoder().raw_decode(t[i:])[0]


DEV_KEYS = ("dev", "developer", "owner", "credit", "credits", "poweredby", "madeby", "by")
# upstream fields that must never reach the client (could expose main API key / url)
SENSITIVE = ("key", "apikey", "token", "jwt", "secret", "password", "authorization", "url", "api", "mainapi")


def _nk(k):
    return str(k).lower().replace("_", "").replace(" ", "").replace("-", "")


def _kind(k):
    n = _nk(k)
    if "token" in n:
        return None
    if "remain" in n or n in ("left", "keyleft", "requestsleft"):
        return "remaining"
    if "used" in n or n in ("usage", "keyusage"):
        return "used"
    if "limit" in n or "quota" in n or n in ("totalrequests", "maxrequests", "totalkeyrequests"):
        return "limit"
    return None


def _fit(old, num, other=None):
    if isinstance(old, bool):
        return old
    if isinstance(old, int):
        return num
    if isinstance(old, float):
        return float(num)
    if isinstance(old, str):
        if "/" in old and other is not None:
            return f"{other[0]}/{other[1]}"
        if re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", old):
            return str(num)
        if re.search(r"\d", old):
            return re.sub(r"\d+", str(num), old, count=1)
        return str(num)
    return num


def rewrite(obj, vals):
    rem, used, lim = vals["remaining"], vals["used"], vals["limit"]
    if isinstance(obj, list):
        return [rewrite(x, vals) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out, has_dev = {}, False
    for k, v in obj.items():
        if _nk(k) in SENSITIVE:
            continue
        if isinstance(v, (dict, list)):
            out[k] = rewrite(v, vals)
            continue
        if _nk(k) in DEV_KEYS:
            out[k] = DEV
            has_dev = True
            continue
        kind = _kind(k)
        if kind == "remaining":
            out[k] = _fit(v, rem, (rem, lim))
        elif kind == "used":
            out[k] = _fit(v, used, (used, lim))
        elif kind == "limit":
            out[k] = _fit(v, lim, (rem, lim))
        else:
            out[k] = v
    if not has_dev and obj is not None and vals.get("top") is obj:
        out["Dev"] = DEV
    return out


def clean_dev(data, vals):
    vals["top"] = data[0] if isinstance(data, list) and data else data
    res = rewrite(data, vals)
    top = res[0] if isinstance(res, list) and res else res
    if isinstance(top, dict) and not any(_nk(k) in DEV_KEYS for k in top.keys()):
        top["Dev"] = DEV
    return res


def json_out(obj):
    return Response(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), mimetype="application/json")


def likes_given(d):
    if not isinstance(d, dict):
        return None
    g = d.get("LikesGivenByAPI")
    if g is not None:
        return to_int(g, 0)
    try:
        if d.get("LikesafterCommand") is not None and d.get("LikesbeforeCommand") is not None:
            return max(to_int(d["LikesafterCommand"]) - to_int(d["LikesbeforeCommand"]), 0)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ pages / headers
def _page():
    for d in (BASE, os.getcwd(), "/var/task", os.path.join(BASE, "..")):
        try:
            with open(os.path.join(d, "index.html"), "rb") as f:
                return f.read()
        except Exception:
            continue
    return b"index.html missing"


@app.route("/")
@app.route("/index.html")
def home():
    r = make_response(_page())
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = CSP
    return resp


@app.errorhandler(404)
def not_found(e):
    p = request.path
    if p.startswith(("/api", "/like", "/limit")):
        return jsonify({"status": 0, "error": "NOT FOUND", "Dev": DEV}), 404
    return home()


@app.errorhandler(413)
def too_big(e):
    return jsonify(ok=False, error="Too large"), 413


@app.errorhandler(500)
def server_err(e):
    return jsonify(ok=False, error="Server error"), 500


@app.route("/health")
def health():
    return jsonify(ok=True)


# ------------------------------------------------------------------ public api
@app.route("/api/login", methods=["POST"])
def login():
    """One endpoint for both admin and user keys. Wrong keys are rate-limited per IP."""
    if rl_over("loginfail", 10, 600):
        return too_many()
    k = norm(body().get("key"))
    if KEY_RE.match(k):
        if is_admin(k):
            return jsonify(ok=True, role="admin", token=make_token())
        try:
            v = get_key(k)
        except requests.RequestException:
            return jsonify(ok=False, error="db"), 500
        if v:
            return jsonify(ok=True, role="user", used=to_int(v.get("used")), limit=to_int(v.get("limit")),
                           active=v.get("active", True) is not False)
    rl_add("loginfail")
    return jsonify(ok=False), 401


@app.route("/limit")
def limit():
    if rl_over("limit", 120, 60):
        return too_many()
    rl_add("limit")
    if rl_over("limitfail", 15, 600):
        return too_many()
    k = norm(request.args.get("key"))
    if not KEY_RE.match(k):
        rl_add("limitfail")
        return jsonify(error="invalid"), 404
    try:
        v = get_key(k)
    except requests.RequestException:
        return jsonify(error="db"), 500
    if not v:
        rl_add("limitfail")
        return jsonify(error="invalid"), 404
    return jsonify(used=to_int(v.get("used")), limit=to_int(v.get("limit")), active=v.get("active", True) is not False)


@app.route("/like")
def like():
    def fail(msg, code=200):
        return jsonify({"status": 0, "error": msg, "Dev": DEV}), code

    if rl_over("like", 12, 60):
        return fail("TOO MANY REQUESTS", 429)
    rl_add("like")
    if rl_over("keyfail", 15, 600):
        return fail("TOO MANY ATTEMPTS", 429)

    k = norm(request.args.get("key"))
    uid = (request.args.get("uid") or "").strip()
    server = (request.args.get("server_name") or "bd").strip().lower()
    if not KEY_RE.match(k):
        rl_add("keyfail")
        return fail("INVALID KEY")
    if not re.fullmatch(r"\d{5,15}", uid):
        return fail("INVALID UID")
    if server not in SERVERS:
        return fail("INVALID SERVER")

    try:
        err, v = reserve(k)          # atomic: 1 use taken now
        url = main_url() if not err else ""
    except requests.RequestException:
        return fail("DATABASE ERROR")
    if err:
        if err == "INVALID KEY":
            rl_add("keyfail")
        return fail(err)

    lim, used0 = to_int(v.get("limit")), to_int(v.get("used"))
    keep = False
    try:
        if not url:
            return fail("MAIN API NOT SET")
        if url_problem(url):
            return fail("MAIN API NOT REACHABLE")
        try:
            r = build_upstream(url, uid, server)
            data = parse_json(r)
        except ValueError:
            app.logger.warning("upstream invalid response HTTP %s", getattr(r, "status_code", "?"))
            return fail("MAIN API ERROR: invalid response")
        except requests.RequestException as e:
            app.logger.warning("upstream error %s", type(e).__name__)   # never log/return the URL
            return fail("MAIN API NOT REACHABLE")

        given = likes_given(data[0] if isinstance(data, list) and data else data)
        keep = given is not None and given > 0     # deduct only when likes were added
        used = used0 + 1 if keep else used0
        vals = {"limit": lim, "used": used, "remaining": max(lim - used, 0)}
        return json_out(clean_dev(data, vals))
    finally:
        if not keep:
            refund(k)


# ------------------------------------------------------------------ admin api (token based)
@app.route("/api/admin/state")
@need_admin
def admin_state():
    keys = store.get(f"{ROOT}/keys") or {}
    rows = []
    for k, v in keys.items():
        if isinstance(v, dict):
            rows.append(dict(key=k, limit=to_int(v.get("limit")), used=to_int(v.get("used")),
                             active=v.get("active", True) is not False, created=v.get("created", 0)))
    rows.sort(key=lambda x: x["created"], reverse=True)
    return jsonify(ok=True, main_url=main_url(), keys=rows)


@app.route("/api/admin/url", methods=["POST"])
@need_admin
def admin_url():
    u = (body().get("url") or "")
    u = u.strip() if isinstance(u, str) else ""
    if len(u) > 800:
        return jsonify(ok=False, error="URL too long"), 400
    if u:
        if not u.startswith(("http://", "https://")):
            return jsonify(ok=False, error="URL must start with http:// or https://"), 400
        p = url_problem(u)
        if p:
            return jsonify(ok=False, error=p), 400
    store.put(f"{ROOT}/config/main_url", u)
    return jsonify(ok=True)


@app.route("/api/admin/gen", methods=["POST"])
@need_admin
def admin_gen():
    b = body()
    limit_ = min(max(to_int(b.get("limit"), 10), 0), 10_000_000)
    count = min(max(to_int(b.get("count"), 1), 1), 100)
    custom = norm(b.get("key"))
    prefix = re.sub(r"[^A-Z0-9_-]", "", norm(b.get("prefix")))[:20] or "XENON"
    if custom:
        if not KEY_RE.match(custom) or custom == ADMIN_KEY:
            return jsonify(ok=False, error="Invalid key (A-Z 0-9 _ - , 3-64 chars)"), 400
        if get_key(custom):
            return jsonify(ok=False, error="Key already exists"), 400
        names = [custom]
    else:
        alpha = string.ascii_uppercase + string.digits
        names = []
        while len(names) < count:
            n = prefix + "_" + "".join(secrets.choice(alpha) for _ in range(12))
            if n != ADMIN_KEY and n not in names and not get_key(n):
                names.append(n)
    now = int(time.time())
    for n in names:
        store.put(f"{ROOT}/keys/{n}", {"limit": limit_, "used": 0, "active": True, "created": now})
    return jsonify(ok=True, keys=names, limit=limit_)


@app.route("/api/admin/limit", methods=["POST"])
@need_admin
def admin_limit():
    b = body()
    k, act, amt = norm(b.get("key")), b.get("action"), max(to_int(b.get("amount")), 0)
    if not KEY_RE.match(k):
        return jsonify(ok=False, error="Bad key"), 400
    v = get_key(k)
    if not v:
        return jsonify(ok=False, error="Key not found"), 404
    lim = to_int(v.get("limit"))
    if act == "add":
        lim += amt
    elif act == "sub":
        lim = max(lim - amt, 0)
    elif act == "set":
        lim = amt
    elif act == "reset":
        store.patch(f"{ROOT}/keys/{k}", {"used": 0})
    elif act == "toggle":
        store.patch(f"{ROOT}/keys/{k}", {"active": not (v.get("active", True) is not False)})
    else:
        return jsonify(ok=False, error="Bad action"), 400
    if act in ("add", "sub", "set"):
        store.patch(f"{ROOT}/keys/{k}", {"limit": min(lim, 10_000_000)})
    return jsonify(ok=True)


@app.route("/api/admin/delete", methods=["POST"])
@need_admin
def admin_delete():
    k = norm(body().get("key"))
    if not KEY_RE.match(k):          # empty/odd key could wipe the whole keys node before
        return jsonify(ok=False, error="Bad key"), 400
    store.delete(f"{ROOT}/keys/{k}")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
