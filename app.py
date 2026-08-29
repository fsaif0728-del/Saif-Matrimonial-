import os
import io
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort, send_from_directory, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageFilter, ImageOps

# ======================================================================
# CONFIG
# ======================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Persistent data lives under DATA_DIR so it can be pointed at a mounted
# volume in production (see README). Defaults to the project folder for
# local/dev use.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

DB_PATH = os.path.join(DATA_DIR, "matrimonial.db")
PRIVATE_ORIGINALS_DIR = os.path.join(DATA_DIR, "storage", "private", "profile_originals")
PRIVATE_PROOFS_DIR = os.path.join(DATA_DIR, "storage", "private", "payment_proofs")
PREVIEW_DIR = os.path.join(BASE_DIR, "static", "previews")

for d in (PRIVATE_ORIGINALS_DIR, PRIVATE_PROOFS_DIR, PREVIEW_DIR):
    os.makedirs(d, exist_ok=True)

ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB per image

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # hard cap per request (8 MB)

# Session / cookie hardening
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.permanent_session_lifetime = timedelta(minutes=45)

# ----------------------------------------------------------------------
# Business configuration (env-overridable)
# ----------------------------------------------------------------------
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Saif Matrimonial Services")
BUSINESS_TAGLINE = os.environ.get("BUSINESS_TAGLINE", "Trusted Connections, Blessed Beginnings")
BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "7762023966")
BUSINESS_LOCATION = os.environ.get("BUSINESS_LOCATION", "Kolkata, India")
UNLOCK_PRICE = os.environ.get("UNLOCK_PRICE", "11")
UPI_ID = os.environ.get("UPI_ID", "yourupi@bank")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
# Password is hashed in memory at startup -- never stored/compared in plain text.
ADMIN_PASSWORD_HASH = generate_password_hash(os.environ.get("ADMIN_PASSWORD", "changeme123"))


@app.context_processor
def inject_globals():
    return dict(
        business_name=BUSINESS_NAME,
        business_tagline=BUSINESS_TAGLINE,
        business_phone=BUSINESS_PHONE,
        business_location=BUSINESS_LOCATION,
        unlock_price=UNLOCK_PRICE,
    )


