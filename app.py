import os
import json
from flask import Flask, request, jsonify, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
import anthropic
import gspread
from datetime import datetime, timedelta
from flask_cors import CORS

load_dotenv()

app = Flask(__name__, static_folder='frontend/dist', static_url_path='')
CORS(app)

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
twilio_client = Client(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_gc():
    creds = os.environ.get("GOOGLE_CREDENTIALS")
    if creds:
        return gspread.service_account_from_dict(json.loads(creds))
    return gspread.service_account(filename="credentials.json")

# ── Clinic config ─────────────────────────────────────────────────────────────
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
        "receptionist_name": "Sophie",
        "sheet_id": os.environ.get("SHEET_ID", "1Mrvkgwi62F1CZ0MEe5RavIc4L8wVuAO_I1SD0WxyP-E"),
        "twilio_number": "whatsapp:+14155238886",
    },
}

conversations = {}
# Store last message per patient for chat view
chat_log = {}  # phone -> list of {role, text, time}


def get_clinic(number):
    return CLINICS.get(number, list(CLINICS.values())[0])


def get_sheet():
    clinic = list(CLINICS.values())[0]
    gc = get_gc()
    sh = gc.open_by_key(clinic["sheet_id"])
    return sh.get_worksheet(0)


def build_prompt(clinic):
    prac = "\n".join(f"  - {p}" for p in clinic["practitioners"])
    return f"""You are {clinic['receptionist_name']}, the friendly virtual receptionist at {clinic['name']} in Melbourne, Australia.
Personality: Warm, empathetic, conversational. Never robotic. Use patient name once known. Light Australian warmth.
Keep replies 2-4 sentences max.
Clinic: {clinic['name']} | {clinic['phone']} | {clinic['hours']}
Practitioners:\n{prac}
Available slots: Mon-Fri at 9am, 11am, 2pm, 4pm.
Rules: Never reveal you are AI unless directly asked. If distressed or complex medical question, offer human team.
Always end with an open question or offer to help further.
If patient replies YES → they are confirming appointment. If NO → offer to reschedule."""


def get_ai_reply(sender, clinic_number, message):
    clinic = get_clinic(clinic_number)
    key = f"{sender}:{clinic_number}"
    if key not in conversations:
        conversations[key] = []
    conversations[key].append({"role": "user", "content": message})
    history = conversations[key][-20:]
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=build_prompt(clinic),
            messages=history
        )
        reply = resp.content[0].text
        conversations[key].append({"role": "assistant", "content": reply})

        # Update status based on reply
        msg_lower = message.strip().lower()
        if msg_lower in ["yes", "y", "confirm", "yep", "yeah", "sure"]:
            update_status_by_phone(sender.replace("whatsapp:", ""), "Confirmed")
        elif msg_lower in ["no", "n", "cancel", "cancelled"]:
            update_status_by_phone(sender.replace("whatsapp:", ""), "Cancelled")

        # Log to chat
        phone = sender.replace("whatsapp:", "")
        if phone not in chat_log:
            chat_log[phone] = []
        chat_log[phone].append({"role": "patient", "text": message, "time": datetime.now().strftime("%I:%M %p")})
        chat_log[phone].append({"role": "sophie", "text": reply, "time": datetime.now().strftime("%I:%M %p")})

        return reply
    except Exception as e:
        print(f"Claude error: {e}")
        return f"Sorry, having a little trouble. Please call {clinic['phone']} and our team will help!"


def update_status_by_phone(phone, status):
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        headers = ws.row_values(1)
        phone_clean = phone.replace(" ", "").replace("+", "")
        if "Status" not in headers:
            ws.update_cell(1, len(headers) + 1, "Status")
            headers.append("Status")
        status_col = headers.index("Status") + 1
        for i, row in enumerate(records, 2):
            rp = str(row.get("Phone Number", "")).replace(" ", "").replace("+", "")
            if rp == phone_clean:
                ws.update_cell(i, status_col, status)
                break
    except Exception as e:
        print(f"Status update error: {e}")


def send_whatsapp(phone, message, from_number):
    try:
        if not phone.startswith("+"):
            phone = "+" + phone
        twilio_client.messages.create(from_=from_number, to=f"whatsapp:{phone}", body=message)
        return True
    except Exception as e:
        print(f"Twilio error: {e}")
        return False


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get("Body", "").strip()
    sender = request.values.get("From", "")
    clinic_num = request.values.get("To", "").replace("whatsapp:", "")
    clinic = get_clinic(clinic_num)
    print(f"\n📨 [{clinic['name']}] {sender}: {msg}")
    reply = get_ai_reply(sender, clinic_num, msg)
    print(f"💬 Sophie: {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/api/clinic")
