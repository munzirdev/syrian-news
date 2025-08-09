import os
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
import openai, requests, base64, sqlite3
from requests.auth import HTTPBasicAuth
import threading
from datetime import datetime, timedelta
import time

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change_this_secret")

# --- إعدادات من متغيرات البيئة ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
WP_USER = os.getenv("WP_USER")
WP_APP_PASS = os.getenv("WP_APP_PASS")
WP_URL = os.getenv("WP_URL")

openai.api_key = OPENAI_API_KEY

DB_FILE = "keywords.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE,
            category TEXT,
            interval_hours INTEGER DEFAULT 12,
            last_post TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword_id INTEGER,
            title TEXT,
            url TEXT,
            published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()

def generate_article(keyword):
    prompt = f"اكتب خبر عن {keyword} بطول 150 كلمة بأسلوب إخباري مميز وجذاب."
    response = openai.Completion.create(
        model="text-davinci-003",
        prompt=prompt,
        max_tokens=250,
        temperature=0.7,
    )
    return response.choices[0].text.strip()

def get_image_url(keyword):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": keyword, "per_page": 1}
    response = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params)
    data = response.json()
    if data.get("photos"):
        return data["photos"][0]["src"]["medium"]
    return None

def upload_image_to_wp(image_url):
    try:
        image_data = requests.get(image_url).content
        filename = image_url.split("/")[-1]
        credentials = f"{WP_USER}:{WP_APP_PASS}"
        token = base64.b64encode(credentials.encode())
        headers = {
            "Content-Disposition": f"attachment; filename={filename}",
            "Authorization": f"Basic {token.decode('utf-8')}",
            "Content-Type": "image/jpeg"
        }
        response = requests.post(f"{WP_URL}/media", headers=headers, data=image_data)
        if response.status_code == 201:
            return response.json()["id"]
        else:
            print("Failed to upload media:", response.text)
            return None
    except Exception as e:
        print("Error uploading media:", e)
        return None

def post_article_to_wp(title, content, category_slug, featured_media_id):
    cat_resp = requests.get(f"{WP_URL}/categories?slug={category_slug}")
    if cat_resp.status_code != 200 or not cat_resp.json():
        return None, "تصنيف غير موجود"
    category_id = cat_resp.json()[0]['id']

    post = {
        "title": title,
        "content": content,
        "categories": [category_id],
        "featured_media": featured_media_id if featured_media_id else None,
        "status": "publish"
    }
    response = requests.post(f"{WP_URL}/posts", auth=HTTPBasicAuth(WP_USER, WP_APP_PASS), json=post)
    if response.status_code == 201:
        return response.json()["link"], None
    else:
        return None, response.text

def auto_publish():
    while True:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                now = datetime.utcnow()
                c.execute("SELECT id, keyword, category, interval_hours, last_post FROM keywords")
                rows = c.fetchall()
                for row in rows:
                    kid, keyword, category, interval_hours, last_post = row
                    should_post = False
                    if last_post is None:
                        should_post = True
                    else:
                        last_post_time = datetime.strptime(last_post, '%Y-%m-%d %H:%M:%S')
                        if now - last_post_time > timedelta(hours=interval_hours):
                            should_post = True
                    if should_post:
                        print(f"Publishing for keyword: {keyword}")
                        article = generate_article(keyword)
                        image_url = get_image_url(keyword)
                        featured_media_id = upload_image_to_wp(image_url) if image_url else None
                        title = f"خبر عن {keyword}"
                        post_url, error = post_article_to_wp(title, article, category, featured_media_id)
                        if post_url:
                            c.execute("INSERT INTO posts (keyword_id, title, url) VALUES (?, ?, ?)", (kid, title, post_url))
                            c.execute("UPDATE keywords SET last_post = ? WHERE id = ?", (now.strftime('%Y-%m-%d %H:%M:%S'), kid))
                            conn.commit()
                            print(f"Published: {post_url}")
                        else:
                            print(f"Failed to post: {error}")
            time.sleep(3600)
        except Exception as e:
            print("Error in auto_publish:", e)
            time.sleep(60)

# --- Routes and views ---

from functools import wraps

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
            user = c.fetchone()
            if user:
                session['logged_in'] = True
                session['username'] = username
                return redirect(url_for('dashboard'))
            else:
                return render_template('login.html', error="بيانات الدخول غير صحيحة")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT id, keyword, category, interval_hours, last_post FROM keywords")
        keywords = c.fetchall()
        c.execute("SELECT p.id, k.keyword, p.title, p.url, p.published_at FROM posts p LEFT JOIN keywords k ON p.keyword_id = k.id ORDER BY p.published_at DESC LIMIT 50")
        posts = c.fetchall()
    return render_template('dashboard.html', keywords=keywords, posts=posts)

@app.route('/add_keyword', methods=['POST'])
@login_required
def add_keyword():
    data = request.json
    keyword = data.get('keyword')
    category = data.get('category')
    interval_hours = data.get('interval_hours', 12)
    if not keyword or not category:
        return jsonify(success=False, error="الكلمة المفتاحية والتصنيف مطلوبان")
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO keywords (keyword, category, interval_hours) VALUES (?, ?, ?)", (keyword, category, interval_hours))
            conn.commit()
            return jsonify(success=True)
        except sqlite3.IntegrityError:
            return jsonify(success=False, error="الكلمة المفتاحية موجودة مسبقاً")

@app.route('/publish_now/<int:keyword_id>', methods=['POST'])
@login_required
def publish_now(keyword_id):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT keyword, category FROM keywords WHERE id=?", (keyword_id,))
        row = c.fetchone()
        if not row:
            return jsonify(success=False, error="الكلمة المفتاحية غير موجودة")
        keyword, category = row
        try:
            article = generate_article(keyword)
            image_url = get_image_url(keyword)
            featured_media_id = upload_image_to_wp(image_url) if image_url else None
            title = f"خبر عن {keyword}"
            post_url, error = post_article_to_wp(title, article, category, featured_media_id)
            if post_url:
                now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                c.execute("INSERT INTO posts (keyword_id, title, url, published_at) VALUES (?, ?, ?, ?)", (keyword_id, title, post_url, now))
                c.execute("UPDATE keywords SET last_post=? WHERE id=?", (now, keyword_id))
                conn.commit()
                return jsonify(success=True, url=post_url)
            else:
                return jsonify(success=False, error=error)
        except Exception as e:
            return jsonify(success=False, error=str(e))

from flask import render_template_string

login_html = """ 
<!-- (نفس قالب تسجيل الدخول الذي لديك، لم أغيره هنا) -->
"""

dashboard_html = """
<!-- (نفس قالب لوحة التحكم الذي لديك، لم أغيره هنا) -->
"""

@app.route('/login.html')
def login_template():
    return render_template_string(login_html)

@app.route('/dashboard.html')
def dashboard_template():
    return render_template_string(dashboard_html)

@app.context_processor
def override_url_for():
    return dict(url_for=url_for)

# --- استدعاء الإعدادات وتشغيل الخيط عند بداية تحميل السكربت ---
def setup():
    init_db()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username='admin'")
        if not c.fetchone():
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("admin", "admin123"))
            conn.commit()
    thread = threading.Thread(target=auto_publish, daemon=True)
    thread.start()

setup()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
