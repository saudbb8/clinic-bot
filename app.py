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

app = Flask(__name__)
CORS(app)

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
twilio_client = Client(
    os.environ.get("TWILIO_ACCOUNT_SID"),
    os.environ.get("TWILIO_AUTH_TOKEN")
)

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_gc():
    creds = os.environ.get("GOOGLE_CREDENTIALS")
    if creds:
        return gspread.service_account_from_dict(json.loads(creds))
    return gspread.service_account(filename="credentials.json")

def get_sheet(tab=0):
    clinic = list(CLINICS.values())[0]
    gc = get_gc()
    sh = gc.open_by_key(clinic["sheet_id"])
    return sh.get_worksheet(tab)

def get_or_create_chat_sheet():
    """Get or create a Conversations sheet to store chat history."""
    clinic = list(CLINICS.values())[0]
    gc = get_gc()
    sh = gc.open_by_key(clinic["sheet_id"])
    try:
        ws = sh.worksheet("Conversations")
    except:
        ws = sh.add_worksheet(title="Conversations", rows=1000, cols=5)
        ws.append_row(["Phone", "Name", "Role", "Message", "Time"])
    return ws

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

conversations = {}  # in-memory conversation history for Claude context


def get_clinic(number=""):
    return CLINICS.get(number, list(CLINICS.values())[0])


def build_prompt(clinic):
    prac = "\n".join(f"  - {p}" for p in clinic["practitioners"])
    return f"""You are {clinic['receptionist_name']}, the friendly virtual receptionist at {clinic['name']} in Melbourne, Australia.
Personality: Warm, empathetic, conversational. Never robotic. Use patient name once known. Light Australian warmth (No worries, Absolutely).
Keep replies 2-4 sentences max. Never use numbered menus.
Clinic: {clinic['name']} | {clinic['phone']} | {clinic['hours']}
Practitioners:\n{prac}
Available slots: Mon-Fri at 9am, 11am, 2pm, 4pm.
Rules:
- NEVER reveal you are AI unless directly asked
- If patient seems distressed or has complex medical question, offer human team
- Always end with an open question or offer to help further
- If patient replies YES -> they are confirming their appointment
- If patient replies NO -> offer to reschedule"""


def log_message_to_sheet(phone, name, role, message):
    """Save every message to the Conversations sheet."""
    try:
        ws = get_or_create_chat_sheet()
        ws.append_row([
            phone,
            name or "Unknown",
            role,
            message,
            datetime.now().strftime("%d/%m/%Y %I:%M %p")
        ])
    except Exception as e:
        print(f"Chat log error: {e}")


def get_patient_name(phone):
    """Look up patient name from appointments sheet."""
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        phone_clean = phone.replace("+", "").replace(" ", "")
        for r in records:
            rp = str(r.get("Phone Number", "")).replace("+", "").replace(" ", "")
            if rp == phone_clean:
                return r.get("Patient Name", "")
    except:
        pass
    return ""


def get_ai_reply(sender, clinic_number, message):
    clinic = get_clinic(clinic_number)
    key = f"{sender}:{clinic_number}"
    phone = sender.replace("whatsapp:", "")
    name = get_patient_name(phone)

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

        # Auto-update status based on reply
        msg_lower = message.strip().lower()
        if msg_lower in ["yes", "y", "confirm", "yep", "yeah", "sure"]:
            update_status_by_phone(phone, "Confirmed")
        elif msg_lower in ["no", "n", "cancel", "cancelled"]:
            update_status_by_phone(phone, "Cancelled")

        # Log both messages to sheet
        log_message_to_sheet(phone, name, "patient", message)
        log_message_to_sheet(phone, name, "sophie", reply)

        return reply
    except Exception as e:
        print(f"Claude error: {e}")
        return f"Sorry, having a little trouble right now. Please call {clinic['phone']} and our team will help!"


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
        twilio_client.messages.create(
            from_=from_number,
            to=f"whatsapp:{phone}",
            body=message
        )
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
            records = [r for r in records
                      if str(r.get("Appointment Date", "")).strip() == date_str]
        return jsonify({"appointments": records, "total": len(records)})
    except Exception as e:
        print(f"Appointments error: {e}")
        return jsonify({"appointments": [], "total": 0, "error": str(e)})


