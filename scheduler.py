import os
import json
import gspread
from twilio.rest import Client
from datetime import datetime, timedelta
from dotenv import load_dotenv
import schedule
import time

load_dotenv()

# ── Twilio credentials ────────────────────────────────────────────────────────
TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ── Google credentials from environment (no credentials.json file needed) ─────
def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds_dict = json.loads(creds_json)
        return gspread.service_account_from_dict(creds_dict)
    else:
        return gspread.service_account(filename="credentials.json")

# ── Clinic configurations ─────────────────────────────────────────────────────
CLINICS = {
    "Melbourne Physio": {
        "twilio_number": "whatsapp:+14155238886",
        "sheet_id": "1Mrvkgwi62F1CZ0MEe5RavIc4L8wVuAO_I1SD0WxyP-E",
    },
    "Collins Street Chiro": {
        "twilio_number": "whatsapp:+14155238886",
        "sheet_id": "",
    },
}


def get_tomorrows_appointments(sheet_id: str) -> list:
    """Read tomorrow's appointments from Google Sheet."""
    if not sheet_id:
        return []
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(sheet_id)
        worksheet = sh.get_worksheet(0)
        all_rows = worksheet.get_all_records()

        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d/%m/%Y")

        appointments = [
            row for row in all_rows
            if str(row.get("Appointment Date", "")).strip() == tomorrow
            and str(row.get("Reminder Sent", "")).strip().lower() != "yes"
        ]

        return appointments

    except Exception as e:
        print(f"Sheet error: {e}")
        return []


def send_reminder(patient: dict, clinic_name: str, twilio_number: str, sheet_id: str):
    """Send a WhatsApp reminder to one patient."""
    name       = patient.get("Patient Name", "there")
    phone      = str(patient.get("Phone Number", "")).strip()
    date       = patient.get("Appointment Date", "tomorrow")
    time_slot  = patient.get("Appointment Time", "your scheduled time")
    row_number = patient.get("Row Number")

    if not phone:
        print(f"❌ No phone number for {name} — skipping")
        return

    if not phone.startswith("+"):
        phone = "+" + phone

    message = (
        f"Hi {name}! This is Sophie from {clinic_name} 😊\n\n"
        f"Just a friendly reminder that you have an appointment "
        f"tomorrow ({date}) at {time_slot}.\n\n"
        f"Could you reply YES to confirm, or NO if you need to reschedule? "
        f"I'm here to help either way!"
    )

    try:
        twilio_client.messages.create(
            from_=twilio_number,
            to=f"whatsapp:{phone}",
            body=message
        )
        print(f"✅ Reminder sent to {name} ({phone})")

        # Mark reminder as sent in Google Sheet
        if row_number:
            gc = get_gspread_client()
            sh = gc.open_by_key(sheet_id)
            worksheet = sh.get_worksheet(0)
            headers = worksheet.row_values(1)
            if "Reminder Sent" in headers:
                col = headers.index("Reminder Sent") + 1
                worksheet.update_cell(row_number, col, "Yes")

    except Exception as e:
        print(f"❌ Failed to send to {name}: {e}")


def run_daily_reminders():
    """Main job — runs every day at 2pm, sends reminders for tomorrow."""
    print(f"\n🕐 Running daily reminders — {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    for clinic_name, config in CLINICS.items():
        print(f"\n🏥 Processing {clinic_name}...")

        if not config["sheet_id"]:
            print(f"   No sheet configured — skipping.")
            continue

        appointments = get_tomorrows_appointments(config["sheet_id"])

        if not appointments:
            print(f"   No appointments tomorrow or all reminders already sent.")
            continue

        print(f"   Found {len(appointments)} appointment(s) to remind:")

        for i, patient in enumerate(appointments, 1):
            patient["Row Number"] = i + 1
            send_reminder(
                patient,
                clinic_name,
                config["twilio_number"],
                config["sheet_id"]
            )

    print(f"\n✅ Daily reminders complete.")


# ── Schedule to run every day at 2:00pm ──────────────────────────────────────
schedule.every().day.at("14:00").do(run_daily_reminders)

if __name__ == "__main__":
    print("📅 Appointment reminder scheduler started")
    print("⏰ Will send reminders daily at 2:00pm")
    print("🏥 Clinics configured:", list(CLINICS.keys()))

    # Check credentials
    if os.environ.get("GOOGLE_CREDENTIALS"):
        print("✅ Google credentials loaded from environment")
    elif os.path.exists("credentials.json"):
        print("✅ Google credentials loaded from credentials.json")
    else:
        print("⚠️  No Google credentials found!")

    if TWILIO_SID and TWILIO_TOKEN:
        print("✅ Twilio credentials found")
    else:
        print("⚠️  Twilio credentials missing!")

    print("\nRunning first check now...\n")
    run_daily_reminders()

    while True:
        schedule.run_pending()
        time.sleep(60)