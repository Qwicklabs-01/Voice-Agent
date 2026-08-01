import os
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# Config
VAPI_PRIVATE_KEY = os.getenv("VAPI_PRIVATE_KEY")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID")
VAPI_PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID")
GOOGLE_SHEET_DOCUMENT_ID = os.getenv("GOOGLE_SHEET_DOCUMENT_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")

# Setup Google Sheets
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    credentials = Credentials.from_service_account_file(
        "service_account.json", scopes=scopes
    )
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(GOOGLE_SHEET_DOCUMENT_ID)
    worksheet = sh.worksheet(GOOGLE_SHEET_NAME)
    print(f"Successfully connected to Google Sheet: {sh.title}")
except Exception as e:
    print(f"Error connecting to Google Sheets: {e}")
    worksheet = None

def normalize_phone_number(phone):
    """Normalize phone number to E.164 format."""
    digits = "".join(filter(str.isdigit, str(phone)))
    if len(digits) == 10:
        return f"+91{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"

def send_ntfy_notification(message):
    """Send a push notification via ntfy.sh."""
    if not NTFY_TOPIC or NTFY_TOPIC == "your_secret_ntfy_topic_here":
        print("NTFY_TOPIC not configured. Skipping push notification.")
        return
        
    try:
        response = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode('utf-8'))
        response.raise_for_status()
        print("Successfully sent ntfy notification.")
    except Exception as e:
        print(f"Error sending ntfy notification: {e}")

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    data = request.json
    if not data:
        return jsonify({"error": "No JSON data provided"}), 400
        
    lead_name = data.get("name", "Unknown Lead")
    lead_phone = data.get("phone")
    lead_company = data.get("company", "Unknown Company")
    lead_request = data.get("request", "None")
    
    if not lead_phone:
        return jsonify({"error": "Phone number is required"}), 400
        
    normalized_phone = normalize_phone_number(lead_phone)
    print(f"Processing lead: {lead_name} ({normalized_phone})")
    
    # 1. Initiate Vapi Call
    vapi_url = "https://api.vapi.ai/call"
    headers = {
        "Authorization": f"Bearer {VAPI_PRIVATE_KEY}",
        "Content-Type": "application/json"
    }
    
    khushi_system_prompt = f"""You are Khushi, an AI voice agent calling on behalf of a corporate travel company. You are talking to {lead_name} from {lead_company}.
    
Strictly follow this script:
[Opener]
Wait for the user to answer the phone. Then say:
"Hi, this is Khushi. What is your name, or whom am I speaking with? Can I call you by your name?"
Wait for them to answer and tell you their name.
"Hey [Their Name]. I know I caught you off guard. Do you have 30 seconds?"
Wait for them to say yes/okay.

[Value Proposition]
"Thanks! I run a corporate travel company. We handle everything from domestic and international air tickets to rail tickets and bulk hotel bookings for businesses. Because of our B2B network, we usually save companies about 15% on their overall travel budget."

[The Discovery Survey]
"Quick question: who currently handles all the ticketing and hotel arrangements for your team—is it HR, or do employees book it themselves?"
Listen to their answer and acknowledge it.
"Got it. And what’s the biggest headache with that right now? Managing train and flight cancellations, or just the sheer time it takes to book everything?"
Listen to their answer and acknowledge it.

[The Ask]
"That makes sense. If we could take that entire workload off your plate at no extra cost, would you be open to a 5-minute chat next week to see how it works?"

Handle objections gracefully. If they say employees book themselves, mention it costs more because of retail rates and ask for a quick chat to compare. If they have a travel portal, mention the lack of human support for cancellations.
"""

    payload = {
        "assistantId": VAPI_ASSISTANT_ID,
        "phoneNumberId": VAPI_PHONE_NUMBER_ID,
        "customer": {
            "number": normalized_phone,
            "name": lead_name
        },
        "assistantOverrides": {
            "firstMessage": "Hi, this is Khushi. What is your name, or whom am I speaking with? Can I call you by your name?",
            "variableValues": {
                "lead_name": lead_name,
                "lead_company_name": lead_company,
                "lead_request": lead_request
            },
            "model": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": khushi_system_prompt
                    }
                ]
            },
            "analysisPlan": {
                "structuredDataPlan": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "Answer": {"type": "string", "description": "Who handles ticketing and hotel arrangements"},
                            "Information": {"type": "string", "description": "The biggest problem they have with booking travel"}
                        }
                    }
                }
            }
        }
    }
    
    try:
        print("Initiating Vapi call...")
        response = requests.post(vapi_url, headers=headers, json=payload)
        if response.status_code != 201 and response.status_code != 200:
            print(f"Vapi Error Response: {response.text}")
        response.raise_for_status()
        call_data = response.json()
        call_id = call_data.get("id")
        print(f"Call initiated successfully. Call ID: {call_id}")
    except Exception as e:
        print(f"Failed to initiate Vapi call: {e}")
        return jsonify({"error": "Failed to initiate Vapi call", "details": str(e)}), 400
        
    # 2. Poll for call completion
    print("Polling for call completion...")
    call_status = "queued"
    call_result_data = {}
    
    while call_status not in ["ended", "completed", "failed"]:
        time.sleep(5)
        try:
            poll_resp = requests.get(f"{vapi_url}/{call_id}", headers=headers)
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()
            call_status = poll_data.get("status")
            print(f"Current call status: {call_status}")
            
            if call_status in ["ended", "completed"]:
                call_result_data = poll_data
        except Exception as e:
            print(f"Error polling call status: {e}")
            break
            
    # 3. Extract Data & Write to Google Sheets
    is_voicemail = "Yes"
    answer = ""
    information = ""
    
    analysis = call_result_data.get("analysis", {})
    structured_data = analysis.get("structuredData", {})
    
    # Simple voicemail detection logic (Vapi often provides a successEvaluation or endedReason)
    ended_reason = call_result_data.get("endedReason", "")
    if "voicemail" in ended_reason.lower() or not analysis:
        is_voicemail = "Yes"
    else:
        is_voicemail = "No"
        answer = structured_data.get("Answer", "")
        information = structured_data.get("Information", "")

    print(f"Voicemail: {is_voicemail}")
    
    if worksheet:
        try:
            row_data = [
                lead_name,
                normalized_phone,
                lead_company,
                is_voicemail,
                answer,
                information
            ]
            worksheet.append_row(row_data)
            print("Successfully appended row to Google Sheet.")
        except Exception as e:
            print(f"Failed to write to Google Sheet: {e}")
            
    # 4. Send ntfy notification
    notification_msg = (
        f"New Lead Processed: {lead_name}\n"
        f"Voicemail: {is_voicemail}\n"
        f"Answer: {answer}\n"
        f"Information: {information}"
    )
    send_ntfy_notification(notification_msg)
            
    return jsonify({
        "status": "success", 
        "call_id": call_id,
        "is_voicemail": is_voicemail
    })

if __name__ == '__main__':
    print("Starting Outbound Lead Qualifier Server...")
    app.run(host='0.0.0.0', port=5001)
