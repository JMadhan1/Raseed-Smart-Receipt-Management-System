import os
import json
import base64
import re
import secrets
import hashlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, url_for, session, render_template_string
from flask_cors import CORS
from google.cloud import vision
from google.cloud import firestore
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import google_auth_oauthlib.flow
import google.generativeai as genai
from google.auth import default
from dotenv import load_dotenv
import logging

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Gemini API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Google OAuth config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8080/api/auth/google/callback")

# Supported languages
LANGUAGES = {
    'en': {'name': 'English', 'flag': '🇬🇧'},
    'te': {'name': 'తెలుగు', 'flag': '🇮🇳'},
    'kn': {'name': 'ಕನ್ನಡ', 'flag': '🇮🇳'}
}

# Initialize Google Cloud clients
try:
    credentials, project = default()
    vision_client = vision.ImageAnnotatorClient()
    db = firestore.Client()
    model = genai.GenerativeModel("models/gemini-1.5-pro-latest")
    logger.info("Google Cloud services initialized successfully")
except Exception as e:
    logger.error(f"Error initializing Google Cloud services: {e}")
    vision_client = None
    db = None
    model = None

def extract_amounts_from_text(text):
    """Improved amount extraction with better pattern matching"""
    amount_patterns = [
        r'(?:total|amount|balance|due)\s*:?\s*[\$\£\€]?\s*(\d+\.\d{2})',
        r'[\$\£\€](\d+\.\d{2})\b',
        r'\b(\d+\.\d{2})\s*[\$\£\€]',
        r'(?:subtotal|sub-total)\s*[\$\£\€]?\s*(\d+\.\d{2})',
        r'\b(\d{1,3}(?:,\d{3})*\.\d{2})\b'
    ]
    amounts = []
    for pattern in amount_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            try:
                clean_match = match.replace(',', '')
                amount = float(clean_match)
                if 0.01 <= amount <= 99999.99:
                    amounts.append(amount)
            except ValueError:
                continue
    return sorted(list(set(amounts)), reverse=True)

def extract_tax_from_text(text):
    """Extract tax amount specifically"""
    tax_patterns = [
        r'(?:tax|gst|vat|hst)\s*[\$\£\€]?\s*(\d+\.\d{2})',
        r'(?:tax|gst|vat|hst)\s*:?\s*[\$\£\€]?\s*(\d+\.\d{2})',
        r'[\$\£\€](\d+\.\d{2})\s*(?:tax|gst|vat|hst)'
    ]
    for pattern in tax_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            try:
                return float(matches[0].replace(',', ''))
            except ValueError:
                continue
    return 0.00

def extract_items_from_text(text):
    """Extract items with prices"""
    item_pattern = r'(.+?)\s+([\$\£\€]?\s*\d+\.\d{2})\b'
    matches = re.findall(item_pattern, text)
    items = []
    for match in matches:
        name = match[0].strip()
        price_str = match[1].replace('$', '').replace(',', '').strip()
        try:
            price = float(price_str)
            if price > 0 and len(name) > 1:
                items.append({"name": name, "price": price})
        except ValueError:
            continue
    return items

def parse_receipt_with_fallback(text):
    """Parse receipt with improved logic"""
    amounts = extract_amounts_from_text(text)
    tax = extract_tax_from_text(text)
    items = extract_items_from_text(text)
    item_total = sum(item['price'] for item in items) if items else 0
    total = amounts[0] if amounts else item_total + tax
    subtotal = total - tax if tax > 0 else item_total
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    merchant = lines[0] if lines else "Unknown"
    return {
        "merchant": merchant,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": round(total, 2),
        "tax": round(tax, 2),
        "subtotal": round(subtotal, 2),
        "items": items[:20],
        "category": "Other"
    }

def get_user_info(user_id):
    try:
        if not db:
            return None
        user_ref = db.collection("users").document(user_id)
        user_doc = user_ref.get()
        if user_doc.exists:
            return user_doc.to_dict()
        return None
    except Exception as e:
        logger.error(f"Error getting user info: {e}")
        return None

def create_or_update_user(user_info):
    try:
        if not db:
            return False
        user_ref = db.collection("users").document(user_info['sub'])
        user_ref.set({
            'email': user_info['email'],
            'name': user_info.get('name', ''),
            'picture': user_info.get('picture', ''),
            'last_login': datetime.now().isoformat(),
            'language': user_info.get('language', 'en')
        }, merge=True)
        return True
    except Exception as e:
        logger.error(f"Error creating/updating user: {e}")
        return False

def hash_password(password):
    """Hash password with salt"""
    salt = secrets.token_hex(16)
    pwdhash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return salt + pwdhash.hex()

def verify_password(stored_password, provided_password):
    """Verify password against hash"""
    salt = stored_password[:32]
    stored_hash = stored_password[32:]
    pwdhash = hashlib.pbkdf2_hmac('sha256', provided_password.encode(), salt.encode(), 100000)
    return pwdhash.hex() == stored_hash

