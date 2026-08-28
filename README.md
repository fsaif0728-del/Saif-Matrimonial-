# Saif Matrimonial Services — Web App

A pay-per-profile matrimonial site: browse blurred/locked profiles for free,
pay ₹11 via UPI to request one profile, admin manually confirms payment
(via WhatsApp screenshot) and unlocks it for that phone number.

## What's included
- **User side**: browse profiles → request unlock → scan QR & pay → check
  status on "My Requests" → view full profile once admin confirms.
- **Admin side**: login → add profiles (one at a time, with photo) →
  review pending unlock requests → confirm or reject.
- SQLite database (`matrimonial.db`, auto-created on first run) — no
  separate database server needed.

## 1. Run it locally (to test)

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

Default admin login: **admin / changeme123**
⚠️ Change this before going live — see step 2.

## 2. Before you deploy — required changes

1. **Change the admin password.** Set environment variables instead of
   using the defaults in `app.py`:
   ```bash
   export ADMIN_USERNAME="youradminname"
   export ADMIN_PASSWORD="a-strong-password"
   export SECRET_KEY="a-long-random-string"
   export BUSINESS_PHONE="7762023966"
   export UNLOCK_PRICE="11"
   ```
2. **Add your real UPI QR code image** at `static/upi_qr.png` (export it
   from Google Pay / PhonePe / your bank app — "Show QR code" → save image).
3. Review `admin_add_profile.html` if you want extra fields (e.g. father's
   name, height, sect) — easy to add as new columns.

## 3. Hosting (proper website)

This is a standard Flask app, so any Python host works. Easiest options:

- **Render.com** (free tier available): connect your GitHub repo → Render
  auto-detects Flask → set the env vars above in the dashboard → done.
  Start command: `gunicorn app:app`
- **PythonAnywhere**: good for beginners, has a free tier, upload files
  directly without git.
- **Railway.app**: similar to Render, very quick setup.

Whichever you pick:
- Set the environment variables from step 2 in that platform's dashboard
  (never commit passwords into the code).
- The SQLite file (`matrimonial.db`) and uploaded photos
  (`static/uploads/`) need **persistent storage** — on Render use a
  "Disk"; on PythonAnywhere the filesystem is already persistent.
- Point your domain (e.g. saifmatrimonial.com) at the host once it's live.

## 4. Important operational notes

- **Manual verification only works if you check requests regularly.**
  Check `/admin/requests` (or set a phone reminder) so paying users aren't
  left waiting.
- Real photos are uploaded by you (admin) with the person's consent —
  make sure you have permission from each person before publishing their
  photo and details.
- Consider adding a simple consent/terms checkbox to your profile intake
  form (outside this app) before you add someone's profile.
- The blur-until-paid photo technique is fine as a preview; just make sure
  every unlocked user actually gets the real, correct profile — that's
  what keeps this legitimate and keeps users trusting you.

## File structure
```
matrimonial_app/
├── app.py                  # Flask app (all routes)
├── requirements.txt
├── matrimonial.db          # auto-created SQLite DB (not in repo initially)
├── static/
│   ├── css/style.css
│   ├── upi_qr.png           # ADD YOUR REAL QR CODE HERE
│   └── uploads/              # profile photos land here
└── templates/
    ├── base.html
    ├── index.html            # profile gallery
    ├── unlock.html           # payment + request form
    ├── my_requests.html      # status checker
    ├── profile_full.html     # unlocked profile view
    ├── admin_login.html
    ├── admin_dashboard.html
    ├── admin_add_profile.html
    ├── admin_profiles.html
    └── admin_requests.html
```
