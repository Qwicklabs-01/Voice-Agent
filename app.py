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
    
    # Khushi Digital Services Cold Calling Script
    khushi_system_prompt = f"""You are Khushi, an AI voice agent calling on behalf of a creative agency. You are talking to {lead_name} from {lead_company}.
    
Strictly follow this script:
[Opener]
Wait for the user to answer the phone. Then say:
"Hi, this is Khushi. Is this {lead_name}?"
Wait for them to say yes.
"Hey {lead_name}. I know I caught you off guard. Do you have 30 seconds?"
Wait for them to say yes/okay.

[Value Proposition]
"Thanks! I run a creative agency. We handle everything from web design and video editing to full-scale marketing. Our whole goal is to help your brand stand out to customers and look significantly better than your competitors."

[The Discovery Survey]
"Quick question: what's your main focus right now—getting more leads, or making your brand stand out more?"
Listen to their answer and acknowledge it.
"Got it. And when was the last time you updated your website or ran a new video marketing campaign?"
Listen to their answer and acknowledge it.

[The Ask]
"Makes sense. We'd love to show you exactly how we can help you stand out. I can make a free, 2-minute video mockup showing some quick improvements for your brand. If I send it over, would you be open to a 5-minute chat next week?"

Handle objections gracefully. If they say they have an agency, say you handle overflow work and ask to send a portfolio. If they have no budget, say you aren't selling anything today and ask to send the mockup anyway.
"""

    payload = {
        "assistantId": VAPI_ASSISTANT_ID,
        "phoneNumberId": VAPI_PHONE_NUMBER_ID,
        "customer": {
            "number": normalized_phone,
            "name": lead_name
        },
        "assistantOverrides": {
            "variableValues": {
                "lead_name": lead_name,
                "lead_company_name": lead_company,
                "lead_request": lead_request
            },
            "model": {
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
                            "PrimaryFocus": {"type": "string", "description": "Whether their focus is getting leads or standing out"},
                            "WebsiteStatus": {"type": "string", "description": "When they last updated their website or ran a campaign"}
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
    call_outcome = "Unknown"
    primary_focus = ""
    website_status = ""
    
    analysis = call_result_data.get("analysis", {})
    structured_data = analysis.get("structuredData", {})
    
    # Simple voicemail detection logic (Vapi often provides a successEvaluation or endedReason)
    ended_reason = call_result_data.get("endedReason", "")
    if "voicemail" in ended_reason.lower() or not analysis:
        call_outcome = "Voicemail / No Answer"
    else:
        call_outcome = "Completed"
        primary_focus = structured_data.get("PrimaryFocus", "")
        website_status = structured_data.get("WebsiteStatus", "")

    print(f"Call Outcome: {call_outcome}")
    
    if worksheet:
        try:
            row_data = [
                lead_name,
                normalized_phone,
                lead_company,
                call_outcome,
                primary_focus,
                website_status
            ]
            worksheet.append_row(row_data)
            print("Successfully appended row to Google Sheet.")
        except Exception as e:
            print(f"Failed to write to Google Sheet: {e}")
            
    # 4. Send ntfy notification
    notification_msg = (
        f"New Lead Processed: {lead_name}\n"
        f"Outcome: {call_outcome}\n"
        f"Primary Focus: {primary_focus}\n"
        f"Website Status: {website_status}"
    )
    send_ntfy_notification(notification_msg)
            
    return jsonify({
        "status": "success", 
        "call_id": call_id,
        "outcome": call_outcome
    })

if __name__ == '__main__':
    print("Starting Outbound Lead Qualifier Server...")
    app.run(host='0.0.0.0', port=5001)