def get_language_text(lang):
    """Return UI text in the selected language"""
    texts = {
        'en': {
            'dashboard_title': '🧾 Raseed Dashboard',
            'dashboard_subtitle': 'AI-powered receipt processing and analysis',
            'upload_receipt': '📸 Upload Receipt',
            'upload_text': 'Click to upload or drag and drop your receipt image',
            'upload_supported': 'Supports JPG, PNG, GIF',
            'process_btn': 'Process Receipt',
            'ai_assistant': '🤖 AI Assistant',
            'ai_placeholder': 'Ask about your spending, receipts, or get financial insights...',
            'ask_ai': 'Ask AI',
            'ai_welcome': 'Welcome to Raseed AI Assistant! Upload some receipts and ask me questions about your spending patterns, favorite stores, or get financial insights.',
            'your_receipts': '📋 Your Receipts',
            'refresh_receipts': '🔄 Refresh Receipts',
            'no_receipts': 'No receipts found. Upload your first receipt!',
            'logout': 'Logout',
            'total_receipts': 'Total Receipts',
            'total_spent': 'Total Spent',
            'top_category': 'Top Category',
            'avg_spend': 'Avg. per Receipt',
            'create_wallet': '📱 Create Wallet Pass',
            'processing': 'Processing your receipt...',
            'thinking': 'AI is thinking...',
            'loading': 'Loading receipts...',
            'receipt_processed': '✅ Receipt Processed Successfully!',
            'error_processing': '❌ Error Processing Receipt'
        },
        'te': {
            'dashboard_title': '🧾 రసీద్ డాష్బోర్డ్',
            'dashboard_subtitle': 'AI-శక్తితో రసీద్ ప్రాసెసింగ్ మరియు విశ్లేషణ',
            'upload_receipt': '📸 రసీద్ అప్లోడ్ చేయండి',
            'upload_text': 'రసీద్ ఇమేజ్‌ను అప్లోడ్ చేయడానికి క్లిక్ చేయండి లేదా డ్రాగ్ చేసి డ్రాప్ చేయండి',
            'upload_supported': 'JPG, PNG, GIF లను మద్దతు ఇస్తుంది',
            'process_btn': 'రసీద్ ప్రాసెస్ చేయండి',
            'ai_assistant': '🤖 AI సహాయకుడు',
            'ai_placeholder': 'మీ ఖర్చులు, రసీదులు గురించి అడగండి లేదా ఆర్థిక అంతర్దృష్టులను పొందండి...',
            'ask_ai': 'AI ని అడగండి',
            'ai_welcome': 'రసీద్ AI సహాయకుడికి స్వాగతం! కొన్ని రసీదులను అప్లోడ్ చేసి, మీ ఖర్చు నమూనాలు, మీకు ఇష్టమైన దుకాణాలు లేదా ఆర్థిక అంతర్దృష్టుల గురించి నన్ను ప్రశ్నించండి.',
            'your_receipts': '📋 మీ రసీదులు',
            'refresh_receipts': '🔄 రసీదులను రిఫ్రెష్ చేయండి',
            'no_receipts': 'రసీదులు కనుగొనబడలేదు. మీ మొదటి రసీదును అప్లోడ్ చేయండి!',
            'logout': 'లాగ్ అవుట్',
            'total_receipts': 'మొత్తం రసీదులు',
            'total_spent': 'మొత్తం ఖర్చు',
            'top_category': 'అత్యధిక వర్గం',
            'avg_spend': 'ప్రతి రసీదుకు సగటు ఖర్చు',
            'create_wallet': '📱 వాలెట్ పాస్ సృష్టించండి',
            'processing': 'మీ రసీదును ప్రాసెస్ చేస్తోంది...',
            'thinking': 'AI ఆలోచిస్తోంది...',
            'loading': 'రసీదులు లోడ్ అవుతున్నాయి...',
            'receipt_processed': '✅ రసీదు విజయవంతంగా ప్రాసెస్ చేయబడింది!',
            'error_processing': '❌ రసీదును ప్రాసెస్ చేయడంలో లోపం'
        },
        'kn': {
            'dashboard_title': '🧾 ರಸೀದ್ ಡ್ಯಾಶ್‌ಬೋರ್ಡ್',
            'dashboard_subtitle': 'AI-ಶಕ್ತಿಯುತ ರಸೀದಿ ಸಂಸ್ಕರಣೆ ಮತ್ತು ವಿಶ್ಲೇಷಣೆ',
            'upload_receipt': '📸 ರಸೀದಿ ಅಪ್‌ಲೋಡ್ ಮಾಡಿ',
            'upload_text': 'ರಸೀದಿ ಚಿತ್ರವನ್ನು ಅಪ್‌ಲೋಡ್ ಮಾಡಲು ಕ್ಲಿಕ್ ಮಾಡಿ ಅಥವಾ ಎಳೆದು ಬಿಡಿ',
            'upload_supported': 'JPG, PNG, GIF ಗಳನ್ನು ಬೆಂಬಲಿಸುತ್ತದೆ',
            'process_btn': 'ರಸೀದಿ ಸಂಸ್ಕರಿಸಿ',
            'ai_assistant': '🤖 AI ಸಹಾಯಕ',
            'ai_placeholder': 'ನಿಮ್ಮ ಖರ್ಚು, ರಸೀದಿಗಳ ಬಗ್ಗೆ ಕೇಳಿ ಅಥವಾ ಆರ್ಥಿಕ ಒಳನೋಟಗಳನ್ನು ಪಡೆಯಿರಿ...',
            'ask_ai': 'AI ಗೆ ಕೇಳಿ',
            'ai_welcome': 'ರಸೀದ್ AI ಸಹಾಯಕಕ್ಕೆ ಸುಸ್ವಾಗತ! ಕೆಲವು ರಸೀದಿಗಳನ್ನು ಅಪ್‌ಲೋಡ್ ಮಾಡಿ ಮತ್ತು ನಿಮ್ಮ ಖರ್ಚಿನ ಮಾದರಿಗಳು, ನಿಮ್ಮ ನೆಚ್ಚಿನ ಅಂಗಡಿಗಳು ಅಥವಾ ಆರ್ಥಿಕ ಒಳನೋಟಗಳ ಬಗ್ಗೆ ನನ್ನನ್ನು ಪ್ರಶ್ನಿಸಿ.',
            'your_receipts': '📋 ನಿಮ್ಮ ರಸೀದಿಗಳು',
            'refresh_receipts': '🔄 ರಸೀದಿಗಳನ್ನು ರಿಫ್ರೆಶ್ ಮಾಡಿ',
            'no_receipts': 'ರಸೀದಿಗಳು ಕಂಡುಬಂದಿಲ್ಲ. ನಿಮ್ಮ ಮೊದಲ ರಸೀದಿಯನ್ನು ಅಪ್‌ಲೋಡ್ ಮಾಡಿ!',
            'logout': 'ಲಾಗ್ ಔಟ್',
            'total_receipts': 'ಒಟ್ಟು ರಸೀದಿಗಳು',
            'total_spent': 'ಒಟ್ಟು ಖರ್ಚು',
            'top_category': 'ಅಗ್ರ ವರ್ಗ',
            'avg_spend': 'ಪ್ರತಿ ರಸೀದಿಗೆ ಸರಾಸರಿ ಖರ್ಚು',
            'create_wallet': '📱 ವಾಲೆಟ್ ಪಾಸ್ ರಚಿಸಿ',
            'processing': 'ನಿಮ್ಮ ರಸೀದಿಯನ್ನು ಸಂಸ್ಕರಿಸಲಾಗುತ್ತಿದೆ...',
            'thinking': 'AI ಯೋಚಿಸುತ್ತಿದೆ...',
            'loading': 'ರಸೀದಿಗಳನ್ನು ಲೋಡ್ ಮಾಡಲಾಗುತ್ತಿದೆ...',
            'receipt_processed': '✅ ರಸೀದಿ ಯಶಸ್ವಿಯಾಗಿ ಸಂಸ್ಕರಿಸಲ್ಪಟ್ಟಿದೆ!',
            'error_processing': '❌ ರಸೀದಿ ಸಂಸ್ಕರಣೆಯಲ್ಲಿ ದೋಷ'
        }
    }
    return texts.get(lang, texts['en'])

@app.route("/")
def index():
    """Landing page with project information"""
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Raseed - Smart Receipt Manager</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            overflow-x: hidden;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            text-align: center;
            color: white;
            margin-bottom: 60px;
            animation: fadeInUp 1s ease-out;
        }
        
        .header h1 {
            font-size: 4rem;
            margin-bottom: 20px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            animation: glow 2s ease-in-out infinite alternate;
        }
        
        .header p {
            font-size: 1.5rem;
            opacity: 0.9;
            margin-bottom: 30px;
        }
        
        .hero-section {
            background: rgba(255,255,255,0.1);
            border-radius: 25px;
            padding: 50px;
            margin-bottom: 50px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.2);
            animation: slideInLeft 1s ease-out;
        }
        
        .hero-content {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 50px;
            align-items: center;
        }
        
        .hero-text {
            color: white;
        }
        
        .hero-text h2 {
            font-size: 2.5rem;
            margin-bottom: 20px;
        }
        
        .hero-text p {
            font-size: 1.2rem;
            line-height: 1.6;
            margin-bottom: 30px;
        }
        
        .features-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 30px;
            margin-bottom: 60px;
        }
        
        .feature-card {
            background: rgba(255,255,255,0.95);
            border-radius: 20px;
            padding: 40px;
            text-align: center;
            box-shadow: 0 15px 35px rgba(0,0,0,0.1);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
            animation: fadeInUp 1s ease-out;
        }
        
        .feature-card:hover {
            transform: translateY(-10px);
            box-shadow: 0 25px 50px rgba(0,0,0,0.2);
        }
        
        .feature-icon {
            font-size: 4rem;
            margin-bottom: 20px;
            display: block;
        }
        
        .feature-card h3 {
            color: #667eea;
            font-size: 1.5rem;
            margin-bottom: 15px;
        }
        
        .feature-card p {
            color: #666;
            line-height: 1.6;
        }
        
        .cta-section {
            text-align: center;
            margin-top: 60px;
        }
        
        .btn {
            display: inline-block;
            padding: 15px 40px;
            margin: 10px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            text-decoration: none;
            border-radius: 50px;
            font-size: 1.1rem;
            font-weight: 600;
            transition: all 0.3s ease;
            box-shadow: 0 10px 30px rgba(102, 126, 234, 0.3);
            animation: pulse 2s ease-in-out infinite;
        }
        
        .btn:hover {
            transform: translateY(-3px);
            box-shadow: 0 15px 40px rgba(102, 126, 234, 0.4);
        }
        
        .btn-secondary {
            background: linear-gradient(135deg, #4CAF50, #45a049);
            box-shadow: 0 10px 30px rgba(76, 175, 80, 0.3);
        }
        
        .floating-elements {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: -1;
        }
        
        .floating-element {
            position: absolute;
            font-size: 2rem;
            opacity: 0.1;
            animation: float 6s ease-in-out infinite;
        }
        
        .floating-element:nth-child(1) { top: 10%; left: 10%; animation-delay: 0s; }
        .floating-element:nth-child(2) { top: 20%; right: 10%; animation-delay: 1s; }
        .floating-element:nth-child(3) { bottom: 20%; left: 20%; animation-delay: 2s; }
        .floating-element:nth-child(4) { bottom: 10%; right: 20%; animation-delay: 3s; }
        
        @keyframes glow {
            from { text-shadow: 0 0 20px rgba(255,255,255,0.5); }
            to { text-shadow: 0 0 30px rgba(255,255,255,0.8); }
        }
        
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(30px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        @keyframes slideInLeft {
            from { opacity: 0; transform: translateX(-50px); }
            to { opacity: 1; transform: translateX(0); }
        }
        
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.05); }
        }
        
        @keyframes float {
            0%, 100% { transform: translateY(0px); }
            50% { transform: translateY(-20px); }
        }
        
        @media (max-width: 768px) {
            .hero-content {
                grid-template-columns: 1fr;
                text-align: center;
            }
            
            .header h1 {
                font-size: 2.5rem;
            }
            
            .hero-text h2 {
                font-size: 2rem;
            }
            
            .hero-section {
                padding: 30px 20px;
            }
        }
    </style>
