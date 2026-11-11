# Mr Ai Xenon - Free Fire Like API (Flask) - HARDENED v2
# ALL secrets live in environment variables only (never in code, index.html or plaintext DB).
# First run:  python make_secrets.py   -> prints every env var you need.
from dotenv import load_dotenv
load_dotenv()
import os, re, json, time, hmac, hashlib, secrets, string, threading, socket, ipaddress, random, base64
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, parse_qs, unquote, urljoin
import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, request, jsonify, make_response, Response, g

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # tiny bodies only

KEY_RE = re.compile(r"^[A-Z0-9_-]{3,64}$")        # existing user keys keep working
NEW_KEY_RE = re.compile(r"^[A-Z0-9_-]{8,64}$")    # custom keys created from now on must be 8+ chars
ROOT = "xenon_web"
SERVERS = {"bd"}  # allowed server_name values (whitelist)


# ------------------------------------------------------------------ config (no weak defaults)
def _env(name, minlen=0):
    v = os.environ.get(name, "").strip()
    if len(v) < minlen:
        raise RuntimeError("Missing/short env %s - run: python make_secrets.py" % name)
    return v


ADMIN_USER = _env("ADMIN_USER", 4)
ADMIN_PASS_HASH = _env("ADMIN_PASS_HASH", 40)       # scrypt hash, never the password itself
SESSION_SECRET = _env("SESSION_SECRET", 32)
DATA_KEY = _env("DATA_KEY", 40)                      # Fernet key: encrypts the Main API URL at rest
ADMIN_TOTP_SECRET = re.sub(r"\s", "", os.environ.get("ADMIN_TOTP_SECRET", "")).upper()  # optional 2FA
ENV_MAIN_API = os.environ.get("MAIN_API_URL", "").strip()   # optional: keep Main API out of the DB completely
try:
    FERNET = Fernet(DATA_KEY.encode())
except Exception:
    raise RuntimeError("DATA_KEY invalid - run: python make_secrets.py")

DEV = os.environ.get("DEV_TAG", "@Jihad_69xi")
FB_URL = os.environ.get("FIREBASE_URL", "https://mr-ai-xenon-default-rtdb.firebaseio.com").rstrip("/")
FB_SECRET = os.environ.get("FIREBASE_SECRET", "")
USE_LOCAL = os.environ.get("USE_LOCAL", "0") == "1"
if not USE_LOCAL and not FB_SECRET:
    raise RuntimeError("Set env FIREBASE_SECRET and lock Firebase rules (.read/.write = false)")
UP_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "240"))
ALLOW_PRIVATE_UPSTREAM = os.environ.get("ALLOW_PRIVATE_UPSTREAM", "0") == "1"
IS_VERCEL = bool(os.environ.get("VERCEL"))
COOKIE = "xn_adm"
SESS_SHORT, SESS_LONG = 2 * 3600, 30 * 86400


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
    ip = ""
    if IS_VERCEL:                                    # only trust this header when Vercel sets it
        ip = (request.headers.get("X-Vercel-Forwarded-For") or "").split(",")[0].strip()
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



# ---- persistent (DB-backed) lockouts: survive serverless cold starts, escalate exponentially
def _ip_hash():
    return hmac.new(SESSION_SECRET.encode(), client_ip().encode(), hashlib.sha256).hexdigest()[:32]


def lock_left(ns):
    rec = store.get(f"{ROOT}/sec/lock_{ns}/{_ip_hash()}")
    if isinstance(rec, dict):
        return max(to_int(rec.get("until")) - int(time.time()), 0)
    return 0


def lock_fail(ns, threshold, base):
    path = f"{ROOT}/sec/lock_{ns}/{_ip_hash()}"
    rec = store.get(path)
    rec = rec if isinstance(rec, dict) else {}
    now = int(time.time())
    n = to_int(rec.get("n")) + 1 if now - to_int(rec.get("t")) < 86400 else 1
    until = now + min(base * 2 ** (n - threshold), 86400) if n >= threshold else 0
    store.put(path, {"n": n, "t": now, "until": until})


def lock_clear(ns):
    store.delete(f"{ROOT}/sec/lock_{ns}/{_ip_hash()}")


# ---- admin password (scrypt) + optional TOTP 2FA
def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_password(pw, stored):
    try:
        tag, n, r, p, salt, h = stored.split(".")
        if tag != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode("utf-8"), salt=_b64d(salt), n=int(n), r=int(r), p=int(p),
                             dklen=32, maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(got, _b64d(h))
    except Exception:
        return False


def _totp(secret_b32, counter):
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    d = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    o = d[-1] & 15
    return "%06d" % ((int.from_bytes(d[o:o + 4], "big") & 0x7FFFFFFF) % 1000000)


