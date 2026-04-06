import os
import json
from flask import Flask, request, render_template, jsonify, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
import anthropic
import gspread
from datetime import datetime, timedelta

load_dotenv()

app = Flask(__name__)
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
twilio_client = Client(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))

# ── Google Sheets client ──────────────────────────────────────────────────────
def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        return gspread.service_account_from_dict(json.loads(creds_json))
    return gspread.service_account(filename="credentials.json")

# ── Clinic configurations ─────────────────────────────────────────────────────
CLINICS = {
    "+16625164516": {
        "name": "Melbourne Physio",
        "address": "123 Collins Street, Melbourne VIC 3000",
        "phone": "(03) 9000 0000",
        "hours": "Monday to Friday, 8am to 6pm",
        "practitioners": [
            "Dr. James Chen — General Physiotherapy",
            "Dr. Sarah Mills — Sports Physiotherapy",
            "Dr. Anika Patel — Clinical Pilates",
        ],
        "services": ["Physiotherapy", "Sports rehab", "Pilates", "Dry needling", "Massage"],
        "parking": "On-site parking, first 30 minutes free",
        "receptionist_name": "Sophie",
        "sheet_id": os.environ.get("SHEET_ID", "1Mrvkgwi62F1CZ0MEe5RavIc4L8wVuAO_I1SD0WxyP-E"),
        "twilio_number": "whatsapp:+14155238886",
    },
}

DEFAULT_CLINIC = {
    "name": "the clinic",
    "phone": "our main line",
    "hours": "business hours",
    "practitioners": [],
    "services": [],
    "receptionist_name": "Sophie",
    "sheet_id": "",
    "twilio_number": "whatsapp:+14155238886",
}

conversations = {}


def get_clinic(twilio_number: str) -> dict:
    return CLINICS.get(twilio_number, list(CLINICS.values())[0])


def build_system_prompt(clinic: dict) -> str:
    practitioners_list = "\n".join(f"  - {p}" for p in clinic["practitioners"])
    services_list = ", ".join(clinic["services"])
    return f"""
You are {clinic['receptionist_name']}, the friendly virtual receptionist at {clinic['name']} in Melbourne, Australia.

Your personality:
- Warm, empathetic, genuinely caring like a great human receptionist
- Conversational and natural, NEVER robotic or use numbered menus
- Use the patient name once you know it
- Light Australian warmth: No worries, Absolutely, Of course
- Keep replies concise, 2 to 4 sentences max

Clinic details:
- Name: {clinic['name']}
- Phone: {clinic['phone']}
- Hours: {clinic['hours']}

Practitioners:
{practitioners_list}

Services: {services_list}

Available slots: Monday to Friday at 9am, 11am, 2pm, 4pm.

Rules:
- NEVER reveal you are AI unless patient directly asks
- If patient seems distressed or asks complex medical questions, offer to connect with human team
- Never make up appointment details you do not have
- Always end with an open question or offer to help further
- If patient replies YES to a reminder, update their status as confirmed
- If patient replies NO, help them reschedule
"""


def get_ai_reply(sender: str, clinic_number: str, patient_message: str) -> str:
    clinic = get_clinic(clinic_number)
    system_prompt = build_system_prompt(clinic)
    conv_key = f"{sender}:{clinic_number}"

    if conv_key not in conversations:
        conversations[conv_key] = []

    conversations[conv_key].append({"role": "user", "content": patient_message})
    history = conversations[conv_key][-20:]

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system_prompt,
            messages=history
        )
        ai_reply = response.content[0].text
        conversations[conv_key].append({"role": "assistant", "content": ai_reply})

        # Auto-update status based on patient reply
        msg_lower = patient_message.strip().lower()
        if msg_lower in ["yes", "y", "confirm", "confirmed", "yep", "yeah", "sure"]:
            update_patient_status_by_phone(sender.replace("whatsapp:", ""), "Confirmed")
        elif msg_lower in ["no", "n", "cancel", "cancelled"]:
            update_patient_status_by_phone(sender.replace("whatsapp:", ""), "Cancelled")

        return ai_reply
    except Exception as e:
        print(f"Claude API error: {e}")
        return f"So sorry, having a little trouble right now. Please call {clinic['phone']} and our team will help!"


def update_patient_status_by_phone(phone: str, status: str):
    """Update patient status in Google Sheet when they reply."""
    try:
        for clinic in CLINICS.values():
            if not clinic.get("sheet_id"):
                continue
            gc = get_gspread_client()
            sh = gc.open_by_key(clinic["sheet_id"])
            ws = sh.get_worksheet(0)
            records = ws.get_all_records()
            headers = ws.row_values(1)

            phone_clean = phone.replace(" ", "").replace("+", "")
            status_col = headers.index("Status") + 1 if "Status" in headers else None

            if not status_col:
                ws.update_cell(1, len(headers) + 1, "Status")
                status_col = len(headers) + 1

            for i, row in enumerate(records, 2):
                row_phone = str(row.get("Phone Number", "")).replace(" ", "").replace("+", "")
                if row_phone == phone_clean:
                    ws.update_cell(i, status_col, status)
                    print(f"✅ Updated {phone} status to {status}")
                    break
    except Exception as e:
        print(f"Status update error: {e}")


