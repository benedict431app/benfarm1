import os
import json
import uuid
import base64
import requests
from datetime import datetime, timedelta
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, session, send_from_directory)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from config import Config
from models import (db, User, InventoryItem, Customer, Sale, SaleItem,
                    Communication, DiseaseReport, Notification, WeatherData,
                    AdminUser, PaymentRecord)
import google.generativeai as genai
from PIL import Image
import io

app = Flask(__name__)
app.config.from_object(Config)

print(f"Database URL: {app.config['SQLALCHEMY_DATABASE_URI']}")

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

cohere_api_key = app.config.get('COHERE_API_KEY', '')
cohere_model = app.config.get('COHERE_CHAT_MODEL', 'command-r7b-12-2024')
cohere_vision_model = app.config.get('COHERE_VISION_MODEL', 'command-a-vision-07-2025')
free_scan_limit = app.config.get('FREE_SCAN_LIMIT', 3)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


with app.app_context():
    db.create_all()
    from models import AdminUser
    admin_email = app.config.get('ADMIN_EMAIL', 'admin@benfarming.com')
    admin_exists = AdminUser.query.filter_by(email=admin_email).first()
    if not admin_exists:
        admin = AdminUser(
            email=admin_email,
            full_name=app.config.get('ADMIN_FULL_NAME', 'System Administrator'),
            is_super_admin=True
        )
        admin_password = app.config.get('ADMIN_PASSWORD', 'admin123')
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        print("Default admin user created")

try:
    from admin_routes import admin_bp
    from community_routes import community_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(community_bp)
    print("Blueprints registered successfully")
except ImportError as e:
    print(f"Note: Some blueprints not found: {e}")


# ---------------------------------------------------------------------------
# Tuma Payment Helpers
# ---------------------------------------------------------------------------

def get_tuma_token():
    payload = {
        "email": app.config['TUMA_BUSINESS_EMAIL'],
        "api_key": app.config['TUMA_API_KEY']
    }
    try:
        resp = requests.post(app.config['TUMA_AUTH_URL'], json=payload, timeout=30)
        print(f"Tuma auth response: {resp.status_code} {resp.text}")
        if resp.status_code == 200:
            return resp.json().get('data', {}).get('token')
    except Exception as e:
        print(f"Tuma token error: {e}")
    return None