def api_clinic():
    clinic = list(CLINICS.values())[0]
    return jsonify({
        "name": clinic["name"],
        "phone": clinic["phone"],
        "hours": clinic["hours"],
        "address": clinic.get("address", ""),
        "practitioners": clinic["practitioners"],
        "services": clinic["services"],
    })


@app.route("/api/appointments")
def api_appointments():
    date_str = request.args.get("date", "")
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        if date_str and date_str != "all":
            records = [r for r in records if str(r.get("Appointment Date", "")).strip() == date_str]
        # Add row numbers
        all_rows = ws.get_all_records()
        for r in records:
            for i, row in enumerate(all_rows, 2):
                if row.get("Patient Name") == r.get("Patient Name") and row.get("Appointment Date") == r.get("Appointment Date"):
                    r["_row"] = i
                    break
        return jsonify({"appointments": records, "total": len(records)})
    except Exception as e:
        print(f"Appointments error: {e}")
        return jsonify({"appointments": [], "total": 0, "error": str(e)})


@app.route("/api/appointments/today")
def api_today():
    today = datetime.now().strftime("%d/%m/%Y")
    return api_appointments_for_date(today)


def api_appointments_for_date(date_str):
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        filtered = [r for r in records if str(r.get("Appointment Date", "")).strip() == date_str]
        return jsonify({"appointments": filtered, "total": len(filtered), "date": date_str})
    except Exception as e:
        return jsonify({"appointments": [], "total": 0})


@app.route("/api/stats")
def api_stats():
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        today = datetime.now().strftime("%d/%m/%Y")
        today_appts = [r for r in records if str(r.get("Appointment Date", "")).strip() == today]
        confirmed = len([r for r in today_appts if (r.get("Status") or "").lower() in ["confirmed", "yes"]])
        pending = len([r for r in today_appts if (r.get("Status") or "").lower() not in ["confirmed", "yes", "cancelled", "no"]])
        cancelled = len([r for r in today_appts if (r.get("Status") or "").lower() in ["cancelled", "no"]])

        # Week stats
        week_appts = []
        for i in range(7):
            d = (datetime.now() - timedelta(days=i)).strftime("%d/%m/%Y")
            week_appts.extend([r for r in records if str(r.get("Appointment Date", "")).strip() == d])
        week_noshows = len([r for r in week_appts if (r.get("Status") or "").lower() in ["cancelled", "no"]])

        return jsonify({
            "today_total": len(today_appts),
            "confirmed": confirmed,
            "pending": pending,
            "cancelled": cancelled,
            "week_noshows": week_noshows,
            "total_patients": len(set(r.get("Phone Number", "") for r in records if r.get("Phone Number"))),
            "confirmation_rate": round(confirmed / len(today_appts) * 100) if today_appts else 0,
        })
    except Exception as e:
        print(f"Stats error: {e}")
        return jsonify({"today_total": 0, "confirmed": 0, "pending": 0, "cancelled": 0, "week_noshows": 0, "total_patients": 0, "confirmation_rate": 0})


@app.route("/api/book", methods=["POST"])
def api_book():
    data = request.get_json()
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    date = data.get("date", "")
    time = data.get("time", "")
    practitioner = data.get("practitioner", "Any available")
    notes = data.get("notes", "")
    try:
        formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except:
        formatted_date = date
    try:
        ws = get_sheet()
        headers = ws.row_values(1)
        row = []
        for h in headers:
            if h == "Patient Name": row.append(name)
            elif h == "Phone Number": row.append(phone)
            elif h == "Appointment Date": row.append(formatted_date)
            elif h == "Appointment Time": row.append(time)
            elif h == "Practitioner": row.append(practitioner)
            elif h == "Notes": row.append(notes)
            else: row.append("")
        if not row:
            row = [name, phone, formatted_date, time, "", "", practitioner]
        ws.append_row(row)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/send_reminder", methods=["POST"])
