import os
import json
import threading
import time
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

# ── Settings (in-memory, persisted to sheet) ──────────────────────────────────
SETTINGS = {
    "auto_refresh": True,
    "email_notifications": False,
    "sound_alerts": False,
    "dark_mode": True,
    "accent_color": "#00d97e",
    "welcome_message": True,
    "auto_confirm": True,
    "handle_reschedule": True,
    "post_visit_followup": False,
    "reminder_24hr": True,
    "reminder_1hr": True,
    "reminder_30min": True,
    "reminder_time": "14:00",
    "clinic_name": "Melbourne Physio",
    "clinic_address": "123 Collins Street, Melbourne VIC 3000",
    "clinic_phone": "(03) 9000 0000",
    "clinic_hours": "Monday to Friday, 8am to 6pm",
    "sophie_name": "Sophie",
}

# ── Clinic config ─────────────────────────────────────────────────────────────
CLINICS = {
    "+1 575 586 8929": {
        "name": os.environ.get("CLINIC_NAME", "Melbourne Physio"),
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


def get_chat_sheet():
    clinic = list(CLINICS.values())[0]
    gc = get_gc()
    sh = gc.open_by_key(clinic["sheet_id"])
    try:
        return sh.worksheet("Conversations")
    except:
        ws = sh.add_worksheet(title="Conversations", rows=1000, cols=5)
        ws.append_row(["Phone", "Name", "Role", "Message", "Time"])
        return ws


def get_settings_sheet():
    clinic = list(CLINICS.values())[0]
    gc = get_gc()
    sh = gc.open_by_key(clinic["sheet_id"])
    try:
        return sh.worksheet("Settings")
    except:
        ws = sh.add_worksheet(title="Settings", rows=100, cols=2)
        ws.append_row(["Key", "Value"])
        return ws


def save_settings_to_sheet():
    try:
        ws = get_settings_sheet()
        ws.clear()
        ws.append_row(["Key", "Value"])
        for k, v in SETTINGS.items():
            ws.append_row([k, str(v)])
    except Exception as e:
        print(f"Settings save error: {e}")


def load_settings_from_sheet():
    try:
        ws = get_settings_sheet()
        records = ws.get_all_records()
        for r in records:
            key = r.get("Key", "")
            val = r.get("Value", "")
            if key in SETTINGS:
                if isinstance(SETTINGS[key], bool):
                    SETTINGS[key] = val.lower() == "true"
                else:
                    SETTINGS[key] = val
    except Exception as e:
        print(f"Settings load error: {e}")


def log_message(phone, name, role, message):
    try:
        ws = get_chat_sheet()
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
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        pc = phone.replace("+", "").replace(" ", "")
        for r in records:
            rp = str(r.get("Phone Number", "")).replace("+", "").replace(" ", "")
            if rp == pc:
                return r.get("Patient Name", "")
    except:
        pass
    return ""


def get_clinic(number=""):
    return CLINICS.get(number, list(CLINICS.values())[0])


def build_prompt(clinic):
    prac = "\n".join(f"  - {p}" for p in clinic["practitioners"])
    name = SETTINGS.get("sophie_name", "Sophie")
    return f"""You are {name}, the friendly virtual receptionist at {clinic['name']} in Melbourne, Australia.
Personality: Warm, empathetic, conversational. Never robotic. Use patient name once known.
Keep replies 2-4 sentences max. Never use numbered menus.
Clinic: {clinic['name']} | {clinic['phone']} | {clinic['hours']}
Practitioners:\n{prac}
Available slots: Mon-Fri at 9am, 11am, 2pm, 4pm.
Rules:
- NEVER reveal you are AI unless directly asked
- If patient distressed or complex medical question, offer human team
- Always end with open question or offer to help
- If patient replies YES -> confirming appointment, update status
- If patient replies NO -> offer to reschedule"""


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


def update_status_by_phone(phone, status):
    try:
        ws = get_sheet()
        records = ws.get_all_records()
        headers = ws.row_values(1)
        pc = phone.replace(" ", "").replace("+", "")
        if "Status" not in headers:
            ws.update_cell(1, len(headers) + 1, "Status")
            headers.append("Status")
        col = headers.index("Status") + 1
        for i, row in enumerate(records, 2):
            rp = str(row.get("Phone Number", "")).replace(" ", "").replace("+", "")
            if rp == pc:
                ws.update_cell(i, col, status)
                print(f"✅ Status updated to {status} for {phone}")
                break
    except Exception as e:
        print(f"Status update error: {e}")


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

        # Auto-update status if setting is on
        if SETTINGS.get("auto_confirm", True):
            msg_lower = message.strip().lower()
            if msg_lower in ["yes", "y", "confirm", "yep", "yeah", "sure"]:
                update_status_by_phone(phone, "Confirmed")
            elif msg_lower in ["no", "n", "cancel", "cancelled"]:
                update_status_by_phone(phone, "Cancelled")

        # Log messages
        log_message(phone, name, "patient", message)
        log_message(phone, name, "sophie", reply)

        return reply
    except Exception as e:
        print(f"Claude error: {e}")
        return f"Sorry, having a little trouble. Please call {clinic['phone']} and our team will help!"


# ── BACKGROUND REMINDER THREAD ────────────────────────────────────────────────
def reminder_worker():
    """Runs in background, checks every 30 minutes and sends reminders."""
    print("📅 Reminder worker started")
    time.sleep(30)  # Wait 30s for app to fully start

    TIME_SLOTS = {
        "9:00 AM": 9, "11:00 AM": 11,
        "2:00 PM": 14, "4:00 PM": 16,
    }

    def get_appt_datetime(date_str, time_str):
        try:
            d = datetime.strptime(date_str, "%d/%m/%Y")
            h = TIME_SLOTS.get(time_str, 9)
            return d.replace(hour=h, minute=0, second=0)
        except:
            return None

    def mark_reminder(ws, row_num, col_name):
        try:
            headers = ws.row_values(1)
            if col_name not in headers:
                ws.update_cell(1, len(headers) + 1, col_name)
                headers.append(col_name)
            col = headers.index(col_name) + 1
            ws.update_cell(row_num, col, "Sent")
        except Exception as e:
            print(f"Mark reminder error: {e}")

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%d/%m/%Y")
            tomorrow_str = (now + timedelta(days=1)).strftime("%d/%m/%Y")
            clinic = list(CLINICS.values())[0]

            ws = get_sheet()
            all_rows = ws.get_all_records()

            appts = [
                {"row": i + 2, **r}
                for i, r in enumerate(all_rows)
                if str(r.get("Appointment Date", "")).strip() in [today_str, tomorrow_str]
                and (r.get("Status") or "").lower() not in ["cancelled", "no"]
            ]

            for appt in appts:
                name = appt.get("Patient Name", "there")
                phone = str(appt.get("Phone Number", "")).strip()
                date_str = appt.get("Appointment Date", "")
                time_str = appt.get("Appointment Time", "")
                row_num = appt.get("row")
                first_name = name.split()[0] if name else "there"

                if not phone:
                    continue

                appt_dt = get_appt_datetime(date_str, time_str)
                if not appt_dt:
                    continue

                mins = (appt_dt - now).total_seconds() / 60

                # 24hr reminder
                if SETTINGS.get("reminder_24hr", True):
                    sent = str(appt.get("Reminder 24hr", "")).lower() == "sent"
                    if not sent and 1380 <= mins <= 1500:
                        msg = (
                            f"Hi {first_name}! This is {SETTINGS.get('sophie_name','Sophie')} "
                            f"from {clinic['name']} 😊\n\n"
                            f"Just a friendly reminder that you have an appointment "
                            f"tomorrow ({date_str}) at {time_str}.\n\n"
                            f"Reply YES to confirm or NO if you need to reschedule. "
                            f"I'm here to help!"
                        )
                        if send_whatsapp(phone, msg, clinic["twilio_number"]):
                            mark_reminder(ws, row_num, "Reminder 24hr")
                            log_message(phone.replace("+",""), name, "sophie", msg)
                            print(f"✅ 24hr reminder → {name}")

                # 1hr reminder
                if SETTINGS.get("reminder_1hr", True):
                    sent = str(appt.get("Reminder 1hr", "")).lower() == "sent"
                    if not sent and 50 <= mins <= 70:
                        msg = (
                            f"Hi {first_name}! {SETTINGS.get('sophie_name','Sophie')} "
                            f"from {clinic['name']} here 😊\n\n"
                            f"Your appointment is in about 1 hour at {time_str} today. "
                            f"See you soon! Reply if you need anything."
                        )
                        if send_whatsapp(phone, msg, clinic["twilio_number"]):
                            mark_reminder(ws, row_num, "Reminder 1hr")
                            log_message(phone.replace("+",""), name, "sophie", msg)
                            print(f"✅ 1hr reminder → {name}")

                # 30min follow-up (only if 24hr sent but no confirmation)
                if SETTINGS.get("reminder_30min", True):
                    sent_30 = str(appt.get("Reminder 30min", "")).lower() == "sent"
                    sent_24 = str(appt.get("Reminder 24hr", "")).lower() == "sent"
                    status = (appt.get("Status") or "").lower()
                    if not sent_30 and sent_24 and status not in ["confirmed","yes"] and 25 <= mins <= 35:
                        msg = (
                            f"Hi {first_name}! Just checking in — your appointment is "
                            f"in 30 minutes at {time_str}.\n\n"
                            f"Quick reply YES to confirm you're on your way, "
                            f"or call us on {clinic['phone']} if you need to reschedule. 😊"
                        )
                        if send_whatsapp(phone, msg, clinic["twilio_number"]):
                            mark_reminder(ws, row_num, "Reminder 30min")
                            log_message(phone.replace("+",""), name, "sophie", msg)
                            print(f"✅ 30min reminder → {name}")

                # Post-visit follow-up
                if SETTINGS.get("post_visit_followup", False):
                    sent_pv = str(appt.get("Post Visit", "")).lower() == "sent"
                    if not sent_pv and -150 <= mins <= -110:
                        msg = (
                            f"Hi {first_name}! Hope your session at {clinic['name']} "
                            f"went well today 😊\n\n"
                            f"How are you feeling? We'd love to see you again — "
                            f"just reply anytime to book your next appointment."
                        )
                        if send_whatsapp(phone, msg, clinic["twilio_number"]):
                            mark_reminder(ws, row_num, "Post Visit")
                            log_message(phone.replace("+",""), name, "sophie", msg)
                            print(f"✅ Post-visit → {name}")

        except Exception as e:
            print(f"Reminder worker error: {e}")

        time.sleep(1800)  # Check every 30 minutes


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
        "name": SETTINGS.get("clinic_name", clinic["name"]),
        "phone": SETTINGS.get("clinic_phone", clinic["phone"]),
        "hours": SETTINGS.get("clinic_hours", clinic["hours"]),
        "address": SETTINGS.get("clinic_address", clinic.get("address", "")),
        "practitioners": clinic["practitioners"],
        "services": clinic["services"],
    })


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(SETTINGS)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json()
    for k, v in data.items():
        if k in SETTINGS:
            SETTINGS[k] = v
    # Update clinic name dynamically
    if "clinic_name" in data:
        list(CLINICS.values())[0]["name"] = data["clinic_name"]
    if "sophie_name" in data:
        list(CLINICS.values())[0]["receptionist_name"] = data["sophie_name"]
    threading.Thread(target=save_settings_to_sheet, daemon=True).start()
    return jsonify({"success": True, "settings": SETTINGS})


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
            day_appts = [r for r in records
                        if str(r.get("Appointment Date", "")).strip() == d]
            week_noshows += len([r for r in day_appts
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
        return jsonify({
            "today_total": 0, "confirmed": 0, "pending": 0,
            "cancelled": 0, "week_noshows": 0,
            "total_patients": 0, "confirmation_rate": 0
        })


@app.route("/api/book", methods=["POST"])
def api_book():
    data = request.get_json()
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    date = data.get("date", "")
    time_slot = data.get("time", "")
    practitioner = data.get("practitioner", "Any available")
    notes = data.get("notes", "")
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
            elif h == "Appointment Time":  row.append(time_slot)
            elif h == "Practitioner":      row.append(practitioner)
            elif h == "Notes":             row.append(notes)
            else:                          row.append("")
        if not row:
            row = [name, phone, formatted_date, time_slot, "", "", practitioner]
        ws.append_row(row)

        # Send welcome message instantly in background thread
        if send_welcome and SETTINGS.get("welcome_message", True):
            clinic = list(CLINICS.values())[0]
            first_name = name.split()[0] if name else "there"
            sophie_name = SETTINGS.get("sophie_name", "Sophie")
            welcome_msg = (
                f"Hi {first_name}! This is {sophie_name} from {clinic['name']} 😊\n\n"
                f"Your appointment has been booked:\n"
                f"📅 {formatted_date} at {time_slot}\n"
                f"👩‍⚕️ {practitioner.split('—')[0].strip()}\n\n"
                f"I'll send you a reminder the day before. "
                f"Reply anytime if you need to reschedule or have questions!"
            )

            def send_async():
                sent = send_whatsapp(phone, welcome_msg, clinic["twilio_number"])
                if sent:
                    log_message(phone.replace("+", ""), name, "sophie", welcome_msg)
                    print(f"✅ Welcome message sent to {name} ({phone})")
                else:
                    print(f"⚠️ Could not send welcome to {phone} — may need to join sandbox first")

            threading.Thread(target=send_async, daemon=True).start()

        return jsonify({"success": True})
    except Exception as e:
        print(f"Book error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/send_reminder", methods=["POST"])
def api_send_reminder():
    data = request.get_json()
    name = data.get("name", "")
    phone = data.get("phone", "")
    date = data.get("date", "")
    time_slot = data.get("time", "")
    clinic = list(CLINICS.values())[0]
    sophie_name = SETTINGS.get("sophie_name", "Sophie")
    first_name = name.split()[0] if name else "there"
    message = (
        f"Hi {first_name}! This is {sophie_name} from {clinic['name']} 😊\n\n"
        f"Just a reminder about your appointment"
        + (f" on {date} at {time_slot}" if date and time_slot else "")
        + ".\n\nReply YES to confirm or NO if you need to reschedule. I'm here to help!"
    )

    def send_async():
        success = send_whatsapp(phone, message, clinic["twilio_number"])
        if success:
            log_message(phone.replace("+", ""), name, "sophie", message)
            try:
                ws = get_sheet()
                records = ws.get_all_records()
                headers = ws.row_values(1)
                if "Reminder Sent" not in headers:
                    ws.update_cell(1, len(headers)+1, "Reminder Sent")
                    headers.append("Reminder Sent")
                col = headers.index("Reminder Sent") + 1
                pc = phone.replace(" ", "").replace("+", "")
                for i, row in enumerate(records, 2):
                    rp = str(row.get("Phone Number","")).replace(" ","").replace("+","")
                    if rp == pc:
                        ws.update_cell(i, col, "Yes")
                        break
            except Exception as e:
                print(f"Reminder update error: {e}")

    threading.Thread(target=send_async, daemon=True).start()
    return jsonify({"success": True})


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
            ws.update_cell(1, len(headers)+1, "Status")
            headers.append("Status")
        col = headers.index("Status") + 1
        for i, row in enumerate(records, 2):
            if row.get("Patient Name")==name and row.get("Appointment Date")==date:
                ws.update_cell(i, col, status)
                return jsonify({"success": True})
        return jsonify({"success": False, "error": "Patient not found"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/chat/conversations")
def api_conversations():
    try:
        ws = get_chat_sheet()
        records = ws.get_all_records()
        convos = {}
        for r in records:
            phone = str(r.get("Phone", "")).strip()
            if not phone:
                continue
            if phone not in convos:
                convos[phone] = {
                    "phone": phone,
                    "name": r.get("Name", "Unknown"),
                    "messages": [],
                    "status": "",
                    "last_appointment": ""
                }
            convos[phone]["messages"].append({
                "role": r.get("Role", "patient"),
                "text": r.get("Message", ""),
                "time": r.get("Time", "")
            })
        # Enrich with appointment data
        try:
            appt_ws = get_sheet()
            appt_records = appt_ws.get_all_records()
            for phone, convo in convos.items():
                pc = phone.replace("+","").replace(" ","")
                for r in appt_records:
                    rp = str(r.get("Phone Number","")).replace("+","").replace(" ","")
                    if rp == pc:
                        convo["status"] = r.get("Status","")
                        convo["last_appointment"] = r.get("Appointment Date","")
                        if not convo["name"] or convo["name"]=="Unknown":
                            convo["name"] = r.get("Patient Name", convo["name"])
                        break
        except:
            pass
        return jsonify({"conversations": list(convos.values())})
    except Exception as e:
        return jsonify({"conversations": [], "error": str(e)})


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    data = request.get_json()
    phone = data.get("phone", "")
    message = data.get("message", "")
    name = data.get("name", "")
    if not phone or not message:
        return jsonify({"success": False, "error": "Phone and message required"})
    clinic = list(CLINICS.values())[0]

    def send_async():
        success = send_whatsapp(phone, message, clinic["twilio_number"])
        if success:
            log_message(phone.replace("+",""), name, "sophie", message)

    threading.Thread(target=send_async, daemon=True).start()
    return jsonify({"success": True})


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
                        if str(r.get("Appointment Date","")).strip()==d_str]
            confirmed = len([r for r in day_appts
                            if (r.get("Status") or "").lower() in ["confirmed","yes"]])
            noshows = len([r for r in day_appts
                          if (r.get("Status") or "").lower() in ["cancelled","no"]])
            daily.append({"date":d.strftime("%a"),"total":len(day_appts),"confirmed":confirmed,"noshows":noshows})
        total = len(records)
        conf_total = len([r for r in records if (r.get("Status") or "").lower() in ["confirmed","yes"]])
        return jsonify({
            "daily": daily,
            "total_appointments": total,
            "total_confirmed": conf_total,
            "confirmation_rate": round(conf_total/total*100) if total else 0,
            "revenue_saved": conf_total * 120,
        })
    except Exception as e:
        return jsonify({"daily":[],"total_appointments":0,"confirmation_rate":0,"revenue_saved":0})


@app.route("/dashboard")
@app.route("/")
def serve():
    return send_from_directory("templates", "dashboard.html")


if __name__ == "__main__":
    print("✅ Claude API" if os.environ.get("ANTHROPIC_API_KEY") else "⚠️ No Claude API key")
    print(f"🏥 {len(CLINICS)} clinic(s)")
    print(f"📊 http://localhost:3900/dashboard")

    # Load saved settings
    try:
        load_settings_from_sheet()
        print("✅ Settings loaded from sheet")
    except Exception as e:
        print(f"⚠️ Could not load settings: {e}")

    # Start background reminder thread
    reminder_thread = threading.Thread(target=reminder_worker, daemon=True)
    reminder_thread.start()
    print("📅 Reminder worker started in background")

    app.run(debug=False, port=3900, threaded=True)