def initiate_tuma_stk(phone, amount=10.0, callback_url=None):
    token = get_tuma_token()
    if not token:
        return False, None, "Failed to authenticate with payment gateway"

    phone = phone.strip().replace('+', '').replace(' ', '').replace('-', '')
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif phone.startswith('7') or phone.startswith('1'):
        phone = '254' + phone

    if not callback_url:
        callback_url = url_for('tuma_callback', _external=True)

    payload = {
        "amount": amount,
        "phone": phone,
        "callback_url": callback_url,
        "description": "Plant disease scan - KES 10"
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(app.config['TUMA_STK_URL'], json=payload, headers=headers, timeout=30)
        print(f"Tuma STK response: {resp.status_code} {resp.text}")
        data = resp.json()
        if resp.status_code == 200 and data.get('success'):
            return True, data.get('data', {}).get('checkout_request_id'), None
        return False, None, data.get('message', 'Payment initiation failed')
    except Exception as e:
        print(f"Tuma STK error: {e}")
        return False, None, f"Payment gateway error: {str(e)}"


# ---------------------------------------------------------------------------
# Public / auth routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.user_type == 'farmer':
            return redirect(url_for('farmer_dashboard'))
        elif current_user.user_type == 'agrovet':
            return redirect(url_for('agrovet_dashboard'))
        elif current_user.user_type == 'extension_officer':
            return redirect(url_for('officer_dashboard'))
        elif current_user.user_type == 'learning_institution':
            return redirect(url_for('institution_dashboard'))
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        full_name = request.form.get('full_name')
        user_type = request.form.get('user_type')
        phone_number = request.form.get('phone_number')
        location = request.form.get('location')

        if User.query.filter_by(email=email).first():
            flash('Email already registered', 'error')
            return redirect(url_for('register'))

        user = User(
            email=email,
            full_name=full_name,
            user_type=user_type,
            phone_number=phone_number,
            location=location
        )
        user.set_password(password)

        profile_file = request.files.get('profile_picture')
        if profile_file and profile_file.filename:
            if allowed_file(profile_file.filename):
                ext = profile_file.filename.rsplit('.', 1)[1].lower()
                filename = secure_filename(f"profile_{email}_{uuid.uuid4().hex[:8]}.{ext}")
                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                profile_file.save(filepath)
                user.profile_picture = filename
            else:
                flash('Invalid profile picture format. Allowed: png, jpg, jpeg, gif', 'error')
                return redirect(url_for('register'))

        db.session.add(user)
        db.session.commit()

        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))

    return render_template('auth/register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid email or password', 'error')
    return render_template('auth/login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Profile & settings
# ---------------------------------------------------------------------------

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_user.full_name = request.form.get('full_name', current_user.full_name).strip() or current_user.full_name
        current_user.phone_number = request.form.get('phone_number', '').strip() or None
        current_user.location = request.form.get('location', '').strip() or None
        current_user.bio = request.form.get('bio', '').strip() or None

        profile_file = request.files.get('profile_picture')
        if profile_file and profile_file.filename:
            if allowed_file(profile_file.filename):
                ext = profile_file.filename.rsplit('.', 1)[1].lower()
                filename = secure_filename(f"profile_{current_user.id}_{uuid.uuid4().hex[:8]}.{ext}")
                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                profile_file.save(filepath)

                if current_user.profile_picture:
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], current_user.profile_picture)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except OSError:
                            pass

                current_user.profile_picture = filename
            else:
                flash('Invalid image format. Allowed: png, jpg, jpeg, gif.', 'error')
                return redirect(url_for('profile'))

        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        if new_password:
            if new_password != confirm_password:
                flash('Passwords do not match.', 'error')
                return redirect(url_for('profile'))
            if len(new_password) < 6:
                flash('Password must be at least 6 characters.', 'error')
                return redirect(url_for('profile'))
            current_user.set_password(new_password)

        db.session.commit()
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))

    return render_template('profile/settings.html')


@app.route('/profile/picture/remove', methods=['POST'])
@login_required
def remove_profile_picture():
    if current_user.profile_picture:
        old_path = os.path.join(app.config['UPLOAD_FOLDER'], current_user.profile_picture)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
        current_user.profile_picture = None
        db.session.commit()
    flash('Profile picture removed.', 'info')
    return redirect(url_for('profile'))


# ---------------------------------------------------------------------------
# Farmer routes
# ---------------------------------------------------------------------------

@app.route('/farmer/dashboard')
@login_required
def farmer_dashboard():
    if current_user.user_type != 'farmer':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).order_by(Notification.created_at.desc()).limit(5).all()
    disease_reports = DiseaseReport.query.filter_by(farmer_id=current_user.id).order_by(DiseaseReport.created_at.desc()).limit(10).all()
    return render_template('farmer/dashboard.html', notifications=notifications, disease_reports=disease_reports)


