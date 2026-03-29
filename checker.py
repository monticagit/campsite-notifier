#!/usr/bin/env python3
"""
California Beach Campsite Availability Checker
Supports two modes:
  - Local loop:  python checker.py             (reads config.json, runs forever)
  - GitHub CI:   python checker.py --once      (reads subscribers.json, runs once)
"""

import requests
import json
import time
import smtplib
import os
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT             = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE      = os.path.join(ROOT, "config.json")
SUBSCRIBERS_FILE = os.path.join(ROOT, "subscribers.json")
STATE_FILE       = os.path.join(ROOT, "state.json")
BASE_URL         = "https://calirdr.usedirect.com/rdr/rdr"

# ── Park name → API search term ────────────────────────────────────────────
BEACH_PARKS = {
    # Big Sur
    "Kirk Creek Campground":              "Kirk Creek",
    "Limekiln State Park":                "Limekiln",
    "Julia Pfeiffer Burns State Park":    "Julia Pfeiffer",
    "Plaskett Creek Campground":          "Plaskett",
    "Andrew Molera State Park":           "Andrew Molera",
    "Pfeiffer Big Sur State Park":        "Pfeiffer Big Sur",
    # Santa Cruz & Aptos
    "New Brighton State Beach":           "New Brighton",
    "Manresa Uplands State Beach":        "Manresa",
    "Sunset State Beach":                 "Sunset",
    "Seacliff State Beach":               "Seacliff",
    # Monterey
    "Asilomar State Beach":               "Asilomar",
    # Central Coast
    "Morro Bay State Park":               "Morro Bay",
    "Morro Strand State Beach":           "Morro Strand",
    "Pismo State Beach - Oceano":         "Oceano",
    "El Capitan State Beach":             "El Capitan",
    "Refugio State Beach":                "Refugio",
    "Emma Wood State Beach":              "Emma Wood",
    "McGrath State Beach":                "McGrath",
    # Southern CA
    "Leo Carrillo State Park":            "Leo Carrillo",
    "Point Mugu State Park":              "Point Mugu",
    "Doheny State Beach":                 "Doheny",
    "San Clemente State Beach":           "San Clemente",
    "Crystal Cove State Park":            "Crystal Cove",
    "Bolsa Chica State Beach":            "Bolsa Chica",
    "Silver Strand State Beach":          "Silver Strand",
    "Carpinteria State Beach":            "Carpinteria",
    # Northern CA
    "Bodega Dunes (Sonoma Coast SP)":     "Bodega Dunes",
    "Wrights Beach (Sonoma Coast SP)":    "Wrights Beach",
    "MacKerricher State Park":            "MacKerricher",
    "Westport-Union Landing SB":          "Westport",
    "Half Moon Bay State Beach":          "Half Moon Bay",
    "Bean Hollow State Beach":            "Bean Hollow",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.reservecalifornia.com/",
}


# ── API helpers ────────────────────────────────────────────────────────────

def discover_facility_id(search_name):
    try:
        url = f"{BASE_URL}/fd/facilities/namecontains/{quote(search_name)}/web/true/active/true"
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not isinstance(data, list):
            return None
        for facility in data:
            ftype = str(facility.get("FacilityType", "")).lower()
            fname = str(facility.get("Name", "")).lower()
            if "camp" in ftype or "camp" in fname or "beach" in fname:
                return facility.get("FacilityId") or facility.get("Id")
        return data[0].get("FacilityId") or data[0].get("Id")
    except Exception as e:
        print(f"    Discovery error ({search_name}): {e}")
        return None