# ======================================================================
# DATABASE
# ======================================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS profile_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_val INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            gender TEXT NOT NULL,
            city TEXT NOT NULL,
            marital_status TEXT,
            education TEXT,
            profession TEXT,
            community TEXT,
            bio TEXT,
            contact_number TEXT,
            photo_original_name TEXT,
            photo_preview_name TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS unlock_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT UNIQUE NOT NULL,
            profile_id INTEGER NOT NULL,
            user_name TEXT,
            user_phone TEXT NOT NULL,
            payment_proof_name TEXT,
            message TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles (id) ON DELETE CASCADE
        );
        """
    )
    db.execute("INSERT OR IGNORE INTO profile_counter (id, next_val) VALUES (1, 10001)")
    db.commit()
    db.close()


def next_profile_code(db):
    """Atomically reserve the next profile code. Codes are never reused,
    even after a profile is deleted."""
    row = db.execute("SELECT next_val FROM profile_counter WHERE id = 1").fetchone()
    next_val = row["next_val"]
    db.execute("UPDATE profile_counter SET next_val = ? WHERE id = 1", (next_val + 1,))
    return f"SMS{next_val}"


def new_request_code():
    # Short, human-typeable code the user needs (with their phone number)
    # to check request status later.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no ambiguous chars (0/O, 1/I/L)
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ======================================================================
# CSRF PROTECTION (lightweight, no external dependency)
# ======================================================================
def get_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(24)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def enforce_csrf():
    if request.method == "POST":
        token = session.get("_csrf_token")
        submitted = request.form.get("csrf_token")
        if not token or not submitted or not secrets.compare_digest(token, submitted):
            abort(400)


# ======================================================================
# SECURITY HEADERS
# ======================================================================
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'"
    )
    return resp


# ======================================================================
# LOGIN RATE LIMITING (simple in-memory; fine for a single small worker)
# ======================================================================
_login_attempts = {}
LOGIN_MAX_ATTEMPTS = 6
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_LOCKOUT_SECONDS = 15 * 60


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def is_login_locked(ip):
    entry = _login_attempts.get(ip)
    if not entry:
        return False
    count, first_seen, locked_until = entry
    if locked_until and time.time() < locked_until:
        return True
    return False


def register_failed_login(ip):
    count, first_seen, locked_until = _login_attempts.get(ip, (0, time.time(), None))
    now = time.time()
    if now - first_seen > LOGIN_WINDOW_SECONDS:
        count, first_seen = 0, now
    count += 1
    locked_until = now + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_ATTEMPTS else None
    _login_attempts[ip] = (count, first_seen, locked_until)


def clear_login_attempts(ip):
    _login_attempts.pop(ip, None)


# ======================================================================
# IMAGE HANDLING (validate -> re-encode -> strip EXIF -> private original
# + public blurred preview). Filenames are random; never derived from
# user input, so there is nothing to path-traverse with.
# ======================================================================
class ImageValidationError(Exception):
    pass


def _load_validated_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        raise ImageValidationError("Unsupported file type. Use JPG, PNG or WEBP.")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        raise ImageValidationError("Image is too large (max 6 MB).")
    if size == 0:
        raise ImageValidationError("Empty file.")

    try:
        img = Image.open(file_storage.stream)
        img.verify()  # raises if not a real image
    except Exception:
        raise ImageValidationError("This doesn't look like a valid image file.")

    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img)  # respect rotation, then...
    img = img.convert("RGB")  # ...re-encoding below strips all EXIF metadata
    return img


def save_profile_photo(file_storage):
    """Returns (original_filename, preview_filename) or (None, None)."""
    img = _load_validated_image(file_storage)
    if img is None:
        return None, None

    original_name = secrets.token_hex(16) + ".jpg"
    img.save(os.path.join(PRIVATE_ORIGINALS_DIR, original_name), "JPEG", quality=88)

    preview = img.copy()
    preview.thumbnail((320, 320))
    preview = preview.filter(ImageFilter.GaussianBlur(radius=14))
    preview_name = secrets.token_hex(16) + ".jpg"
    preview.save(os.path.join(PREVIEW_DIR, preview_name), "JPEG", quality=55)

    return original_name, preview_name


def save_payment_proof(file_storage):
    img = _load_validated_image(file_storage)
    if img is None:
        return None
    proof_name = secrets.token_hex(16) + ".jpg"
    img.save(os.path.join(PRIVATE_PROOFS_DIR, proof_name), "JPEG", quality=85)
    return proof_name


def delete_file_quietly(directory, filename):
    if not filename:
        return
    try:
        os.remove(os.path.join(directory, filename))
    except OSError:
        pass


# ======================================================================
# ACCESS CONTROL HELPERS
# ======================================================================
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def verified_phone():
    """The phone number the current visitor has proven ownership of this
    session, or None. Proof = they knew a valid (phone, request_code) pair
    issued only to the original requester."""
    return session.get("verified_phone")


def user_has_unlocked(db, profile_id, phone):
    if not phone:
        return None
    return db.execute(
        "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? AND status = 'unlocked'",
        (profile_id, phone),
    ).fetchone()


PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def valid_indian_phone(phone):
    return bool(PHONE_RE.match((phone or "").strip()))


# ======================================================================
# PUBLIC ROUTES
# ======================================================================
@app.route("/")
def index():
    db = get_db()
    profiles = db.execute(
        "SELECT * FROM profiles WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    return render_template("index.html", profiles=profiles)


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/profile/<profile_code>")
def profile_preview(profile_code):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ? AND is_active = 1", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)
    already_unlocked = user_has_unlocked(db, profile["id"], verified_phone())
    return render_template("profile_preview.html", profile=profile, already_unlocked=already_unlocked)


@app.route("/profile/<profile_code>/preview-image")
def profile_preview_image(profile_code):
    """Public, safe: only ever serves the degraded/blurred preview."""
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)
    ).fetchone()
    if not profile or not profile["photo_preview_name"]:
        abort(404)
    return send_from_directory(PREVIEW_DIR, profile["photo_preview_name"])


@app.route("/profile/<profile_code>/photo")
def profile_original_photo(profile_code):
    """Protected: only served if this session has proven, unlocked access
    to this specific profile. Storage path is never exposed to the client."""
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)

    unlocked = user_has_unlocked(db, profile["id"], verified_phone())
    if not unlocked:
        abort(403)

    if not profile["photo_original_name"]:
        abort(404)
    return send_from_directory(PRIVATE_ORIGINALS_DIR, profile["photo_original_name"])


@app.route("/profile/<profile_code>/unlock", methods=["GET", "POST"])
def unlock(profile_code):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ? AND is_active = 1", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)

    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()[:120]
        user_phone = request.form.get("user_phone", "").strip()
        message = request.form.get("message", "").strip()[:500]

        errors = []
        if not valid_indian_phone(user_phone):
            errors.append("Please enter a valid 10-digit Indian mobile number.")

        proof_file = request.files.get("payment_proof")
        proof_name = None
        if not proof_file or not proof_file.filename:
            errors.append("Please upload your payment screenshot.")
        else:
            try:
                proof_name = save_payment_proof(proof_file)
            except ImageValidationError as e:
                errors.append(str(e))

        if not errors:
            existing = db.execute(
                "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? AND status = 'pending'",
                (profile["id"], user_phone),
            ).fetchone()
            if existing:
                errors.append("You already have a pending request for this profile with this number.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("unlock.html", profile=profile, upi_id=UPI_ID)

        request_code = new_request_code()
        db.execute(
            """
            INSERT INTO unlock_requests
            (request_code, profile_id, user_name, user_phone, payment_proof_name, message, status, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (request_code, profile["id"], user_name, user_phone, proof_name, message, datetime.now().isoformat()),
        )
        db.commit()

        # Prove phone ownership immediately for this session so the user
        # can check status without re-typing the code right away.
        session.permanent = True
        session["verified_phone"] = user_phone

        return render_template("request_submitted.html", profile=profile, request_code=request_code)

    return render_template("unlock.html", profile=profile, upi_id=UPI_ID)


