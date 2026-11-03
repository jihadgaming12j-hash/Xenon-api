# Mr Ai Xenon - Free Fire Like API (Flask)
# Run: Termux -> python app.py | Render -> gunicorn app:app | Vercel -> api/index.py
import os, re, json, time, hmac, secrets, string, threading
import requests
try:
    import urllib3; urllib3.disable_warnings()
except Exception:
    pass
from flask import Flask, request, jsonify, send_from_directory, make_response, Response

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "Xenon_ai").strip().upper()
DEV = os.environ.get("DEV_TAG", "@Jihad_69xi")
FB_URL = os.environ.get("FIREBASE_URL", "https://mr-ai-xenon-default-rtdb.firebaseio.com").rstrip("/")
FB_SECRET = os.environ.get("FIREBASE_SECRET", "")   # optional: Firebase database secret (if rules are locked)
USE_LOCAL = os.environ.get("USE_LOCAL", "0") == "1"  # 1 = save in data.json (Termux / VPS)
UP_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "240"))  # main API can take 1-1.5 min
ROOT = "xenon_web"
KEY_RE = re.compile(r"^[A-Z0-9_-]{3,64}$")


# ------------------------------------------------------------------ vercel path fix
class FixVercelPath:
    """Vercel rewrites every URL to /api/index -> restore the real path (/, /like, /limit, /api/admin/..)."""
    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        p = environ.get("PATH_INFO", "")
        if p in ("/api/index", "/api/index.py"):
            from urllib.parse import parse_qs, unquote
            orig = None
            m = environ.get("HTTP_X_NOW_ROUTE_MATCHES", "")
            if m:
                g = parse_qs(m, keep_blank_values=True).get("1")
                if g is not None:
                    orig = "/" + unquote(g[0]).lstrip("/")
            if orig is None:  # header missing -> guess from query string
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

    def put(self, path, val):
        requests.put(self._u(path), params=self._p(), json=val, timeout=12).raise_for_status()

    def patch(self, path, val):
        requests.patch(self._u(path), params=self._p(), json=val, timeout=12).raise_for_status()

    def delete(self, path):
        requests.delete(self._u(path), params=self._p(), timeout=12).raise_for_status()

    def incr(self, path, field, n=1):  # atomic server-side increment
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


# ------------------------------------------------------------------ helpers
def norm(k):
    return (k or "").strip().upper()


def is_admin(k):
    return hmac.compare_digest(norm(k).encode(), ADMIN_KEY.encode())


def need_admin(fn):
    def w(*a, **kw):
        if not is_admin(request.headers.get("X-Admin-Key", "")):
            return jsonify(ok=False, error="Unauthorized"), 401
        try:
            return fn(*a, **kw)
        except requests.RequestException as e:
            return jsonify(ok=False, error="Database error: " + str(e)[:120]), 500
    w.__name__ = fn.__name__
    return w


def get_key(k):
    v = store.get(f"{ROOT}/keys/{k}")
    return v if isinstance(v, dict) else None


def main_url():
    return store.get(f"{ROOT}/config/main_url") or ""


def to_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


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
        u, params = url, {"uid": uid, "server_name": server}
    # Termux/VPS often has SSL, proxy or DNS problems -> try safe variants one by one
    variants = [dict(), dict(verify=False), dict(verify=False, noproxy=True)]
    last = None
    for opt in variants:
        for _ in range(2):
            try:
                ses = requests.Session()
                if opt.get("noproxy"):
                    ses.trust_env = False
                return ses.get(u, params=params, headers=UA, timeout=(10, UP_TIMEOUT),
                               allow_redirects=True, verify=opt.get("verify", True))
            except requests.ReadTimeout:
                raise                                   # like probably sent, never resend
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
        return json.JSONDecoder().raw_decode(t[i:])[0]  # JSON hidden inside extra text/html


DEV_KEYS = ("dev", "developer", "owner", "credit", "credits", "poweredby", "madeby", "by")


def _nk(k):
    return str(k).lower().replace("_", "").replace(" ", "").replace("-", "")


def _kind(k):
    """Which limit-field is this key? remaining / used / limit / None"""
    n = _nk(k)
    if "remain" in n or n in ("left", "keyleft", "requestsleft"):
        return "remaining"
    if "used" in n or n in ("usage", "keyusage"):
        return "used"
    if "limit" in n or "quota" in n or n in ("totalrequests", "maxrequests", "totalkeyrequests"):
        return "limit"
    return None


def _fit(old, num, other=None):
    """Put our number in the same type/format the main API used."""
    if isinstance(old, bool):
        return old
    if isinstance(old, int):
        return num
    if isinstance(old, float):
        return float(num)
    if isinstance(old, str):
        if "/" in old and other is not None:      # "5/10" style
            return f"{other[0]}/{other[1]}"
        if re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", old):
            return str(num)
        if re.search(r"\d", old):                 # "5 requests" style
            return re.sub(r"\d+", str(num), old, count=1)
        return str(num)
    return num