@app.route('/farmer/verify-plant', methods=['POST'])
@login_required
def verify_plant():
    """Step 1: Check if image is a plant and identify the plant name."""
    if current_user.user_type != 'farmer':
        return jsonify({'error': 'Access denied'}), 403

    if 'plant_image' not in request.files:
        return jsonify({'error': 'No image provided'}), 400

    file = request.files['plant_image']
    if not (file and allowed_file(file.filename)):
        return jsonify({'error': 'Invalid file type'}), 400

    image_bytes = file.read()
    image_b64 = base64.b64encode(image_bytes).decode('utf-8')
    file.seek(0)

    try:
        headers = {
            'Authorization': f'Bearer {cohere_api_key}',
            'Content-Type': 'application/json',
        }

        prompt = """You are helping a farmer analyze a crop.

Look at this image and return ONLY a valid JSON object:

{
  "is_plant": true or false,
  "plant_name": "common English name of the plant, or 'Unknown' if you truly cannot tell",
  "confidence": "high, medium, or low"
}

Guidelines:
- Assume the user uploaded a plant unless you see clear evidence it is NOT a plant (e.g., a person, a car, a document, a screenshot).
- A blurry or low-quality photo of leaves, a stem, or a field is still a plant.
- If you can see green organic material (leaves, stems, fruit), set is_plant to true.
- If the image is completely unrelated to agriculture (a selfie, a text message, a building), set is_plant to false.
- For plant_name: if you recognize the plant, name it. If you are unsure, say "Unknown" but still set is_plant to true.
- Never reject an image just because it is low quality or you are unsure of the exact species.
"""

        payload = {
            'model': cohere_vision_model,
            'message': prompt,
            'temperature': 0.2,
            'max_tokens': 300,
            'image_url': {
                "url": f"data:image/jpeg;base64,{image_b64}",
                "detail": "high"
            }
        }

        response = requests.post('https://api.cohere.ai/v1/chat', json=payload, headers=headers, timeout=30)
        result = response.json()

        if response.status_code == 200 and 'text' in result:
            cleaned = result['text'].strip()
            if cleaned.startswith('```'):
                cleaned = cleaned.split('```', 1)[1]
                if cleaned.lower().startswith('json'):
                    cleaned = cleaned[4:]
                cleaned = cleaned.rsplit('```', 1)[0]
            cleaned = cleaned.strip()
            try:
                verification = json.loads(cleaned)
            except Exception:
                return jsonify({
                    'success': True,
                    'verification': {
                        'is_plant': True,
                        'plant_name': 'Unknown',
                        'confidence': 'low'
                    }
                })
            return jsonify({'success': True, 'verification': verification})
        else:
            print(f"Cohere vision error {response.status_code}: {result}")
            return jsonify({
                'success': True,
                'verification': {
                    'is_plant': True,
                    'plant_name': 'Unknown',
                    'confidence': 'low'
                }
            })

    except Exception as e:
        print(f"Vision verification error: {e}")
        return jsonify({
            'success': True,
            'verification': {
                'is_plant': True,
                'plant_name': 'Unknown',
                'confidence': 'low'
            }
        })