</head>
<body>
    <div class="floating-elements">
        <div class="floating-element">🧾</div>
        <div class="floating-element">📊</div>
        <div class="floating-element">💰</div>
        <div class="floating-element">🤖</div>
    </div>
    
    <div class="container">
        <header class="header">
            <h1>🧾 Raseed</h1>
            <p>Smart Receipt Manager powered by AI</p>
        </header>
        
        <div class="hero-section">
            <div class="hero-content">
                <div class="hero-text">
                    <h2>Transform Your Receipt Management</h2>
                    <p>Raseed is an intelligent receipt management system that uses cutting-edge AI technology to digitize, organize, and analyze your receipts. Say goodbye to paper clutter and hello to smart financial insights.</p>
                    <p>With Google Cloud Vision API and Gemini AI, Raseed automatically extracts key information from your receipts and provides personalized spending analytics through natural language conversations.</p>
                </div>
                <div class="hero-visual">
                    <div style="font-size: 8rem; text-align: center; color: rgba(255,255,255,0.8);">
                        📱💡🧾
                    </div>
                </div>
            </div>
        </div>
        
        <div class="features-grid">
            <div class="feature-card">
                <span class="feature-icon">📸</span>
                <h3>Smart OCR Recognition</h3>
                <p>Advanced Google Cloud Vision API automatically extracts text, amounts, dates, and merchant information from your receipt images with high accuracy.</p>
            </div>
            
            <div class="feature-card">
                <span class="feature-icon">🤖</span>
                <h3>AI-Powered Analysis</h3>
                <p>Gemini AI processes your receipts intelligently, categorizes purchases, and provides detailed spending insights through natural conversations.</p>
            </div>
            
            <div class="feature-card">
                <span class="feature-icon">🎤</span>
                <h3>Voice Assistant</h3>
                <p>Ask questions about your spending using voice commands. Get instant answers about your expenses, favorite stores, and financial patterns.</p>
            </div>
            
            <div class="feature-card">
                <span class="feature-icon">☁️</span>
                <h3>Cloud Storage</h3>
                <p>Secure Google Firestore database keeps your receipts safe and accessible from anywhere. Never lose a receipt again!</p>
            </div>
            
            <div class="feature-card">
                <span class="feature-icon">📊</span>
                <h3>Spending Analytics</h3>
                <p>Visualize your spending patterns, track categories, and get personalized recommendations to optimize your financial habits.</p>
            </div>
            
            <div class="feature-card">
                <span class="feature-icon">📱</span>
                <h3>Mobile Wallet Integration</h3>
                <p>Generate digital wallet passes for your receipts, making them easily accessible on your mobile device for returns and warranties.</p>
            </div>
        </div>
        
        <div class="cta-section">
            <h2 style="color: white; margin-bottom: 30px; font-size: 2.5rem;">Ready to Get Started?</h2>
            <a href="/login" class="btn">Sign In</a>
            <a href="/signup" class="btn btn-secondary">Create Account</a>
        </div>
    </div>
</body>
</html>
""")

@app.route("/login")
def login_page():
    """Login page"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In - Raseed</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        
        .login-container {
            max-width: 450px;
            width: 100%;
            background: rgba(255,255,255,0.95);
            border-radius: 25px;
            padding: 50px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.2);
            animation: slideIn 0.6s ease-out;
        }
        
        .login-header {
            text-align: center;
            margin-bottom: 40px;
        }
        
        .login-title {
            font-size: 2.5rem;
            color: #667eea;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
        }
        
        .login-subtitle {
            color: #666;
            font-size: 1.1rem;
        }
        
        .form-group {
            margin-bottom: 25px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 600;
        }
        
        .form-group input {
            width: 100%;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 1rem;
            transition: all 0.3s ease;
            background: rgba(255,255,255,0.8);
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .btn {
            width: 100%;
            padding: 15px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            margin-bottom: 20px;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px rgba(102, 126, 234, 0.3);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        
        .google-btn {
            background: #4285F4;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }
        
        .google-btn:hover {
            background: #357ABD;
        }
        
        .divider {
            text-align: center;
            margin: 30px 0;
            position: relative;
            color: #666;
        }
        
        .divider::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 0;
            right: 0;
            height: 1px;
            background: #e0e0e0;
        }
        
        .divider span {
            background: rgba(255,255,255,0.95);
            padding: 0 20px;
        }
        
        .links {
            text-align: center;
            margin-top: 30px;
        }
        
        .links a {
            color: #667eea;
            text-decoration: none;
            margin: 0 10px;
            font-weight: 500;
            transition: color 0.3s ease;
        }
        
        .links a:hover {
            color: #764ba2;
            text-decoration: underline;
        }
        
        .error-message {
            background: #ffebee;
            color: #c62828;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            border-left: 4px solid #f44336;
            animation: shake 0.5s ease-in-out;
        }
        
        .success-message {
            background: #e8f5e8;
            color: #2e7d32;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            border-left: 4px solid #4caf50;
        }
        
        @keyframes slideIn {
            from { opacity: 0; transform: translateY(-30px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-5px); }
            75% { transform: translateX(5px); }
        }
        
        @media (max-width: 480px) {
            .login-container {
                padding: 30px 20px;
            }
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="login-header">
            <h1 class="login-title">🧾 Raseed</h1>
            <p class="login-subtitle">Sign in to your account</p>
        </div>
        
        <div id="errorMessage" style="display: none;" class="error-message"></div>
        <div id="successMessage" style="display: none;" class="success-message"></div>
        
        <form id="loginForm">
            <div class="form-group">
                <label for="email">Email Address</label>
                <input type="email" id="email" name="email" required>
            </div>
            
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required>
            </div>
            
            <button type="submit" class="btn" id="loginBtn">Sign In</button>
        </form>
        
        <div class="divider">
            <span>or</span>
        </div>
        
        <button class="btn google-btn" onclick="signInWithGoogle()">
            <img src="https://upload.wikimedia.org/wikipedia/commons/5/53/Google_%22G%22_Logo.svg" width="20" height="20" alt="Google">
            Sign in with Google
        </button>
        
        <div class="links">
            <a href="/signup">Create Account</a>
            <a href="/">Back to Home</a>
        </div>
    </div>
    
    <script>
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            const btn = document.getElementById('loginBtn');
            
            btn.disabled = true;
            btn.textContent = 'Signing in...';
            
            try {
                const response = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ email, password })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showMessage('Login successful! Redirecting...', 'success');
                    setTimeout(() => {
                        window.location.href = '/dashboard';
                    }, 1500);
                } else {
                    showMessage(data.error || 'Login failed', 'error');
                }
            } catch (error) {
                showMessage('Network error. Please try again.', 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'Sign In';
            }
        });
        
        function signInWithGoogle() {
            window.location.href = '/api/auth/google';
        }
        
        function showMessage(message, type) {
            const errorDiv = document.getElementById('errorMessage');
            const successDiv = document.getElementById('successMessage');
            
            errorDiv.style.display = 'none';
            successDiv.style.display = 'none';
            
            if (type === 'error') {
                errorDiv.textContent = message;
                errorDiv.style.display = 'block';
            } else {
                successDiv.textContent = message;
                successDiv.style.display = 'block';
            }
        }
        
        // Show error from URL params
        const urlParams = new URLSearchParams(window.location.search);
        const error = urlParams.get('error');
        if (error) {
            showMessage(error, 'error');
        }
    </script>