def rewrite(obj, vals):
    """Main API JSON stays exactly the same (same keys, same order).
    Only the limit fields (our key's) and the dev tag are changed."""
    rem, used, lim = vals["remaining"], vals["used"], vals["limit"]
    if isinstance(obj, list):
        return [rewrite(x, vals) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out, has_dev = {}, False
    for k, v in obj.items():
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
    # Dev tag must exist at top level (bottom) if upstream had none
    top = res[0] if isinstance(res, list) and res else res
    if isinstance(top, dict) and not any(_nk(k) in DEV_KEYS for k in top.keys()):
        top["Dev"] = DEV
    return res


def json_out(obj):
    """Same order as the main API, unicode kept as-is (no sorting)."""
    return Response(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                    mimetype="application/json")


def likes_given(d):
    if not isinstance(d, dict):
        return None
    g = d.get("LikesGivenByAPI")
    if g is not None:
        return to_int(g, 0)
    try:  # fallback: after - before
        if d.get("LikesafterCommand") is not None and d.get("LikesbeforeCommand") is not None:
            return max(to_int(d["LikesafterCommand"]) - to_int(d["LikesbeforeCommand"]), 0)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ pages
def _page():
    """Website html: from index.html if the host bundled it, else the copy embedded below."""
    for d in (BASE, os.getcwd(), "/var/task", os.path.join(BASE, "..")):
        try:
            with open(os.path.join(d, "index.html"), "rb") as f:
                return f.read()
        except Exception:
            continue
    import zlib, base64
    return zlib.decompress(base64.b64decode("".join(_PAGE_B64.split())))


@app.route("/")
@app.route("/index.html")
def home():
    r = make_response(_page())
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store"
    return r


@app.errorhandler(404)
def not_found(e):
    p = request.path
    if p.startswith(("/api", "/like", "/limit")):
        return jsonify({"status": 0, "error": "NOT FOUND", "path": p, "Dev": DEV}), 404
    return home()  # any other page -> website


@app.route("/health")
def health():
    return jsonify(ok=True)


# ------------------------------------------------------------------ public api
@app.route("/limit")
def limit():
    k = norm(request.args.get("key"))
    if not KEY_RE.match(k):
        return jsonify(error="invalid"), 404
    try:
        v = get_key(k)
    except requests.RequestException:
        return jsonify(error="db"), 500
    if not v:
        return jsonify(error="invalid"), 404
    return jsonify(used=to_int(v.get("used")), limit=to_int(v.get("limit")), active=v.get("active", True) is not False)


@app.route("/like")
def like():
    def fail(msg, code=200):
        return jsonify({"status": 0, "error": msg, "Dev": DEV}), code

    k = norm(request.args.get("key"))
    uid = (request.args.get("uid") or "").strip()
    server = (request.args.get("server_name") or "bd").strip().lower()
    if not KEY_RE.match(k):
        return fail("INVALID KEY")
    if not re.fullmatch(r"\d{5,15}", uid):
        return fail("INVALID UID")
    try:
        v = get_key(k)
        url = main_url()
    except requests.RequestException:
        return fail("DATABASE ERROR")
    if not v:
        return fail("INVALID KEY")
    if v.get("active", True) is False:
        return fail("KEY DISABLED")
    if to_int(v.get("used")) >= to_int(v.get("limit")):
        return fail("LIMIT FINISHED - contact owner for more limit")
    if not url:
        return fail("MAIN API NOT SET")

    dbg = norm(request.args.get("debug")) == ADMIN_KEY
    try:
        r = build_upstream(url, uid, server)
        data = parse_json(r)
    except ValueError:
        out = {"status": 0, "error": "MAIN API ERROR: invalid response", "Dev": DEV}
        out["detail"] = f"HTTP {r.status_code}: {(r.text or '')[:300]}"
        return jsonify(out)
    except requests.RequestException as e:
        out = {"status": 0, "error": "MAIN API NOT REACHABLE", "Dev": DEV}
        out["detail"] = f"{type(e).__name__}: {str(e)[:300]}"
        return jsonify(out)

    given = likes_given(data[0] if isinstance(data, list) and data else data)
    lim, used = to_int(v.get("limit")), to_int(v.get("used"))
    # Limit rule: only when likes added > 0 (0 / jwt update / error => no deduction)
    if given is not None and given > 0:
        try:
            store.incr(f"{ROOT}/keys/{k}", "used", 1)
            used += 1
        except requests.RequestException:
            pass
    vals = {"limit": lim, "used": used, "remaining": max(lim - used, 0)}
    return json_out(clean_dev(data, vals))


# ------------------------------------------------------------------ admin api
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    body = request.get_json(silent=True) or {}
    if is_admin(body.get("key", "")):
        return jsonify(ok=True)
    return jsonify(ok=False), 401


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
    u = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if u and not u.startswith(("http://", "https://")):
        return jsonify(ok=False, error="URL must start with http:// or https://"), 400
    store.put(f"{ROOT}/config/main_url", u)
    return jsonify(ok=True)


@app.route("/api/admin/gen", methods=["POST"])
@need_admin
def admin_gen():
    b = request.get_json(silent=True) or {}
    limit_ = max(to_int(b.get("limit"), 10), 0)
    count = min(max(to_int(b.get("count"), 1), 1), 100)
    custom = norm(b.get("key"))
    prefix = re.sub(r"[^A-Z0-9_-]", "", norm(b.get("prefix"))) or "XENON"
    made = []
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
            n = prefix + "_" + "".join(secrets.choice(alpha) for _ in range(8))
            if n != ADMIN_KEY and n not in names and not get_key(n):
                names.append(n)
    now = int(time.time())
    for n in names:
        store.put(f"{ROOT}/keys/{n}", {"limit": limit_, "used": 0, "active": True, "created": now})
        made.append(n)
    return jsonify(ok=True, keys=made, limit=limit_)


@app.route("/api/admin/limit", methods=["POST"])
@need_admin
def admin_limit():
    b = request.get_json(silent=True) or {}
    k, act, amt = norm(b.get("key")), b.get("action"), max(to_int(b.get("amount")), 0)
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
        store.patch(f"{ROOT}/keys/{k}", {"limit": lim})
    return jsonify(ok=True)


@app.route("/api/admin/delete", methods=["POST"])
@need_admin
def admin_delete():
    k = norm((request.get_json(silent=True) or {}).get("key"))
    store.delete(f"{ROOT}/keys/{k}")
    return jsonify(ok=True)


_PAGE_B64 = """
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
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