def send_whatsapp(to_number: str, message: str, from_number: str) -> bool:
    """Send a WhatsApp message via Twilio."""
    try:
        if not to_number.startswith("+"):
            to_number = "+" + to_number
        twilio_client.messages.create(
            from_=from_number,
            to=f"whatsapp:{to_number}",
            body=message
        )
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    sender = request.values.get("From", "")
    clinic_number = request.values.get("To", "").replace("whatsapp:", "")
    clinic = get_clinic(clinic_number)
    print(f"\n📨 [{clinic['name']}] From {sender}: {incoming_msg}")
    reply = get_ai_reply(sender, clinic_number, incoming_msg)
    print(f"💬 Sophie: {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/dashboard")
def dashboard():
    return send_from_directory("templates", "dashboard.html")


@app.route("/appointments")
def get_appointments():
    """Return appointments for a specific date."""
    date_str = request.args.get("date", "")
    if not date_str:
        today = datetime.now()
        date_str = today.strftime("%d/%m/%Y")

    clinic = list(CLINICS.values())[0]
    sheet_id = clinic.get("sheet_id", "")

    if not sheet_id:
        return jsonify({"appointments": [], "clinic_name": clinic["name"]})

    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(sheet_id)
        ws = sh.get_worksheet(0)
        records = ws.get_all_records()

        appointments = [
            row for row in records
            if str(row.get("Appointment Date", "")).strip() == date_str.strip()
        ]

        return jsonify({
            "appointments": appointments,
            "clinic_name": clinic["name"],
            "date": date_str
        })
    except Exception as e:
        print(f"Dashboard error: {e}")
        return jsonify({"appointments": [], "clinic_name": clinic["name"], "error": str(e)})


@app.route("/book", methods=["GET", "POST"])
def book():
    if request.method == "GET":
        return send_from_directory("templates", "dashboard.html")

    data = request.get_json()
    name         = data.get("name", "").strip()
    phone        = data.get("phone", "").strip()
    date         = data.get("date", "")
    time         = data.get("time", "")
    practitioner = data.get("practitioner", "Any available")

    try:
        formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except:
        formatted_date = date

    try:
        clinic = list(CLINICS.values())[0]
        gc = get_gspread_client()
        sh = gc.open_by_key(clinic["sheet_id"])
        ws = sh.get_worksheet(0)
        headers = ws.row_values(1)

        # Build row matching headers
        row = []
        for h in headers:
            if h == "Patient Name": row.append(name)
            elif h == "Phone Number": row.append(phone)
            elif h == "Appointment Date": row.append(formatted_date)
            elif h == "Appointment Time": row.append(time)
            elif h == "Reminder Sent": row.append("")
            elif h == "Status": row.append("")
            elif h == "Practitioner": row.append(practitioner)
            else: row.append("")

        if not row:
            row = [name, phone, formatted_date, time, "", "", practitioner]

        ws.append_row(row)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Booking error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/send_reminder", methods=["POST"])
def send_reminder_manual():
    """Manually send a reminder to a patient from the dashboard."""
    data = request.get_json()
    name  = data.get("name", "")
    phone = data.get("phone", "")

    clinic = list(CLINICS.values())[0]

    message = (
        f"Hi {name}! This is Sophie from {clinic['name']} 😊\n\n"
        f"Just a friendly reminder about your upcoming appointment.\n\n"
        f"Could you reply YES to confirm, or NO if you need to reschedule? "
        f"I'm here to help either way!"
    )

    success = send_whatsapp(phone, message, clinic["twilio_number"])

    if success:
        try:
            gc = get_gspread_client()
            sh = gc.open_by_key(clinic["sheet_id"])
            ws = sh.get_worksheet(0)
            records = ws.get_all_records()
            headers = ws.row_values(1)
            reminder_col = headers.index("Reminder Sent") + 1 if "Reminder Sent" in headers else None

            if reminder_col:
                phone_clean = phone.replace(" ", "").replace("+", "")
                for i, row in enumerate(records, 2):
                    row_phone = str(row.get("Phone Number", "")).replace(" ", "").replace("+", "")
                    if row_phone == phone_clean:
                        ws.update_cell(i, reminder_col, "Yes")
                        break
        except Exception as e:
            print(f"Sheet update error: {e}")

    return jsonify({"success": success})


@app.route("/update_status", methods=["POST"])
def update_status():
    """Update appointment status from dashboard."""
    data = request.get_json()
    name   = data.get("name", "")
    date   = data.get("date", "")
    status = data.get("status", "")

    try:
        clinic = list(CLINICS.values())[0]
        gc = get_gspread_client()
        sh = gc.open_by_key(clinic["sheet_id"])
        ws = sh.get_worksheet(0)
        records = ws.get_all_records()
        headers = ws.row_values(1)

        status_col = headers.index("Status") + 1 if "Status" in headers else None
        if not status_col:
            ws.update_cell(1, len(headers) + 1, "Status")
            status_col = len(headers) + 1

        for i, row in enumerate(records, 2):
            if row.get("Patient Name") == name and row.get("Appointment Date") == date:
                ws.update_cell(i, status_col, status)
                return jsonify({"success": True})

        return jsonify({"success": False, "error": "Patient not found"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "Sophie AI Receptionist",
        "clinics": len(CLINICS),
        "dashboard": "/dashboard"
    })


if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print("✅ Claude API key found" if api_key else "⚠️  ANTHROPIC_API_KEY not set!")
    print(f"🏥 {len(CLINICS)} clinic(s) configured")
    print(f"📊 Dashboard: http://localhost:3900/dashboard")
    print(f"📱 Webhook: http://localhost:3900/whatsapp")
    app.run(debug=True, port=3900)