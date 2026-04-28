# ProERP — نظام إدارة الأعمال المتكامل

نظام ERP متكامل مبني بـ Python/Flask مع واجهة HTML عربية احترافية.

---

## 🚀 المميزات

- **المبيعات** — فواتير بيع ذكية مع بحث فوري للأصناف
- **المشتريات** — فواتير شراء مع تحديث تلقائي للمخزون
- **تحويلات المخازن** — نظام موافقات كامل (طلب → موافقة/رفض مع السبب)
- **الجرد والمخزون** — رصيد لحظي لكل مخزن مع تنبيهات النقص
- **العملاء** — كشوف حساب + تسجيل دفعات + تتبع الأرصدة
- **الموردون** — إدارة المشتريات والأرصدة
- **الموظفون** — سجل موظفين كامل
- **المرتجعات** — مرتجعات مبيعات مع إعادة رصيد للمخزون
- **المصاريف** — تتبع المصاريف والنفقات
- **التقارير** — تقرير مبيعات، جرد، كشوف عملاء
- **المستخدمون** — صلاحيات (مدير / مشرف / مستخدم)
- **الفروع والمخازن** — دعم فروع متعددة بمخازن مستقلة

---

## 📦 متطلبات التشغيل

- Python 3.10+
- pip

---

## ⚡ تشغيل محلي سريع

```bash
# 1. استنسخ المشروع
git clone <repo-url>
cd erp_system

# 2. أنشئ بيئة افتراضية
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

# 3. ثبّت المتطلبات
pip install -r requirements.txt

# 4. انسخ ملف البيئة
cp .env.example .env

# 5. شغّل التطبيق
python app.py
```

افتح المتصفح على: **http://localhost:5000**

- المستخدم: `admin`
- كلمة المرور: `admin123`

---

## 🌐 الرفع على سيرفر مدفوع

### الطريقة 1: VPS (DigitalOcean / Hostinger / Contabo)

```bash
# على السيرفر - Ubuntu 22.04
sudo apt update && sudo apt install python3-pip python3-venv nginx -y

# انسخ المشروع
git clone <repo> /var/www/erp
cd /var/www/erp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# اضبط المتغيرات
cp .env.example .env
nano .env  # غيّر SECRET_KEY و DATABASE_URL

# شغّل كـ service
sudo nano /etc/systemd/system/erp.service
```

محتوى ملف الـ service:
```ini
[Unit]
Description=ProERP Application
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/erp
Environment="PATH=/var/www/erp/venv/bin"
EnvironmentFile=/var/www/erp/.env
ExecStartPre=/var/www/erp/venv/bin/python wsgi.py
ExecStart=/var/www/erp/venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable erp
sudo systemctl start erp

# اضبط nginx
sudo cp nginx.conf /etc/nginx/sites-available/erp
sudo ln -s /etc/nginx/sites-available/erp /etc/nginx/sites-enabled/
# عدّل server_name في nginx.conf لاسم النطاق
sudo nginx -t && sudo systemctl reload nginx

# SSL مجاني
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
```

---

### الطريقة 2: Docker (أسرع وأسهل)

```bash
# على أي سيرفر فيه Docker
docker-compose up -d
```

سيشتغل على **port 5000** مع PostgreSQL تلقائياً.

---

### الطريقة 3: Railway.app (مجاني جزئياً - الأسهل)

1. ادفع الكود على GitHub
2. اذهب لـ [railway.app](https://railway.app)
3. "New Project" → "Deploy from GitHub"
4. أضف **PostgreSQL** كـ plugin
5. أضف المتغيرات في Environment Variables:
   - `SECRET_KEY` = أي نص عشوائي طويل
   - `DATABASE_URL` = يُملأ تلقائياً من PostgreSQL plugin
6. انشر — خلاص! 🎉

---

### الطريقة 4: Render.com

1. ادفع على GitHub
2. New Web Service → اربطه بالـ repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn wsgi:app -c gunicorn.conf.py`
5. أضف PostgreSQL database
6. أضف Environment Variables

---

## 🗄️ قاعدة البيانات

### SQLite (افتراضي - للتطوير)
```
DATABASE_URL=sqlite:///erp.db
```

### PostgreSQL (للإنتاج - موصى به)
```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

تأكد أن `psycopg2-binary` مثبّت (موجود في requirements.txt).

---

## 👤 المستخدمون الافتراضيون

| المستخدم | كلمة المرور | الصلاحية |
|----------|------------|----------|
| admin | admin123 | مدير النظام |

**⚠️ غيّر كلمة المرور فور التثبيت!**

---

## 🔐 الأمان في الإنتاج

1. غيّر `SECRET_KEY` لقيمة عشوائية طويلة
2. غيّر كلمة مرور `admin`
3. استخدم PostgreSQL بدل SQLite
4. فعّل HTTPS مع Let's Encrypt
5. لا تشغّل أبداً بـ `debug=True`

---

## 🏗️ بنية المشروع

```
erp_system/
├── app.py              # التطبيق الرئيسي (Routes + Models)
├── wsgi.py             # نقطة الدخول لـ Gunicorn
├── gunicorn.conf.py    # إعدادات Gunicorn
├── requirements.txt    # المكتبات المطلوبة
├── Procfile            # للـ Heroku/Railway
├── Dockerfile          # للـ Docker
├── docker-compose.yml  # للتطوير المحلي بـ PostgreSQL
├── nginx.conf          # إعدادات Nginx
├── .env.example        # نموذج ملف البيئة
├── static/
│   ├── css/style.css   # أنماط إضافية
│   └── js/main.js      # JavaScript المساعد
└── templates/
    ├── base.html        # القالب الأساسي
    ├── login.html       # صفحة الدخول
    ├── dashboard.html   # لوحة التحكم
    ├── sales.html / sale_form.html / sale_detail.html
    ├── purchases.html / purchase_form.html / purchase_detail.html
    ├── transfers.html / transfer_form.html / transfer_detail.html
    ├── inventory.html
    ├── customers.html / customer_form.html / customer_statement.html
    ├── suppliers.html / supplier_form.html
    ├── employees.html / employee_form.html
    ├── expenses.html / expense_form.html
    ├── products.html / product_form.html
    ├── sale_returns.html / sale_return_form.html
    ├── reports.html / report_sales.html / report_inventory.html
    ├── users.html / branches.html / warehouses.html
    └── ...
```

---

## 📞 الدعم

النظام مفتوح المصدر وقابل للتطوير والتخصيص.
