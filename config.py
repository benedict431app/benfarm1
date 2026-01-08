import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Security
    SECRET_KEY = os.getenv('SECRET_KEY', os.getenv('SESSION_SECRET', 'dev-secret-key-change-in-production'))
    
    # Database - Use PostgreSQL on Render, SQLite locally
    database_url = os.getenv('DATABASE_URL', 'sqlite:///database.db')
    
    # Fix PostgreSQL URL format for Render/Railway
    if database_url and database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_DATABASE_URI = database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # File uploads
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
    
    # Ensure upload folder exists
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    # API Keys
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    PERENUAL_API_KEY = os.getenv('PERENUAL_API_KEY')
    OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')
    PLANTID_API_KEY = os.getenv('PLANTID_API_KEY')
    WEGLOT_API_KEY = os.getenv('WEGLOT_API_KEY')
    COHERE_API_KEY = os.getenv('COHERE_API_KEY')
    
    # Flask configuration
    PREFERRED_URL_SCHEME = 'https'