</body>
</html>
""")

@app.route("/signup")
def signup_page():
    """Signup page"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Create Account - Raseed</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        
        .signup-container {
            max-width: 450px;
            width: 100%;
            background: rgba(255,255,255,0.95);
            border-radius: 25px;
            padding: 50px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.2);
            animation: slideIn 0.6s ease-out;
        }
        
        .signup-header {
            text-align: center;
            margin-bottom: 40px;
        }
        
        .signup-title {
            font-size: 2.5rem;
            color: #667eea;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
        }
        
        .signup-subtitle {
            color: #666;
            font-size: 1.1rem;
        }
        
        .form-group {
            margin-bottom: 25px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 600;
        }
        
        .form-group input {
            width: 100%;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 1rem;
            transition: all 0.3s ease;
            background: rgba(255,255,255,0.8);
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .btn {
            width: 100%;
            padding: 15px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            margin-bottom: 20px;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px rgba(102, 126, 234, 0.3);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        
        .google-btn {
            background: #4285F4;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }
        
        .google-btn:hover {
            background: #357ABD;
        }
        
        .divider {
            text-align: center;
            margin: 30px 0;
            position: relative;
            color: #666;
        }
        
        .divider::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 0;
            right: 0;
            height: 1px;
            background: #e0e0e0;
        }
        
        .divider span {
            background: rgba(255,255,255,0.95);
            padding: 0 20px;
        }
        
        .links {
            text-align: center;
            margin-top: 30px;
        }
        
        .links a {
            color: #667eea;
            text-decoration: none;
            margin: 0 10px;
            font-weight: 500;
            transition: color 0.3s ease;
        }
        
        .links a:hover {
            color: #764ba2;
            text-decoration: underline;
        }
        
        .error-message {
            background: #ffebee;
            color: #c62828;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            border-left: 4px solid #f44336;
            animation: shake 0.5s ease-in-out;
        }
        
        .success-message {
            background: #e8f5e8;
            color: #2e7d32;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            border-left: 4px solid #4caf50;
        }
        
        @keyframes slideIn {
            from { opacity: 0; transform: translateY(-30px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-5px); }
            75% { transform: translateX(5px); }
        }
        
        @media (max-width: 480px) {
            .signup-container {
                padding: 30px 20px;
            }
        }
    </style>
</head>
<body>
    <div class="signup-container">
        <div class="signup-header">
            <h1 class="signup-title">🧾 Raseed</h1>
            <p class="signup-subtitle">Create your account</p>
        </div>
        
        <div id="errorMessage" style="display: none;" class="error-message"></div>
        <div id="successMessage" style="display: none;" class="success-message"></div>
        
        <form id="signupForm">
            <div class="form-group">
                <label for="name">Full Name</label>
                <input type="text" id="name" name="name" required>
            </div>
            
            <div class="form-group">
                <label for="email">Email Address</label>
                <input type="email" id="email" name="email" required>
            </div>
            
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required minlength="8">
            </div>
            
            <div class="form-group">
                <label for="confirmPassword">Confirm Password</label>
                <input type="password" id="confirmPassword" name="confirmPassword" required minlength="8">
            </div>
            
            <button type="submit" class="btn" id="signupBtn">Create Account</button>
        </form>
        
        <div class="divider">
            <span>or</span>
        </div>
        
        <button class="btn google-btn" onclick="signInWithGoogle()">
            <img src="https://upload.wikimedia.org/wikipedia/commons/5/53/Google_%22G%22_Logo.svg" width="20" height="20" alt="Google">
            Sign up with Google
        </button>
        
        <div class="links">
            <a href="/login">Already have an account? Sign In</a>
            <a href="/">Back to Home</a>
        </div>
    </div>
    
    <script>
        document.getElementById('signupForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const name = document.getElementById('name').value;
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            const confirmPassword = document.getElementById('confirmPassword').value;
            const btn = document.getElementById('signupBtn');
            
            if (password !== confirmPassword) {
                showMessage('Passwords do not match', 'error');
                return;
            }
            
            btn.disabled = true;
            btn.textContent = 'Creating account...';
            
            try {
                const response = await fetch('/api/auth/signup', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ name, email, password })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showMessage('Account created successfully! Redirecting...', 'success');
                    setTimeout(() => {
                        window.location.href = '/dashboard';
                    }, 1500);
                } else {
                    showMessage(data.error || 'Signup failed', 'error');
                }
            } catch (error) {
                showMessage('Network error. Please try again.', 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'Create Account';
            }
        });
        
        function signInWithGoogle() {
            window.location.href = '/api/auth/google';
        }
        
        function showMessage(message, type) {
            const errorDiv = document.getElementById('errorMessage');
            const successDiv = document.getElementById('successMessage');
            
            errorDiv.style.display = 'none';
            successDiv.style.display = 'none';
            
            if (type === 'error') {
                errorDiv.textContent = message;
                errorDiv.style.display = 'block';
            } else {
                successDiv.textContent = message;
                successDiv.style.display = 'block';
            }
        }
        
        // Show error from URL params
        const urlParams = new URLSearchParams(window.location.search);
        const error = urlParams.get('error');
        if (error) {
            showMessage(error, 'error');
        }
    </script>
