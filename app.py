import os
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

app = Flask(__name__)

# ── Anthropic client ──────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── Per-patient conversation memory ──────────────────────────────────────────
conversations = {}   # phone -> list of {role, content} messages

# ── System prompt — this is your "receptionist personality" ──────────────────
SYSTEM_PROMPT = """
You are Sophie, the friendly virtual receptionist at Melbourne Physio — a warm, professional physiotherapy clinic in Melbourne, Australia.

Your personality:
- Warm, empathetic, and genuinely caring — like a great human receptionist
- Conversational and natural — NEVER sound robotic or use numbered menus
- Use the patient's name once you know it
- Occasionally use light Australian warmth ("No worries!", "Absolutely!", "Of course!")
- Keep replies concise — 2 to 4 sentences max per message
- Use natural line breaks, not bullet points

What you can help with:
1. Confirming upcoming appointments
2. Rescheduling appointments (available slots: Mon-Fri, 9am / 11am / 2pm / 4pm)
3. Cancelling appointments
4. Answering general questions about the clinic
5. Connecting patient with a human receptionist if needed

Clinic details:
- Name: Melbourne Physio
- Address: 123 Collins Street, Melbourne VIC 3000
- Phone: (03) 9000 0000
- Hours: Monday to Friday, 8am to 6pm
- Practitioners: Dr. James Chen (physio), Dr. Sarah Mills (sports physio), Dr. Anika Patel (pilates)
- Parking: Available on-site, first 30 minutes free

Important rules:
- NEVER reveal you are an AI unless the patient directly and explicitly asks "are you a real person?" or "are you a bot?" or "am I talking to AI?" — in that case be honest, warm, and explain you are a virtual assistant
- If a patient seems frustrated, distressed, or has a complex medical question, immediately offer to connect them with a human receptionist
- If you don't know something specific about their appointment, acknowledge it naturally and offer to have the team follow up
- Never make up specific appointment details you don't have
- Always end with an open question or offer to help further

Tone examples:
BAD:  "Please select option 1 to confirm, option 2 to reschedule."
GOOD: "Happy to help! Did you want to confirm your appointment or find a different time?"

BAD:  "Your appointment has been rescheduled."
GOOD: "Perfect, I've got you in for Tuesday at 11am with Dr. Chen. You'll get a reminder the day before — anything else I can help with?"
"""


def get_ai_reply(phone: str, patient_message: str) -> str:
    """Send patient message to Claude and get a natural reply."""

    if phone not in conversations:
        conversations[phone] = []

    conversations[phone].append({
        "role": "user",
        "content": patient_message
    })

    # Keep last 20 messages only
    history = conversations[phone][-20:]

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=history
        )

        ai_reply = response.content[0].text

        conversations[phone].append({
            "role": "assistant",
            "content": ai_reply
        })

        return ai_reply

    except Exception as e:
        print(f"Claude API error: {e}")
        return (
            "So sorry, I'm having a little trouble right now. "
            "Please call us on (03) 9000 0000 and our team will help! 😊"
        )


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    sender = request.values.get("From", "")

    print(f"\n📨 From {sender}: {incoming_msg}")

    reply = get_ai_reply(sender, incoming_msg)

    print(f"💬 Sophie: {reply}")

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/")
def home():
    return "Melbourne Physio — Sophie is online ✅"


if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY not set!")
        print("   Run this first:")
        print("   export ANTHROPIC_API_KEY='your-key-here'")
        print()
    else:
        print("✅ Claude API key found")
    print("🚀 Melbourne Physio bot starting...")
    print("📱 Webhook: http://localhost:3900/whatsapp")
    print("🤖 AI receptionist: Sophie")
    app.run(debug=True, port=3900)