def check_availability(facility_id, start_date, nights):
    end_date = start_date + timedelta(days=nights)
    payload = {
        "FacilityID":       str(facility_id),
        "StartDate":        start_date.strftime("%m-%d-%Y"),
        "EndDate":          end_date.strftime("%m-%d-%Y"),
        "Nights":           nights,
        "UnitTypeId":       0,
        "InSeasonOnly":     "false",
        "WebOnly":          "true",
        "IsADA":            "false",
        "SleepingUnitId":   83,
        "MinVehicleLength": 0,
        "UnitSort":         "orderby",
        "NightlyRate":      0,
        "ShowPopup":        "false",
        "RestrictADA":      "false",
    }
    resp = requests.post(f"{BASE_URL}/search/grid", json=payload, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_available_sites(data, nights):
    available = []
    units = (
        data.get("AvailableUnits")
        or data.get("Facility", {}).get("Units")
        or data.get("Units")
        or {}
    )
    if not isinstance(units, dict):
        return available
    for unit_id, unit in units.items():
        if not isinstance(unit, dict):
            continue
        slices = unit.get("Slices", {})
        if isinstance(slices, dict) and slices:
            free = sum(1 for s in slices.values() if isinstance(s, dict) and s.get("IsFree"))
            is_avail = free >= nights
        else:
            is_avail = bool(unit.get("IsFree") or unit.get("Available"))
        if is_avail:
            available.append({
                "id":   str(unit_id),
                "name": unit.get("Name", f"Site {unit_id}"),
                "type": unit.get("UnitTypeName", "Campsite"),
            })
    return available


# ── Email ──────────────────────────────────────────────────────────────────

def send_alert_email(gmail, app_password, to_email, subscriber_name, alerts, interval_min):
    park_names = [a["park"] for a in alerts]
    subject = (
        f"🏕 Beach Campsite Open! "
        f"{', '.join(park_names[:2])}"
        f"{'...' if len(park_names) > 2 else ''}"
    )

    blocks_html = ""
    blocks_text = ""
    for alert in alerts:
        site_items = "".join(
            f"<li>🏕 <strong>{s['name']}</strong> &mdash; {s['type']}</li>"
            for s in alert["sites"][:6]
        )
        more = f"<li style='color:#888'>+ {len(alert['sites'])-6} more…</li>" if len(alert["sites"]) > 6 else ""
        blocks_html += f"""
        <div style="background:#f0faf5;border-left:4px solid #2d6a4f;
                    padding:16px 20px;margin:16px 0;border-radius:6px">
          <h3 style="margin:0 0 4px;color:#1b4332;font-family:Georgia,serif">{alert['park']}</h3>
          <p style="margin:0 0 10px;color:#555;font-size:14px">📅 {alert['dates']}</p>
          <ul style="margin:0;padding-left:20px;color:#333;font-size:14px">{site_items}{more}</ul>
        </div>"""
        blocks_text += f"\n📍 {alert['park']}\n   {alert['dates']}\n"
        blocks_text += "\n".join(f"   • {s['name']} ({s['type']})" for s in alert["sites"]) + "\n"

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f0;font-family:Georgia,serif">
<div style="max-width:600px;margin:32px auto;border-radius:12px;overflow:hidden;
            box-shadow:0 8px 40px rgba(0,0,0,0.15)">
  <div style="background:linear-gradient(135deg,#1b4332 0%,#2d6a4f 100%);
              padding:32px 24px;text-align:center;color:white">
    <div style="font-size:48px;margin-bottom:8px">🏕</div>
    <h1 style="margin:0;font-size:28px;font-weight:400">Hey {subscriber_name}!</h1>
    <p style="margin:8px 0 0;opacity:0.8;font-size:15px">A beach campsite just opened up</p>
  </div>
  <div style="background:white;padding:24px 28px">
    {blocks_html}
    <div style="text-align:center;margin:28px 0 20px">
      <a href="https://www.reservecalifornia.com"
         style="display:inline-block;background:#2d6a4f;color:white;
                padding:14px 36px;border-radius:8px;text-decoration:none;
                font-size:16px;font-family:sans-serif;font-weight:500">
        Book Now on ReserveCalifornia →
      </a>
    </div>
    <p style="color:#aaa;font-size:12px;text-align:center;margin-top:20px;font-family:sans-serif">
      California Beach Campsite Monitor · Checking every {interval_min} minutes
    </p>
  </div>
</div>
</body></html>"""

    plain = f"Hey {subscriber_name}! Beach campsites opened:\n{blocks_text}\nBook at: https://www.reservecalifornia.com"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail
    msg["To"]      = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail, app_password)
        smtp.send_message(msg)
    print(f"    📧 Email sent → {to_email}")


# ── Per-subscriber check ───────────────────────────────────────────────────

def check_subscriber(subscriber, state, gmail, app_password):
    name        = subscriber.get("name", "Friend")
    email       = subscriber["notify_email"]
    parks       = subscriber["parks"]
    start_date  = datetime.strptime(subscriber["start_date"], "%Y-%m-%d")
    nights      = int(subscriber["nights"])
    end_date    = start_date + timedelta(days=nights)
    date_label  = f"{start_date.strftime('%b %-d')}–{end_date.strftime('%-d, %Y')} ({nights} nights)"

    facility_ids = state.setdefault("facility_ids", {})
    sub_state    = state.setdefault("subscribers", {}).setdefault(email, {"available": {}})
    prev_avail   = sub_state.get("available", {})
    curr_avail   = {}
    alerts       = []

    print(f"\n  👤 {name} ({email})")

    for park_name in parks:
        search_term = BEACH_PARKS.get(park_name, park_name.split()[0])

        if park_name not in facility_ids:
            print(f"    🔍 Finding '{park_name}'…")
            fid = discover_facility_id(search_term)
            if fid:
                facility_ids[park_name] = fid
                print(f"       ✅ ID {fid}")
            else:
                print(f"       ⚠️  Not found — skipping")
                continue

        fid = facility_ids[park_name]
        try:
            data  = check_availability(fid, start_date, nights)
            sites = extract_available_sites(data, nights)

            curr_ids = set(s["id"] for s in sites)
            prev_ids = set(prev_avail.get(park_name, []))
            new_ids  = curr_ids - prev_ids
            curr_avail[park_name] = list(curr_ids)

            if new_ids:
                new_sites = [s for s in sites if s["id"] in new_ids]
                alerts.append({"park": park_name, "sites": new_sites, "dates": date_label})
                print(f"    🎉 NEW at {park_name}: {len(new_sites)} site(s)!")
            else:
                status = f"{len(sites)} open" if sites else "none"
                print(f"    😴 {park_name}: {status}")

        except requests.HTTPError as e:
            print(f"    ❌ {park_name}: HTTP {e.response.status_code}")
            curr_avail[park_name] = list(prev_avail.get(park_name, []))
        except Exception as e:
            print(f"    ❌ {park_name}: {e}")
            curr_avail[park_name] = list(prev_avail.get(park_name, []))

    sub_state["available"]    = curr_avail
    sub_state["last_checked"] = datetime.now().isoformat()

    if alerts:
        interval = 15  # default for CI mode
        try:
            send_alert_email(gmail, app_password, email, name, alerts, interval)
        except Exception as e:
            print(f"    ❌ Email failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────

def run_once(gmail, app_password):
    """Single-pass mode for GitHub Actions."""
    if not os.path.exists(SUBSCRIBERS_FILE):
        print("❌  subscribers.json not found.")
        sys.exit(1)

    with open(SUBSCRIBERS_FILE) as f:
        subscribers = json.load(f)

    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                state = {}

    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n🏕  Campsite Check — {ts}")
    print(f"   {len(subscribers)} subscriber(s)\n")

    for sub in subscribers:
        check_subscriber(sub, state, gmail, app_password)

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print("\n✅ Done. State saved.")


def run_loop(config):
    """Continuous loop mode for local use."""
    gmail       = config["gmail"]
    app_pass    = config["app_password"]
    interval    = int(config.get("interval_minutes", 15)) * 60

    # Convert single-config to subscriber format
    subscriber = {
        "name":         "You",
        "notify_email": config["notify_email"],
        "parks":        config["parks"],
        "start_date":   config["start_date"],
        "nights":       config["nights"],
    }

    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                state = {}

    print("\n🏕  California Beach Campsite Monitor (local mode)")
    print("─" * 50)
    print(f"Parks    : {', '.join(config['parks'][:3])}{'...' if len(config['parks'])>3 else ''}")
    print(f"Dates    : {config['start_date']}  ×  {config['nights']} nights")
    print(f"Interval : every {config.get('interval_minutes',15)} minutes")
    print("─" * 50 + "\n")

    check_num = 0
    while True:
        check_num += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Check #{check_num}")
        check_subscriber(subscriber, state, gmail, app_pass)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        next_t = (datetime.now() + timedelta(seconds=interval)).strftime("%H:%M:%S")
        print(f"  ⏰ Next check at {next_t}\n")
        time.sleep(interval)


def main():
    once_mode = "--once" in sys.argv

    if once_mode:
        gmail    = os.environ.get("GMAIL_ADDRESS", "")
        app_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
        if not gmail or not app_pass:
            print("❌  Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD environment variables (GitHub Secrets).")
            sys.exit(1)
        run_once(gmail, app_pass)
    else:
        if not os.path.exists(CONFIG_FILE):
            print(f"❌  config.json not found. Open index.html to generate one.")
            sys.exit(1)
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        run_loop(config)


if __name__ == "__main__":
    main()