</body>
</html>
""")

@app.route("/dashboard")
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    
    user_info = get_user_info(session['user_id'])
    if not user_info:
        return redirect(url_for('logout'))
    
    lang = user_info.get('language', 'en')
    texts = get_language_text(lang)
    
    # Debug prints
    print("User Info:", user_info)
    print("Texts:", texts)
    print("Languages:", LANGUAGES)
    
    # Ensure all required variables are defined
    if not all([user_info, texts, LANGUAGES]):
        return "Error: Missing required data", 500
    
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ texts.dashboard_title }}</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            text-align: center;
            margin-bottom: 40px;
            color: white;
            position: relative;
            animation: fadeInDown 1s ease-out;
        }
        .header h1 {
            font-size: 3rem;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        .header p {
            font-size: 1.2rem;
            opacity: 0.9;
        }
        .main-content {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
            margin-bottom: 40px;
            animation: fadeIn 1s ease-out;
        }
        .card {
            background: white;
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 15px 35px rgba(0,0,0,0.1);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 20px 45px rgba(0,0,0,0.15);
        }
        .upload-area {
            border: 3px dashed #667eea;
            border-radius: 15px;
            padding: 40px;
            text-align: center;
            margin-bottom: 20px;
            transition: all 0.3s ease;
            cursor: pointer;
        }
        .upload-area:hover {
            background: #f8f9ff;
            border-color: #5a6fd8;
        }
        .upload-icon {
            font-size: 3rem;
            color: #667eea;
            margin-bottom: 15px;
        }
        .file-input {
            display: none;
        }
        .btn {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 25px;
            font-size: 1rem;
            cursor: pointer;
            transition: all 0.3s ease;
            margin: 10px 5px;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.3);
        }
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        .btn-logout {
            background: #ff5252;
            position: absolute;
            top: 0;
            right: 0;
        }
        .query-section {
            margin-top: 20px;
            display: flex;
            align-items: center;
        }
        .query-input {
            flex: 1;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px 0 0 10px;
            font-size: 1rem;
            margin-bottom: 15px;
            transition: border-color 0.3s ease;
        }
        .query-input:focus {
            outline: none;
            border-color: #667eea;
        }
        .mic-btn {
            background: #4caf50;
            color: white;
            border: none;
            padding: 15px;
            border-radius: 0 10px 10px 0;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .mic-btn:hover {
            background: #388e3c;
        }
        .mic-btn.listening {
            animation: pulse 1.5s infinite;
        }
        .response-area {
            background: #f8f9ff;
            border-radius: 10px;
            padding: 20px;
            margin-top: 20px;
            min-height: 100px;
            white-space: pre-wrap;
            line-height: 1.6;
        }
        .receipts-section {
            grid-column: 1 / -1;
            margin-top: 20px;
        }
        .receipts-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .receipt-card {
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            border-left: 4px solid #667eea;
            transition: transform 0.3s ease;
        }
        .receipt-card:hover {
            transform: translateY(-3px);
        }
        .receipt-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .receipt-merchant {
            font-weight: bold;
            color: #667eea;
            font-size: 1.2rem;
        }
        .receipt-total {
            font-size: 1.5rem;
            font-weight: bold;
            color: #4caf50;
        }
        .receipt-details {
            font-size: 0.9rem;
            color: #666;
            margin-bottom: 10px;
        }
        .receipt-items {
            margin-top: 10px;
        }
        .receipt-item {
            display: flex;
            justify-content: space-between;
            margin-bottom: 5px;
            font-size: 0.9rem;
        }
        .loading {
            display: none;
            text-align: center;
            padding: 20px;
        }
        .spinner {
            border: 4px solid #f3f3f3;
            border-top: 4px solid #667eea;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto 15px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.1); }
        }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes fadeInDown {
            from { opacity: 0; transform: translateY(-20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .error-message {
            background: #ffebee;
            color: #c62828;
            padding: 15px;
            border-radius: 10px;
            margin: 10px 0;
            border-left: 4px solid #f44336;
        }
        .success-message {
            background: #e8f5e8;
            color: #2e7d32;
            padding: 15px;
            border-radius: 10px;
            margin: 10px 0;
            border-left: 4px solid #4caf50;
        }
        .user-section {
            background: rgba(255,255,255,0.1);
            padding: 20px;
            border-radius: 15px;
            margin-bottom: 30px;
            color: white;
            animation: fadeIn 1s ease-out;
        }
        .user-profile {
            display: flex;
            align-items: center;
            margin-bottom: 20px;
        }
        .user-avatar {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            margin-right: 15px;
            border: 2px solid white;
        }
        .user-info {
            flex: 1;
        }
        .user-name {
            font-weight: bold;
            margin-bottom: 5px;
        }
        .user-email {
            font-size: 0.9rem;
            opacity: 0.8;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        .stat-card {
            background: rgba(255,255,255,0.2);
            border-radius: 10px;
            padding: 15px;
            text-align: center;
        }
        .stat-value {
            font-size: 1.8rem;
            font-weight: bold;
            margin-bottom: 5px;
        }
        .stat-label {
            font-size: 0.9rem;
            opacity: 0.8;
        }
        .language-selector {
            position: absolute;
            top: 20px;
            left: 20px;
            z-index: 100;
        }
        .language-btn {
            background: rgba(255,255,255,0.2);
            border: none;
            color: white;
            padding: 8px 15px;
            border-radius: 20px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s ease;
        }
        .language-btn:hover {
            background: rgba(255,255,255,0.3);
        }
        .language-dropdown {
            position: absolute;
            top: 100%;
            left: 0;
            background: white;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            overflow: hidden;
            display: none;
        }
        .language-dropdown.show {
            display: block;
        }
        .language-option {
            padding: 10px 15px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }
        .language-option:hover {
            background: #f5f5f5;
        }
        @media (max-width: 768px) {
            .main-content {
                grid-template-columns: 1fr;
            }
            .header h1 {
                font-size: 2rem;
            }
            .card {
                padding: 20px;
            }
            .language-selector {
                top: 10px;
                left: 10px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="language-selector">
            <button class="language-btn" id="languageBtn">
                <span id="currentLanguageFlag">{{ LANGUAGES[lang].flag }}</span>
                <span id="currentLanguageName">{{ LANGUAGES[lang].name }}</span>
                <span>▼</span>
            </button>
            <div class="language-dropdown" id="languageDropdown">
                {% for code, lang_data in LANGUAGES.items() %}
                <div class="language-option" onclick="changeLanguage('{{ code }}')">
                    <span>{{ lang_data.flag }}</span>
                    <span>{{ lang_data.name }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        
        <div class="header">
            <h1>{{ texts.dashboard_title }}</h1>
            <p>{{ texts.dashboard_subtitle }}</p>
            <button class="btn btn-logout" onclick="logout()">{{ texts.logout }}</button>
        </div>
        
        <div class="user-section">
            <div class="user-profile">
                <img id="userAvatar" class="user-avatar" src="{{ user_info.picture or 'https://www.gravatar.com/avatar/default?s=200' }}" alt="User Avatar">
                <div class="user-info">
                    <div id="userName" class="user-name">{{ user_info.name }}</div>
                    <div id="userEmail" class="user-email">{{ user_info.email }}</div>
                </div>
            </div>
            <div class="stats-grid">
                <div class="stat-card">
                    <div id="totalReceipts" class="stat-value">0</div>
                    <div class="stat-label">{{ texts.total_receipts }}</div>
                </div>
                <div class="stat-card">
                    <div id="totalSpent" class="stat-value">$0.00</div>
                    <div class="stat-label">{{ texts.total_spent }}</div>
                </div>
                <div class="stat-card">
                    <div id="topCategory" class="stat-value">-</div>
                    <div class="stat-label">{{ texts.top_category }}</div>
                </div>
                <div class="stat-card">
                    <div id="avgSpend" class="stat-value">$0.00</div>
                    <div class="stat-label">{{ texts.avg_spend }}</div>
                </div>
            </div>
        </div>
        
        <div class="main-content">
            <div class="card">
                <h2>{{ texts.upload_receipt }}</h2>
                <div class="upload-area" onclick="document.getElementById('fileInput').click()">
                    <div class="upload-icon">📷</div>
                    <p>{{ texts.upload_text }}</p>
                    <p style="font-size: 0.9rem; color: #666; margin-top: 10px;">{{ texts.upload_supported }}</p>
                </div>
                <input type="file" id="fileInput" class="file-input" accept="image/*" onchange="handleFileSelect(event)">
                <div class="loading" id="uploadLoading">
                    <div class="spinner"></div>
                    <p>{{ texts.processing }}</p>
                </div>
                <div id="uploadResult"></div>
                <button class="btn" onclick="processReceipt()" id="processBtn" disabled>{{ texts.process_btn }}</button>
            </div>
            
            <div class="card">
                <h2>{{ texts.ai_assistant }}</h2>
                <div class="query-section">
                    <input type="text" class="query-input" id="queryInput" placeholder="{{ texts.ai_placeholder }}">
                    <button class="mic-btn" title="Start voice input" onclick="startListening()" id="micButton">🎤</button>
                </div>
                <button class="btn" onclick="queryAI()">{{ texts.ask_ai }}</button>
                <div class="loading" id="queryLoading">
                    <div class="spinner"></div>
                    <p>{{ texts.thinking }}</p>
                </div>
                <div class="response-area" id="aiResponse">
                    {{ texts.ai_welcome }}
                </div>
            </div>
        </div>
        
        <div class="card receipts-section">
            <h2>{{ texts.your_receipts }}</h2>
            <button class="btn" onclick="loadReceipts()">{{ texts.refresh_receipts }}</button>
            <div class="loading" id="receiptsLoading">
                <div class="spinner"></div>
                <p>{{ texts.loading }}</p>
            </div>
            <div class="receipts-grid" id="receiptsGrid">
                <!-- Receipts will be loaded here -->
            </div>
        </div>
    </div>
    
    <script>
        const API_BASE_URL = window.location.origin;
        const TEXTS = {{ texts|tojson }};
        const LANGUAGES = {{ LANGUAGES|tojson }};
        let selectedFile = null;
        let recognition = null;
        let isListening = false;
        let currentLanguage = '{{ lang }}';
        
        // Initialize voice recognition if available
        function initVoiceRecognition() {
            if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
                const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
                recognition = new SpeechRecognition();
                recognition.continuous = false;
                recognition.interimResults = false;
                recognition.lang = currentLanguage === 'te' ? 'te-IN' : 
                                  currentLanguage === 'kn' ? 'kn-IN' : 'en-US';
                
                recognition.onresult = function(event) {
                    const transcript = event.results[0][0].transcript;
                    document.getElementById('queryInput').value = transcript;
                    stopListening();
                    queryAI();
                };
                
                recognition.onerror = function(event) {
                    console.error('Voice recognition error:', event.error);
                    stopListening();
                    showMessage('Voice recognition error: ' + event.error, 'error');
                };
                
                recognition.onend = function() {
                    if (isListening) {
                        recognition.start(); // Restart if still in listening mode
                    }
                };
            } else {
                document.getElementById('micButton').style.display = 'none';
            }
        }
        
        function startListening() {
            if (!recognition) {
                showMessage('Voice recognition not supported in your browser', 'error');
                return;
            }
            
            if (isListening) {
                stopListening();
                return;
            }
            
            isListening = true;
            document.getElementById('micButton').classList.add('listening');
            document.getElementById('queryInput').placeholder = "Listening...";
            recognition.lang = currentLanguage === 'te' ? 'te-IN' : 
                              currentLanguage === 'kn' ? 'kn-IN' : 'en-US';
            recognition.start();
        }
        
        function stopListening() {
            isListening = false;
            document.getElementById('micButton').classList.remove('listening');
            document.getElementById('queryInput').placeholder = TEXTS.ai_placeholder;
            if (recognition) {
                recognition.stop();
            }
        }
        
        function toggleLanguageDropdown() {
            const dropdown = document.getElementById('languageDropdown');
            dropdown.classList.toggle('show');
        }
        
        function changeLanguage(lang) {
            currentLanguage = lang;
            document.getElementById('languageDropdown').classList.remove('show');
            document.getElementById('currentLanguageFlag').textContent = LANGUAGES[lang].flag;
            document.getElementById('currentLanguageName').textContent = LANGUAGES[lang].name;
            
            // Update UI text
            fetch(`${API_BASE_URL}/api/update-language`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include',
                body: JSON.stringify({ language: lang })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    window.location.reload();
                }
            });
        }
        
        document.addEventListener('DOMContentLoaded', () => {
            initVoiceRecognition();
            setupEventListeners();
            loadUserProfile();
            loadReceipts();
            loadStats();
            
            // Close language dropdown when clicking outside
            document.addEventListener('click', (e) => {
                if (!e.target.closest('.language-selector')) {
                    document.getElementById('languageDropdown').classList.remove('show');
                }
            });
            
            document.getElementById('languageBtn').addEventListener('click', toggleLanguageDropdown);
        });
        
        function setupEventListeners() {
            const uploadArea = document.querySelector('.upload-area');
            uploadArea.addEventListener('dragover', (e) => {
                e.preventDefault();
                uploadArea.classList.add('dragover');
            });
            
            uploadArea.addEventListener('dragleave', () => {
                uploadArea.classList.remove('dragover');
            });
            
            uploadArea.addEventListener('drop', (e) => {
                e.preventDefault();
                uploadArea.classList.remove('dragover');
                const files = e.dataTransfer.files;
                if (files.length > 0) handleFileSelect({ target: { files } });
            });
            
            document.getElementById('queryInput').addEventListener('keypress', (e) => {
                if (e.key === 'Enter') queryAI();
            });
        }
        
        async function loadUserProfile() {
            try {
                const res = await fetch(`${API_BASE_URL}/api/user-info`, { 
                    credentials: 'include' 
                });
                const data = await res.json();
                
                if (data.success) {
                    document.getElementById('userName').textContent = data.user.name || 'User';
                    document.getElementById('userEmail').textContent = data.user.email || '';
                    
                    if (data.user.picture) {
                        document.getElementById('userAvatar').src = data.user.picture;
                    }
                }
            } catch (error) {
                console.error('Error loading user profile:', error);
            }
        }
        
        async function loadStats() {
            try {
                const res = await fetch(`${API_BASE_URL}/api/stats`, {
                    credentials: 'include'
                });
                const data = await res.json();
                
                if (data.success) {
                    document.getElementById('totalReceipts').textContent = data.stats.total_receipts || 0;
                    document.getElementById('totalSpent').textContent = `$${data.stats.total_spent?.toFixed(2) || '0.00'}`;
                    document.getElementById('topCategory').textContent = data.stats.top_category || '-';
                    document.getElementById('avgSpend').textContent = `$${data.stats.avg_spend?.toFixed(2) || '0.00'}`;
                }
            } catch (error) {
                console.error('Error loading stats:', error);
            }
        }
        
        async function logout() {
            try {
                await fetch(`${API_BASE_URL}/api/logout`, { 
                    method: 'POST', 
                    credentials: 'include' 
                });
                window.location.href = '/login';
            } catch (error) {
                console.error('Logout error:', error);
                showMessage(TEXTS.error_processing, 'error');
            }
        }
        
        function handleFileSelect(event) {
            const file = event.target.files[0];
            if (!file) return;
            
            selectedFile = file;
            document.getElementById('processBtn').disabled = false;
            
            const reader = new FileReader();
            reader.onload = (e) => {
                const area = document.querySelector('.upload-area');
                area.innerHTML = `
                    <img src="${e.target.result}" style="max-width: 100%; max-height: 200px; border-radius: 10px;">
                    <p style="margin-top: 10px;">✅ ${file.name} selected</p>
                `;
            };
            reader.readAsDataURL(file);
        }
        
        async function processReceipt() {
            if (!selectedFile) {
                showMessage('Please select a receipt image first', 'error');
                return;
            }
            
            const loading = document.getElementById('uploadLoading');
            const result = document.getElementById('uploadResult');
            const btn = document.getElementById('processBtn');
            
            loading.style.display = 'block';
            btn.disabled = true;
            result.innerHTML = '';
            
            try {
                const base64 = await fileToBase64(selectedFile);
                
                const response = await fetch(`${API_BASE_URL}/api/process-receipt`, {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json' 
                    },
                    credentials: 'include',
                    body: JSON.stringify({ imageData: base64 })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    result.innerHTML = `
                        <div class="success-message">
                            <h3>${TEXTS.receipt_processed}</h3>
                            <p><strong>${currentLanguage === 'te' ? 'వ్యాపారి' : currentLanguage === 'kn' ? 'ವ್ಯಾಪಾರಿ' : 'Merchant'}:</strong> ${data.data.merchant || 'Unknown'}</p>
                            <p><strong>${currentLanguage === 'te' ? 'మొత్తం' : currentLanguage === 'kn' ? 'ಒಟ್ಟು' : 'Total'}:</strong> $${data.data.total?.toFixed(2) || '0.00'}</p>
                            <p><strong>${currentLanguage === 'te' ? 'తేదీ' : currentLanguage === 'kn' ? 'ದಿನಾಂಕ' : 'Date'}:</strong> ${data.data.date || 'Unknown date'}</p>
                            <p><strong>${currentLanguage === 'te' ? 'వస్తువులు' : currentLanguage === 'kn' ? 'ವಸ್ತುಗಳು' : 'Items'}:</strong> ${data.data.items?.length || 0} ${currentLanguage === 'te' ? 'వస్తువు(లు)' : currentLanguage === 'kn' ? 'ವಸ್ತು(ಗಳು)' : 'item(s)'}</p>
                        </div>
                    `;
                    
                    resetUploadForm();
                    setTimeout(() => {
                        loadReceipts();
                        loadStats();
                    }, 1000);
                } else {
                    throw new Error(data.error || 'Processing failed');
                }
            } catch (error) {
                console.error('Error:', error);
                result.innerHTML = `
                    <div class="error-message">
                        <h3>${TEXTS.error_processing}</h3>
                        <p>${error.message}</p>
                    </div>
                `;
            } finally {
                loading.style.display = 'none';
                btn.disabled = false;
            }
        }
        
        async function queryAI() {
            const query = document.getElementById('queryInput').value.trim();
            if (!query) {
                showMessage('Please enter a question', 'error');
                return;
            }
            
            const loading = document.getElementById('queryLoading');
            const responseDiv = document.getElementById('aiResponse');
            
            loading.style.display = 'block';
            responseDiv.innerHTML = '';
            
            try {
                const res = await fetch(`${API_BASE_URL}/api/process-query`, {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json' 
                    },
                    credentials: 'include',
                    body: JSON.stringify({ 
                        query,
                        language: currentLanguage 
                    })
                });
                
                const data = await res.json();
                
                if (data.success) {
                    responseDiv.innerHTML = data.response;
                    
                    // Speak the response if speech synthesis is available
                    if ('speechSynthesis' in window) {
                        const utterance = new SpeechSynthesisUtterance();
                        utterance.text = data.response.replace(/<[^>]*>/g, '');
                        utterance.lang = currentLanguage === 'te' ? 'te-IN' : 
                                        currentLanguage === 'kn' ? 'kn-IN' : 'en-US';
                        speechSynthesis.speak(utterance);
                    }
                    
                    document.getElementById('queryInput').value = '';
                } else {
                    throw new Error(data.error || 'Query failed');
                }
            } catch (error) {
                console.error('Error:', error);
                responseDiv.innerHTML = `❌ ${currentLanguage === 'te' ? 'లోపం' : currentLanguage === 'kn' ? 'ತಪ್ಪು' : 'Error'}: ${error.message}`;
            } finally {
                loading.style.display = 'none';
            }
        }
        
        async function loadReceipts() {
            const loading = document.getElementById('receiptsLoading');
            const grid = document.getElementById('receiptsGrid');
            
            loading.style.display = 'block';
            grid.innerHTML = '';
            
            try {
                const res = await fetch(`${API_BASE_URL}/api/get-receipts`, {
                    credentials: 'include'
                });
                
                const data = await res.json();
                
                if (data.success) {
                    if (data.receipts.length === 0) {
                        grid.innerHTML = `
                            <p style="text-align: center; color: #666; grid-column: 1 / -1;">
                                ${TEXTS.no_receipts}
                            </p>
                        `;
                    } else {
                        data.receipts.forEach(receipt => {
                            const card = createReceiptCard(receipt);
                            grid.appendChild(card);
                        });
                    }
                } else {
                    throw new Error(data.error || 'Failed to load receipts');
                }
            } catch (error) {
                console.error('Error:', error);
                grid.innerHTML = `
                    <p style="color: #f44336; grid-column: 1 / -1;">
                        ❌ ${currentLanguage === 'te' ? 'రసీదులను లోడ్ చేయడంలో లోపం' : currentLanguage === 'kn' ? 'ರಸೀದಿಗಳನ್ನು ಲೋಡ್ ಮಾಡುವಲ್ಲಿ ದೋಷ' : 'Error loading receipts'}: ${error.message}
                    </p>
                `;
            } finally {
                loading.style.display = 'none';
            }
        }
        
        function createReceiptCard(receipt) {
            const parsed = receipt.parsedData || {};
            const items = parsed.items || [];
            
            const card = document.createElement('div');
            card.className = 'receipt-card';
            
            card.innerHTML = `
                <div class="receipt-header">
                    <div class="receipt-merchant">${parsed.merchant || (currentLanguage === 'te' ? 'తెలియదు' : currentLanguage === 'kn' ? 'ತಿಳಿದಿಲ್ಲ' : 'Unknown')}</div>
                    <div class="receipt-total">$${parsed.total?.toFixed(2) || '0.00'}</div>
                </div>
                <div class="receipt-details">
                    <p>📅 ${parsed.date || (currentLanguage === 'te' ? 'తెలియని తేదీ' : currentLanguage === 'kn' ? 'ತಿಳಿದಿಲ್ಲದ ದಿನಾಂಕ' : 'Unknown date')}</p>
                    <p>🏷️ ${parsed.category || (currentLanguage === 'te' ? 'వర్గీకరించబడలేదు' : currentLanguage === 'kn' ? 'ವರ್ಗೀಕರಿಸದ' : 'Uncategorized')}</p>
                    <p>📊 ${currentLanguage === 'te' ? 'పన్ను' : currentLanguage === 'kn' ? 'ತೆರಿಗೆ' : 'Tax'}: $${parsed.tax?.toFixed(2) || '0.00'}</p>
                </div>
                <div class="receipt-items">
                    <strong>${currentLanguage === 'te' ? 'వస్తువులు' : currentLanguage === 'kn' ? 'ವಸ್ತುಗಳು' : 'Items'} (${items.length}):</strong>
                    ${items.slice(0, 3).map(item => `
                        <div class="receipt-item">
                            <span>${item.name || (currentLanguage === 'te' ? 'వస్తువు' : currentLanguage === 'kn' ? 'ವಸ್ತು' : 'Item')}</span>
                            <span>$${item.price?.toFixed(2) || '0.00'}</span>
                        </div>
                    `).join('')}
                    ${items.length > 3 ? `<div style="font-size: 0.8rem; color: #666;">${currentLanguage === 'te' ? '... మరియు మరిన్ని' : currentLanguage === 'kn' ? '... ಮತ್ತು ಹೆಚ್ಚು' : '... and more'}</div>` : ''}
                </div>
                <button class="btn" onclick="createWalletPass('${receipt.receiptId}')" style="margin-top: 15px; padding: 8px 16px; font-size: 0.9rem;">
                    ${TEXTS.create_wallet}
                </button>
            `;
            
            return card;
        }
        
        async function createWalletPass(receiptId) {
            try {
                const res = await fetch(`${API_BASE_URL}/api/create-wallet-pass`, {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json' 
                    },
                    credentials: 'include',
                    body: JSON.stringify({ receiptId })
                });
                
                const data = await res.json();
                
                if (data.success) {
                    showMessage(currentLanguage === 'te' ? 'వాలెట్ పాస్ విజయవంతంగా సృష్టించబడింది!' : 
                              currentLanguage === 'kn' ? 'ವಾಲೆಟ್ ಪಾಸ್ ಯಶಸ್ವಿಯಾಗಿ ರಚಿಸಲ್ಪಟ್ಟಿದೆ!' : 
                              'Wallet pass created successfully!', 'success');
                    console.log('Wallet pass data:', data.walletPass);
                } else {
                    throw new Error(data.error || 'Failed to create wallet pass');
                }
            } catch (error) {
                console.error('Error:', error);
                showMessage(currentLanguage === 'te' ? 'వాలెట్ పాస్ సృష్టించడంలో లోపం' : 
                          currentLanguage === 'kn' ? 'ವಾಲೆಟ್ ಪಾಸ್ ರಚಿಸುವಲ್ಲಿ ದೋಷ' : 
                          'Error creating wallet pass', 'error');
            }
        }
        
        function resetUploadForm() {
            selectedFile = null;
            document.getElementById('fileInput').value = '';
            document.getElementById('processBtn').disabled = true;
            document.querySelector('.upload-area').innerHTML = `
                <div class="upload-icon">📷</div>
                <p>${TEXTS.upload_text}</p>
                <p style="font-size: 0.9rem; color: #666; margin-top: 10px;">${TEXTS.upload_supported}</p>
            `;
        }
        
        function fileToBase64(file) {
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.readAsDataURL(file);
                reader.onload = () => resolve(reader.result);
                reader.onerror = error => reject(error);
            });
        }
        
        function showMessage(message, type) {
            const div = document.createElement('div');
            div.className = type === 'success' ? 'success-message' : 'error-message';
            div.textContent = message;
            div.style.position = 'fixed';
            div.style.top = '20px';
            div.style.right = '20px';
            div.style.zIndex = '1000';
            div.style.maxWidth = '300px';
            div.style.padding = '15px';
            div.style.borderRadius = '10px';
            div.style.boxShadow = '0 5px 15px rgba(0,0,0,0.1)';
            div.style.animation = 'fadeIn 0.3s ease-out';
            
            document.body.appendChild(div);
            
            setTimeout(() => {
                div.style.animation = 'fadeOut 0.3s ease-out';
                setTimeout(() => div.remove(), 300);
            }, 3000);
        }
    </script>
</body>
</html>
""", user_info=user_info, texts=texts, LANGUAGES=LANGUAGES, lang=lang)