@app.route("/verify-access", methods=["GET", "POST"])
def verify_access():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        code = request.form.get("request_code", "").strip().upper()
        db = get_db()
        match = db.execute(
            "SELECT * FROM unlock_requests WHERE user_phone = ? AND request_code = ?",
            (phone, code),
        ).fetchone()
        if not match:
            flash("Phone number and request code don't match any request.", "error")
            return render_template("verify_access.html")
        session.permanent = True
        session["verified_phone"] = phone
        return redirect(url_for("my_requests"))
    return render_template("verify_access.html")


@app.route("/my-requests")
def my_requests():
    phone = verified_phone()
    if not phone:
        return redirect(url_for("verify_access"))
    db = get_db()
    rows = db.execute(
        """
        SELECT ur.*, p.profile_code, p.name, p.age, p.city
        FROM unlock_requests ur JOIN profiles p ON p.id = ur.profile_id
        WHERE ur.user_phone = ?
        ORDER BY ur.requested_at DESC
        """,
        (phone,),
    ).fetchall()
    return render_template("my_requests.html", rows=rows, phone=phone)


@app.route("/my-requests/exit")
def exit_verification():
    session.pop("verified_phone", None)
    return redirect(url_for("index"))


@app.route("/profile/<profile_code>/full")
def profile_full(profile_code):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not profile:
        abort(404)
    unlocked = user_has_unlocked(db, profile["id"], verified_phone())
    if not unlocked:
        flash("This profile isn't unlocked for your verified number yet.", "error")
        return redirect(url_for("verify_access"))
    return render_template("profile_full.html", profile=profile)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ======================================================================