def api_send_reminder():
    data = request.get_json()
    name = data.get("name", "")
    phone = data.get("phone", "")
    date = data.get("date", "")
    time_slot = data.get("time", "")
    clinic = list(CLINICS.values())[0]
    message = (
        f"Hi {name}! This is Sophie from {clinic['name']} 😊\n\n"
        f"Just a reminder about your appointment"
        + (f" on {date} at {time_slot}" if date and time_slot else "")
        + ".\n\nCould you reply YES to confirm, or NO if you need to reschedule? I'm here to help!"
    )
    # Log to chat
    if phone not in chat_log:
        chat_log[phone] = []
    chat_log[phone].append({"role": "sophie", "text": message, "time": datetime.now().strftime("%I:%M %p")})

    success = send_whatsapp(phone, message, clinic["twilio_number"])
    if success:
        try:
            ws = get_sheet()
            records = ws.get_all_records()
            headers = ws.row_values(1)
            reminder_col = headers.index("Reminder Sent") + 1 if "Reminder Sent" in headers else None
            if reminder_col:
                phone_clean = phone.replace(" ", "").replace("+", "")
                for i, row in enumerate(records, 2):
                    rp = str(row.get("Phone Number", "")).replace(" ", "").replace("+", "")
                    if rp == phone_clean:
                        ws.update_cell(i, reminder_col, "Yes")
                        break
        except Exception as e:
            print(f"Reminder sheet update error: {e}")
    return jsonify({"success": success})


@app.route("/api/update_status", methods=["POST"])
def api_update_status():
    data = request.get_json()
    name = data.get("name", "")
    date = data.get("date", "")
    status = data.get("status", "")
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        headers = ws.row_values(1)
        if "Status" not in headers:
            ws.update_cell(1, len(headers) + 1, "Status")
            headers.append("Status")
        status_col = headers.index("Status") + 1
        for i, row in enumerate(records, 2):
            if row.get("Patient Name") == name and row.get("Appointment Date") == date:
                ws.update_cell(i, status_col, status)
                return jsonify({"success": True})
        return jsonify({"success": False, "error": "Patient not found"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/chat/conversations")
def api_conversations():
    """Return all patients who have had WhatsApp conversations."""
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        patients = {}
        for r in records:
            phone = str(r.get("Phone Number", "")).strip()
            if phone and phone not in patients:
                patients[phone] = {
                    "name": r.get("Patient Name", "Unknown"),
                    "phone": phone,
                    "last_appointment": r.get("Appointment Date", ""),
                    "status": r.get("Status", ""),
                    "messages": chat_log.get(phone.replace("+", "").replace(" ", ""), [])
                }
        return jsonify({"conversations": list(patients.values())})
    except Exception as e:
        return jsonify({"conversations": [], "error": str(e)})


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    """Send a manual message to a patient as Sophie."""
    data = request.get_json()
    phone = data.get("phone", "")
    message = data.get("message", "")
    name = data.get("name", "")
    if not phone or not message:
        return jsonify({"success": False, "error": "Phone and message required"})
    clinic = list(CLINICS.values())[0]
    success = send_whatsapp(phone, message, clinic["twilio_number"])
    if success:
        phone_key = phone.replace("+", "").replace(" ", "")
        if phone_key not in chat_log:
            chat_log[phone_key] = []
        chat_log[phone_key].append({"role": "sophie", "text": message, "time": datetime.now().strftime("%I:%M %p")})
    return jsonify({"success": success})


@app.route("/api/analytics")
def api_analytics():
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        # Last 7 days
        daily = []
        for i in range(6, -1, -1):
            d = (datetime.now() - timedelta(days=i))
            d_str = d.strftime("%d/%m/%Y")
            day_appts = [r for r in records if str(r.get("Appointment Date", "")).strip() == d_str]
            confirmed = len([r for r in day_appts if (r.get("Status") or "").lower() in ["confirmed", "yes"]])
            noshows = len([r for r in day_appts if (r.get("Status") or "").lower() in ["cancelled", "no"]])
            daily.append({
                "date": d.strftime("%a"),
                "total": len(day_appts),
                "confirmed": confirmed,
                "noshows": noshows
            })
        total = len(records)
        conf_total = len([r for r in records if (r.get("Status") or "").lower() in ["confirmed", "yes"]])
        return jsonify({
            "daily": daily,
            "total_appointments": total,
            "total_confirmed": conf_total,
            "confirmation_rate": round(conf_total / total * 100) if total else 0,
            "revenue_saved": (total - len([r for r in records if (r.get("Status") or "").lower() in ["cancelled", "no"]])) * 120,
        })
    except Exception as e:
        return jsonify({"daily": [], "total_appointments": 0, "confirmation_rate": 0, "revenue_saved": 0})


@app.route("/dashboard")
@app.route("/")
def serve():
    return send_from_directory("templates", "dashboard.html")


if __name__ == "__main__":
    print("✅ Claude API key found" if os.environ.get("ANTHROPIC_API_KEY") else "⚠️ No API key")
    print(f"🏥 {len(CLINICS)} clinic(s) configured")
    print(f"📊 Dashboard: http://localhost:3900/dashboard")
    print(f"📡 API: http://localhost:3900/api/")
    app.run(debug=True, port=3900)