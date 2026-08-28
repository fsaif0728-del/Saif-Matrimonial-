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
ADMIN_USERNAME = os.environ.ge