@app.route("/api/auth/google")
def google_auth():
    # Create flow instance to manage the OAuth 2.0 Authorization Grant Flow steps
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config={
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI]
            }
        },
        scopes=["openid", "email", "profile"]
    )
    
    # Generate URL for request to Google's OAuth 2.0 server
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    
    # Store the state so the callback can verify the auth server response
    session['state'] = state
    
    return redirect(authorization_url)

@app.route("/api/auth/google/callback")
def google_auth_callback():
    try:
        # Verify state
        if request.args.get('state') != session.get('state'):
            return redirect(url_for('login_page', error="Invalid state parameter"))

        # Create flow instance with the same client config
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            client_config={
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [REDIRECT_URI]
                }
            },
            scopes=["openid", "email", "profile"],
            state=session['state']
        )
        
        # Exchange authorization code for tokens
        flow.fetch_token(authorization_response=request.url)
        
        # Get ID token from credentials
        credentials = flow.credentials
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID
        )

        # Verify token audience
        if id_info['aud'] != GOOGLE_CLIENT_ID:
            raise ValueError("Invalid audience")

        # Store user information in session
        session['user_id'] = id_info['sub']
        session['user_email'] = id_info['email']
        session['user_name'] = id_info.get('name', '')
        session['user_picture'] = id_info.get('picture', '')
        
        # Create or update user in database
        create_or_update_user({
            'sub': id_info['sub'],
            'email': id_info['email'],
            'name': id_info.get('name', ''),
            'picture': id_info.get('picture', ''),
            'language': 'en'  # Default language
        })

        return redirect(url_for('dashboard'))

    except Exception as e:
        logger.error(f"Google auth callback error: {str(e)}")
        return redirect(url_for('login_page', error="Authentication failed"))

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.get_json()
        if not data or "email" not in data or "password" not in data:
            return jsonify({"error": "Missing email or password"}), 400
        
        # In a real app, you would verify the credentials against your database
        # For this example, we'll simulate a successful login
        if data["email"] == "user@example.com" and data["password"] == "password":
            user_info = {
                'sub': 'local_user_' + secrets.token_hex(8),
                'email': data["email"],
                'name': 'Local User',
                'picture': 'https://www.gravatar.com/avatar/default?s=200',
                'language': 'en'
            }
            
            # Update or create user in Firestore
            create_or_update_user(user_info)
            
            # Set session data
            session['user_id'] = user_info['sub']
            session['user_email'] = user_info['email']
            session['user_name'] = user_info.get('name', '')
            session['user_picture'] = user_info.get('picture', '')
            
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Invalid credentials"}), 401
    
    except Exception as e:
        logger.error(f"Error during login: {str(e)}")
        return jsonify({"error": "Login failed"}), 500

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    try:
        data = request.get_json()
        if not data or "email" not in data or "password" not in data or "name" not in data:
            return jsonify({"error": "Missing required fields"}), 400
        
        # In a real app, you would create a new user in your database
        # For this example, we'll simulate a successful signup
        user_info = {
            'sub': 'new_user_' + secrets.token_hex(8),
            'email': data["email"],
            'name': data["name"],
            'picture': 'https://www.gravatar.com/avatar/default?s=200',
            'language': 'en'
        }
        
        # Update or create user in Firestore
        create_or_update_user(user_info)
        
        # Set session data
        session['user_id'] = user_info['sub']
        session['user_email'] = user_info['email']
        session['user_name'] = user_info.get('name', '')
        session['user_picture'] = user_info.get('picture', '')
        
        return jsonify({"success": True})
    
    except Exception as e:
        logger.error(f"Error during signup: {str(e)}")
        return jsonify({"error": "Signup failed"}), 500

