import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "matrimonial.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB uploads

# --------------------------------------------------------------------
# Config you should change before going live
# --------------------------------------------------------------------
BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "7762023966")
BUSINESS_LOCATION = os.environ.get("BUSINESS_LOCATION", "Kolkata, India")
UNLOCK_PRICE = os.environ.get("UNLOCK_PRICE", "11")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
# Default password is "changeme123" -- CHANGE THIS via env var ADMIN_PASSWORD before deploying
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")


# --------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
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
            photo_filename TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS unlock_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            user_phone TEXT NOT NULL,
            user_name TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles (id)
        );
        """
    )
    db.commit()
    db.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def next_profile_code(db):
    row = db.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()
    return f"SMS{10000 + row['c'] + 1}"


# --------------------------------------------------------------------
# Admin auth
# --------------------------------------------------------------------
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------
# Public (user-facing) routes
# --------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    profiles = db.execute(
        "SELECT * FROM profiles WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    return render_template(
        "index.html",
        profiles=profiles,
        price=UNLOCK_PRICE,
        phone=BUSINESS_PHONE,
        location=BUSINESS_LOCATION,
    )


@app.route("/profile/<profile_code>/unlock", methods=["GET", "POST"])
def unlock(profile_code):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ? AND is_active = 1", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)

    if request.method == "POST":
        user_phone = request.form.get("user_phone", "").strip()
        user_name = request.form.get("user_name", "").strip()
        if not user_phone:
            flash("Please enter your phone number.", "error")
        else:
            existing = db.execute(
                "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? "
                "AND status = 'pending'",
                (profile["id"], user_phone),
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO unlock_requests (profile_id, user_phone, user_name, status, requested_at) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (profile["id"], user_phone, user_name, datetime.now().isoformat()),
                )
                db.commit()
            return redirect(url_for("my_requests", phone=user_phone))

    return render_template(
        "unlock.html",
        profile=profile,
        price=UNLOCK_PRICE,
        phone=BUSINESS_PHONE,
    )


@app.route("/my-requests")
def my_requests():
    phone = request.args.get("phone", "").strip()
    db = get_db()
    requests_rows = []
    if phone:
        requests_rows = db.execute(
            """
            SELECT ur.*, p.profile_code, p.name, p.age, p.city, p.photo_filename
            FROM unlock_requests ur
            JOIN profiles p ON p.id = ur.profile_id
            WHERE ur.user_phone = ?
            ORDER BY ur.requested_at DESC
            """,
            (phone,),
        ).fetchall()
    return render_template("my_requests.html", phone=phone, rows=requests_rows)


@app.route("/profile/<profile_code>/full")
def profile_full(profile_code):
    phone = request.args.get("phone", "").strip()
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)

    unlocked = db.execute(
        "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? AND status = 'unlocked'",
        (profile["id"], phone),
    ).fetchone()

    if not unlocked:
        flash("This profile isn't unlocked for that phone number yet.", "error")
        return redirect(url_for("my_requests", phone=phone))

    return render_template("profile_full.html", profile=profile, business_phone=BUSINESS_PHONE)


# --------------------------------------------------------------------
# Admin routes
# --------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    total_profiles = db.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
    pending_count = db.execute(
        "SELECT COUNT(*) c FROM unlock_requests WHERE status = 'pending'"
    ).fetchone()["c"]
    unlocked_count = db.execute(
        "SELECT COUNT(*) c FROM unlock_requests WHERE status = 'unlocked'"
    ).fetchone()["c"]
    return render_template(
        "admin_dashboard.html",
        total_profiles=total_profiles,
        pending_count=pending_count,
        unlocked_count=unlocked_count,
    )


@app.route("/admin/profiles")
@admin_required
def admin_profiles():
    db = get_db()
    profiles = db.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
    return render_template("admin_profiles.html", profiles=profiles)


@app.route("/admin/profiles/add", methods=["GET", "POST"])
@admin_required
def admin_add_profile():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        age = request.form.get("age", "").strip()
        gender = request.form.get("gender", "").strip()
        city = request.form.get("city", "").strip()
        marital_status = request.form.get("marital_status", "").strip()
        education = request.form.get("education", "").strip()
        profession = request.form.get("profession", "").strip()
        community = request.form.get("community", "").strip()
        bio = request.form.get("bio", "").strip()
        contact_number = request.form.get("contact_number", "").strip()

        if not (name and age and gender and city):
            flash("Name, age, gender and city are required.", "error")
            return render_template("admin_add_profile.html")

        photo_filename = None
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename):
            code_hint = next_profile_code(db)
            ext = photo.filename.rsplit(".", 1)[1].lower()
            photo_filename = secure_filename(f"{code_hint}.{ext}")
            photo.save(os.path.join(app.config["UPLOAD_FOLDER"], photo_filename))

        profile_code = next_profile_code(db)
        db.execute(
            """
            INSERT INTO profiles
            (profile_code, name, age, gender, city, marital_status, education,
             profession, community, bio, contact_number, photo_filename, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_code, name, int(age), gender, city, marital_status, education,
                profession, community, bio, contact_number, photo_filename,
                datetime.now().isoformat(),
            ),
        )
        db.commit()
        flash(f"Profile {profile_code} added.", "success")
        return redirect(url_for("admin_profiles"))

    return render_template("admin_add_profile.html")


@app.route("/admin/profiles/<int:profile_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_profile(profile_id):
    db = get_db()
    db.execute(
        "UPDATE profiles SET is_active = 1 - is_active WHERE id = ?", (profile_id,)
    )
    db.commit()
    return redirect(url_for("admin_profiles"))


@app.route("/admin/profiles/<int:profile_id>/delete", methods=["POST"])
@admin_required
def admin_delete_profile(profile_id):
    db = get_db()
    db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    db.execute("DELETE FROM unlock_requests WHERE profile_id = ?", (profile_id,))
    db.commit()
    flash("Profile deleted.", "success")
    return redirect(url_for("admin_profiles"))


@app.route("/admin/requests")
@admin_required
def admin_requests():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    if status_filter == "all":
        rows = db.execute(
            """
            SELECT ur.*, p.profile_code, p.name
            FROM unlock_requests ur JOIN profiles p ON p.id = ur.profile_id
            ORDER BY ur.requested_at DESC
            """
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT ur.*, p.profile_code, p.name
            FROM unlock_requests ur JOIN profiles p ON p.id = ur.profile_id
            WHERE ur.status = ?
            ORDER BY ur.requested_at DESC
            """,
            (status_filter,),
        ).fetchall()
    return render_template("admin_requests.html", rows=rows, status_filter=status_filter)


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


# --------------------------------------------------------------------
if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        init_db()
    else:
        init_db()  # safe: CREATE TABLE IF NOT EXISTS
    app.run(host="0.0.0.0", port=5000, debug=True)
