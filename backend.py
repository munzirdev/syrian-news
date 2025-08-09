import os
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
import openai, requests, base64, sqlite3
from requests.auth import HTTPBasicAuth
from functools import wraps
from datetime import datetime, timedelta
import threading
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

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

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
<!DOCTYPE html>
<html lang="ar">
<head>
<meta charset="UTF-8" />
<title>تسجيل الدخول</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container mt-5" style="max-width: 400px;">
  <h3 class="mb-4 text-center">تسجيل الدخول</h3>
  {% if error %}
  <div class="alert alert-danger">{{ error }}</div>
  {% endif %}
  <form method="post">
    <div class="mb-3">
      <label for="username" class="form-label">اسم المستخدم</label>
      <input type="text" class="form-control" id="username" name="username" required />
    </div>
    <div class="mb-3">
      <label for="password" class="form-label">كلمة المرور</label>
      <input type="password" class="form-control" id="password" name="password" required />
    </div>
    <button type="submit" class="btn btn-primary w-100">دخول</button>
  </form>
</div>
</body>
</html>
"""

dashboard_html = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8" />
<title>لوحة التحكم</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body {background: #f9f9f9;}
  .container {margin-top: 20px;}
  table th, table td {vertical-align: middle;}
</style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-primary">
  <div class="container-fluid">
    <a class="navbar-brand" href="#">لوحة التحكم - موقع الأخبار السورية</a>
    <div>
      <a href="{{ url_for('logout') }}" class="btn btn-danger btn-sm">تسجيل خروج</a>
    </div>
  </div>
</nav>
<div class="container">
  <h4 class="mt-4">إضافة كلمة مفتاحية جديدة</h4>
  <form id="addKeywordForm" class="row g-3 mb-4">
    <div class="col-md-6">
      <input type="text" class="form-control" id="keyword" placeholder="الكلمة المفتاحية" required>
    </div>
    <div class="col-md-4">
      <select id="category" class="form-select" required>
        <option value="">اختر التصنيف</option>
        <option value="syrian-in-turkey">سوري في تركيا</option>
        <option value="syrian-affairs">الشأن السوري</option>
        <option value="arab-news">الأخبار العربية</option>
      </select>
    </div>
    <div class="col-md-2">
      <input type="number" id="interval_hours" class="form-control" min="1" max="168" value="12" title="الفاصل الزمني للنشر بالساعات" required>
    </div>
    <div class="col-12">
      <button type="submit" class="btn btn-success">إضافة</button>
    </div>
  </form>

  <h4>الكلمات المفتاحية الحالية</h4>
  <table class="table table-striped table-bordered">
    <thead>
      <tr>
        <th>الكلمة المفتاحية</th>
        <th>التصنيف</th>
        <th>الفاصل الزمني (ساعة)</th>
        <th>آخر نشر</th>
        <th>إجراء</th>
      </tr>
    </thead>
    <tbody>
      {% for k in keywords %}
      <tr>
        <td>{{ k[1] }}</td>
        <td>{{ k[2] }}</td>
        <td>{{ k[3] }}</td>
        <td>{{ k[4] or "لم ينشر بعد" }}</td>
        <td><button class="btn btn-primary btn-sm publish-now" data-id="{{ k[0] }}">نشر الآن</button></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h4>آخر المنشورات</h4>
  <table class="table table-bordered table-hover">
    <thead>
      <tr>
        <th>الكلمة المفتاحية</th>
        <th>عنوان المقال</th>
        <th>رابط المقال</th>
        <th>تاريخ النشر</th>
      </tr>
    </thead>
    <tbody>
      {% for p in posts %}
      <tr>
        <td>{{ p[1] }}</td>
        <td>{{ p[2] }}</td>
        <td><a href="{{ p[3] }}" target="_blank">{{ p[3] }}</a></td>
        <td>{{ p[4] }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<script>
document.getElementById('addKeywordForm').addEventListener('submit', async function(e) {
  e.preventDefault();
  const keyword = document.getElementById('keyword').value.trim();
  const category = document.getElementById('category').value;
  const interval_hours = parseInt(document.getElementById('interval_hours').value);

  if(!keyword || !category) {
    alert('يرجى ملء جميع الحقول.');
    return;
  }

  const response = await fetch('/add_keyword', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({keyword, category, interval_hours})
  });
  const data = await response.json();
  if(data.success) {
    alert('تمت الإضافة بنجاح!');
    location.reload();
  } else {
    alert('خطأ: ' + data.error);
  }
});

document.querySelectorAll('.publish-now').forEach(button => {
  button.addEventListener('click', async () => {
    const id = button.getAttribute('data-id');
    button.disabled = true;
    button.textContent = 'جاري النشر...';
    const response = await fetch('/publish_now/' + id, { method: 'POST' });
    const data = await response.json();
    if(data.success) {
      alert('تم النشر بنجاح! رابط المقال: ' + data.url);
      location.reload();
    } else {
      alert('خطأ: ' + data.error);
      button.disabled = false;
      button.textContent = 'نشر الآن';
    }
  });
});
</script>
</body>
</html>
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

@app.before_first_request
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