@app.route("/api/update-language", methods=["POST"])
def update_language():
    if 'user_id' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        data = request.get_json()
        if not data or "language" not in data:
            return jsonify({"error": "Missing language parameter"}), 400
        
        if data["language"] not in LANGUAGES:
            return jsonify({"error": "Unsupported language"}), 400
        
        # Update user's language preference
        user_ref = db.collection("users").document(session['user_id'])
        user_ref.update({
            'language': data["language"]
        })
        
        # Update session
        session['language'] = data["language"]
        
        return jsonify({"success": True})
    
    except Exception as e:
        logger.error(f"Error updating language: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "vision": vision_client is not None,
            "firestore": db is not None,
            "gemini": model is not None
        }
    })

@app.route("/api/user-info")
def user_info():
    if 'user_id' in session:
        user_info = get_user_info(session['user_id'])
        return jsonify({
            "success": True,
            "user": {
                "sub": session.get("user_id"),
                "email": session.get("user_email"),
                "name": session.get("user_name"),
                "picture": session.get("user_picture"),
                "language": user_info.get("language", "en") if user_info else "en"
            }
        })
    return jsonify({"success": False, "error": "Not authenticated"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/api/process-receipt", methods=["POST"])
def process_receipt():
    if vision_client is None or model is None or db is None:
        return jsonify({"error": "Backend services not initialized"}), 500
    
    try:
        data = request.get_json()
        if not data or "imageData" not in data:
            return jsonify({"error": "Missing required field: imageData"}), 400
        
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401
        
        image_data = data["imageData"]
        if image_data.startswith('data:'):
            image_data = image_data.split(',')[1]
        
        image = vision.Image(content=base64.b64decode(image_data))
        response = vision_client.text_detection(image=image)
        texts = response.text_annotations

        if not texts:
            return jsonify({"error": "No text found in image"}), 400
        
        extracted_text = texts[0].description

        try:
            prompt = f"""Extract receipt data from this text:
            {extracted_text}
            Return JSON with:
            - merchant (string)
            - date (YYYY-MM-DD)
            - total (number)
            - tax (number)
            - subtotal (number)
            - items (array of objects with name and price)
            - category (string)
            """
            result = model.generate_content(prompt)
            receipt_data = json.loads(result.text)
        except Exception as e:
            logger.warning(f"Gemini parsing failed: {e}")
            receipt_data = parse_receipt_with_fallback(extracted_text)

        receipt_id = f"receipt_{datetime.now().timestamp()}"
        receipt_ref = db.collection("users").document(session['user_id']).collection("receipts").document(receipt_id)
        receipt_ref.set({
            "receiptId": receipt_id,
            "userId": session['user_id'],
            "timestamp": datetime.now().isoformat(),
            "parsedData": receipt_data,
            "rawText": extracted_text
        })

        return jsonify({
            "success": True,
            "data": receipt_data,
            "receiptId": receipt_id
        })
    
    except Exception as e:
        logger.error(f"Error processing receipt: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/get-receipts")
def get_receipts():
    try:
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401
        
        if not db:
            return jsonify({"error": "Database not initialized"}), 500
        
        docs = db.collection("users").document(session['user_id']).collection("receipts").order_by("timestamp", direction=firestore.Query.DESCENDING).stream()
        receipts = [doc.to_dict() for doc in docs]
        
        return jsonify({"success": True, "receipts": receipts})
    
    except Exception as e:
        logger.error(f"Error getting receipts: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats")
def get_stats():
    try:
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401
        
        if not db:
            return jsonify({"error": "Database not initialized"}), 500
        
        docs = db.collection("users").document(session['user_id']).collection("receipts").stream()
        receipts = [doc.to_dict() for doc in docs]
        
        total_receipts = len(receipts)
        total_spent = sum(receipt.get('parsedData', {}).get('total', 0) for receipt in receipts)
        
        # Calculate category counts
        category_counts = {}
        for receipt in receipts:
            category = receipt.get('parsedData', {}).get('category', 'Uncategorized')
            category_counts[category] = category_counts.get(category, 0) + 1
        
        top_category = max(category_counts.items(), key=lambda x: x[1], default=('None', 0))[0]
        avg_spend = total_spent / total_receipts if total_receipts > 0 else 0
        
        return jsonify({
            "success": True,
            "stats": {
                "total_receipts": total_receipts,
                "total_spent": total_spent,
                "top_category": top_category,
                "avg_spend": avg_spend
            }
        })
    
    except Exception as e:
        logger.error(f"Error getting stats: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/process-query", methods=["POST"])
def process_query():
    try:
        data = request.get_json()
        if not data or "query" not in data:
            return jsonify({"error": "Missing required field: query"}), 400
        
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401
        
        if not db or not model:
            return jsonify({"error": "Backend services not initialized"}), 500
        
        # Get the last 10 receipts for context
        docs = db.collection("users").document(session['user_id']).collection("receipts").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(10).stream()
        receipts = [doc.to_dict() for doc in docs]
        context = [receipt.get('parsedData', {}) for receipt in receipts]

        language = data.get("language", "en")
        language_prompt = ""
        
        if language == "te":
            language_prompt = "Respond in Telugu language with proper script."
        elif language == "kn":
            language_prompt = "Respond in Kannada language with proper script."
        else:
            language_prompt = "Respond in English."

        prompt = f"""User query: "{data["query"]}"
        Language: {language_prompt}
        Based on these receipts:
        {json.dumps(context, indent=2)}
        Provide a helpful, concise response in HTML format with basic styling."""
        
        result = model.generate_content(prompt)
        return jsonify({"success": True, "response": result.text})
    
    except Exception as e:
        logger.error(f"Error processing query: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/create-wallet-pass", methods=["POST"])
def create_wallet_pass():
    try:
        data = request.get_json()
        if not data or "receiptId" not in data:
            return jsonify({"error": "Missing receiptId"}), 400
        
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401
        
        # In a real app, you would generate an actual wallet pass
        # For this example, we'll return a simulated response
        return jsonify({
            "success": True,
            "walletPass": {
                "passType": "storeCard",
                "serialNumber": data["receiptId"],
                "description": "Receipt stored in wallet",
                "organizationName": "Raseed",
                "logoText": "Receipt",
                "backgroundColor": "rgb(102,126,234)",
                "foregroundColor": "rgb(255,255,255)",
                "barcode": {
                    "message": data["receiptId"],
                    "format": "PKBarcodeFormatQR"
                }
            }
        })
    
    except Exception as e:
        logger.error(f"Error creating wallet pass: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Remove in production!
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)