# ADMIN — AUTH
# ======================================================================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    ip = _client_ip()
    if request.method == "POST":
        if is_login_locked(ip):
            flash("Too many failed attempts. Please try again in a few minutes.", "error")
            return render_template("admin_login.html")

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            clear_login_attempts(ip)
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            nxt = request.args.get("next")
            return redirect(nxt or url_for("admin_dashboard"))

        register_failed_login(ip)
        flash("Invalid credentials.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


# ======================================================================
# ADMIN — DASHBOARD
# ======================================================================
@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    stats = {
        "total": db.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"],
        "active": db.execute("SELECT COUNT(*) c FROM profiles WHERE is_active=1").fetchone()["c"],
        "hidden": db.execute("SELECT COUNT(*) c FROM profiles WHERE is_active=0").fetchone()["c"],
        "pending": db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='pending'").fetchone()["c"],
        "unlocked": db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='unlocked'").fetchone()["c"],
        "rejected": db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='rejected'").fetchone()["c"],
    }
    recent_pending = db.execute(
        """
        SELECT ur.*, p.profile_code, p.name FROM unlock_requests ur
        JOIN profiles p ON p.id = ur.profile_id
        WHERE ur.status = 'pending' ORDER BY ur.requested_at DESC LIMIT 5
        """
    ).fetchall()
    return render_template("admin_dashboard.html", stats=stats, recent_pending=recent_pending)


# ======================================================================
# ADMIN — PROFILES
# ======================================================================
@app.route("/admin/profiles")
@admin_required
def admin_profiles():
    db = get_db()
    profiles = db.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
    return render_template("admin_profiles.html", profiles=profiles)


def _profile_form_fields():
    age_raw = request.form.get("age", "").strip()
    return {
        "name": request.form.get("name", "").strip()[:120],
        "age": age_raw,
        "gender": request.form.get("gender", "").strip(),
        "city": request.form.get("city", "").strip()[:120],
        "marital_status": request.form.get("marital_status", "").strip()[:60],
        "education": request.form.get("education", "").strip()[:150],
        "profession": request.form.get("profession", "").strip()[:150],
        "community": request.form.get("community", "").strip()[:150],
        "bio": request.form.get("bio", "").strip()[:1500],
        "contact_number": request.form.get("contact_number", "").strip()[:20],
    }


@app.route("/admin/profiles/add", methods=["GET", "POST"])
@admin_required
def admin_add_profile():
    db = get_db()
    if request.method == "POST":
        f = _profile_form_fields()
        errors = []
        if not (f["name"] and f["age"] and f["gender"] and f["city"]):
            errors.append("Name, age, gender and city are required.")
        try:
            age = int(f["age"])
            if age < 18 or age > 90:
                errors.append("Age must be between 18 and 90.")
        except ValueError:
            errors.append("Age must be a number.")
            age = None

        original_name = preview_name = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                original_name, preview_name = save_profile_photo(photo)
            except ImageValidationError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_add_profile.html", form=f)

        profile_code = next_profile_code(db)
        db.execute(
            """
            INSERT INTO profiles
            (profile_code, name, age, gender, city, marital_status, education, profession,
             community, bio, contact_number, photo_original_name, photo_preview_name,
             is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                profile_code, f["name"], age, f["gender"], f["city"], f["marital_status"],
                f["education"], f["profession"], f["community"], f["bio"], f["contact_number"],
                original_name, preview_name, datetime.now().isoformat(),
            ),
        )
        db.commit()
        flash(f"Profile {profile_code} added.", "success")
        return redirect(url_for("admin_profiles"))

    return render_template("admin_add_profile.html", form=None)


@app.route("/admin/profiles/<int:profile_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_profile(profile_id):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not profile:
        abort(404)

    if request.method == "POST":
        f = _profile_form_fields()
        errors = []
        if not (f["name"] and f["age"] and f["gender"] and f["city"]):
            errors.append("Name, age, gender and city are required.")
        try:
            age = int(f["age"])
            if age < 18 or age > 90:
                errors.append("Age must be between 18 and 90.")
        except ValueError:
            errors.append("Age must be a number.")
            age = None

        new_original, new_preview = None, None
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                new_original, new_preview = save_profile_photo(photo)
            except ImageValidationError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_edit_profile.html", profile=profile, form=f)

        if new_original:
            delete_file_quietly(PRIVATE_ORIGINALS_DIR, profile["photo_original_name"])
            delete_file_quietly(PREVIEW_DIR, profile["photo_preview_name"])
            db.execute(
                "UPDATE profiles SET photo_original_name=?, photo_preview_name=? WHERE id=?",
                (new_original, new_preview, profile_id),
            )

        db.execute(
            """
            UPDATE profiles SET name=?, age=?, gender=?, city=?, marital_status=?, education=?,
            profession=?, community=?, bio=?, contact_number=? WHERE id=?
            """,
            (
                f["name"], age, f["gender"], f["city"], f["marital_status"], f["education"],
                f["profession"], f["community"], f["bio"], f["contact_number"], profile_id,
            ),
        )
        db.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("admin_profiles"))

    return render_template("admin_edit_profile.html", profile=profile, form=None)


@app.route("/admin/profiles/<int:profile_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_profile(profile_id):
    db = get_db()
    db.execute("UPDATE profiles SET is_active = 1 - is_active WHERE id = ?", (profile_id,))
    db.commit()
    return redirect(url_for("admin_profiles"))


@app.route("/admin/profiles/<int:profile_id>/delete", methods=["POST"])
@admin_required
def admin_delete_profile(profile_id):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if profile:
        delete_file_quietly(PRIVATE_ORIGINALS_DIR, profile["photo_original_name"])
        delete_file_quietly(PREVIEW_DIR, profile["photo_preview_name"])
        db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        db.commit()
        flash("Profile deleted.", "success")
    return redirect(url_for("admin_profiles"))


# ======================================================================
# ADMIN — UNLOCK REQUESTS
# ======================================================================
@app.route("/admin/requests")
@admin_required
def admin_requests():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    query = """
        SELECT ur.*, p.profile_code, p.name AS profile_name
        FROM unlock_requests ur JOIN profiles p ON p.id = ur.profile_id
    """
    if status_filter != "all":
        query += " WHERE ur.status = ? ORDER BY ur.requested_at DESC"
        rows = db.execute(query, (status_filter,)).fetchall()
    else:
        query += " ORDER BY ur.requested_at DESC"
        rows = db.execute(query).fetchall()
    return render_template("admin_requests.html", rows=rows, status_filter=status_filter)


@app.route("/admin/payment-proof/<int:request_id>")
@admin_required
def admin_payment_proof(request_id):
    db = get_db()
    row = db.execute("SELECT * FROM unlock_requests WHERE id = ?", (request_id,)).fetchone()
    if not row or not row["payment_proof_name"]:
        abort(404)
    return send_from_directory(PRIVATE_PROOFS_DIR, row["payment_proof_name"])


@app.route("/admin/requests/<int:request_id>/<action>", methods=["POST"])
@admin_required
def admin_decide_request(request_id, action):
    if action not in ("unlock", "reject"):
        abort(400)
    db = get_db()
    new_status = "unlocked" if action == "unlock" else "rejected"
    db.execute(
        "UPDATE unlock_requests SET status = ?, decided_at = ? WHERE id = ?",
        (new_status, datetime.now().isoformat(), request_id),
    )
    db.commit()
    flash(f"Request marked as {new_status}.", "success")
    return redirect(url_for("admin_requests"))


# ======================================================================
# ERROR PAGES
# ======================================================================
@app.errorhandler(404)
def err_404(e):
    return render_template("error.html", code=404, title="Page Not Found",
                            message="The page you're looking for doesn't exist or has moved."), 404


@app.errorhandler(403)
def err_403(e):
    return render_template("error.html", code=403, title="Access Denied",
                            message="You don't have permission to view this."), 403


@app.errorhandler(413)
def err_413(e):
    return render_template("error.html", code=413, title="File Too Large",
                            message="The file you tried to upload is too large."), 413


@app.errorhandler(400)
def err_400(e):
    return render_template("error.html", code=400, title="Something Went Wrong",
                            message="Your form session expired. Please go back and try again."), 400


@app.errorhandler(500)
def err_500(e):
    return render_template("error.html", code=500, title="Something Went Wrong",
                            message="An unexpected error occurred. Please try again shortly."), 500


# ======================================================================
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
else:
    # Gunicorn / production import path never runs __main__.
    init_db()