@app.route('/farmer/detect-disease', methods=['GET', 'POST'])
@login_required
def detect_disease():
    if current_user.user_type != 'farmer':
        flash('Access denied', 'error')
        return redirect(url_for('index'))

    if request.method == 'POST':
        # Payment gate
        if current_user.disease_scan_count >= free_scan_limit:
            paid_record = PaymentRecord.query.filter_by(
                user_id=current_user.id,
                status='completed',
                used=False
            ).order_by(PaymentRecord.created_at.desc()).first()

            if not paid_record:
                return jsonify({
                    'requires_payment': True,
                    'message': f'You have used your {free_scan_limit} free scans. Pay KES 10 to continue.',
                    'amount': 10
                }), 402

            paid_record.used = True
            db.session.commit()

        if 'plant_image' not in request.files:
            return jsonify({'error': 'No image provided'}), 400

        file = request.files['plant_image']
        description = request.form.get('description', '')

        if file and allowed_file(file.filename):
            filename = secure_filename(f"plant_{current_user.id}_{datetime.utcnow().timestamp()}_{file.filename}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            with open(filepath, 'rb') as f:
                image_b64 = base64.b64encode(f.read()).decode('utf-8')

            try:
                headers = {
                    'Authorization': f'Bearer {cohere_api_key}',
                    'Content-Type': 'application/json',
                }

                prompt = f"""You are a senior agricultural plant pathologist.

Analyze this plant health situation using BOTH the provided image AND the user's description.

User description: "{description}"

Return this exact JSON schema (no markdown, no code fences, no extra text):
{{
  "table": {{
    "plant_name": "common English name",
    "scientific_name": "Latin binomial",
    "local_name": "common local/Kenyan name, or 'Not applicable'",
    "disease_type": "name of the disease or disorder",
    "organic_treatment": "specific organic or cultural treatment steps, 2-4 sentences",
    "other_treatments": "conventional chemical or biological treatments, 2-4 sentences",
    "prevention": "preventive measures, 2-4 sentences"
  }},
  "paragraphs": {{
    "causes": "Elaborate paragraph (150-250 words) explaining the causal organism or factor, how it spreads, favourable conditions, and lifecycle.",
    "preventive_care": "Elaborate paragraph (150-250 words) on preventive care: crop rotation, spacing, sanitation, resistant varieties, irrigation, mulching, monitoring.",
    "treatment": "Elaborate paragraph (150-250 words) on treatment: step-by-step organic first, then conventional options, application timing, safety, expected recovery.",
    "other_information": "Elaborate paragraph (150-250 words) with other relevant information: economic impact, look-alike diseases, when to consult an extension officer."
  }}
}}

Base your diagnosis on the VISUAL symptoms in the image combined with the user's textual description. If the image and description conflict, note the discrepancy in the 'other_information' paragraph.

Return JSON only."""

                chat_payload = {
                    'model': cohere_vision_model,
                    'message': prompt,
                    'temperature': 0.4,
                    'max_tokens': 1800,
                    'image_url': {
                        "url": f"data:image/jpeg;base64,{image_b64}",
                        "detail": "high"
                    }
                }

                response = requests.post('https://api.cohere.ai/v1/chat', json=chat_payload, headers=headers)
                result = response.json()

                analysis_text = None
                structured = None
                if response.status_code == 200 and 'text' in result:
                    analysis_text = result['text'].strip()
                    cleaned = analysis_text
                    if cleaned.startswith('```'):
                        cleaned = cleaned.split('```', 1)[1]
                        if cleaned.lower().startswith('json'):
                            cleaned = cleaned[4:]
                        cleaned = cleaned.rsplit('```', 1)[0]
                    cleaned = cleaned.strip()
                    try:
                        structured = json.loads(cleaned)
                    except Exception as parse_err:
                        print(f"Cohere JSON parse error: {parse_err}. Raw: {analysis_text[:500]}")
                        structured = None

                if structured and isinstance(structured, dict):
                    t = structured.get('table', {})
                    p = structured.get('paragraphs', {})
                    analysis = (
                        f"Plant: {t.get('plant_name','Unknown')} ({t.get('scientific_name','Unknown')})\n"
                        f"Local name: {t.get('local_name','Unknown')}\n"
                        f"Disease: {t.get('disease_type','Unknown')}\n"
                        f"Organic treatment: {t.get('organic_treatment','')}\n"
                        f"Other treatments: {t.get('other_treatments','')}\n"
                        f"Prevention: {t.get('prevention','')}\n\n"
                        f"Causes: {p.get('causes','')}\n\n"
                        f"Preventive care: {p.get('preventive_care','')}\n\n"
                        f"Treatment: {p.get('treatment','')}\n\n"
                        f"Other information: {p.get('other_information','')}"
                    )
                else:
                    print(f"Cohere error {response.status_code}: {result}")
                    analysis = analysis_text or "Unable to analyze plant health at the moment. Please consult with an agricultural officer."

                report = DiseaseReport(
                    farmer_id=current_user.id,
                    plant_image=filename,
                    plant_description=description,
                    treatment_recommendation=analysis,
                    latitude=current_user.latitude,
                    longitude=current_user.longitude,
                    location=current_user.location
                )
                db.session.add(report)

                current_user.disease_scan_count = (current_user.disease_scan_count or 0) + 1
                db.session.commit()

                return jsonify({
                    'success': True,
                    'analysis': analysis,
                    'structured': structured,
                    'report_id': report.id
                })

            except Exception as e:
                return jsonify({'error': str(e)}), 500
        else:
            return jsonify({'error': 'Invalid file type'}), 400

    return render_template('farmer/detect_disease.html')


@app.route('/farmer/weather')
@login_required
def farmer_weather():
    if current_user.user_type != 'farmer':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    location = request.args.get('location', current_user.location or 'Nairobi')
    try:
        weather_url = f"http://api.openweathermap.org/data/2.5/weather?q={location}&appid={app.config['OPENWEATHER_API_KEY']}&units=metric"
        response = requests.get(weather_url)
        weather_data = response.json()
        forecast_url = f"http://api.openweathermap.org/data/2.5/forecast?q={location}&appid={app.config['OPENWEATHER_API_KEY']}&units=metric"
        forecast_response = requests.get(forecast_url)
        forecast_data = forecast_response.json()
        return render_template('farmer/weather.html', weather=weather_data, forecast=forecast_data)
    except Exception as e:
        flash(f'Error fetching weather data: {str(e)}', 'error')
        return render_template('farmer/weather.html', weather=None, forecast=None)


@app.route('/farmer/agrovets')
@login_required
def farmer_agrovets():
    if current_user.user_type != 'farmer':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    agrovets = User.query.filter_by(user_type='agrovet', is_active=True).all()
    return render_template('farmer/agrovets.html', agrovets=agrovets)


# ---------------------------------------------------------------------------
# Agrovet routes
# ---------------------------------------------------------------------------

@app.route('/agrovet/dashboard')
@login_required
def agrovet_dashboard():
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    total_products = InventoryItem.query.filter_by(agrovet_id=current_user.id).count()
    low_stock_items = InventoryItem.query.filter_by(agrovet_id=current_user.id).filter(InventoryItem.quantity <= InventoryItem.reorder_level).count()
    total_customers = Customer.query.filter_by(agrovet_id=current_user.id).count()
    today = datetime.utcnow().date()
    today_sales = Sale.query.filter_by(agrovet_id=current_user.id).filter(db.func.date(Sale.sale_date) == today).all()
    today_revenue = sum(sale.total_amount for sale in today_sales)
    recent_sales = Sale.query.filter_by(agrovet_id=current_user.id).order_by(Sale.sale_date.desc()).limit(10).all()
    notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).order_by(Notification.created_at.desc()).limit(5).all()
    return render_template('agrovet/dashboard.html',
                           total_products=total_products,
                           low_stock_items=low_stock_items,
                           total_customers=total_customers,
                           today_revenue=today_revenue,
                           recent_sales=recent_sales,
                           notifications=notifications)


