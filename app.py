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
@app.route("/")
def home():
    r = make_response(send_from_directory(BASE, "index.html"))
    r.headers["Cache-Control"] = "no-store"
    return r


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
