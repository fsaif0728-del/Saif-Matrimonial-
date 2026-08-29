# Saif Matrimonial Services — v2 (Production-Ready)

A completely rebuilt, security-hardened version of the matrimonial platform.
Browsing is free; unlocking a specific profile costs ₹11 (configurable),
verified manually by the admin from an in-site payment screenshot upload.

## What changed from v1 (and why)

| Area | v1 | v2 |
|---|---|---|
| Photo protection | CSS blur on the real image (the original file was still downloadable) | Original stored **outside** the public folder; only a server-generated, degraded blurred preview is ever public. Original is served through a route that checks authorization on every request. |
| Payment proof | WhatsApp screenshot only | Upload directly on the site, stored privately, viewable only by logged-in admin |
| "My Requests" access | Trusted a `?phone=` URL parameter — anyone could view anyone's requests by guessing/changing the number | Requires a one-time **phone + request code** verification, then a signed, time-limited, HttpOnly session cookie |
| CSRF | None | Every form is protected with a per-session CSRF token |
| Profile codes | `SELECT COUNT(*)` (would repeat after deletions) | Dedicated counter table — codes are never reused |
| Admin login | Plain env password compare | Password is hashed in memory; login is rate-limited (6 attempts / 15 min lockout) |
| Security headers | None | X-Frame-Options, X-Content-Type-Options, CSP, Referrer-Policy |
| Error pages | Flask defaults | Custom branded 404 / 403 / 413 / 500 pages |
| Image uploads | Saved as-is | Validated, re-encoded (strips EXIF/GPS metadata), size-limited, random filenames (no path traversal risk) |

## How photo protection actually works

1. Admin uploads a photo → server validates it's a real image, strips all
   metadata, and creates **two** files:
   - **Original** (re-encoded JPEG) → saved to a private folder that is
     never inside `static/` and has no predictable URL.
   - **Preview** → shrunk to 320px and heavily Gaussian-blurred → saved to
     `static/previews/` (safe to be public).
2. The public site and profile cards only ever reference the preview.
3. The original is served only via `/profile/<code>/photo`, which checks
   — on every single request — whether the current visitor's session has
   an **unlocked** request for that exact profile. No session match → `403`.
4. Filenames are random 32-character hex strings, never derived from user
   input, so there's nothing to path-traverse or guess.

**Honest limitation:** no website can stop someone from screenshotting or
photographing what's already displayed on their own unlocked screen. This
system prevents casual downloading/direct-linking/scraping of full-quality
originals — it doesn't claim to be screenshot-proof.

## How "My Requests" access works now

- After submitting an unlock request, the user sees a one-time **request
  code** (e.g. `A7K9M3QX`) and is told to save it.
- To check status later (especially from a different device), they enter
  their **phone number + that code** at `/verify-access`.
- On a match, the server sets a signed, HttpOnly, SameSite cookie valid for
  45 minutes — that's what "My Requests" and the unlocked profile page
  actually check, not a URL parameter.
- This is a practical middle ground without needing a paid SMS/OTP
  provider. `verify_access()` in `app.py` is written as a single, isolated
  function — swapping it for real SMS OTP later (e.g. via MSG91, Twilio)
  is a contained change, not a rewrite.

## Running locally / in Google Colab

```bash
pip install -r requirements.txt
python app.py
```

Opens on `http://localhost:5000` (or `0.0.0.0:5000`, so it also works
through a Colab tunnel). Admin: `admin` / `changeme123` by default —
**change this** via environment variables before going further.

Colab tunnel note: this app is tunnel-agnostic. Start Flask as above, then
separately expose port 5000 with whatever tunnel tool you prefer (e.g.
Cloudflare Tunnel, `localtunnel`, etc). Nothing in `app.py` depends on a
specific tunnel provider.

## Before deploying for real

1. Set these environment variables (see `.env.example`):
   `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `BUSINESS_PHONE`,
   `BUSINESS_LOCATION`, `UNLOCK_PRICE`, `UPI_ID`, `SESSION_COOKIE_SECURE=true`
2. Add your real UPI QR code image at `static/upi_qr.png`.
3. **Add persistent storage.** On Railway/Render, attach a volume and set
   `DATA_DIR` to its mount path (e.g. `/data`) — otherwise the database,
   uploaded photos, and payment screenshots are wiped on every redeploy.
4. Review `/privacy` wording and adjust to your actual practices.

## Deployment

Works as-is with:
```
gunicorn app:app
```
(already in the `Procfile`). Never run with `debug=True` in production —
this app never does; `app.run(..., debug=False)` is hardcoded for the dev
entrypoint.

## Project structure

```
matrimonial_v2/
├── app.py                    # all routes, security, image processing
├── requirements.txt
├── Procfile
├── .env.example
├── .gitignore
├── static/
│   ├── css/style.css
│   ├── js/main.js
│   ├── previews/             # PUBLIC — generated blurred previews only
│   └── upi_qr.png            # ADD YOUR REAL QR CODE HERE
├── storage/private/          # NEVER made public — originals + payment proofs
│   ├── profile_originals/
│   └── payment_proofs/
└── templates/                # all pages, including custom error pages
```

## Known trade-offs (documented on purpose, not hidden)

- **Rate limiting is in-memory** — fine for a single small worker; won't
  share state across multiple gunicorn workers/dynos. Fine for this scale;
  move to Redis-backed limiting if you outgrow it.
- **"My Requests" uses a code, not SMS OTP** — see above; upgradeable later.
- **SQLite** — simple and sufficient at this scale; migrate to Postgres if
  you outgrow single-file concurrency.