@app.route('/agrovet/inventory')
@login_required
def agrovet_inventory():
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    items = InventoryItem.query.filter_by(agrovet_id=current_user.id).all()
    return render_template('agrovet/inventory.html', items=items)


@app.route('/agrovet/inventory/add', methods=['GET', 'POST'])
@login_required
def add_inventory():
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        item = InventoryItem(
            agrovet_id=current_user.id,
            product_name=request.form.get('product_name'),
            category=request.form.get('category'),
            description=request.form.get('description'),
            quantity=int(request.form.get('quantity', 0)),
            unit=request.form.get('unit'),
            price=float(request.form.get('price')),
            cost_price=float(request.form.get('cost_price', 0)),
            reorder_level=int(request.form.get('reorder_level', 10)),
            supplier=request.form.get('supplier'),
            sku=request.form.get('sku')
        )
        db.session.add(item)
        db.session.commit()
        flash('Product added successfully!', 'success')
        return redirect(url_for('agrovet_inventory'))
    return render_template('agrovet/add_inventory.html')


@app.route('/agrovet/inventory/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def edit_inventory(item_id):
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    item = InventoryItem.query.get_or_404(item_id)
    if item.agrovet_id != current_user.id:
        flash('Access denied', 'error')
        return redirect(url_for('agrovet_inventory'))
    if request.method == 'POST':
        item.product_name = request.form.get('product_name')
        item.category = request.form.get('category')
        item.description = request.form.get('description')
        item.quantity = int(request.form.get('quantity', 0))
        item.unit = request.form.get('unit')
        item.price = float(request.form.get('price'))
        item.cost_price = float(request.form.get('cost_price', 0))
        item.reorder_level = int(request.form.get('reorder_level', 10))
        item.supplier = request.form.get('supplier')
        item.sku = request.form.get('sku')
        db.session.commit()
        flash('Product updated successfully!', 'success')
        return redirect(url_for('agrovet_inventory'))
    return render_template('agrovet/edit_inventory.html', item=item)


@app.route('/agrovet/inventory/delete/<int:item_id>', methods=['POST'])
@login_required
def delete_inventory(item_id):
    if current_user.user_type != 'agrovet':
        return jsonify({'error': 'Access denied'}), 403
    item = InventoryItem.query.get_or_404(item_id)
    if item.agrovet_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    db.session.delete(item)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/agrovet/pos')
@login_required
def agrovet_pos():
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    items = InventoryItem.query.filter_by(agrovet_id=current_user.id).filter(InventoryItem.quantity > 0).all()
    customers = Customer.query.filter_by(agrovet_id=current_user.id).all()
    return render_template('agrovet/pos.html', items=items, customers=customers)


@app.route('/agrovet/pos/checkout', methods=['POST'])
@login_required
def pos_checkout():
    if current_user.user_type != 'agrovet':
        return jsonify({'error': 'Access denied'}), 403
    data = request.get_json()
    cart_items = data.get('items', [])
    customer_id = data.get('customer_id')
    payment_method = data.get('payment_method', 'cash')
    if not cart_items:
        return jsonify({'error': 'Cart is empty'}), 400

    total_amount = 0
    receipt_number = f"RCP{current_user.id}{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    sale = Sale(
        agrovet_id=current_user.id,
        customer_id=customer_id if customer_id else None,
        total_amount=0,
        payment_method=payment_method,
        receipt_number=receipt_number
    )
    db.session.add(sale)
    db.session.flush()

    for cart_item in cart_items:
        item = InventoryItem.query.get(cart_item['id'])
        if not item or item.agrovet_id != current_user.id:
            continue
        quantity = cart_item['quantity']
        if item.quantity < quantity:
            return jsonify({'error': f'Insufficient stock for {item.product_name}'}), 400
        subtotal = item.price * quantity
        total_amount += subtotal
        sale_item = SaleItem(
            sale_id=sale.id,
            product_name=item.product_name,
            quantity=quantity,
            unit_price=item.price,
            subtotal=subtotal
        )
        db.session.add(sale_item)
        item.quantity -= quantity

    sale.total_amount = total_amount
    if customer_id:
        customer = Customer.query.get(customer_id)
        if customer:
            customer.total_purchases += total_amount
            customer.last_purchase = datetime.utcnow()
    db.session.commit()
    return jsonify({
        'success': True,
        'receipt_number': receipt_number,
        'total_amount': total_amount,
        'sale_id': sale.id
    })


