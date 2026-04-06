import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import anthropic

load_dotenv()
app = Flask(__name__)
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── CLINIC CONFIGURATIONS ─────────────────────────────────────────────────────
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
        "booking_link": "https://melbournephysio.com.au/book",
    },
    "+61432123321": {
        "name": "Collins Street Chiro",
        "address": "456 Collins Street, Melbourne VIC 3000",
        "phone": "(03) 8000 1111",
        "hours": "Monday to Saturday, 7am to 7pm",
        "practitioners": [
            "Dr. Mark Davis — Chiropractor",
            "Dr. Lisa Wong — Chiropractor and Remedial Massage",
        ],
        "services": ["Chiropractic adjustments", "Remedial massage", "Posture correction"],
        "parking": "Street parking available on Collins St",
        "receptionist_name": "Sophie",
        "booking_link": "https://collinschiro.com.au/book",
    },
    "+61244433300": {
        "name": "Fitzroy Sports Physio",
        "address": "78 Smith Street, Fitzroy VIC 3065",
        "phone": "(03) 7000 2222",
        "hours": "Monday to Friday, 7am to 8pm, Saturday 8am to 2pm",
        "practitioners": [
            "Dr. Tom Nguyen — Sports Physiotherapist",
            "Dr. Emma Clarke — Physiotherapist and Pilates",
        ],
        "services": ["Sports injury rehab", "Running assessments", "Pilates", "Taping"],
        "parking": "Street parking on Smith St",
        "receptionist_name": "Sophie",
        "booking_link": "https://fitzroysportsphysio.com.au/book",
    },
}

DEFAULT_CLINIC = {
    "name": "the clinic",
    "address": "Please call us for our address",
    "phone": "our main line",
    "hours": "business hours",
    "practitioners": [],
    "services": [],
    "parking": "Please call us for parking info",
    "receptionist_name": "Sophie",
    "booking_link": None,
}

# ── CONVERSATION MEMORY ───────────────────────────────────────────────────────
conversations = {}


def get_clinic(twilio_number: str) -> dict:
    return CLINICS.get(twilio_number, DEFAULT_CLINIC)


def build_system_prompt(clinic: dict) -> str:
    practitioners_list = "\n".join(f"  - {p}" for p in clinic["practitioners"])
    services_list = ", ".join(clinic["services"])
    return f"""
You are {clinic['receptionist_name']}, the friendly virtual receptionist at {clinic['name']} in Melbourne, Australia.

Your personality:
- Warm, empathetic, genuinely caring like a great human receptionist
- Conversational and natural, NEVER robotic or numbered menus
- Use the patient's name once you know it
- Light Australian warmth: No worries, Absolutely, Of course
- Keep replies concise, 2 to 4 sentences max

Clinic details:
- Name: {clinic['name']}
- Address: {clinic['address']}
- Phone: {clinic['phone']}
- Hours: {clinic['hours']}
- Parking: {clinic['parking']}

Our practitioners:
{practitioners_list}

Our services: {services_list}

{"Booking link: " + clinic['booking_link'] if clinic['booking_link'] else ""}

Available appointment slots: Monday to Friday at 9am, 11am, 2pm, and 4pm.

Rules:
- NEVER reveal you are AI unless patient directly asks are you a bot or am I talking to AI
- If patient is distressed or has complex medical question, offer human team
- Never make up appointment details you do not have
- Always end with an open question or offer to help further
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
        return ai_reply
    except Exception as e:
        print(f"Claude API error: {e}")
        return (
            f"So sorry, I'm having a little trouble right now. "
            f"Please call us on {clinic['phone']} and our team will help!"
        )


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    sender = request.values.get("From", "")
    clinic_number = request.values.get("To", "")
    clinic = get_clinic(clinic_number)
    print(f"\n📨 [{clinic['name']}] From {sender}: {incoming_msg}")
    reply = get_ai_reply(sender, clinic_number, incoming_msg)
    print(f"💬 Sophie ({clinic['name']}): {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/")
def home():
    clinic_list = " | ".join(c['name'] for c in CLINICS.values())
    return f"Sophie AI Receptionist — {len(CLINICS)} clinics online: {clinic_list}"


if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print("✅ Claude API key found" if api_key else "⚠️  ANTHROPIC_API_KEY not set!")
    print(f"🏥 {len(CLINICS)} clinics configured:")
    for number, clinic in CLINICS.items():
        print(f"   {number} → {clinic['name']}")
    print("📱 Webhook: http://localhost:3900/whatsapp")
    app.run(debug=True, port=3900)