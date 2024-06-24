import os
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

APPLICATION_KEY = os.getenv('APPLICATION_KEY')
API_KEY = os.getenv('API_KEY')
MAC_ADDRESS = os.getenv('MAC_ADDRESS')

def fetch_ecowit_data(application_key, api_key, mac):
    url = "https://api.ecowitt.net/api/v3/device/real_time"
    params = {
        "application_key": application_key,
        "api_key": api_key,
        "mac": mac,
        "call_back": "all"
    }
    response = requests.get(url, params=params)
    return response.json()

@app.route('/get_ecowit_data', methods=['GET'])
def get_ecowit_data():
    data = fetch_ecowit_data(APPLICATION_KEY, API_KEY, MAC_ADDRESS)
    return jsonify(data)

@app.route('/chatgpt_webhook', methods=['POST'])
def chatgpt_webhook():
    data = fetch_ecowit_data(APPLICATION_KEY, API_KEY, MAC_ADDRESS)
    return jsonify(data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("Starting Flask server on port", port)
    app.run(host='0.0.0.0', port=port)