@app.route('/agrovet/crm')
@login_required
def agrovet_crm():
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    customers = Customer.query.filter_by(agrovet_id=current_user.id).order_by(Customer.created_at.desc()).all()
    return render_template('agrovet/crm.html', customers=customers)


@app.route('/agrovet/crm/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        customer = Customer(
            agrovet_id=current_user.id,
            name=request.form.get('name'),
            email=request.form.get('email'),
            phone=request.form.get('phone'),
            address=request.form.get('address'),
            customer_type=request.form.get('customer_type'),
            notes=request.form.get('notes')
        )
        db.session.add(customer)
        db.session.commit()
        flash('Customer added successfully!', 'success')
        return redirect(url_for('agrovet_crm'))
    return render_template('agrovet/add_customer.html')


@app.route('/agrovet/crm/view/<int:customer_id>')
@login_required
def view_customer(customer_id):
    if current_user.user_type != 'agrovet':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    customer = Customer.query.get_or_404(customer_id)
    if customer.agrovet_id != current_user.id:
        flash('Access denied', 'error')
        return redirect(url_for('agrovet_crm'))
    communications = Communication.query.filter_by(customer_id=customer_id).order_by(Communication.date.desc()).all()
    purchases = Sale.query.filter_by(customer_id=customer_id).order_by(Sale.sale_date.desc()).all()
    return render_template('agrovet/view_customer.html', customer=customer, communications=communications, purchases=purchases)