def totp_ok(code):
    code = re.sub(r"\s", "", code if isinstance(code, str) else "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    now = int(time.time() // 30)
    last = to_int(store.get(f"{ROOT}/sec/totp_last"))
    for c in (now - 1, now, now + 1):
        if c > last and hmac.compare_digest(_totp(ADMIN_TOTP_SECRET, c), code):
            store.put(f"{ROOT}/sec/totp_last", c)         # a code can never be used twice
            return True
    return False


# ---- admin sessions: random id in an HttpOnly cookie, only its hash is stored server-side
def _https():
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"


def _sid_hash(sid):
    return hashlib.sha256(("sid|" + sid).encode()).hexdigest()


def purge_sessions():
    try:
        now, n = time.time(), 0
        for h, rec in (store.get(f"{ROOT}/sessions") or {}).items():
            if not isinstance(rec, dict) or to_int(rec.get("exp")) < now:
                store.delete(f"{ROOT}/sessions/{h}")
                n += 1
                if n >= 20:
                    break
    except Exception:
        pass


def new_session(remember):
    sid = secrets.token_urlsafe(32)
    ttl = SESS_LONG if remember else SESS_SHORT
    now = int(time.time())
    store.put(f"{ROOT}/sessions/{_sid_hash(sid)}", {"exp": now + ttl, "created": now, "rem": bool(remember)})
    purge_sessions()
    return sid, ttl


def session_path():
    sid = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", sid):
        return None
    path = f"{ROOT}/sessions/{_sid_hash(sid)}"
    rec = store.get(path)
    if not isinstance(rec, dict):
        return None
    if to_int(rec.get("exp")) < time.time():
        store.delete(path)
        return None
    return path


def csrf_ok():
    """State-changing admin calls need our custom header + same-origin (plus SameSite=Strict cookie)."""
    if request.method in ("GET", "HEAD"):
        return True
    if request.headers.get("X-Requested-With") != "xenon-admin":
        return False
    o = request.headers.get("Origin")
    if o:
        try:
            return urlsplit(o).netloc == request.host
        except Exception:
            return False
    return request.headers.get("Sec-Fetch-Site") in (None, "same-origin")


def too_many_adm(left):
    return jsonify(ok=False, error="Too many attempts", retry=left), 429


def norm(k):
    return (k if isinstance(k, str) else "").strip().upper()


def body():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def need_admin(fn):
    def w(*a, **kw):
        try:
            if not csrf_ok():
                return jsonify(ok=False, error="Bad request"), 403
            if not session_path():
                return jsonify(ok=False, error="Unauthorized"), 401
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



def to_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def _enc(u):
    return "enc1:" + FERNET.encrypt(u.encode()).decode()


def _dec(v):
    if not isinstance(v, str) or not v:
        return ""
    if v.startswith("enc1:"):
        try:
            return FERNET.decrypt(v[5:].encode()).decode()
        except (InvalidToken, ValueError):
            return ""
    return v                                   # legacy plaintext (auto-encrypted on next admin visit)


def main_url():
    if ENV_MAIN_API:
        return ENV_MAIN_API
    return _dec(store.get(f"{ROOT}/config/main_url"))


def mask_url(u):
    try:
        sp = urlsplit(re.sub(r"[{}]", "", u))
        return f"{sp.scheme}://{sp.hostname}/••••••" if sp.hostname else "hidden"
    except Exception:
        return "hidden"


def secret_bits(url):
    """Every piece of the Main API URL that must never appear in any response."""
    bits = {url.strip()}
    try:
        sp = urlsplit(url.strip())
        if sp.hostname and len(sp.hostname) >= 4:
            bits.add(sp.hostname)
        for _, v in parse_qsl(sp.query, keep_blank_values=False):
            if len(v) >= 6 and not (v.startswith("{") and v.endswith("}")):
                bits.add(v)
        for seg in sp.path.split("/"):
            if len(seg) >= 16:
                bits.add(seg)
        if sp.password:
            bits.add(sp.password)
    except Exception:
        pass
    return sorted((b for b in bits if b), key=len, reverse=True)


def scrub(o, bits):
    if isinstance(o, str):
        for b in bits:
            if b in o:
                o = o.replace(b, "***")
        return o
    if isinstance(o, list):
        return [scrub(x, bits) for x in o]
    if isinstance(o, dict):
        return {k: scrub(v, bits) for k, v in o.items()}
    return o


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



class Blocked(requests.RequestException):
    pass


def _fetch(ses, u, params):
    """GET with manual redirects: every hop is SSRF-checked, body capped at 1 MB."""
    for _ in range(4):
        r = ses.get(u, params=params, headers=UA, timeout=(10, UP_TIMEOUT), allow_redirects=False, stream=True)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            nxt = urljoin(r.url, r.headers["Location"])
            r.close()
            if url_problem(nxt):
                raise Blocked("blocked redirect")
            u, params = nxt, None
            continue
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > 1_000_000:
                r.close()
                raise ValueError("upstream too large")
        r._content, r._content_consumed = buf, True
        return r
    raise requests.TooManyRedirects("too many redirects")


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
                return _fetch(ses, u, params)
            except (requests.ReadTimeout, Blocked):
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
    g.nonce = secrets.token_urlsafe(16)                       # per-response CSP nonce (no 'unsafe-inline' scripts)
    html = _page().replace(b"<script>", ('<script nonce="%s">' % g.nonce).encode())
    r = make_response(html)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r


def _csp():
    return ("default-src 'none'; script-src 'self' 'nonce-%s'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'; "
            "upgrade-insecure-requests") % getattr(g, "nonce", "")


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = _csp()
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
    """User access-key login only (admin has its own secure login). Failures are locked out per IP, persistently."""
    try:
        if lock_left("usr"):
            return too_many()
        k = norm(body().get("key"))
        if KEY_RE.match(k):
            v = get_key(k)
            if v:
                return jsonify(ok=True, role="user", used=to_int(v.get("used")), limit=to_int(v.get("limit")),
                               active=v.get("active", True) is not False)
        lock_fail("usr", 10, 600)
    except requests.RequestException:
        return jsonify(ok=False, error="db"), 500
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
    keep, r = False, None
    try:
        if not url:
            return fail("MAIN API NOT SET")
        if url_problem(url):
            return fail("MAIN API NOT REACHABLE")
        try:
            r = build_upstream(url, uid, server)
            data = scrub(parse_json(r), secret_bits(url))      # main API url / key can never reach the client
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


# ------------------------------------------------------------------ admin api (HttpOnly session cookie)
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    if not csrf_ok():
        return jsonify(ok=False, error="Bad request"), 403
    try:
        left = lock_left("adm")
        if left:
            return too_many_adm(left)
        b = body()
        user, pw, code, remember = b.get("user"), b.get("pass"), b.get("code"), b.get("remember") is True
        if not (isinstance(user, str) and isinstance(pw, str)) or len(user) > 100 or len(pw) > 200:
            user, pw = "", ""
        ok_user = hmac.compare_digest(user.encode(), ADMIN_USER.encode())
        ok_pw = verify_password(pw, ADMIN_PASS_HASH)        # always computed -> same timing for wrong user
        ok = ok_user and ok_pw
        if ok and ADMIN_TOTP_SECRET:
            ok = totp_ok(code)
        if not ok:
            lock_fail("adm", 5, 900)                          # 5 fails -> 15 min lock, doubling up to 24h
            time.sleep(0.6)
            return jsonify(ok=False, error="Invalid credentials"), 401
        lock_clear("adm")
        sid, ttl = new_session(remember)
    except requests.RequestException:
        return jsonify(ok=False, error="Database error"), 500
    resp = jsonify(ok=True, remember=remember)
    resp.set_cookie(COOKIE, sid, max_age=ttl, httponly=True, secure=_https(), samesite="Strict", path="/api/admin")
    return resp


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    if not csrf_ok():
        return jsonify(ok=False, error="Bad request"), 403
    try:
        p = session_path()
        if p:
            store.delete(p)
    except requests.RequestException:
        pass
    resp = jsonify(ok=True)
    resp.delete_cookie(COOKIE, path="/api/admin")
    return resp


@app.route("/api/admin/session")
@need_admin
def admin_session():
    return jsonify(ok=True)


@app.route("/api/admin/state")
@need_admin
def admin_state():
    raw = store.get(f"{ROOT}/config/main_url")
    if isinstance(raw, str) and raw and not raw.startswith("enc1:"):
        store.put(f"{ROOT}/config/main_url", _enc(raw))      # migrate old plaintext -> encrypted
    url = main_url()
    keys = store.get(f"{ROOT}/keys") or {}
    rows = []
    for k, v in keys.items():
        if isinstance(v, dict):
            rows.append(dict(key=k, limit=to_int(v.get("limit")), used=to_int(v.get("used")),
                             active=v.get("active", True) is not False, created=v.get("created", 0)))
    rows.sort(key=lambda x: x["created"], reverse=True)
    return jsonify(ok=True, main_url_masked=mask_url(url) if url else "",
                   main_url_source="env" if ENV_MAIN_API else ("db" if url else "none"), keys=rows)


@app.route("/api/admin/url", methods=["POST"])
@need_admin
def admin_url():
    if ENV_MAIN_API:
        return jsonify(ok=False, error="Main API is locked by server env (MAIN_API_URL)"), 400
    u = (body().get("url") or "")
    u = u.strip() if isinstance(u, str) else ""
    if not u:
        return jsonify(ok=False, error="Paste the new Main API URL"), 400
    if len(u) > 800:
        return jsonify(ok=False, error="URL too long"), 400
    if not u.startswith(("http://", "https://")):
        return jsonify(ok=False, error="URL must start with http:// or https://"), 400
    p = url_problem(u)
    if p:
        return jsonify(ok=False, error=p), 400
    store.put(f"{ROOT}/config/main_url", _enc(u))           # encrypted at rest, never returned to any client
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
        if not NEW_KEY_RE.match(custom):
            return jsonify(ok=False, error="Invalid key (A-Z 0-9 _ - , 8-64 chars)"), 400
        if get_key(custom):
            return jsonify(ok=False, error="Key already exists"), 400
        names = [custom]
    else:
        alpha = string.ascii_uppercase + string.digits
        names = []
        while len(names) < count:
            n = prefix + "_" + "".join(secrets.choice(alpha) for _ in range(16))
            if n not in names and not get_key(n):
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
