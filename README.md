Raseed - Smart Receipt Manager
🌐 Live Demo
Experience Raseed now: https://raseed-842527385575.us-central1.run.app/

📝 Overview
Raseed is an AI-powered receipt management system that helps users digitize, organize, and analyze their receipts. The application uses Google Cloud Vision API for text extraction from receipt images and Gemini AI for intelligent processing and analysis. Users can track their spending patterns, get financial insights, and manage receipts through a user-friendly web interface.

✨ Key Features
Smart OCR Recognition: Extracts text, amounts, dates, and merchant information from receipt images

AI-Powered Analysis: Gemini AI processes receipts and provides spending insights

Multi-language Support: English, Telugu (తెలుగు), and Kannada (ಕನ್ನಡ) interfaces

Cloud Storage: Securely stores receipts in Google Firestore

Spending Analytics: Visualizes spending patterns and categories

Voice Assistant: Supports voice queries about spending

Mobile Wallet Integration: Generates digital wallet passes for receipts

🛠️ Technology Stack
Backend: Python with Flask

Frontend: HTML, CSS, JavaScript

Database: Google Firestore (NoSQL)

AI Services:

Google Cloud Vision API (OCR)

Google Gemini AI (Natural Language Processing)

Authentication: Google OAuth 2.0

Hosting: Google Cloud Run

🚀 Getting Started
Prerequisites
Python 3.9+

Google Cloud account with:

Vision API enabled

Firestore database

Gemini API access

Google OAuth 2.0 credentials

Installation
Clone the repository:

bash
git clone https://github.com/yourusername/raseed.git
cd raseed
Create and activate a virtual environment:

bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
Install dependencies:

bash
pip install -r requirements.txt
Create a .env file with your configuration:

ini
FLASK_SECRET_KEY=your-secret-key
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GEMINI_API_KEY=your-gemini-api-key
REDIRECT_URI=http://localhost:8080/api/auth/google/callback
Run the application:

bash
python app.py
The application will be available at http://localhost:8080.

📂 Project Structure
text
raseed/
├── app.py                # Main application file
├── requirements.txt      # Python dependencies
├── .env                  # Environment variables
├── static/               # Static assets (CSS, JS, images)
└── templates/            # HTML templates (embedded in app.py)
🌍 Multi-language Support
Supported languages:

English (en)

Telugu (te)

Kannada (kn)

Language can be changed via the UI dropdown, which updates the user's preference in Firestore.

📜 License
This project is licensed under the MIT License - see the LICENSE file for details.

🤝 Contributing
Contributions are welcome! Please open an issue or submit a pull request.