@app.route('/agrovet/crm/communication/<int:customer_id>', methods=['POST'])
@login_required
def add_communication(customer_id):
    if current_user.user_type != 'agrovet':
        return jsonify({'error': 'Access denied'}), 403
    customer = Customer.query.get_or_404(customer_id)
    if customer.agrovet_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    communication = Communication(
        customer_id=customer_id,
        communication_type=request.form.get('communication_type'),
        subject=request.form.get('subject'),
        message=request.form.get('message'),
        follow_up_date=datetime.strptime(request.form.get('follow_up_date'), '%Y-%m-%d') if request.form.get('follow_up_date') else None
    )
    db.session.add(communication)
    db.session.commit()
    flash('Communication log added successfully!', 'success')
    return redirect(url_for('view_customer', customer_id=customer_id))


# ---------------------------------------------------------------------------
# SEO routes
# ---------------------------------------------------------------------------

@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(app.static_folder, 'sitemap.xml', mimetype='application/xml')


@app.route('/ai-crop-disease-detection')
def ai_crop_disease_detection():
    return render_template('seo/ai_crop_disease_detection.html')


@app.route('/agrovets')
def agrovets_index():
    return render_template('seo/agrovets_index.html')


@app.route('/agrovets/nairobi')
def agrovets_nairobi():
    return render_template('seo/agrovets_nairobi.html')


# ---------------------------------------------------------------------------
# Other dashboards
# ---------------------------------------------------------------------------

@app.route('/officer/dashboard')
@login_required
def officer_dashboard():
    if current_user.user_type != 'extension_officer':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    all_disease_reports = DiseaseReport.query.order_by(DiseaseReport.created_at.desc()).limit(50).all()
    farmers = User.query.filter_by(user_type='farmer').all()
    return render_template('officer/dashboard.html', disease_reports=all_disease_reports, farmers=farmers)


@app.route('/institution/dashboard')
@login_required
def institution_dashboard():
    if current_user.user_type != 'learning_institution':
        flash('Access denied', 'error')
        return redirect(url_for('index'))
    return render_template('institution/dashboard.html')


# ---------------------------------------------------------------------------
# Chat / diagnostics
# ---------------------------------------------------------------------------

@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data = request.get_json()
    message = data.get('message', '')
    if not message:
        return jsonify({'success': False, 'error': 'No message provided'})
    try:
        headers = {
            'Authorization': f'Bearer {cohere_api_key}',
            'Content-Type': 'application/json',
        }
        chat_payload = {
            'model': cohere_model,
            'message': message,
            'temperature': 0.7,
            'max_tokens': 500,
            'preamble': 'You are a helpful agricultural assistant specializing in farming, crops, livestock, and agricultural practices. Provide practical, concise advice to farmers.'
        }
        response = requests.post('https://api.cohere.ai/v1/chat', json=chat_payload, headers=headers)
        result = response.json()
        if response.status_code == 200 and 'text' in result:
            ai_response = result['text']
        else:
            error_msg = result.get('message', 'Unknown error from Cohere API')
            print(f"Cohere chat error {response.status_code}: {result}")
            return jsonify({
                'success': False,
                'error': f'Cohere API error: {error_msg}'
            })
        return jsonify({'success': True, 'response': ai_response})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Chat service error: {str(e)}'})


@app.route('/test-api')
def test_api():
    try:
        headers = {
            'Authorization': f'Bearer {cohere_api_key}',
            'Content-Type': 'application/json',
        }
        test_payload = {
            'model': cohere_model,
            'message': 'Please respond with "API test successful" if you are working.',
            'temperature': 0.7,
            'max_tokens': 20
        }
        response = requests.post('https://api.cohere.ai/v1/chat', json=test_payload, headers=headers)
        result = response.json()
        if response.status_code == 200 and 'text' in result:
            test_response = result['text']
            return f"Cohere API is working! Response: {test_response}"
        else:
            error_msg = result.get('message', 'Unknown error')
            return f"Cohere API error: {response.status_code} - {error_msg}"
    except Exception as e:
        return f"Cohere API Error: {str(e)}"


