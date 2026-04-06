import os
import json
import gspread
from twilio.rest import Client
from datetime import datetime, timedelta
from dotenv import load_dotenv
import schedule
import time

load_dotenv()

TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

CLINICS = {
    "Melbourne Physio": {
        "twilio_number": "whatsapp:+14155238886",
        "sheet_id": os.environ.get("SHEET_ID", "1Mrvkgwi62F1CZ0MEe5RavIc4L8wVuAO_I1SD0WxyP-E"),
        "phone": "(03) 9000 0000",
    },
}

TIME_SLOTS = {
    "9:00 AM":  9,
    "11:00 AM": 11,
    "2:00 PM":  14,
    "4:00 PM":  16,
}


def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        return gspread.service_account_from_dict(json.loads(creds_json))
    return gspread.service_account(filename="credentials.json")


def get_appointments(sheet_id: str, target_date: str) -> list:
    if not sheet_id:
        return []
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(sheet_id)
        ws = sh.get_worksheet(0)
        records = ws.get_all_records()
        return [
            {"row": i + 2, **row}
            for i, row in enumerate(records)
            if str(row.get("Appointment Date", "")).strip() == target_date
            and str(row.get("Status", "")).lower() not in ["cancelled", "no"]
        ]
    except Exception as e:
        print(f"Sheet error: {e}")
        return []


def send_whatsapp(phone: str, message: str, from_number: str) -> bool:
    if not phone.startswith("+"):
        phone = "+" + phone
    try:
        twilio_client.messages.create(
            from_=from_number,
            to=f"whatsapp:{phone}",
            body=message
        )
        return True
    except Exception as e:
        print(f"Send error: {e}")
        return False


def mark_reminder_sent(sheet_id: str, row_num: int, reminder_type: str):
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(sheet_id)
        ws = sh.get_worksheet(0)
        headers = ws.row_values(1)

        col_name = f"Reminder {reminder_type}"
        if col_name not in headers:
            ws.update_cell(1, len(headers) + 1, col_name)
            col = len(headers) + 1
        else:
            col = headers.index(col_name) + 1

        ws.update_cell(row_num, col, "Sent")
    except Exception as e:
        print(f"Mark reminder error: {e}")


def get_appointment_datetime(date_str: str, time_str: str) -> datetime:
    try:
        date = datetime.strptime(date_str, "%d/%m/%Y")
        hour = TIME_SLOTS.get(time_str, 9)
        return date.replace(hour=hour, minute=0, second=0)
    except:
        return None


def check_and_send_reminders():
    now = datetime.now()
    today_str = now.strftime("%d/%m/%Y")
    tomorrow_str = (now + timedelta(days=1)).strftime("%d/%m/%Y")

    print(f"\n⏰ Checking reminders — {now.strftime('%d/%m/%Y %H:%M')}")

    for clinic_name, config in CLINICS.items():
        sheet_id = config["sheet_id"]
        if not sheet_id:
            continue

        # Get today AND tomorrow appointments
        appointments = (
            get_appointments(sheet_id, today_str) +
            get_appointments(sheet_id, tomorrow_str)
        )

        for appt in appointments:
            name      = appt.get("Patient Name", "there")
            phone     = str(appt.get("Phone Number", "")).strip()
            date_str  = appt.get("Appointment Date", "")
            time_str  = appt.get("Appointment Time", "")
            row_num   = appt.get("row")

            if not phone:
                continue

            appt_dt = get_appointment_datetime(date_str, time_str)
            if not appt_dt:
                continue

            mins_until = (appt_dt - now).total_seconds() / 60

            # ── 24 HOUR REMINDER ─────────────────────────────────────────────
            sent_24hr = str(appt.get("Reminder 24hr", "")).lower() == "sent"
            if not sent_24hr and 1380 <= mins_until <= 1500:
                message = (
                    f"Hi {name}! This is Sophie from {clinic_name} 😊\n\n"
                    f"Just a friendly reminder that you have an appointment "
                    f"tomorrow ({date_str}) at {time_str}.\n\n"
                    f"Could you reply YES to confirm, or NO if you need to reschedule?"
                )
                if send_whatsapp(phone, message, config["twilio_number"]):
                    mark_reminder_sent(sheet_id, row_num, "24hr")
                    print(f"✅ 24hr reminder sent to {name}")

            # ── 1 HOUR REMINDER ──────────────────────────────────────────────
            sent_1hr = str(appt.get("Reminder 1hr", "")).lower() == "sent"
            if not sent_1hr and 50 <= mins_until <= 70:
                message = (
                    f"Hi {name}! Sophie here from {clinic_name} 😊\n\n"
                    f"Just a heads up — your appointment is in about 1 hour "
                    f"at {time_str} today.\n\n"
                    f"See you soon! Reply if you need anything."
                )
                if send_whatsapp(phone, message, config["twilio_number"]):
                    mark_reminder_sent(sheet_id, row_num, "1hr")
                    print(f"✅ 1hr reminder sent to {name}")

            # ── 30 MIN REMINDER (if no reply to 24hr) ────────────────────────
            sent_30min = str(appt.get("Reminder 30min", "")).lower() == "sent"
            status = str(appt.get("Status", "")).lower()
            if not sent_30min and sent_24hr and status not in ["confirmed", "yes"] and 25 <= mins_until <= 35:
                message = (
                    f"Hi {name}, Sophie from {clinic_name} again! 😊\n\n"
                    f"Your appointment is in 30 minutes at {time_str}.\n\n"
                    f"Quick reply YES to confirm you're on your way, "
                    f"or call us on {config['phone']} if you need to reschedule."
                )
                if send_whatsapp(phone, message, config["twilio_number"]):
                    mark_reminder_sent(sheet_id, row_num, "30min")
                    print(f"✅ 30min reminder sent to {name}")

    print("✅ Reminder check complete.")


# Run every 30 minutes
schedule.every(30).minutes.do(check_and_send_reminders)

if __name__ == "__main__":
    print("📅 Smart reminder scheduler started")
    print("⏰ Checks every 30 minutes")
    print("📬 Sends: 24hr reminder → 1hr reminder → 30min follow-up")
    print("🏥 Clinics:", list(CLINICS.keys()))

    if os.environ.get("GOOGLE_CREDENTIALS"):
        print("✅ Google credentials loaded from environment")
    elif os.path.exists("credentials.json"):
        print("✅ Google credentials loaded from file")
    else:
        print("⚠️  No Google credentials found!")

    print("\nRunning first check now...\n")
    check_and_send_reminders()

    while True:
        schedule.run_pending()
        time.sleep(60)