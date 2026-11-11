#!/usr/bin/env python3
"""Generate every secret for Mr Ai Xenon. Run it on YOUR computer. Never commit/share the output.

  python make_secrets.py          -> random 24-char password
  python make_secrets.py --own    -> hash a password you type (min 14 chars, mixed)
"""
import base64, getpass, hashlib, os, secrets, string, sys

N, R, P = 2 ** 15, 8, 1


def b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, dklen=32, maxmem=128 * 1024 * 1024)
    return "scrypt.%d.%d.%d.%s.%s" % (N, R, P, b64(salt), b64(h))


def strong(pw):
    return (len(pw) >= 14 and any(c.islower() for c in pw) and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw) and any(c in string.punctuation for c in pw))


def gen_password(n=24):
    alpha = string.ascii_letters + string.digits + "!@#%^*-_=+?"
    while True:
        pw = "".join(secrets.choice(alpha) for _ in range(n))
        if strong(pw):
            return pw


def main():
    if "--own" in sys.argv:
        pw = getpass.getpass("Admin password (14+ chars, upper+lower+digit+symbol): ")
        if not strong(pw) or pw != getpass.getpass("Repeat: "):
            sys.exit("Weak or mismatching password. Use 14+ chars with upper, lower, digit and symbol.")
        shown = False
    else:
        pw, shown = gen_password(), True
    user = "xenon_" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    totp = base64.b32encode(os.urandom(20)).decode()
    print("\n=== Put these in Vercel / .env (server side) ===\n")
    print("ADMIN_USER=" + user)
    print("ADMIN_PASS_HASH=" + hash_pw(pw))
    print("SESSION_SECRET=" + secrets.token_urlsafe(48))
    print("DATA_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode())
    print("ADMIN_TOTP_SECRET=" + totp)
    print("\n=== Your login (save in a password manager / browser) ===\n")
    print("Username:", user)
    if shown:
        print("Password:", pw)
    print("\n2FA: add this to Google Authenticator / Authy (Enter a setup key, time based):")
    print("  " + totp)
    print("  otpauth://totp/MrAiXenon:" + user + "?secret=" + totp + "&issuer=MrAiXenon")
    print("\nThe password is NOT stored anywhere - only its hash. Lose it = run this script again.\n")


if __name__ == "__main__":
    main()