@app.route('/test-tuma')
def test_tuma():
    phone = request.args.get('phone', '').strip()
    if not phone:
        return (
            "Usage: /test-tuma?phone=2547XXXXXXXX<br><br>"
            f"TUMA_BUSINESS_EMAIL set: {bool(app.config.get('TUMA_BUSINESS_EMAIL'))}<br>"
            f"TUMA_API_KEY set: {bool(app.config.get('TUMA_API_KEY'))}<br>"
            f"TUMA_AUTH_URL: {app.config.get('TUMA_AUTH_URL')}<br>"
            f"TUMA_STK_URL: {app.config.get('TUMA_STK_URL')}"
        )
    token = get_tuma_token()
    if not token:
        return "Tuma auth FAILED — check Render logs for the raw response. Verify TUMA_BUSINESS_EMAIL and TUMA_API_KEY."
    success, checkout_id, error = initiate_tuma_stk(phone, amount=10.0)
    if success:
        return f"Tuma STK push SUCCESS. checkout_request_id={checkout_id}. Check the phone {phone} for the M-PESA prompt."
    return f"Tuma STK push FAILED: {error}. Check Render logs for the raw response."


@app.route('/list-models')
def list_models():
    try:
        headers = {
            'Authorization': f'Bearer {cohere_api_key}',
            'Content-Type': 'application/json',
        }
        response = requests.get('https://api.cohere.ai/v1/models', headers=headers)
        result = response.json()
        if response.status_code == 200:
            models = result.get('models', [])
            model_list = "\n".join([f"- {model['name']}" for model in models])
            return f"Available Cohere models:\n{model_list}"
        else:
            return f"Error fetching models: {response.status_code} - {result}"
    except Exception as e:
        return f"Error: {str(e)}"


@app.route('/favicon.ico')
def favicon():
    return '', 404


@app.route('/notifications/mark-read/<int:notification_id>', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    notification.is_read = True
    db.session.commit()
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Payment routes
# ---------------------------------------------------------------------------

@app.route('/payment/initiate-scan', methods=['POST'])
@login_required
def initiate_scan_payment():
    data = request.get_json()
    phone = (data.get('phone') or '').strip()
    if not phone:
        return jsonify({'success': False, 'error': 'Phone number required'}), 400
    success, checkout_id, error = initiate_tuma_stk(phone, amount=10.0)
    if success:
        record = PaymentRecord(
            user_id=current_user.id,
            checkout_request_id=checkout_id,
            amount=10.0,
            status='pending'
        )
        db.session.add(record)
        db.session.commit()
        return jsonify({'success': True, 'checkout_request_id': checkout_id})
    print(f"Tuma initiate-scan failed: {error}")
    return jsonify({'success': False, 'error': error}), 400


@app.route('/payment/tuma-callback', methods=['POST'])
def tuma_callback():
    data = request.get_json() or {}
    print(f"Tuma callback received: {data}")
    checkout_id = data.get('checkout_request_id')
    result_code = data.get('result_code')
    receipt = data.get('mpesa_receipt_number', '') or data.get('transaction_id', '')
    if not checkout_id:
        return jsonify({'status': 'ok'}), 200
    record = PaymentRecord.query.filter_by(checkout_request_id=checkout_id).first()
    if not record:
        return jsonify({'status': 'ok'}), 200
    if result_code == 0:
        record.status = 'completed'
        record.mpesa_receipt = receipt
    else:
        record.status = 'failed'
    db.session.commit()
    return jsonify({'status': 'ok'}), 200


@app.template_filter('datetime')
def format_datetime(value):
    if value is None:
        return ""
    return value.strftime('%Y-%m-%d %H:%M')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