@app.route("/api/stats")
def api_stats():
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        today = datetime.now().strftime("%d/%m/%Y")
        today_appts = [r for r in records
                      if str(r.get("Appointment Date", "")).strip() == today]
        confirmed = len([r for r in today_appts
                        if (r.get("Status") or "").lower() in ["confirmed", "yes"]])
        pending = len([r for r in today_appts
                      if (r.get("Status") or "").lower() not in
                      ["confirmed", "yes", "cancelled", "no"]])
        cancelled = len([r for r in today_appts
                        if (r.get("Status") or "").lower() in ["cancelled", "no"]])
        week_noshows = 0
        for i in range(7):
            d = (datetime.now() - timedelta(days=i)).strftime("%d/%m/%Y")
            week_appts = [r for r in records
                         if str(r.get("Appointment Date", "")).strip() == d]
            week_noshows += len([r for r in week_appts
                                if (r.get("Status") or "").lower() in ["cancelled", "no"]])
        return jsonify({
            "today_total": len(today_appts),
            "confirmed": confirmed,
            "pending": pending,
            "cancelled": cancelled,
            "week_noshows": week_noshows,
            "total_patients": len(set(
                r.get("Phone Number", "") for r in records if r.get("Phone Number")
            )),
            "confirmation_rate": round(confirmed / len(today_appts) * 100)
                                  if today_appts else 0,
        })
    except Exception as e:
        print(f"Stats error: {e}")
        return jsonify({
            "today_total": 0, "confirmed": 0, "pending": 0,
            "cancelled": 0, "week_noshows": 0,
            "total_patients": 0, "confirmation_rate": 0
        })


@app.route("/api/book", methods=["POST"])
def api_book():
    data = request.get_json()
    name        = data.get("name", "").strip()
    phone       = data.get("phone", "").strip()
    date        = data.get("date", "")
    time        = data.get("time", "")
    practitioner = data.get("practitioner", "Any available")
    notes       = data.get("notes", "")
    send_welcome = data.get("send_welcome", True)

    try:
        formatted_date = datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except:
        formatted_date = date

    try:
        ws = get_sheet()
        headers = ws.row_values(1)
        row = []
        for h in headers:
            if h == "Patient Name":        row.append(name)
            elif h == "Phone Number":      row.append(phone)
            elif h == "Appointment Date":  row.append(formatted_date)
            elif h == "Appointment Time":  row.append(time)
            elif h == "Practitioner":      row.append(practitioner)
            elif h == "Notes":             row.append(notes)
            else:                          row.append("")
        if not row:
            row = [name, phone, formatted_date, time, "", "", practitioner]
        ws.append_row(row)

        # ── Send welcome WhatsApp message automatically ──────────────────────
        if send_welcome:
            clinic = list(CLINICS.values())[0]
            first_name = name.split()[0] if name else "there"
            welcome_msg = (
                f"Hi {first_name}! This is Sophie from {clinic['name']} 😊\n\n"
                f"Your appointment has been booked for {formatted_date} at {time} "
                f"with {practitioner.split('—')[0].strip()}.\n\n"
                f"I'll send you a reminder the day before. "
                f"Reply anytime if you need to reschedule or have any questions!"
            )
            sent = send_whatsapp(phone, welcome_msg, clinic["twilio_number"])
            if sent:
                log_message_to_sheet(
                    phone.replace("+", ""),
                    name, "sophie", welcome_msg
                )
                print(f"✅ Welcome message sent to {name} ({phone})")
            else:
                print(f"⚠️ Could not send welcome message to {phone}")

        return jsonify({"success": True})
    except Exception as e:
        print(f"Book error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/send_reminder", methods=["POST"])
def api_send_reminder():
    data = request.get_json()
    name      = data.get("name", "")
    phone     = data.get("phone", "")
    date      = data.get("date", "")
    time_slot = data.get("time", "")
    clinic = list(CLINICS.values())[0]
    first_name = name.split()[0] if name else "there"
    message = (
        f"Hi {first_name}! This is Sophie from {clinic['name']} 😊\n\n"
        f"Just a friendly reminder about your appointment"
        + (f" on {date} at {time_slot}" if date and time_slot else "")
        + ".\n\nReply YES to confirm or NO if you need to reschedule. "
        "I'm here to help!"
    )
    success = send_whatsapp(phone, message, clinic["twilio_number"])
    if success:
        log_message_to_sheet(
            phone.replace("+", ""), name, "sophie", message
        )
        try:
            ws = get_sheet()
            records = ws.get_all_records()
            headers = ws.row_values(1)
            reminder_col = (headers.index("Reminder Sent") + 1
                           if "Reminder Sent" in headers else None)
            if reminder_col:
                pc = phone.replace(" ", "").replace("+", "")
                for i, row in enumerate(records, 2):
                    rp = str(row.get("Phone Number","")).replace(" ","").replace("+","")
                    if rp == pc:
                        ws.update_cell(i, reminder_col, "Yes")
                        break
        except Exception as e:
            print(f"Reminder sheet update: {e}")
    return jsonify({"success": success})


@app.route("/api/update_status", methods=["POST"])
def api_update_status():
    data   = request.get_json()
    name   = data.get("name", "")
    date   = data.get("date", "")
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
            if (row.get("Patient Name") == name
                    and row.get("Appointment Date") == date):
                ws.update_cell(i, status_col, status)
                return jsonify({"success": True})
        return jsonify({"success": False, "error": "Patient not found"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/chat/conversations")
def api_conversations():
    """Return all conversations from the Conversations sheet."""
    try:
        ws = get_or_create_chat_sheet()
        records = ws.get_all_records()

        # Group by phone number
        convos = {}
        for r in records:
            phone = str(r.get("Phone", "")).strip()
            if not phone:
                continue
            if phone not in convos:
                convos[phone] = {
                    "phone": phone,
                    "name": r.get("Name", "Unknown"),
                    "messages": []
                }
            convos[phone]["messages"].append({
                "role": r.get("Role", "patient"),
                "text": r.get("Message", ""),
                "time": r.get("Time", "")
            })

        # Also add patient status from appointments sheet
        try:
            appt_ws = get_sheet()
            appt_records = appt_ws.get_all_records()
            for phone, convo in convos.items():
                pc = phone.replace("+", "").replace(" ", "")
                for r in appt_records:
                    rp = str(r.get("Phone Number","")).replace("+","").replace(" ","")
                    if rp == pc:
                        convo["status"] = r.get("Status", "")
                        convo["last_appointment"] = r.get("Appointment Date", "")
                        if not convo["name"] or convo["name"] == "Unknown":
                            convo["name"] = r.get("Patient Name", convo["name"])
                        break
        except:
            pass

        return jsonify({"conversations": list(convos.values())})
    except Exception as e:
        print(f"Conversations error: {e}")
        return jsonify({"conversations": [], "error": str(e)})


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    """Send a manual message to a patient as Sophie."""
    data    = request.get_json()
    phone   = data.get("phone", "")
    message = data.get("message", "")
    name    = data.get("name", "")
    if not phone or not message:
        return jsonify({"success": False, "error": "Phone and message required"})
    clinic = list(CLINICS.values())[0]
    success = send_whatsapp(phone, message, clinic["twilio_number"])
    if success:
        log_message_to_sheet(
            phone.replace("+", ""), name, "sophie", message
        )
    return jsonify({"success": success})


@app.route("/api/analytics")
def api_analytics():
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        daily = []
        for i in range(6, -1, -1):
            d = datetime.now() - timedelta(days=i)
            d_str = d.strftime("%d/%m/%Y")
            day_appts = [r for r in records
                        if str(r.get("Appointment Date","")).strip() == d_str]
            confirmed = len([r for r in day_appts
                            if (r.get("Status") or "").lower() in ["confirmed","yes"]])
            noshows   = len([r for r in day_appts
                            if (r.get("Status") or "").lower() in ["cancelled","no"]])
            daily.append({
                "date": d.strftime("%a"),
                "total": len(day_appts),
                "confirmed": confirmed,
                "noshows": noshows
            })
        total      = len(records)
        conf_total = len([r for r in records
                         if (r.get("Status") or "").lower() in ["confirmed","yes"]])
        return jsonify({
            "daily": daily,
            "total_appointments": total,
            "total_confirmed": conf_total,
            "confirmation_rate": round(conf_total / total * 100) if total else 0,
            "revenue_saved": conf_total * 120,
        })
    except Exception as e:
        return jsonify({
            "daily": [], "total_appointments": 0,
            "confirmation_rate": 0, "revenue_saved": 0
        })


@app.route("/dashboard")
@app.route("/")
def serve():
    return send_from_directory("templates", "dashboard.html")


if __name__ == "__main__":
    print("✅ Claude API key found"
          if os.environ.get("ANTHROPIC_API_KEY") else "⚠️ No API key")
    print(f"🏥 {len(CLINICS)} clinic(s) configured")
    print(f"📊 Dashboard: http://localhost:3900/dashboard")
    print(f"📡 API: http://localhost:3900/api/")
    app.run(debug=True, port=3900)