from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import joinedload
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from functools import wraps
import os
import json
from collections import defaultdict
import re
import hashlib
import secrets
import shutil
import threading
import time as time_module
from werkzeug.utils import secure_filename
from sqlalchemy.engine.url import make_url

app = Flask(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_DIR = os.path.join(_BASE_DIR, 'instance')
os.makedirs(_INSTANCE_DIR, exist_ok=True)

# ── Security ──────────────────────────────────────────────
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'erp-secret-key-change-in-production-2024')

# ── Permanent sessions (لا تنتهي الجلسة عند إغلاق المتصفح) ──
app.config['REMEMBER_COOKIE_DURATION'] = None          # لا تنتهي أبداً
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30 يوم
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True

# ── Database ──────────────────────────────────────────────
def _sqlite_uri_from_json_config():
    cfg = os.path.join(_INSTANCE_DIR, 'database_path.json')
    if not os.path.isfile(cfg):
        return None
    try:
        with open(cfg, encoding='utf-8') as f:
            data = json.load(f)
        raw = (data.get('sqlite_path') or data.get('database_path') or '').strip()
        if not raw:
            return None
        path = os.path.abspath(os.path.expanduser(raw))
        dname = os.path.dirname(path)
        if dname and not os.path.isdir(dname):
            os.makedirs(dname, exist_ok=True)
        return 'sqlite:///' + path.replace('\\', '/')
    except Exception:
        return None


db_url = os.environ.get('DATABASE_URL')
if not db_url:
    db_url = _sqlite_uri_from_json_config()
if not db_url:
    db_url = 'sqlite:///erp.db'
# Heroku/Railway يُرجعون postgres:// — نحوّله لـ postgresql://
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# قواعد ترخيص منفصلة: سريالات جاهزة | سريالات مُستَخدمة
def _sqlite_bind_uri(name):
    return 'sqlite:///' + os.path.join(_INSTANCE_DIR, name).replace('\\', '/')
app.config['SQLALCHEMY_BINDS'] = {
    'license_pool': _sqlite_bind_uri('license_pool.db'),
    'license_used': _sqlite_bind_uri('license_used.db'),
}

# ── Connection Pool ───────────────────────────────────────
# SQLite لا يدعم pool_size/max_overflow — نفرق بين البيئتين
_is_sqlite   = db_url.startswith('sqlite')
_is_postgres = 'postgresql' in db_url

if _is_sqlite:
    from sqlalchemy.pool import StaticPool
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        # اتصال واحد مشترك — يمنع تضارب الـ threads في SQLite
        'connect_args': {'check_same_thread': False},
        'poolclass': StaticPool,
    }
elif _is_postgres:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_recycle':  280,   # تجديد الاتصال قبل timeout
        'pool_pre_ping': True,  # اختبار الاتصال قبل كل query
        'pool_size':     5,     # اتصالات دائمة (آمن على Railway free tier)
        'max_overflow':  10,    # اتصالات إضافية عند الضغط
        'pool_timeout':  30,
        'connect_args':  {'connect_timeout': 10},
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}


db = SQLAlchemy(app)
login_manager = LoginManager(app)

# ── Error logging (يظهر الخطأ كاملاً في السجلات) ──────────
import logging, traceback as _tb
logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

@app.errorhandler(500)
def internal_error(e):
    _logger.error("500 ERROR:\n" + _tb.format_exc())
    db.session.rollback()
    return "Internal Server Error — check logs", 500
login_manager.login_view = 'login'
login_manager.login_message = 'يرجى تسجيل الدخول للوصول لهذه الصفحة'

# ===== MODELS =====

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    full_name = db.Column(db.String(120))
    role = db.Column(db.String(20), default='user')  # developer, admin, manager, user
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(64), nullable=True)
    last_user_agent = db.Column(db.String(256), nullable=True)
    permissions = db.Column(db.Text)  # JSON list of permission keys; empty = استخدام صلاحيات الدور

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text)

class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200))
    phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    users = db.relationship('User', backref='branch', lazy=True)
    warehouses = db.relationship('Warehouse', backref='branch', lazy=True)

class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    manager_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    address = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=True)
    manager = db.relationship('User', foreign_keys=[manager_id])

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    children = db.relationship('Category', backref=db.backref('parent', remote_side=[id]))
    products = db.relationship('Product', backref='category', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    barcode = db.Column(db.String(50))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    unit = db.Column(db.String(20), default='قطعة')
    cost_price = db.Column(db.Float, default=0)
    sell_price = db.Column(db.Float, default=0)
    min_stock = db.Column(db.Float, default=0)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    stock_items = db.relationship('Stock', backref='product', lazy=True)

class Stock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    quantity = db.Column(db.Float, default=0)
    warehouse = db.relationship('Warehouse')
    
    __table_args__ = (db.UniqueConstraint('product_id', 'warehouse_id'),)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.Text)
    credit_limit = db.Column(db.Float, default=0)
    balance = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sales = db.relationship('Sale', backref='customer', lazy=True)

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.Text)
    balance = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    purchases = db.relationship('Purchase', backref='supplier', lazy=True)

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    position = db.Column(db.String(100))
    department = db.Column(db.String(100))
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    salary = db.Column(db.Float, default=0)
    hire_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=True)
    branch = db.relationship('Branch')

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    paid = db.Column(db.Float, default=0)
    remaining = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='completed')
    notes = db.Column(db.Text)
    items = db.relationship('SaleItem', backref='sale', lazy=True, cascade='all, delete-orphan')
    user = db.relationship('User')
    warehouse = db.relationship('Warehouse')

class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    discount = db.Column(db.Float, default=0)
    total = db.Column(db.Float)
    product = db.relationship('Product')

class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    withholding_tax = db.Column(db.Float, default=0)  # خصم / تحصيل 1% من المورد (قابل للتعديل)
    total = db.Column(db.Float, default=0)
    paid = db.Column(db.Float, default=0)
    remaining = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='completed')
    notes = db.Column(db.Text)
    items = db.relationship('PurchaseItem', backref='purchase', lazy=True, cascade='all, delete-orphan')
    user = db.relationship('User')
    warehouse = db.relationship('Warehouse')

class PurchaseItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    total = db.Column(db.Float)
    product = db.relationship('Product')

class SaleReturn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, default=0)
    reason = db.Column(db.Text)
    items = db.relationship('SaleReturnItem', backref='return_order', lazy=True)
    sale = db.relationship('Sale')

class SaleReturnItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('sale_return.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    discount = db.Column(db.Float, default=0)
    extra_discount = db.Column(db.Float, default=0)  # خصم إضافي % على سطر المرتجع
    total = db.Column(db.Float)
    product = db.relationship('Product')

class PurchaseReturn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float, default=0)
    reason = db.Column(db.Text)
    items = db.relationship('PurchaseReturnItem', backref='return_order', lazy=True)
    purchase = db.relationship('Purchase')

class PurchaseReturnItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('purchase_return.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    price = db.Column(db.Float)
    discount = db.Column(db.Float, default=0)
    extra_discount = db.Column(db.Float, default=0)
    total = db.Column(db.Float)
    product = db.relationship('Product')


class StockAdjustmentLog(db.Model):
    """سجل تسويات المخزون اليدوية (للتقرير)."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    old_quantity = db.Column(db.Float, default=0)
    new_quantity = db.Column(db.Float, default=0)
    reason = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship('Product')
    warehouse = db.relationship('Warehouse')
    user = db.relationship('User')


class InventoryMemo(db.Model):
    """مذكرات مخزون: صرف مواد لصالة الإنتاج أو استلام تام من الصالة."""
    id = db.Column(db.Integer, primary_key=True)
    memo_number = db.Column(db.String(50), unique=True)
    memo_type = db.Column(db.String(24), nullable=False)  # issue_production | receive_production
    production_ref = db.Column(db.String(120))  # رقم أمر / صالة الإنتاج
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    items = db.relationship('InventoryMemoItem', backref='memo', lazy=True, cascade='all, delete-orphan')
    warehouse = db.relationship('Warehouse')
    user = db.relationship('User')


class InventoryMemoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    memo_id = db.Column(db.Integer, db.ForeignKey('inventory_memo.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    unit_note = db.Column(db.String(40))  # وحدة الاستلام إن اختلفت عن وحدة الصنف
    product = db.relationship('Product')

class TransferRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_number = db.Column(db.String(50), unique=True)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    approver_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    date_requested = db.Column(db.DateTime, default=datetime.utcnow)
    date_processed = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    notes = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)
    items = db.relationship('TransferItem', backref='transfer', lazy=True, cascade='all, delete-orphan')
    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship('Warehouse', foreign_keys=[to_warehouse_id])
    requester = db.relationship('User', foreign_keys=[requested_by])
    designated_approver = db.relationship('User', foreign_keys=[approver_user_id])
    approver = db.relationship('User', foreign_keys=[approved_by])

class TransferItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey('transfer_request.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Float)
    product = db.relationship('Product')

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    category = db.Column(db.String(100))
    description = db.Column(db.Text)
    amount = db.Column(db.Float)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User')
    branch = db.relationship('Branch')

class CustomerPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    amount = db.Column(db.Float)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    customer = db.relationship('Customer')

class SupplierPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    amount = db.Column(db.Float)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    supplier = db.relationship('Supplier')


class LicensePoolSerial(db.Model):
    """سريالات غير مستخدمة — قاعدة license_pool.db"""
    __bind_key__ = 'license_pool'
    __tablename__ = 'pool_serial'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(48), unique=True, nullable=False)
    code_norm = db.Column(db.String(40), unique=True, nullable=False, index=True)
    plan = db.Column(db.String(32), nullable=False)
    custom_days = db.Column(db.Integer)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LicenseUsedSerial(db.Model):
    """سريالات بعد التفعيل — قاعدة license_used.db"""
    __bind_key__ = 'license_used'
    __tablename__ = 'used_serial'
    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), unique=True, nullable=False)
    code_hint = db.Column(db.String(24))
    plan = db.Column(db.String(32))
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)

# ===== HELPERS =====
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'manager', 'developer']:
            flash('ليس لديك صلاحية للوصول لهذه الصفحة', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


# ── صلاحيات مفصّلة (مفاتيح للواجهة و before_request) ─────────────────
PERMISSION_KEYS = [
    ('dashboard', 'لوحة التحكم'),
    ('sales', 'المبيعات'),
    ('purchases', 'المشتريات'),
    ('returns', 'المرتجعات'),
    ('inventory', 'المخزون والجرد'),
    ('transfers', 'تحويلات المخازن'),
    ('transfer_approve', 'الموافقة على تحويلات المخازن'),
    ('adjust_stock', 'تسوية المخزون'),
    ('customers', 'العملاء'),
    ('suppliers', 'الموردون'),
    ('employees', 'الموظفون'),
    ('expenses', 'المصاريف'),
    ('products', 'الأصناف'),
    ('categories', 'التصنيفات'),
    ('reports', 'التقارير'),
    ('settings', 'الإعدادات (فروع / مخازن / ضريبة البيع)'),
    ('settings_branding', 'إعدادات النظام'),
    ('settings_database', 'إدارة قاعدة البيانات'),
    ('users', 'المستخدمون'),
    ('connected_users', 'المتصلون'),
    ('delete_users', 'حذف مستخدمين'),
    ('record_delete', 'حذف السجلات (عملاء، موردين، أصناف، مصاريف، …)'),
    ('backup', 'النسخ الاحتياطي'),
    ('warehouse_purge', 'حذف نهائي للمخزن (خطير)'),
    ('stock_line_delete', 'حذف سطر صنف من المخزن/الجرد (خطير)'),
]

# تظهر في شاشة الصلاحيات للمطوّر فقط — يمنحها للمستخدمين يدوياً
DEVELOPER_ONLY_PERMS = frozenset({'warehouse_purge', 'stock_line_delete'})

DEFAULT_SETTINGS = {
    'company_name': 'System Makers',
    'app_title': 'النظام المحاسبي الذكي ERP',
    'app_subtitle': 'نظام إدارة أعمال ومحاسبة',
    'program_label': 'System Erp 2026',
    'layout_max_width': '1400px',
    'license_expiry_message': 'انتهى اشتراكك. يرجى التواصل مع المورد لتجديد الترخيص.',
}

# إعدادات إضافية (قابلة للتخزين في AppSetting وللتخصيص حسب الفرع)
EXTRA_APP_SETTINGS_DEFAULTS = {
    'sale_fixed_tax_percent': '0',
    'sale_fixed_tax_enabled': '0',
    'print_mode': 'normal',
    'print_paper_size': 'A4',
    'print_auto_sale': '0',
    'print_auto_purchase': '0',
    'print_auto_sale_return': '0',
    'print_auto_purchase_return': '0',
    'print_auto_copies': '1',
}

GLOBAL_ONLY_SETTING_KEYS = frozenset({
    'installation_license_permanent', 'installation_license_expires', 'license_expiry_message',
})

BRANCH_SCOPED_SETTING_KEYS = frozenset(DEFAULT_SETTINGS.keys()) | frozenset(EXTRA_APP_SETTINGS_DEFAULTS.keys()) - GLOBAL_ONLY_SETTING_KEYS


def _perm_list_from_user(user):
    if not user or not getattr(user, 'permissions', None):
        return None
    try:
        data = json.loads(user.permissions)
        if isinstance(data, list) and len(data) > 0:
            return set(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


MANAGER_DEFAULT_DENIED = frozenset({
    'users', 'backup', 'settings_branding', 'settings_database', 'delete_users', 'transfer_approve',
    'record_delete',
})


def user_can(user, perm: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'role', None) == 'developer':
        return True
    custom = _perm_list_from_user(user)
    if perm in DEVELOPER_ONLY_PERMS:
        if custom is None:
            return False
        return perm in custom
    if custom is not None:
        return perm in custom
    if user.role == 'admin':
        return True
    if user.role == 'manager':
        if perm in MANAGER_DEFAULT_DENIED:
            return False
        return True
    if user.role == 'user':
        return perm in {
            'dashboard', 'sales', 'purchases', 'returns', 'inventory', 'transfers',
            'customers', 'suppliers', 'expenses', 'products', 'categories', 'reports',
        }
    return False


def user_can_approve_transfers(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'role', None) in ('developer', 'admin'):
        return True
    custom = _perm_list_from_user(user)
    if custom is not None:
        return 'transfer_approve' in custom
    return False


def user_can_delete_users_account(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'role', None) == 'developer':
        return True
    custom = _perm_list_from_user(user)
    if custom is not None:
        return 'delete_users' in custom
    if getattr(user, 'role', None) == 'admin':
        return True
    return False


def default_role_permission_set(role: str) -> set:
    """صلاحيات الدور الافتراضية (بدون JSON يدوي) — للمقارنة وعرض نموذج التعديل."""
    keys_all = {k for k, _ in PERMISSION_KEYS}
    if role == 'developer':
        return set(keys_all)
    if role == 'admin':
        return keys_all - DEVELOPER_ONLY_PERMS
    if role == 'manager':
        return keys_all - MANAGER_DEFAULT_DENIED - DEVELOPER_ONLY_PERMS
    if role == 'user':
        return {
            'dashboard', 'sales', 'purchases', 'returns', 'inventory', 'transfers',
            'customers', 'suppliers', 'expenses', 'products', 'categories', 'reports',
        }
    return set()


def effective_selected_permissions_for_form(user, keys_visible: frozenset):
    """ما يُعرض مُحدَّداً في خانات الصلاحيات عند التعديل (الدور أو JSON المحفوظ)."""
    custom = _perm_list_from_user(user)
    if custom is not None:
        return sorted(str(k) for k in custom if str(k) in keys_visible)
    base = default_role_permission_set(user.role or 'user')
    return sorted(str(k) for k in base if k in keys_visible)


def _permissions_form_to_stored(perms_list, role: str, keys_visible: frozenset):
    """تحويل ما أُرسل من النموذج إلى JSON أو None إن طابق افتراضيات الدور."""
    if not perms_list:
        return None
    s = set(perms_list) & keys_visible
    if 'dashboard' not in s:
        s.add('dashboard')
    default = {k for k in default_role_permission_set(role) if k in keys_visible}
    if s == default:
        return None
    return sorted(s)


def record_delete_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not user_can(current_user, 'record_delete'):
            flash('ليس لديك صلاحية حذف السجلات. يمنحها مدير النظام يدوياً من صلاحيات المستخدم.', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


# ترتيب أول صفحة يُسمح بها عند عدم صلاحية «لوحة التحكم» (تجنب حلقة إعادة التوجيه)
_SAFE_HOME_FALLBACK_ROUTES = [
    ('sales', 'sales'),
    ('purchases', 'purchases'),
    ('returns', 'sale_returns'),
    ('inventory', 'inventory'),
    ('transfers', 'transfers'),
    ('adjust_stock', 'adjust_stock'),
    ('customers', 'customers'),
    ('suppliers', 'suppliers'),
    ('employees', 'employees'),
    ('expenses', 'expenses'),
    ('products', 'products'),
    ('categories', 'categories'),
    ('reports', 'report_dashboard'),
    ('settings', 'branches'),
    ('settings_branding', 'app_settings'),
    ('settings_database', 'database_admin'),
    ('users', 'users'),
    ('backup', 'backup'),
]


def safe_home_url_for(user):
    """أول وجهة آمنة بعد الدخول أو عند رفض صلاحية الصفحة — لا يُعاد التوجيه إلى لوحة تحكم غير مسموحة."""
    if not user or not user.is_authenticated:
        return url_for('login')
    if user_can(user, 'dashboard'):
        return url_for('dashboard')
    for perm, endpoint in _SAFE_HOME_FALLBACK_ROUTES:
        if user_can(user, perm):
            return url_for(endpoint)
    return url_for('access_restricted')


def path_required_permission(path: str):
    """أول بادئة مطابقة تحدد الصلاحية المطلوبة؛ None = يكفي تسجيل الدخول."""
    p = (path or '').rstrip('/') or '/'
    if p.startswith('/static'):
        return None
    if p == '/access-restricted' or p.startswith('/access-restricted/'):
        return None
    rules = [
        ('/settings/users', 'users'),
        ('/settings/connected-users', 'connected_users'),
        ('/settings/backup', 'backup'),
        ('/settings/branches', 'settings'),
        ('/settings/warehouses', 'settings'),
        ('/settings/sale-tax', 'settings'),
        ('/settings/app', 'settings_branding'),
        ('/settings/database', 'settings_database'),
        ('/returns/sale', 'returns'),
        ('/returns/purchase', 'returns'),
        ('/inventory/adjust', 'adjust_stock'),
        ('/inventory/memos', 'inventory'),
        ('/transfers', 'transfers'),
        ('/inventory', 'inventory'),
        ('/customers', 'customers'),
        ('/suppliers', 'suppliers'),
        ('/employees', 'employees'),
        ('/expenses', 'expenses'),
        ('/categories', 'categories'),
        ('/products', 'products'),
        ('/sales', 'sales'),
        ('/purchases', 'purchases'),
        ('/reports', 'reports'),
        ('/about', 'dashboard'),
    ]
    for prefix, perm in rules:
        if p == prefix or p.startswith(prefix + '/'):
            return perm
    if p == '/' or p == '':
        return 'dashboard'
    return None


def get_app_settings_dict(branch_id=None):
    out = {**DEFAULT_SETTINGS, **EXTRA_APP_SETTINGS_DEFAULTS}
    try:
        branch_rows = {}
        for row in AppSetting.query.all():
            k = (row.key or '').strip()
            if not k:
                continue
            m = re.match(r'^br(\d+)_(.+)$', k, re.I)
            if m:
                bid = int(m.group(1))
                sub = m.group(2)
                branch_rows.setdefault(bid, {})[sub] = row.value or ''
            else:
                if k in out or k in GLOBAL_ONLY_SETTING_KEYS:
                    out[k] = row.value or ''
        if branch_id and branch_id in branch_rows:
            for sk, sv in branch_rows[branch_id].items():
                if sk in BRANCH_SCOPED_SETTING_KEYS:
                    out[sk] = sv
    except Exception:
        return {**DEFAULT_SETTINGS, **EXTRA_APP_SETTINGS_DEFAULTS}
    return out


BACKUPS_DIR = os.path.join(_INSTANCE_DIR, 'backups')
os.makedirs(BACKUPS_DIR, exist_ok=True)


def permission_keys_for_editor(viewer):
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return [x for x in PERMISSION_KEYS if x[0] not in DEVELOPER_ONLY_PERMS]
    if getattr(viewer, 'role', None) == 'developer':
        return list(PERMISSION_KEYS)
    return [x for x in PERMISSION_KEYS if x[0] not in DEVELOPER_ONLY_PERMS]


def default_permissions_json_for_editor(viewer):
    keys_visible = frozenset(k for k, _ in permission_keys_for_editor(viewer))
    roles = ['user', 'manager', 'admin']
    if getattr(viewer, 'role', None) == 'developer':
        roles.append('developer')
    return {r: sorted(default_role_permission_set(r) & keys_visible) for r in roles}


def resolve_sqlite_main_path():
    try:
        u = make_url(app.config['SQLALCHEMY_DATABASE_URI'])
        if u.drivername != 'sqlite' or not u.database or u.database == ':memory:':
            return None
        dbn = u.database
        # مسار مطلق
        if os.path.isabs(dbn) or (len(dbn) > 2 and dbn[1] == ':'):
            # لو المسار موجود فعلاً ارجعه، وإلا ارجع None
            return dbn if os.path.isfile(dbn) else None
        path = os.path.abspath(os.path.join(_BASE_DIR, dbn))
        return path
    except Exception:
        return None


def _prune_old_backups(keep=25):
    try:
        files = sorted(
            [os.path.join(BACKUPS_DIR, f) for f in os.listdir(BACKUPS_DIR) if f.endswith('.db')],
            key=os.path.getmtime,
            reverse=True,
        )
        for f in files[keep:]:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def get_custom_backup_dir():
    """يرجع المجلد المخصص للنسخ الاحتياطي أو المجلد الافتراضي."""
    try:
        gs = get_app_settings_dict(branch_id=None)
        custom = (gs.get('backup_custom_dir') or '').strip()
        if custom and os.path.isdir(custom):
            return custom
    except Exception:
        pass
    return BACKUPS_DIR


def erp_backup(tag='manual'):
    """نسخ احتياطي يدعم SQLite وPostgreSQL."""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest_dir = get_custom_backup_dir()
    os.makedirs(dest_dir, exist_ok=True)

    # SQLite
    if 'sqlite' in uri:
        src = resolve_sqlite_main_path()
        # لو المسار من الـ config مش موجود، نحاول المسار الافتراضي
        if not src or not os.path.isfile(src):
            fallback = os.path.join(_INSTANCE_DIR, 'erp.db')
            if os.path.isfile(fallback):
                src = fallback
            else:
                # بحث عن أي ملف .db في مجلد التطبيق
                for candidate in [
                    os.path.join(_BASE_DIR, 'erp.db'),
                    os.path.join(_BASE_DIR, 'instance', 'erp.db'),
                ]:
                    if os.path.isfile(candidate):
                        src = candidate
                        break
        if not src or not os.path.isfile(src):
            return None, f'ملف SQLite غير موجود — تحقق من مسار قاعدة البيانات في الإعدادات (المسار الحالي: {uri})'
        dest = os.path.join(dest_dir, f'erp_{tag}_{ts}.db')
        shutil.copy2(src, dest)
        _prune_old_backups(25)
        return dest, None

    # PostgreSQL
    if 'postgresql' in uri:
        import subprocess
        try:
            from sqlalchemy.engine.url import make_url as _mu
            u = _mu(uri)
            dest = os.path.join(dest_dir, f'erp_{tag}_{ts}.sql')
            env = os.environ.copy()
            if u.password:
                env['PGPASSWORD'] = str(u.password)
            cmd = ['pg_dump',
                   '-h', str(u.host or 'localhost'),
                   '-p', str(u.port or 5432),
                   '-U', str(u.username or 'postgres'),
                   '-F', 'p',  # plain SQL
                   '-f', dest,
                   str(u.database)]
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return None, result.stderr[:300]
            _prune_old_backups(25)
            return dest, None
        except FileNotFoundError:
            return None, 'أداة pg_dump غير مثبتة على السيرفر'
        except Exception as ex:
            return None, str(ex)[:200]

    return None, 'نوع قاعدة البيانات غير مدعوم للنسخ الاحتياطي'


def sqlite_backup_to_folder(tag='manual'):
    """للتوافق مع الكود القديم — يستخدم erp_backup داخلياً."""
    dest, err = erp_backup(tag)
    return dest


def warehouse_has_operations(wh_id):
    if Sale.query.filter_by(warehouse_id=wh_id).first():
        return True
    if Purchase.query.filter_by(warehouse_id=wh_id).first():
        return True
    if InventoryMemo.query.filter_by(warehouse_id=wh_id).first():
        return True
    if TransferRequest.query.filter(
        db.or_(TransferRequest.from_warehouse_id == wh_id, TransferRequest.to_warehouse_id == wh_id)
    ).first():
        return True
    return False


def reset_operational_accounting_data():
    """مسح المبيعات والمشتريات والمرتجعات والتحويلات والمصاريف والدفعات وتصفير الأرصدة والمخزون."""
    db.session.query(InventoryMemoItem).delete(synchronize_session=False)
    db.session.query(InventoryMemo).delete(synchronize_session=False)
    db.session.query(StockAdjustmentLog).delete(synchronize_session=False)
    db.session.query(SaleReturnItem).delete(synchronize_session=False)
    db.session.query(SaleReturn).delete(synchronize_session=False)
    db.session.query(PurchaseReturnItem).delete(synchronize_session=False)
    db.session.query(PurchaseReturn).delete(synchronize_session=False)
    db.session.query(TransferItem).delete(synchronize_session=False)
    db.session.query(TransferRequest).delete(synchronize_session=False)
    db.session.query(SaleItem).delete(synchronize_session=False)
    db.session.query(Sale).delete(synchronize_session=False)
    db.session.query(PurchaseItem).delete(synchronize_session=False)
    db.session.query(Purchase).delete(synchronize_session=False)
    db.session.query(CustomerPayment).delete(synchronize_session=False)
    db.session.query(SupplierPayment).delete(synchronize_session=False)
    db.session.query(Expense).delete(synchronize_session=False)
    db.session.query(Customer).update({Customer.balance: 0}, synchronize_session=False)
    db.session.query(Supplier).update({Supplier.balance: 0}, synchronize_session=False)
    db.session.query(Stock).update({Stock.quantity: 0}, synchronize_session=False)
    db.session.commit()


def ensure_schema():
    from sqlalchemy import inspect, text
    try:
        insp = inspect(db.engine)
        tables = insp.get_table_names()
        if 'user' not in tables:
            return
        cols = {c['name'] for c in insp.get_columns('user')}
        if 'permissions' not in cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN permissions TEXT'))
        if 'transfer_request' in tables:
            tcols = {c['name'] for c in insp.get_columns('transfer_request')}
            if 'approver_user_id' not in tcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE transfer_request ADD COLUMN approver_user_id INTEGER'))
        if 'sale_return_item' in tables:
            rcols = {c['name'] for c in insp.get_columns('sale_return_item')}
            if 'discount' not in rcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE sale_return_item ADD COLUMN discount FLOAT DEFAULT 0'))
            if 'extra_discount' not in rcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE sale_return_item ADD COLUMN extra_discount FLOAT DEFAULT 0'))
        if 'purchase_return_item' in tables:
            prcols = {c['name'] for c in insp.get_columns('purchase_return_item')}
            if 'discount' not in prcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE purchase_return_item ADD COLUMN discount FLOAT DEFAULT 0'))
            if 'extra_discount' not in prcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE purchase_return_item ADD COLUMN extra_discount FLOAT DEFAULT 0'))
        if 'purchase' in tables:
            pcols = {c['name'] for c in insp.get_columns('purchase')}
            if 'withholding_tax' not in pcols:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE purchase ADD COLUMN withholding_tax FLOAT DEFAULT 0'))
        if 'user' in tables:
            ucols = {c['name'] for c in insp.get_columns('user')}
            utbl = '"user"' if insp.bind.dialect.name == 'postgresql' else 'user'
            if 'last_seen' not in ucols:
                ls_sql = 'TIMESTAMP' if insp.bind.dialect.name == 'postgresql' else 'DATETIME'
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {utbl} ADD COLUMN last_seen {ls_sql}'))
            if 'last_ip' not in ucols:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {utbl} ADD COLUMN last_ip VARCHAR(64)'))
            if 'last_user_agent' not in ucols:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {utbl} ADD COLUMN last_user_agent VARCHAR(256)'))
        try:
            db.create_all()
        except Exception:
            pass
    except Exception:
        pass


def normalize_license_serial(raw):
    return ''.join((raw or '').upper().replace('-', '').split())


def license_serial_hash(code_norm):
    pepper = app.config.get('SECRET_KEY', '')
    return hashlib.sha256((code_norm + '|' + pepper).encode('utf-8')).hexdigest()


def _parse_license_expiry(val):
    val = (val or '').strip()
    if not val:
        return None
    val = val.replace('T', ' ')
    for fmt, n in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d', 10)):
        try:
            return datetime.strptime(val[:n], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(val.replace('Z', ''))
    except ValueError:
        return None


def installation_license_valid():
    gs = get_app_settings_dict(branch_id=None)
    if gs.get('installation_license_permanent') == '1':
        return True
    dt = _parse_license_expiry(gs.get('installation_license_expires'))
    if not dt:
        return False
    return datetime.utcnow() < dt


def set_installation_license(permanent, expires_at):
    def _set(k, v):
        row = AppSetting.query.filter_by(key=k).first()
        if not row:
            row = AppSetting(key=k)
            db.session.add(row)
        row.value = v if v is not None else ''
    _set('installation_license_permanent', '1' if permanent else '0')
    if permanent:
        _set('installation_license_expires', '')
    else:
        _set('installation_license_expires', expires_at.strftime('%Y-%m-%d %H:%M:%S') if expires_at else '')


def subscription_status_for_ui():
    if not current_user.is_authenticated:
        return None
    gs = get_app_settings_dict(branch_id=None)
    lic_msg = (gs.get('license_expiry_message') or DEFAULT_SETTINGS.get('license_expiry_message', '') or '').strip()
    if getattr(current_user, 'role', None) == 'developer':
        return {'kind': 'developer', 'line': 'مفعّل — مطوّر', 'client_message': lic_msg}
    if gs.get('installation_license_permanent') == '1':
        return {'kind': 'permanent', 'line': 'ترخيص دائم', 'client_message': lic_msg}
    dt = _parse_license_expiry(gs.get('installation_license_expires'))
    if not dt:
        return {'kind': 'none', 'line': 'لم يُفعَّل', 'client_message': lic_msg}
    left = (dt - datetime.utcnow()).days
    if left < 0:
        return {'kind': 'expired', 'line': 'انتهى الاشتراك', 'client_message': lic_msg}
    if left == 0:
        hrs = (dt - datetime.utcnow()).seconds // 3600
        return {'kind': 'timed', 'line': f'أقل من يوم (~{hrs}س)', 'days': 0, 'expires': gs.get('installation_license_expires'), 'client_message': lic_msg}
    return {'kind': 'timed', 'line': f'متبقي {left} يوم', 'days': left, 'expires': gs.get('installation_license_expires'), 'client_message': lic_msg}


def expires_for_serial_plan(plan, custom_days):
    if plan == 'permanent':
        return None, True
    if plan == 'six_months':
        return datetime.utcnow() + timedelta(days=182), False
    if plan == 'one_year':
        return datetime.utcnow() + timedelta(days=365), False
    if plan == 'custom' and custom_days:
        return datetime.utcnow() + timedelta(days=max(1, int(custom_days))), False
    return datetime.utcnow() + timedelta(days=30), False


def generate_one_serial_string():
    a = secrets.token_hex(3).upper()
    b = secrets.token_hex(3).upper()
    c = secrets.token_hex(3).upper()
    return f'{a}-{b}-{c}'


def developer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or getattr(current_user, 'role', None) != 'developer':
            flash('هذه الشاشة للمطوّر فقط', 'error')
            return redirect(safe_home_url_for(current_user))
        return f(*args, **kwargs)
    return decorated


def get_next_number(prefix, model, field):
    last = db.session.query(model).order_by(db.desc(db.text('id'))).first()
    num = (last.id + 1) if last else 1
    return f"{prefix}{num:06d}"


def allocate_entity_code(prefix: str, model, field_name='code'):
    """كود تلقائي فريد (عملاء C، موردين S، موظفين E، أصناف P، …)."""
    max_id = db.session.query(db.func.max(model.id)).scalar() or 0
    n = max_id + 1
    for _ in range(10000):
        cand = f"{prefix}{n:05d}"
        if not db.session.query(model).filter(getattr(model, field_name) == cand).first():
            return cand
        n += 1
    return f"{prefix}{max_id + 1:05d}x"

# ===== CONTEXT PROCESSOR =====
def _pending_transfers_count_for_user(user):
    if not user or not user.is_authenticated:
        return 0
    q = TransferRequest.query.filter_by(status='pending')
    if user_can_approve_transfers(user):
        q = q.filter(
            db.or_(
                TransferRequest.approver_user_id.is_(None),
                TransferRequest.approver_user_id == user.id,
            )
        )
        return q.count()
    return 0


def visible_pending_transfers_query(user):
    q = TransferRequest.query.filter_by(status='pending')
    parts = [TransferRequest.requested_by == user.id]
    if user_can_approve_transfers(user):
        parts.append(
            db.or_(
                TransferRequest.approver_user_id.is_(None),
                TransferRequest.approver_user_id == user.id,
            )
        )
    return q.filter(db.or_(*parts))


def can_user_act_on_transfer(transfer, user) -> bool:
    if not user or not transfer or transfer.status != 'pending':
        return False
    if not user_can_approve_transfers(user):
        return False
    if transfer.requested_by == user.id:
        return False
    if transfer.approver_user_id and transfer.approver_user_id != user.id:
        return False
    return True


def purchase_line_effective_unit_price(purchase, line) -> float:
    sub = float(purchase.subtotal or 0)
    disc = float(purchase.discount or 0)
    if sub <= 0:
        return float(line.price)
    ratio = max(0.0, (sub - disc) / sub)
    return float(line.price) * ratio


def sale_discount_amount_total(sale):
    """مجموع خصومات السطور (نسبة) + خصم الفاتورة — للتقارير."""
    line_part = sum(
        float(it.quantity or 0) * float(it.price or 0) * float(it.discount or 0) / 100.0
        for it in (sale.items or [])
    )
    return line_part + float(sale.discount or 0)


def sale_returnable_quantity(sale, product_id: int) -> float:
    sold = sum(float(it.quantity or 0) for it in (sale.items or []) if it.product_id == int(product_id))
    back = 0.0
    for r in SaleReturn.query.filter_by(sale_id=sale.id).all():
        for ri in r.items:
            if ri.product_id == int(product_id):
                back += float(ri.quantity or 0)
    return max(0.0, sold - back)


def purchase_returnable_quantity(purchase, product_id: int) -> float:
    bought = sum(float(it.quantity or 0) for it in (purchase.items or []) if it.product_id == int(product_id))
    back = 0.0
    for r in PurchaseReturn.query.filter_by(purchase_id=purchase.id).all():
        for ri in r.items:
            if ri.product_id == int(product_id):
                back += float(ri.quantity or 0)
    return max(0.0, bought - back)


def _maybe_bump_last_seen():
    try:
        now = time_module.time()
        last = float(session.get('_ls_bump_at') or 0)
        if now - last < 50:
            return
        session['_ls_bump_at'] = now
        ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        ua = (request.headers.get('User-Agent') or '')[:256]
        User.query.filter_by(id=current_user.id).update({
            'last_seen': datetime.utcnow(),
            'last_ip': ip,
            'last_user_agent': ua,
        })
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.context_processor
def inject_globals():
    bid = getattr(current_user, 'branch_id', None) if current_user.is_authenticated else None
    pending = _pending_transfers_count_for_user(current_user) if current_user.is_authenticated else 0
    gs = get_app_settings_dict(branch_id=bid)
    def can(perm):
        return user_can(current_user, perm) if current_user.is_authenticated else False
    return dict(
        pending_transfers_global=pending, pending_transfers_count=pending,
        app_settings=gs, app_brand_title=gs.get('app_title', DEFAULT_SETTINGS['app_title']),
        app_company=gs.get('company_name', DEFAULT_SETTINGS['company_name']),
        app_subtitle_brand=gs.get('app_subtitle', DEFAULT_SETTINGS['app_subtitle']),
        layout_max_width=gs.get('layout_max_width', '1400px'),
        can=can, PERMISSION_KEYS=PERMISSION_KEYS,
        permission_keys_edit=permission_keys_for_editor(current_user) if current_user.is_authenticated else [x for x in PERMISSION_KEYS if x[0] not in DEVELOPER_ONLY_PERMS],
        subscription_status=subscription_status_for_ui() if current_user.is_authenticated else None,
        license_expiry_message_text=(get_app_settings_dict(branch_id=None).get('license_expiry_message') or DEFAULT_SETTINGS.get('license_expiry_message', '')),
        current_branch_id=bid,
        can_delete_users=user_can_delete_users_account(current_user) if current_user.is_authenticated else False,
    )


@app.before_request
def erp_sqlite_autobackup_start():
    if getattr(erp_sqlite_autobackup_start, '_started', False):
        return
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    erp_sqlite_autobackup_start._started = True
    # يدعم SQLite وPostgreSQL
    if ':memory:' in uri:
        return
    if 'sqlite' not in uri and 'postgresql' not in uri:
        return

    def _loop():
        time_module.sleep(60)  # انتظر دقيقة بعد البدء
        while True:
            try:
                with app.app_context():
                    # جلب وقت النسخ الاحتياطي من الإعدادات
                    gs = get_app_settings_dict(branch_id=None)
                    backup_time = (gs.get('backup_daily_time') or '02:00').strip()
                    try:
                        bh, bm = [int(x) for x in backup_time.split(':')]
                    except Exception:
                        bh, bm = 2, 0
                    now = datetime.now()
                    # حساب الوقت التالي للنسخ
                    next_run = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
                    if next_run <= now:
                        next_run = next_run.replace(day=now.day + 1) if now.day < 28 else next_run + timedelta(days=1)
                    wait_secs = (next_run - now).total_seconds()
                time_module.sleep(max(60, wait_secs))
                with app.app_context():
                    erp_backup('auto')
            except Exception:
                time_module.sleep(3600)

    threading.Thread(target=_loop, daemon=True).start()


@app.before_request
def enforce_route_permissions():
    if not getattr(enforce_route_permissions, '_schema_ok', False):
        enforce_route_permissions._schema_ok = True
        try:
            ensure_schema()
        except Exception:
            pass
    if request.endpoint == 'static' or request.endpoint == 'login':
        return
    if not current_user.is_authenticated:
        return
    _maybe_bump_last_seen()
    ep = request.endpoint or ''
    if getattr(current_user, 'role', None) != 'developer':
        if not installation_license_valid() and ep != 'license_activate':
            return redirect(url_for('license_activate'))
    if getattr(current_user, 'role', None) == 'developer':
        return
    p = request.path or ''
    if p.startswith('/api'):
        return
    need = path_required_permission(p)
    if need and not user_can(current_user, need):
        flash('لا صلاحية للوصول لهذه الصفحة', 'error')
        return redirect(safe_home_url_for(current_user))

# ===== AUTH ROUTES =====
@app.route('/access-restricted')
@login_required
def access_restricted():
    return render_template('access_restricted.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']) and user.is_active:
            login_user(user)
            # تسجيل IP واسم الجهاز فور تسجيل الدخول
            try:
                ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
                ua = (request.headers.get('User-Agent') or '')[:256]
                user.last_seen = datetime.utcnow()
                user.last_ip = ip
                user.last_user_agent = ua
                db.session.commit()
            except Exception:
                db.session.rollback()
            return redirect(safe_home_url_for(user))
        flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/license/activate', methods=['GET', 'POST'])
@login_required
def license_activate():
    if getattr(current_user, 'role', None) == 'developer':
        return redirect(safe_home_url_for(current_user))
    if installation_license_valid():
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        raw = (request.form.get('serial') or '').strip()
        norm = normalize_license_serial(raw)
        if len(norm) < 6:
            flash('أدخل سريالاً صالحاً', 'error')
            return render_template('license_activate.html')
        h = license_serial_hash(norm)
        if LicenseUsedSerial.query.filter_by(code_hash=h).first():
            flash('تم استخدام هذا السريال مسبقاً على هذا النظام أو جهاز آخر', 'error')
            return render_template('license_activate.html')
        pool = LicensePoolSerial.query.filter_by(code_norm=norm).first()
        if not pool:
            flash('السريال غير صالح أو غير موجود في قائمة الترخيص', 'error')
            return render_template('license_activate.html')
        plan = pool.plan or 'one_year'
        custom_days = pool.custom_days
        exp, perm = expires_for_serial_plan(plan, custom_days)
        db.session.delete(pool)
        db.session.add(LicenseUsedSerial(
            code_hash=h,
            code_hint=norm[-10:] if len(norm) >= 10 else norm,
            plan=plan,
            expires_at=exp,
        ))
        set_installation_license(perm, exp)
        db.session.commit()
        flash('تم تفعيل الترخيص بنجاح', 'success')
        return redirect(safe_home_url_for(current_user))
    return render_template('license_activate.html')


@app.route('/license/admin')
@login_required
@developer_required
def license_admin():
    pool = LicensePoolSerial.query.order_by(LicensePoolSerial.created_at.desc()).all()
    used = LicenseUsedSerial.query.order_by(LicenseUsedSerial.activated_at.desc()).all()
    return render_template('license_admin.html', pool=pool, used=used)


@app.route('/license/admin/generate', methods=['POST'])
@login_required
@developer_required
def license_admin_generate():
    try:
        n = min(500, max(1, int(request.form.get('count', 1))))
    except (TypeError, ValueError):
        n = 1
    plan = request.form.get('plan') or 'one_year'
    if plan not in ('six_months', 'one_year', 'permanent', 'custom'):
        plan = 'one_year'
    custom_days = None
    if plan == 'custom':
        try:
            custom_days = max(1, int(request.form.get('custom_days', 30)))
        except (TypeError, ValueError):
            custom_days = 30
    note = (request.form.get('note') or '').strip()[:200]
    created = 0
    for _ in range(n):
        for attempt in range(50):
            s = generate_one_serial_string()
            norm = normalize_license_serial(s)
            if LicensePoolSerial.query.filter_by(code_norm=norm).first():
                continue
            db.session.add(LicensePoolSerial(code=s, code_norm=norm, plan=plan, custom_days=custom_days, note=note or None))
            created += 1
            break
    db.session.commit()
    flash(f'تم إنشاء {created} سريال', 'success')
    return redirect(url_for('license_admin'))


@app.route('/license/admin/message', methods=['POST'])
@login_required
@developer_required
def license_admin_save_message():
    msg = (request.form.get('license_expiry_message') or '').strip()
    row = AppSetting.query.filter_by(key='license_expiry_message').first()
    if not row:
        row = AppSetting(key='license_expiry_message')
        db.session.add(row)
    row.value = msg[:4000]
    db.session.commit()
    flash('تم حفظ رسالة انتهاء الاشتراك للعميل', 'success')
    return redirect(url_for('license_admin'))


@app.route('/license/admin/end-subscription', methods=['POST'])
@login_required
@developer_required
def license_admin_end_subscription():
    # مسح السريال المفعل من قاعدة البيانات — يُطلب سريال جديد عند الدخول
    row = AppSetting.query.filter_by(key='license_serial').first()
    if row:
        db.session.delete(row)
    row2 = AppSetting.query.filter_by(key='license_activated_at').first()
    if row2:
        db.session.delete(row2)
    row3 = AppSetting.query.filter_by(key='license_expires_at').first()
    if row3:
        db.session.delete(row3)
    db.session.commit()
    flash('تم إنهاء الاشتراك الحالي — سيُطلب سريال جديد عند الدخول', 'success')
    return redirect(url_for('license_admin'))


@app.route('/about')
@login_required
def about():
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    return render_template('about.html', settings=gs)

# ===== DASHBOARD =====
@app.route('/')
@login_required
def dashboard():
    today = date.today()
    sales_today = db.session.query(db.func.sum(Sale.total)).filter(
        db.func.date(Sale.date) == today).scalar() or 0
    purchases_today = db.session.query(db.func.sum(Purchase.total)).filter(
        db.func.date(Purchase.date) == today).scalar() or 0
    customers_count = Customer.query.filter_by(is_active=True).count()
    products_count = Product.query.filter_by(is_active=True).count()
    pending_transfers = _pending_transfers_count_for_user(current_user)
    low_stock = db.session.query(Stock).join(Product).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True).count()
    recent_sales = Sale.query.order_by(Sale.date.desc()).limit(5).all()
    recent_transfers = visible_pending_transfers_query(current_user).order_by(
        TransferRequest.date_requested.desc()).limit(5).all()
    return render_template('dashboard.html',
        sales_today=sales_today, purchases_today=purchases_today,
        customers_count=customers_count, products_count=products_count,
        pending_transfers=pending_transfers, low_stock=low_stock,
        recent_sales=recent_sales, recent_transfers=recent_transfers, now=date.today())

# ===== PRODUCTS =====
@app.route('/products')
@login_required
def products():
    q = request.args.get('q', '')
    query = Product.query
    if q:
        query = query.filter(db.or_(Product.name.contains(q), Product.code.contains(q)))
    products = query.filter_by(is_active=True).all()
    categories = Category.query.all()
    return render_template('products.html', products=products, categories=categories, q=q)

@app.route('/products/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        wh_id = request.form.get('warehouse_id')
        if not wh_id:
            flash('اختر المخزن الذي يُسجَّل فيه الصنف', 'error')
            categories = Category.query.all()
            warehouses = Warehouse.query.filter_by(is_active=True).all()
            return render_template('product_form.html', categories=categories, warehouses=warehouses)
        code = (request.form.get('code') or '').strip()
        if not code:
            code = allocate_entity_code('P', Product)
        product = Product(
            code=code,
            name=request.form['name'],
            barcode=request.form.get('barcode'),
            category_id=request.form.get('category_id') or None,
            unit=request.form.get('unit', 'قطعة'),
            cost_price=float(request.form.get('cost_price', 0)),
            sell_price=float(request.form.get('sell_price', 0)),
            min_stock=float(request.form.get('min_stock', 0)),
            description=request.form.get('description')
        )
        db.session.add(product)
        db.session.flush()
        stock = Stock(product_id=product.id, warehouse_id=int(wh_id), quantity=0)
        db.session.add(stock)
        db.session.commit()
        flash('تم إضافة الصنف بنجاح', 'success')
        return redirect(url_for('products'))
    categories = Category.query.all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    suggested = allocate_entity_code('P', Product)
    return render_template('product_form.html', categories=categories, warehouses=warehouses, suggested_code=suggested)

@app.route('/products/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_product(id):
    product = Product.query.get_or_404(id)
    if request.method == 'POST':
        product.code = request.form['code']
        product.name = request.form['name']
        product.barcode = request.form.get('barcode')
        product.category_id = request.form.get('category_id') or None
        product.unit = request.form.get('unit', 'قطعة')
        product.cost_price = float(request.form.get('cost_price', 0))
        product.sell_price = float(request.form.get('sell_price', 0))
        product.min_stock = float(request.form.get('min_stock', 0))
        product.description = request.form.get('description')
        db.session.commit()
        flash('تم تحديث الصنف بنجاح', 'success')
        return redirect(url_for('products'))
    categories = Category.query.all()
    return render_template('product_form.html', product=product, categories=categories)

@app.route('/products/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_product(id):
    product = Product.query.get_or_404(id)
    product.is_active = False
    Stock.query.filter_by(product_id=product.id).delete()
    db.session.commit()
    flash('تم حذف الصنف وإزالة أرصدته من المخازن', 'success')
    return redirect(url_for('products'))

@app.route('/api/product/<int:id>')
@login_required
def api_product(id):
    p = Product.query.get_or_404(id)
    return jsonify({'id': p.id, 'name': p.name, 'price': p.sell_price, 'cost': p.cost_price, 'unit': p.unit})

@app.route('/api/product/search')
@login_required
def api_product_search():
    q = (request.args.get('q') or '').strip()
    warehouse_id = request.args.get('warehouse_id')
    limit = request.args.get('limit', type=int) or 80
    limit = max(1, min(limit, 100))
    query = Product.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(
            Product.name.contains(q),
            Product.code.contains(q),
            Product.barcode.contains(q),
        ))
    products = query.order_by(Product.name).limit(limit).all()
    try:
        warehouse_id_int = int(warehouse_id) if warehouse_id not in (None, '', 'null') else None
    except Exception:
        warehouse_id_int = None

    result = []
    for p in products:
        if warehouse_id_int is not None:
            stock = Stock.query.filter_by(product_id=p.id, warehouse_id=warehouse_id_int).first()
            qty = stock.quantity if stock else 0
        else:
            qty = db.session.query(db.func.coalesce(db.func.sum(Stock.quantity), 0)).filter(Stock.product_id == p.id).scalar() or 0
        result.append({'id': p.id, 'code': p.code, 'name': p.name,
                       'price': p.sell_price, 'cost': p.cost_price, 'unit': p.unit, 'qty': qty})
    return jsonify(result)


@app.route('/products/search')
@login_required
def products_search():
    """
    Search endpoint for invoice line picker (lazy search).
    Returns: id, name, barcode, price, stock (+ code/cost/unit for compatibility)
    """
    q = (request.args.get('q') or '').strip()
    warehouse_id = request.args.get('warehouse_id')
    page = request.args.get('page', type=int) or 1
    limit = request.args.get('limit', type=int) or 20
    page = max(page, 1)
    limit = max(1, min(limit, 100))

    query = Product.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(
            Product.name.contains(q),
            Product.code.contains(q),
            Product.barcode.contains(q),
        ))

    total = query.count()
    products = query.order_by(Product.name).offset((page - 1) * limit).limit(limit).all()

    try:
        warehouse_id_int = int(warehouse_id) if warehouse_id not in (None, '', 'null') else None
    except Exception:
        warehouse_id_int = None

    items = []
    for p in products:
        if warehouse_id_int is not None:
            stock = Stock.query.filter_by(product_id=p.id, warehouse_id=warehouse_id_int).first()
            qty = stock.quantity if stock else 0
        else:
            qty = db.session.query(db.func.coalesce(db.func.sum(Stock.quantity), 0)).filter(Stock.product_id == p.id).scalar() or 0
        items.append({
            'id': p.id,
            'name': p.name,
            'code': p.code,
            'barcode': p.barcode,
            'price': p.sell_price,
            'cost': p.cost_price,
            'unit': p.unit,
            'stock': qty,
            'qty': qty,  # backward compatibility for existing JS widgets
        })

    return jsonify({
        'items': items,
        'page': page,
        'limit': limit,
        'hasMore': (page * limit) < total,
        'total': total,
    })


@app.route('/api/product/by_barcode')
@login_required
def api_product_by_barcode():
    barcode = (request.args.get('barcode') or '').strip()
    warehouse_id = request.args.get('warehouse_id')
    if not barcode:
        return jsonify({'error': 'barcode_required'}), 400

    p = Product.query.filter_by(is_active=True).filter(
        db.or_(Product.barcode == barcode, Product.code == barcode)
    ).first()
    if not p:
        return jsonify({'error': 'not_found'}), 404

    try:
        warehouse_id_int = int(warehouse_id) if warehouse_id not in (None, '', 'null') else None
    except Exception:
        warehouse_id_int = None

    if warehouse_id_int is not None:
        stock = Stock.query.filter_by(product_id=p.id, warehouse_id=warehouse_id_int).first()
        qty = stock.quantity if stock else 0
    else:
        qty = db.session.query(db.func.coalesce(db.func.sum(Stock.quantity), 0)).filter(Stock.product_id == p.id).scalar() or 0

    return jsonify({
        'id': p.id,
        'code': p.code,
        'barcode': p.barcode,
        'name': p.name,
        'price': p.sell_price,
        'cost': p.cost_price,
        'unit': p.unit,
        'qty': qty,
    })

# ===== CUSTOMERS =====
@app.route('/customers')
@login_required
def customers():
    q = request.args.get('q', '')
    query = Customer.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Customer.name.contains(q), Customer.phone.contains(q)))
    customers = query.all()
    return render_template('customers.html', customers=customers, q=q)

@app.route('/customers/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if not code:
            code = allocate_entity_code('C', Customer)
        customer = Customer(
            code=code,
            name=request.form['name'],
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            address=request.form.get('address'),
            credit_limit=float(request.form.get('credit_limit', 0))
        )
        db.session.add(customer)
        db.session.commit()
        flash('تم إضافة العميل بنجاح', 'success')
        return redirect(url_for('customers'))
    suggested = allocate_entity_code('C', Customer)
    return render_template('customer_form.html', suggested_code=suggested)

@app.route('/customers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_customer(id):
    customer = Customer.query.get_or_404(id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code:
            customer.code = code
        customer.name = request.form['name']
        customer.phone = request.form.get('phone')
        customer.email = request.form.get('email')
        customer.address = request.form.get('address')
        customer.credit_limit = float(request.form.get('credit_limit', 0))
        db.session.commit()
        flash('تم تحديث بيانات العميل', 'success')
        return redirect(url_for('customers'))
    return render_template('customer_form.html', customer=customer)

@app.route('/customers/<int:id>/statement')
@login_required
def customer_statement(id):
    customer = Customer.query.get_or_404(id)
    sales = Sale.query.filter_by(customer_id=id).order_by(Sale.date.desc()).all()
    payments = CustomerPayment.query.filter_by(customer_id=id).order_by(CustomerPayment.date.desc()).all()
    returns = SaleReturn.query.join(Sale).filter(Sale.customer_id == id).all()
    return render_template('customer_statement.html', customer=customer, sales=sales, 
                           payments=payments, returns=returns)

@app.route('/customers/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_customer(id):
    customer = Customer.query.get_or_404(id)
    if customer.balance and abs(customer.balance) > 0.0001:
        flash('لا يمكن حذف العميل طالما يوجد رصيد مستحق أو دائن', 'error')
        return redirect(url_for('customers'))
    customer.is_active = False
    db.session.commit()
    flash('تم حذف العميل', 'success')
    return redirect(url_for('customers'))

@app.route('/customers/<int:id>/payment', methods=['POST'])
@login_required
def customer_payment(id):
    customer = Customer.query.get_or_404(id)
    amount = float(request.form['amount'])
    payment = CustomerPayment(customer_id=id, amount=amount, notes=request.form.get('notes'), user_id=current_user.id)
    customer.balance -= amount
    db.session.add(payment)
    db.session.commit()
    flash('تم تسجيل الدفعة بنجاح', 'success')
    return redirect(url_for('customer_statement', id=id))

# ===== SUPPLIERS =====
@app.route('/suppliers')
@login_required
def suppliers():
    q = request.args.get('q', '')
    query = Supplier.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(Supplier.name.contains(q), Supplier.phone.contains(q)))
    suppliers = query.all()
    return render_template('suppliers.html', suppliers=suppliers, q=q)

@app.route('/suppliers/add', methods=['GET', 'POST'])
@login_required
def add_supplier():
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if not code:
            code = allocate_entity_code('S', Supplier)
        supplier = Supplier(
            code=code,
            name=request.form['name'],
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            address=request.form.get('address')
        )
        db.session.add(supplier)
        db.session.commit()
        flash('تم إضافة المورد بنجاح', 'success')
        return redirect(url_for('suppliers'))
    suggested = allocate_entity_code('S', Supplier)
    return render_template('supplier_form.html', suggested_code=suggested)

# ===== SALES =====
@app.route('/sales')
@login_required
def sales():
    page = request.args.get('page', 1, type=int)
    sales = Sale.query.order_by(Sale.date.desc()).paginate(page=page, per_page=20)
    returned_ids = {r[0] for r in db.session.query(SaleReturn.sale_id).distinct().all()}
    return render_template('sales.html', sales=sales, returned_sale_ids=returned_ids)

@app.route('/sales/new', methods=['GET', 'POST'])
@login_required
def new_sale():
    if request.method == 'POST':
        customer_id = request.form.get('customer_id') or None
        warehouse_id = request.form.get('warehouse_id') or None
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')

        # ── Validation: لا حفظ بدون بيانات كاملة ──
        if not warehouse_id:
            flash('يرجى اختيار المخزن قبل الحفظ', 'error')
            return redirect(url_for('new_sale'))
        warehouse_id = int(warehouse_id)
        valid_lines_check = [pid for i, pid in enumerate(product_ids) if pid and float(quantities[i] or 0) > 0 and float(prices[i] or 0) > 0]
        if not valid_lines_check:
            flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل الحفظ', 'error')
            return redirect(url_for('new_sale'))
        paid_val = float(request.form.get('paid', 0) or 0)
        is_ajal  = request.form.get('is_ajal') == '1'
        if paid_val <= 0 and not is_ajal:
            flash('يرجى إدخال المبلغ المدفوع أو تحديد "آجل" لحفظ الفاتورة', 'error')
            return redirect(url_for('new_sale'))

        lines = []
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i])
            price = float(prices[i])
            disc = float(discounts[i]) if i < len(discounts) else 0
            lines.append((int(pid), qty, price, disc))

        # Merge duplicated products into one line to prevent duplicate items per invoice.
        merged = {}
        for pid, qty, price, disc in lines:
            if pid not in merged:
                merged[pid] = {'qty': 0.0, 'price': price, 'disc': disc}
            merged[pid]['qty'] += qty
            merged[pid]['price'] = price
            merged[pid]['disc'] = disc
        lines = [(pid, v['qty'], v['price'], v['disc']) for pid, v in merged.items()]

        for pid, qty, price, disc in lines:
            stock = Stock.query.filter_by(product_id=pid, warehouse_id=warehouse_id).first()
            avail = float(stock.quantity) if stock else 0.0
            if avail + 1e-9 < qty:
                pname = Product.query.get(pid)
                pname = pname.name if pname else str(pid)
                short = qty - avail
                flash(f'المخزون غير كافٍ للصنف «{pname}»: المتوفر {avail:g} والمطلوب {qty:g} — يوجد عجز {short:g}', 'error')
                return redirect(url_for('new_sale'))

        sale = Sale(
            invoice_number=get_next_number('INV', Sale, 'invoice_number'),
            customer_id=customer_id,
            warehouse_id=warehouse_id,
            user_id=current_user.id,
            discount=float(request.form.get('total_discount', 0)),
            tax=0,
            paid=float(request.form.get('paid', 0)),
            notes=request.form.get('notes')
        )
        subtotal = 0
        for pid, qty, price, disc in lines:
            item_total = qty * price * (1 - disc / 100)
            subtotal += item_total
            item = SaleItem(product_id=pid, quantity=qty, price=price, discount=disc, total=item_total)
            sale.items.append(item)
            stock = Stock.query.filter_by(product_id=pid, warehouse_id=warehouse_id).first()
            if stock:
                stock.quantity -= qty

        sale.subtotal = subtotal
        gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
        if (gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'):
            pct = float(gs.get('sale_fixed_tax_percent') or 0)
            sale.tax = round(subtotal * (pct / 100.0), 2)
        else:
            sale.tax = float(request.form.get('tax', 0))
        sale.total = subtotal - sale.discount + sale.tax
        sale.remaining = sale.total - sale.paid

        if customer_id and sale.remaining > 0:
            customer = Customer.query.get(customer_id)
            if customer:
                customer.balance += sale.remaining

        db.session.add(sale)
        db.session.commit()
        flash(f'تم إنشاء الفاتورة {sale.invoice_number} بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_sale') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('sale_detail', id=sale.id, autoprint='1'))
        return redirect(url_for('sale_detail', id=sale.id))

    customers = Customer.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    return render_template(
        'sale_form.html',
        customers=customers,
        warehouses=warehouses,
        sale_tax_auto=(gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'),
        sale_tax_percent=float(gs.get('sale_fixed_tax_percent') or 0),
    )

@app.route('/sales/<int:id>')
@login_required
def sale_detail(id):
    sale = Sale.query.get_or_404(id)
    sale_has_return = SaleReturn.query.filter_by(sale_id=sale.id).first() is not None
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_detail.html',
        sale=sale,
        sale_has_return=sale_has_return,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )

@app.route('/sales/<int:id>/delete', methods=['POST'])
@login_required
def delete_sale(id):
    sale = Sale.query.get_or_404(id)
    if not user_can(current_user, 'sales_purchases_delete'):
        flash('ليس لديك صلاحية حذف الفواتير', 'error')
        return redirect(url_for('sales'))
    if SaleReturn.query.filter_by(sale_id=id).first():
        flash('لا يمكن حذف الفاتورة لوجود مرتجع مرتبط بها', 'error')
        return redirect(url_for('sales'))
    for item in sale.items:
        stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=sale.warehouse_id).first()
        if stock:
            stock.quantity += item.quantity
    if sale.customer_id and sale.remaining and sale.remaining > 0:
        customer = Customer.query.get(sale.customer_id)
        if customer:
            customer.balance -= sale.remaining
    db.session.delete(sale)
    db.session.commit()
    flash(f'تم حذف الفاتورة {sale.invoice_number} بنجاح', 'success')
    return redirect(url_for('sales'))

# ===== PURCHASES =====
@app.route('/purchases')
@login_required
def purchases():
    page = request.args.get('page', 1, type=int)
    purchases = Purchase.query.order_by(Purchase.date.desc()).paginate(page=page, per_page=20)
    returned_purchase_ids = {r[0] for r in db.session.query(PurchaseReturn.purchase_id).distinct().all()}
    return render_template('purchases.html', purchases=purchases, returned_purchase_ids=returned_purchase_ids)

@app.route('/purchases/new', methods=['GET', 'POST'])
@login_required
def new_purchase():
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id') or None
        warehouse_id = request.form.get('warehouse_id') or None
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')

        # ── Validation: لا حفظ بدون بيانات كاملة ──
        if not warehouse_id:
            flash('يرجى اختيار المخزن قبل الحفظ', 'error')
            return redirect(url_for('new_purchase'))
        warehouse_id = int(warehouse_id)
        valid_lines = [pid for i, pid in enumerate(product_ids) if pid and float(quantities[i] or 0) > 0 and float(prices[i] or 0) > 0]
        if not valid_lines:
            flash('يرجى إضافة صنف واحد على الأقل بكمية وسعر صحيحين قبل الحفظ', 'error')
            return redirect(url_for('new_purchase'))
        paid_val = float(request.form.get('paid', 0) or 0)
        is_ajal  = request.form.get('is_ajal') == '1'
        if paid_val <= 0 and not is_ajal:
            flash('يرجى إدخال المبلغ المدفوع أو تحديد "آجل" لحفظ الفاتورة', 'error')
            return redirect(url_for('new_purchase'))

        purchase = Purchase(
            invoice_number=get_next_number('PUR', Purchase, 'invoice_number'),
            supplier_id=supplier_id,
            warehouse_id=warehouse_id,
            user_id=current_user.id,
            discount=float(request.form.get('total_discount', 0)),
            tax=float(request.form.get('tax', 0)),
            withholding_tax=float(request.form.get('withholding_tax', 0) or 0),
            paid=float(request.form.get('paid', 0)),
            notes=request.form.get('notes')
        )
        lines = []
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i])
            price = float(prices[i])
            lines.append((int(pid), qty, price))

        merged = {}
        for pid, qty, price in lines:
            if pid not in merged:
                merged[pid] = {'qty': 0.0, 'price': price}
            merged[pid]['qty'] += qty
            merged[pid]['price'] = price

        subtotal = 0
        for pid, line in merged.items():
            qty = line['qty']
            price = line['price']
            item_total = qty * price
            subtotal += item_total
            item = PurchaseItem(product_id=int(pid), quantity=qty, price=price, total=item_total)
            purchase.items.append(item)
            stock = Stock.query.filter_by(product_id=int(pid), warehouse_id=warehouse_id).first()
            if stock:
                stock.quantity += qty
            else:
                db.session.add(Stock(product_id=int(pid), warehouse_id=int(warehouse_id), quantity=qty))
        
        purchase.subtotal = subtotal
        purchase.total = subtotal - purchase.discount + purchase.tax - (purchase.withholding_tax or 0)
        purchase.remaining = purchase.total - purchase.paid
        
        if supplier_id and purchase.remaining > 0:
            supplier = Supplier.query.get(supplier_id)
            if supplier:
                supplier.balance += purchase.remaining
        
        db.session.add(purchase)
        db.session.commit()
        flash(f'تم إنشاء فاتورة الشراء {purchase.invoice_number} بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_purchase') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('purchase_detail', id=purchase.id, autoprint='1'))
        return redirect(url_for('purchase_detail', id=purchase.id))
    
    suppliers = Supplier.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('purchase_form.html', suppliers=suppliers, warehouses=warehouses)

@app.route('/purchases/<int:id>')
@login_required
def purchase_detail(id):
    purchase = Purchase.query.get_or_404(id)
    purchase_has_return = PurchaseReturn.query.filter_by(purchase_id=purchase.id).first() is not None
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_detail.html',
        purchase=purchase,
        purchase_has_return=purchase_has_return,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )

@app.route('/purchases/<int:id>/delete', methods=['POST'])
@login_required
def delete_purchase(id):
    purchase = Purchase.query.get_or_404(id)
    # التحقق من الصلاحية
    if not user_can(current_user, 'sales_purchases_delete'):
        flash('ليس لديك صلاحية حذف الفواتير', 'error')
        return redirect(url_for('purchases'))
    # منع الحذف لو فيه مرتجع مرتبط
    if PurchaseReturn.query.filter_by(purchase_id=id).first():
        flash('لا يمكن حذف الفاتورة لوجود مرتجع مرتبط بها', 'error')
        return redirect(url_for('purchases'))
    # عكس تأثير الفاتورة على المخزن والرصيد
    for item in purchase.items:
        stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=purchase.warehouse_id).first()
        if stock:
            stock.quantity -= item.quantity
    if purchase.supplier_id and purchase.remaining and purchase.remaining > 0:
        supplier = Supplier.query.get(purchase.supplier_id)
        if supplier:
            supplier.balance -= purchase.remaining
    db.session.delete(purchase)
    db.session.commit()
    flash(f'تم حذف فاتورة الشراء {purchase.invoice_number} بنجاح', 'success')
    return redirect(url_for('purchases'))

# ===== RETURNS =====
@app.route('/returns/sale')
@login_required
def sale_returns():
    returns = SaleReturn.query.order_by(SaleReturn.date.desc()).all()
    return render_template('sale_returns.html', returns=returns)

@app.route('/returns/sale/new', methods=['GET', 'POST'])
@login_required
def new_sale_return():
    if request.method == 'POST':
        sale_id = request.form.get('sale_id') or None
        if not sale_id:
            flash('يرجى اختيار فاتورة البيع قبل الحفظ', 'error')
            return redirect(url_for('new_sale_return'))
        sale = Sale.query.options(joinedload(Sale.items)).get_or_404(sale_id)
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')
        extra_discounts = request.form.getlist('extra_discount[]')

        # ── Validation: لا حفظ بدون أصناف ──
        valid_items_sr = [pid for i, pid in enumerate(product_ids) if pid and float(quantities[i] or 0) > 0]
        if not valid_items_sr:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة قبل الحفظ', 'error')
            return redirect(url_for('new_sale_return'))

        planned_qty = defaultdict(float)
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            planned_qty[int(pid)] += float(quantities[i])
        for pid, pq in planned_qty.items():
            max_ret = sale_returnable_quantity(sale, pid)
            if pq > max_ret + 1e-9:
                flash(f'مجموع الكمية المرتجعة للصنف يتجاوز المتاح ({max_ret:g} وفق الفاتورة والمرتجعات السابقة)', 'error')
                return redirect(url_for('new_sale_return'))

        ret = SaleReturn(
            invoice_number=get_next_number('SRT', SaleReturn, 'invoice_number'),
            sale_id=sale_id, user_id=current_user.id,
            reason=request.form.get('reason')
        )
        total = 0
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i])
            price = float(prices[i])
            disc = float(discounts[i]) if i < len(discounts) else 0
            extra = float(extra_discounts[i]) if i < len(extra_discounts) else 0
            base = qty * price * (1 - disc / 100)
            item_total = round(base * (1 - extra / 100), 4)
            total += item_total
            ret.items.append(SaleReturnItem(
                product_id=int(pid), quantity=qty, price=price, discount=disc,
                extra_discount=extra, total=item_total))
            stock = Stock.query.filter_by(product_id=int(pid), warehouse_id=sale.warehouse_id).first()
            if stock:
                stock.quantity += qty
        ret.total = total
        db.session.add(ret)
        if sale.customer_id:
            customer = Customer.query.get(sale.customer_id)
            if customer:
                customer.balance -= total
        db.session.commit()
        flash('تم تسجيل مرتجع المبيعات بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_sale_return') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('sale_return_detail', id=ret.id, autoprint='1'))
        return redirect(url_for('sale_return_detail', id=ret.id))

    sales = Sale.query.options(
        joinedload(Sale.items).joinedload(SaleItem.product),
        joinedload(Sale.customer),
    ).order_by(Sale.date.desc()).limit(100).all()
    sales_json = []
    for s in sales:
        sales_json.append({
            'id': s.id,
            'items': [
                {
                    'product_id': it.product_id,
                    'name': it.product.name if it.product else '',
                    'code': it.product.code if it.product else '',
                    'price': float(it.price),
                    'discount': float(it.discount or 0),
                    'quantity': float(it.quantity or 0),
                }
                for it in s.items if it.product_id
            ],
        })
    return render_template('sale_return_form.html', sales=sales, sales_json=sales_json)

# ===== TRANSFERS =====
@app.route('/transfers')
@login_required
def transfers():
    status = request.args.get('status', '')
    query = TransferRequest.query
    if status:
        query = query.filter_by(status=status)
    transfers = query.order_by(TransferRequest.date_requested.desc()).all()
    transfer_can_act = {t.id: can_user_act_on_transfer(t, current_user) for t in transfers}
    return render_template('transfers.html', transfers=transfers, status=status, transfer_can_act=transfer_can_act)

@app.route('/transfers/new', methods=['GET', 'POST'])
@login_required
def new_transfer():
    if request.method == 'POST':
        from_wh = int(request.form['from_warehouse_id'])
        to_wh = int(request.form['to_warehouse_id'])
        if from_wh == to_wh:
            flash('لا يمكن التحويل من وإلى نفس المخزن', 'error')
            return redirect(url_for('new_transfer'))
        try:
            approver_id = int(request.form.get('approver_user_id') or 0)
        except (TypeError, ValueError):
            approver_id = 0
        if not approver_id:
            flash('يرجى اختيار المستخدم المكلَّف بالموافقة على التحويل', 'error')
            return redirect(url_for('new_transfer'))
        appr = User.query.get(approver_id)
        if not appr or not appr.is_active or not user_can_approve_transfers(appr):
            flash('المستخدم المختار غير مخوّل بالموافقة على التحويلات', 'error')
            return redirect(url_for('new_transfer'))
        if approver_id == current_user.id:
            flash('لا يمكنك تعيين نفسك للموافقة على طلبك', 'error')
            return redirect(url_for('new_transfer'))

        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')

        transfer = TransferRequest(
            request_number=get_next_number('TRF', TransferRequest, 'request_number'),
            from_warehouse_id=from_wh,
            to_warehouse_id=to_wh,
            requested_by=current_user.id,
            approver_user_id=approver_id,
            notes=request.form.get('notes'),
            status='pending'
        )
        for i, pid in enumerate(product_ids):
            if not pid: continue
            transfer.items.append(TransferItem(product_id=int(pid), quantity=float(quantities[i])))

        db.session.add(transfer)
        db.session.commit()
        flash(f'تم إرسال طلب التحويل {transfer.request_number} بنجاح، في انتظار الموافقة', 'success')
        return redirect(url_for('transfers'))

    warehouses = Warehouse.query.filter_by(is_active=True).all()
    approver_choices = [u for u in User.query.filter_by(is_active=True).order_by(User.full_name, User.username).all()
                        if user_can_approve_transfers(u) and u.id != current_user.id]
    return render_template('transfer_form.html', warehouses=warehouses, approver_choices=approver_choices)

@app.route('/transfers/<int:id>/approve', methods=['POST'])
@login_required
def approve_transfer(id):
    transfer = TransferRequest.query.get_or_404(id)
    if not can_user_act_on_transfer(transfer, current_user):
        flash('لا يمكنك الموافقة على هذا الطلب (لست المكلَّفاً أو أنت صاحب الطلب)', 'error')
        return redirect(url_for('transfer_detail', id=id))
    if transfer.status != 'pending':
        flash('هذا الطلب تم معالجته مسبقاً', 'error')
        return redirect(url_for('transfers'))

    for item in transfer.items:
        from_stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=transfer.from_warehouse_id).first()
        to_stock = Stock.query.filter_by(product_id=item.product_id, warehouse_id=transfer.to_warehouse_id).first()
        
        if not from_stock or from_stock.quantity < item.quantity:
            flash(f'لا يوجد رصيد كافٍ للصنف: {item.product.name}', 'error')
            return redirect(url_for('transfers'))
        
        from_stock.quantity -= item.quantity
        if to_stock:
            to_stock.quantity += item.quantity
        else:
            db.session.add(Stock(product_id=item.product_id, warehouse_id=transfer.to_warehouse_id, quantity=item.quantity))
    
    transfer.status = 'approved'
    transfer.approved_by = current_user.id
    transfer.date_processed = datetime.utcnow()
    db.session.commit()
    flash('تم الموافقة على طلب التحويل وتنفيذه بنجاح', 'success')
    return redirect(url_for('transfers'))

@app.route('/transfers/<int:id>/reject', methods=['POST'])
@login_required
def reject_transfer(id):
    transfer = TransferRequest.query.get_or_404(id)
    if not can_user_act_on_transfer(transfer, current_user):
        flash('لا يمكنك رفض هذا الطلب (لست المكلَّفاً أو أنت صاحب الطلب)', 'error')
        return redirect(url_for('transfer_detail', id=id))
    if transfer.status != 'pending':
        flash('هذا الطلب تم معالجته مسبقاً', 'error')
        return redirect(url_for('transfers'))

    transfer.status = 'rejected'
    transfer.approved_by = current_user.id
    transfer.date_processed = datetime.utcnow()
    transfer.rejection_reason = request.form.get('reason', 'لم يذكر سبب')
    db.session.commit()
    flash('تم رفض طلب التحويل', 'success')
    return redirect(url_for('transfers'))

@app.route('/transfers/<int:id>')
@login_required
def transfer_detail(id):
    transfer = TransferRequest.query.get_or_404(id)
    return render_template(
        'transfer_detail.html',
        transfer=transfer,
        can_act_transfer=can_user_act_on_transfer(transfer, current_user),
    )

# ===== INVENTORY =====
@app.route('/inventory')
@login_required
def inventory():
    warehouse_id = request.args.get('warehouse_id')
    q = request.args.get('q', '')
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    query = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse)
    if warehouse_id:
        query = query.filter(Stock.warehouse_id == warehouse_id)
    if q:
        query = query.filter(Product.name.contains(q))
    stocks = query.all()
    return render_template('inventory.html', stocks=stocks, warehouses=warehouses, 
                           selected_warehouse=warehouse_id, q=q)

# ===== EXPENSES =====
@app.route('/expenses')
@login_required
def expenses():
    expenses = Expense.query.order_by(Expense.date.desc()).all()
    return render_template('expenses.html', expenses=expenses)

@app.route('/expenses/add', methods=['GET', 'POST'])
@login_required
def add_expense():
    if request.method == 'POST':
        expense = Expense(
            category=request.form['category'],
            description=request.form.get('description'),
            amount=float(request.form['amount']),
            branch_id=request.form.get('branch_id') or None,
            user_id=current_user.id
        )
        db.session.add(expense)
        db.session.commit()
        flash('تم إضافة المصروف بنجاح', 'success')
        return redirect(url_for('expenses'))
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template('expense_form.html', branches=branches)

@app.route('/expenses/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_expense(id):
    expense = Expense.query.get_or_404(id)
    if request.method == 'POST':
        expense.category = request.form['category']
        expense.description = request.form.get('description')
        expense.amount = float(request.form['amount'])
        expense.branch_id = request.form.get('branch_id') or None
        db.session.commit()
        flash('تم تحديث المصروف', 'success')
        return redirect(url_for('expenses'))
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template('expense_form.html', branches=branches, expense=expense)

@app.route('/expenses/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_expense(id):
    expense = Expense.query.get_or_404(id)
    db.session.delete(expense)
    db.session.commit()
    flash('تم حذف المصروف', 'success')
    return redirect(url_for('expenses'))

# ===== EMPLOYEES =====
@app.route('/employees')
@login_required
def employees():
    employees = Employee.query.filter_by(is_active=True).all()
    return render_template('employees.html', employees=employees)


@app.route('/employees/<int:id>/pay-salary', methods=['GET', 'POST'])
@login_required
def pay_employee_salary(id):
    emp = Employee.query.get_or_404(id)
    if not emp.is_active:
        flash('الموظف غير نشط', 'error')
        return redirect(url_for('employees'))
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            flash('المبلغ يجب أن يكون أكبر من صفر', 'error')
            return redirect(url_for('pay_employee_salary', id=id))
        desc = (request.form.get('description') or '').strip() or f'صرف راتب — {emp.name} ({emp.code})'
        db.session.add(Expense(
            category='رواتب',
            description=desc,
            amount=amount,
            branch_id=emp.branch_id,
            user_id=current_user.id,
        ))
        db.session.commit()
        flash('تم تسجيل صرف الراتب في المصروفات وتقاريرها', 'success')
        return redirect(url_for('employees'))
    return render_template('employee_pay.html', emp=emp)

@app.route('/employees/add', methods=['GET', 'POST'])
@login_required
def add_employee():
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if not code:
            code = allocate_entity_code('E', Employee)
        emp = Employee(
            code=code,
            name=request.form['name'],
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            position=request.form.get('position'),
            department=request.form.get('department'),
            branch_id=request.form.get('branch_id') or None,
            salary=float(request.form.get('salary', 0)),
            hire_date=datetime.strptime(request.form['hire_date'], '%Y-%m-%d').date() if request.form.get('hire_date') else None
        )
        db.session.add(emp)
        db.session.commit()
        flash('تم إضافة الموظف بنجاح', 'success')
        return redirect(url_for('employees'))
    branches = Branch.query.filter_by(is_active=True).all()
    suggested = allocate_entity_code('E', Employee)
    return render_template('employee_form.html', branches=branches, suggested_code=suggested)

# ===== SETTINGS =====
@app.route('/settings/users')
@login_required
@admin_required
def users():
    q = User.query
    if current_user.role != 'developer':
        q = q.filter(User.role != 'developer')
    users = q.order_by(User.id).all()
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template(
        'users.html', users=users, branches=branches,
        default_perms_by_role=default_permissions_json_for_editor(current_user))

@app.route('/settings/users/add', methods=['POST'])
@login_required
@admin_required
def add_user():
    role = request.form['role']
    if role == 'developer' and current_user.role != 'developer':
        flash('لا يمكن إنشاء حساب مطوّر النظام إلا من حساب المطوّر', 'error')
        return redirect(url_for('users'))
    user = User(
        username=request.form['username'],
        full_name=request.form['full_name'],
        role=role,
        branch_id=request.form.get('branch_id') or None
    )
    user.set_password(request.form['password'])
    keys_visible = frozenset(k for k, _ in permission_keys_for_editor(current_user))
    perms = request.form.getlist('perm')
    if current_user.role != 'developer':
        perms = [p for p in perms if p not in DEVELOPER_ONLY_PERMS]
    stored = _permissions_form_to_stored(perms, role, keys_visible)
    user.permissions = json.dumps(stored, ensure_ascii=False) if stored else None
    db.session.add(user)
    db.session.commit()
    flash('تم إضافة المستخدم بنجاح', 'success')
    return redirect(url_for('users'))

@app.route('/settings/users/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(id):
    u = User.query.get_or_404(id)
    if u.role == 'developer' and current_user.role != 'developer':
        flash('غير مسموح بتعديل هذا الحساب', 'error')
        return redirect(url_for('users'))
    if request.method == 'POST':
        old_role = u.role
        role = request.form.get('role', u.role)
        if role == 'developer' and current_user.role != 'developer':
            flash('لا يمكن تعيين دور مطوّر النظام', 'error')
            return redirect(url_for('edit_user', id=id))
        u.full_name = request.form.get('full_name')
        u.role = role
        u.branch_id = request.form.get('branch_id') or None
        u.is_active = request.form.get('is_active') == 'on'
        pwd = (request.form.get('password') or '').strip()
        if pwd:
            u.set_password(pwd)
        keys_visible = frozenset(k for k, _ in permission_keys_for_editor(current_user))
        if role != old_role:
            u.permissions = None
        else:
            perms = request.form.getlist('perm')
            if current_user.role != 'developer':
                oldp = _perm_list_from_user(u) or set()
                keep_d = [p for p in oldp if p in DEVELOPER_ONLY_PERMS]
                perms = [p for p in perms if p not in DEVELOPER_ONLY_PERMS] + keep_d
            stored = _permissions_form_to_stored(perms, role, keys_visible)
            u.permissions = json.dumps(stored, ensure_ascii=False) if stored else None
        db.session.commit()
        flash('تم حفظ بيانات المستخدم', 'success')
        return redirect(url_for('users'))
    branches = Branch.query.filter_by(is_active=True).all()
    keys_visible = frozenset(k for k, _ in permission_keys_for_editor(current_user))
    selected_perms = effective_selected_permissions_for_form(u, keys_visible)
    return render_template(
        'user_edit.html', u=u, branches=branches, selected_perms=selected_perms,
        default_perms_by_role=default_permissions_json_for_editor(current_user))

@app.route('/settings/branches')
@login_required
@admin_required
def branches():
    branches = Branch.query.all()
    return render_template('branches.html', branches=branches)

@app.route('/settings/branches/add', methods=['POST'])
@login_required
@admin_required
def add_branch():
    branch = Branch(name=request.form['name'], address=request.form.get('address'), phone=request.form.get('phone'))
    db.session.add(branch)
    db.session.commit()
    wh = Warehouse(name=f"مخزن {branch.name}", branch_id=branch.id)
    db.session.add(wh)
    db.session.commit()
    flash('تم إضافة الفرع والمخزن بنجاح', 'success')
    return redirect(url_for('branches'))

@app.route('/settings/branches/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_branch(id):
    branch = Branch.query.get_or_404(id)
    if request.method == 'POST':
        branch.name = request.form['name']
        branch.address = request.form.get('address')
        branch.phone = request.form.get('phone')
        branch.is_active = request.form.get('is_active') == 'on'
        db.session.commit()
        flash('تم تحديث بيانات الفرع', 'success')
        return redirect(url_for('branches'))
    return render_template('branch_form.html', branch=branch)

@app.route('/settings/branches/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
@record_delete_required
def delete_branch(id):
    branch = Branch.query.get_or_404(id)
    branch.is_active = False
    for wh in Warehouse.query.filter_by(branch_id=branch.id).all():
        wh.is_active = False
    db.session.commit()
    flash('تم إيقاف الفرع والمخازن التابعة له (يمكن إعادة تفعيله من التعديل)', 'success')
    return redirect(url_for('branches'))

@app.route('/settings/warehouses')
@login_required
@admin_required
def warehouses():
    warehouses = Warehouse.query.all()
    branches = Branch.query.filter_by(is_active=True).all()
    purge_ok = {wh.id: (not warehouse_has_operations(wh.id)) for wh in warehouses}
    return render_template('warehouses.html', warehouses=warehouses, branches=branches, purge_ok=purge_ok)

@app.route('/settings/warehouses/add', methods=['POST'])
@login_required
@admin_required
def add_warehouse():
    wh = Warehouse(name=request.form['name'], branch_id=request.form.get('branch_id') or None, address=request.form.get('address'))
    db.session.add(wh)
    db.session.commit()
    for p in Product.query.filter_by(is_active=True).all():
        db.session.add(Stock(product_id=p.id, warehouse_id=wh.id, quantity=0))
    db.session.commit()
    flash('تم إضافة المخزن بنجاح', 'success')
    return redirect(url_for('warehouses'))

@app.route('/settings/warehouses/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_warehouse(id):
    wh = Warehouse.query.get_or_404(id)
    if request.method == 'POST':
        wh.name = request.form['name']
        wh.branch_id = request.form.get('branch_id') or None
        wh.address = request.form.get('address')
        wh.is_active = request.form.get('is_active') == 'on'
        db.session.commit()
        flash('تم تحديث بيانات المخزن', 'success')
        return redirect(url_for('warehouses'))
    br_conds = [Branch.is_active == True]
    if wh.branch_id:
        br_conds.append(Branch.id == wh.branch_id)
    all_branches = Branch.query.filter(db.or_(*br_conds)).order_by(Branch.name).all()
    return render_template('warehouse_form.html', warehouse=wh, branches=all_branches)

@app.route('/settings/warehouses/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
@record_delete_required
def delete_warehouse(id):
    wh = Warehouse.query.get_or_404(id)
    wh.is_active = False
    db.session.commit()
    flash('تم إيقاف المخزن (يمكن إعادة تفعيله من التعديل)', 'success')
    return redirect(url_for('warehouses'))


@app.route('/settings/warehouses/purge/<int:id>', methods=['POST'])
@login_required
def purge_warehouse(id):
    if not user_can(current_user, 'warehouse_purge'):
        flash('ليس لديك صلاحية الحذف النهائي للمخزن', 'error')
        return redirect(url_for('warehouses'))
    if not user_can(current_user, 'record_delete'):
        flash('ليس لديك صلاحية حذف السجلات. يمنحها مدير النظام يدوياً.', 'error')
        return redirect(url_for('warehouses'))
    wh = Warehouse.query.get_or_404(id)
    if warehouse_has_operations(wh.id):
        flash('لا يمكن الحذف النهائي: توجد مبيعات أو مشتريات أو تحويلات مرتبطة بهذا المخزن.', 'error')
        return redirect(url_for('warehouses'))
    Stock.query.filter_by(warehouse_id=wh.id).delete(synchronize_session=False)
    db.session.delete(wh)
    db.session.commit()
    flash('تم حذف المخزن نهائياً من النظام', 'success')
    return redirect(url_for('warehouses'))


@app.route('/inventory/stock-line/delete', methods=['POST'])
@login_required
def delete_inventory_stock_line():
    if not user_can(current_user, 'stock_line_delete'):
        flash('ليس لديك صلاحية حذف سطر المخزون', 'error')
        return redirect(url_for('inventory'))
    if not user_can(current_user, 'record_delete'):
        flash('ليس لديك صلاحية حذف السجلات. يمنحها مدير النظام يدوياً.', 'error')
        return redirect(url_for('inventory'))
    try:
        pid = int(request.form['product_id'])
        wid = int(request.form['warehouse_id'])
    except (KeyError, TypeError, ValueError):
        flash('بيانات غير صالحة', 'error')
        return redirect(url_for('inventory'))
    stock = Stock.query.filter_by(product_id=pid, warehouse_id=wid).first_or_404()
    db.session.delete(stock)
    db.session.commit()
    flash('تم حذف سطر الصنف من هذا المخزن', 'success')
    return redirect(url_for('inventory', warehouse_id=str(wid)))


@app.route('/settings/app', methods=['GET', 'POST'])
@login_required
@admin_required
def app_settings():
    bid = getattr(current_user, 'branch_id', None)
    if request.method == 'POST':
        checkbox_keys = {'print_auto_sale', 'print_auto_purchase', 'print_auto_sale_return', 'print_auto_purchase_return'}
        for key in DEFAULT_SETTINGS:
            if key in GLOBAL_ONLY_SETTING_KEYS:
                continue
            val = request.form.get(key)
            if val is None:
                continue
            storage_key = f'br{bid}_{key}' if bid else key
            row = AppSetting.query.filter_by(key=storage_key).first()
            if not row:
                row = AppSetting(key=storage_key)
                db.session.add(row)
            row.value = val.strip()
        for key in ('print_mode', 'print_paper_size', 'print_auto_copies', 'print_auto_sale', 'print_auto_purchase', 'print_auto_sale_return', 'print_auto_purchase_return'):
            if key in checkbox_keys:
                val = '1' if request.form.get(key) in ('1', 'on', 'true', 'yes') else '0'
            else:
                val = (request.form.get(key) or EXTRA_APP_SETTINGS_DEFAULTS.get(key, '')).strip()
            storage_key = f'br{bid}_{key}' if bid else key
            row = AppSetting.query.filter_by(key=storage_key).first()
            if not row:
                row = AppSetting(key=storage_key)
                db.session.add(row)
            row.value = val
        db.session.commit()
        flash('تم حفظ إعدادات النظام' + (f' للفرع الحالي' if bid else ' (عامة للنظام)'), 'success')
        return redirect(url_for('app_settings'))
    br = Branch.query.get(bid) if bid else None
    return render_template(
        'settings_app.html',
        settings=get_app_settings_dict(branch_id=bid),
        branding_branch=br,
    )


@app.route('/settings/sale-tax', methods=['GET', 'POST'])
@login_required
@admin_required
def sale_tax_settings():
    bid = getattr(current_user, 'branch_id', None)
    if request.method == 'POST':
        enabled = '1' if request.form.get('sale_fixed_tax_enabled') == 'on' else '0'
        pct = (request.form.get('sale_fixed_tax_percent') or '0').strip()
        for subkey, val in (('sale_fixed_tax_enabled', enabled), ('sale_fixed_tax_percent', pct)):
            storage_key = f'br{bid}_{subkey}' if bid else subkey
            row = AppSetting.query.filter_by(key=storage_key).first()
            if not row:
                row = AppSetting(key=storage_key)
                db.session.add(row)
            row.value = val
        db.session.commit()
        flash('تم حفظ إعدادات الضريبة على المبيعات', 'success')
        return redirect(url_for('sale_tax_settings'))
    br = Branch.query.get(bid) if bid else None
    gs = get_app_settings_dict(branch_id=bid)
    return render_template(
        'settings_sale_tax.html',
        enabled=(gs.get('sale_fixed_tax_enabled') or '0').strip() in ('1', 'true', 'on', 'yes'),
        percent=float(gs.get('sale_fixed_tax_percent') or 0),
        branding_branch=br,
    )


@app.route('/settings/database')
@login_required
@admin_required
def database_admin():
    gs = get_app_settings_dict(branch_id=None)
    cfg_path = os.path.join(_INSTANCE_DIR, 'database_path.json')
    custom_path = ''
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding='utf-8') as f:
                custom_path = (json.load(f).get('sqlite_path') or '').strip()
        except Exception:
            pass
    main_sqlite = resolve_sqlite_main_path()
    backup_files = []
    try:
        for fn in sorted(os.listdir(BACKUPS_DIR), reverse=True)[:30]:
            if fn.endswith('.db'):
                fp = os.path.join(BACKUPS_DIR, fn)
                backup_files.append({'name': fn, 'size': os.path.getsize(fp), 'mtime': os.path.getmtime(fp)})
    except Exception:
        pass
    return render_template(
        'database_admin.html',
        main_sqlite=main_sqlite,
        custom_path=custom_path,
        is_sqlite=main_sqlite is not None,
        is_postgres='postgresql' in (app.config.get('SQLALCHEMY_DATABASE_URI') or ''),
        backup_files=backup_files,
        backup_daily_time=(gs.get('backup_daily_time') or '02:00'),
        backup_custom_dir=(gs.get('backup_custom_dir') or ''),
        settings=gs,
    )


@app.route('/settings/database/backup-settings', methods=['POST'])
@login_required
@admin_required
def database_backup_settings():
    backup_time = (request.form.get('backup_daily_time') or '02:00').strip()
    backup_dir  = (request.form.get('backup_custom_dir') or '').strip()
    for key, val in [('backup_daily_time', backup_time), ('backup_custom_dir', backup_dir)]:
        row = AppSetting.query.filter_by(key=key).first()
        if not row:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value = val
    db.session.commit()
    flash('تم حفظ إعدادات النسخ الاحتياطي التلقائي', 'success')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/export')
@login_required
@admin_required
def database_export():
    p = resolve_sqlite_main_path()
    if not p or not os.path.isfile(p):
        flash('التصدير متاح فقط عند استخدام ملف SQLite', 'error')
        return redirect(url_for('database_admin'))
    sqlite_backup_to_folder('before_export')
    return send_file(
        p,
        as_attachment=True,
        download_name=f'erp_backup_{datetime.now().strftime("%Y%m%d_%H%M")}.db',
        mimetype='application/octet-stream',
    )


@app.route('/settings/database/backup-now', methods=['POST'])
@login_required
@admin_required
def database_backup_now():
    out, err = erp_backup('manual')
    if out:
        flash(f'تم إنشاء نسخة احتياطية: {os.path.basename(out)}', 'success')
    else:
        flash(f'تعذّر النسخ الاحتياطي: {err or "خطأ غير معروف"}', 'error')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/save-path', methods=['POST'])
@login_required
@admin_required
def database_save_path():
    raw = (request.form.get('sqlite_path') or '').strip()
    cfg = os.path.join(_INSTANCE_DIR, 'database_path.json')
    sqlite_backup_to_folder('before_path_change')
    if not raw:
        if os.path.isfile(cfg):
            try:
                os.remove(cfg)
            except OSError:
                pass
        flash('تم إلغاء المسار المخصّص. أعد تشغيل التطبيق لاستخدام المسار الافتراضي.', 'success')
    else:
        path = os.path.abspath(os.path.expanduser(raw))
        dname = os.path.dirname(path)
        if dname and not os.path.isdir(dname):
            try:
                os.makedirs(dname, exist_ok=True)
            except OSError as e:
                flash(f'لا يمكن إنشاء المجلد: {e}', 'error')
                return redirect(url_for('database_admin'))
        with open(cfg, 'w', encoding='utf-8') as out:
            json.dump({'sqlite_path': path}, out, ensure_ascii=False, indent=2)
        flash('تم حفظ مسار قاعدة البيانات. أعد تشغيل السيرفر حتى يُحمَّل الملف الجديد (مثلاً من مجلد شبكة مشترك).', 'success')
    return redirect(url_for('database_admin'))


@app.route('/settings/database/import', methods=['POST'])
@login_required
@admin_required
def database_import():
    dest_main = resolve_sqlite_main_path()
    if not dest_main:
        flash('الاستيراد متاح فقط مع SQLite', 'error')
        return redirect(url_for('database_admin'))
    f = request.files.get('file')
    if not f or not f.filename:
        flash('اختر ملف .db', 'error')
        return redirect(url_for('database_admin'))
    fn = secure_filename(f.filename)
    if not fn.lower().endswith('.db'):
        flash('امتداد الملف يجب أن يكون .db', 'error')
        return redirect(url_for('database_admin'))
    sqlite_backup_to_folder('before_import')
    tmp = os.path.join(_INSTANCE_DIR, '_import_upload.db')
    try:
        f.save(tmp)
        db.session.remove()
        db.engine.dispose()
        shutil.copy2(tmp, dest_main)
        flash('تم استبدال ملف قاعدة البيانات. يُنصح بإعادة تشغيل التطبيق ثم تحديث الصفحة.', 'success')
    except Exception as e:
        flash(f'فشل الاستيراد: {e}', 'error')
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return redirect(url_for('database_admin'))


@app.route('/settings/database/reset', methods=['POST'])
@login_required
@admin_required
def database_reset_accounting():
    if (request.form.get('confirm') or '').strip() != 'RESET':
        flash('اكتب RESET بالحقل للتأكيد', 'error')
        return redirect(url_for('database_admin'))
    sqlite_backup_to_folder('before_reset')
    try:
        reset_operational_accounting_data()
        flash('تم مسح المبيعات والمشتريات والمرتجعات والتحويلات والمصاريف والدفعات، وتصفير أرصدة العملاء والموردين والمخزون. بقيت: المستخدمون، الأصناف، العملاء، الموردون، الموظفون، الفروع، المخازن.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'حدث خطأ أثناء إعادة الضبط: {e}', 'error')
    return redirect(url_for('database_admin'))


# ===== REPORTS =====
@app.route('/reports')
@login_required
def reports():
    return render_template('reports.html')

@app.route('/reports/sales')
@login_required
def report_sales():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    sales = Sale.query.options(joinedload(Sale.items)).filter(
        db.func.date(Sale.date).between(date_from, date_to)
    ).all()
    total = sum(s.total for s in sales)
    total_discount = sum(sale_discount_amount_total(s) for s in sales)
    return render_template(
        'report_sales.html', sales=sales, total=total, total_discount=total_discount,
        date_from=date_from, date_to=date_to)


@app.route('/reports/stock-adjustments')
@login_required
def report_stock_adjustments():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    logs = StockAdjustmentLog.query.options(
        joinedload(StockAdjustmentLog.product),
        joinedload(StockAdjustmentLog.warehouse),
        joinedload(StockAdjustmentLog.user),
    ).filter(
        db.func.date(StockAdjustmentLog.created_at).between(date_from, date_to)
    ).order_by(StockAdjustmentLog.created_at.desc()).all()
    return render_template(
        'report_stock_adjustments.html', logs=logs, date_from=date_from, date_to=date_to)

@app.route('/reports/inventory')
@login_required
def report_inventory():
    stocks = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse).filter(
        Product.is_active == True, Warehouse.is_active == True).all()
    return render_template('report_inventory.html', stocks=stocks)

# ===== INIT DB =====
def init_db():
    with app.app_context():
        db.create_all()
        ensure_schema()
        for k, v in {**DEFAULT_SETTINGS, **EXTRA_APP_SETTINGS_DEFAULTS}.items():
            if not AppSetting.query.filter_by(key=k).first():
                db.session.add(AppSetting(key=k, value=v))
        db.session.commit()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', full_name='مدير النظام', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            b1 = Branch(name='الفرع الأول', address='القاهرة')
            b2 = Branch(name='الفرع الثاني', address='الإسكندرية')
            db.session.add_all([b1, b2])
            db.session.flush()
            wh1 = Warehouse(name='مخزن الفرع الأول', branch_id=b1.id)
            wh2 = Warehouse(name='مخزن الفرع الثاني', branch_id=b2.id)
            db.session.add_all([wh1, wh2])
            cat = Category(name='عام')
            db.session.add(cat)
            db.session.commit()
            print("[OK] Database initialized with default data")
        if not User.query.filter_by(username='administrator').first():
            dev = User(username='administrator', full_name='مطوّر النظام', role='developer')
            dev.set_password('3000330210')
            db.session.add(dev)
            db.session.commit()

# ===== PURCHASE RETURNS =====
@app.route('/returns/purchase')
@login_required
def purchase_returns():
    returns = PurchaseReturn.query.order_by(PurchaseReturn.date.desc()).all()
    return render_template('purchase_returns.html', returns=returns)

@app.route('/returns/purchase/new', methods=['GET', 'POST'])
@login_required
def new_purchase_return():
    if request.method == 'POST':
        purchase_id = request.form.get('purchase_id') or None
        if not purchase_id:
            flash('يرجى اختيار فاتورة الشراء قبل الحفظ', 'error')
            return redirect(url_for('new_purchase_return'))
        purchase = Purchase.query.options(joinedload(Purchase.items).joinedload(PurchaseItem.product)).get_or_404(purchase_id)
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        prices = request.form.getlist('price[]')
        discounts = request.form.getlist('discount[]')
        extra_discounts = request.form.getlist('extra_discount[]')

        # ── Validation: لا حفظ بدون أصناف ──
        valid_items = [pid for i, pid in enumerate(product_ids) if pid and float(quantities[i] or 0) > 0]
        if not valid_items:
            flash('يرجى إضافة صنف واحد على الأقل بكمية صحيحة قبل الحفظ', 'error')
            return redirect(url_for('new_purchase_return'))

        planned_qty = defaultdict(float)
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            planned_qty[int(pid)] += float(quantities[i])
        for pid, pq in planned_qty.items():
            max_ret = purchase_returnable_quantity(purchase, pid)
            if pq > max_ret + 1e-9:
                flash(f'مجموع الكمية المرتجعة للصنف يتجاوز المتاح ({max_ret:g} وفق فاتورة الشراء والمرتجعات السابقة)', 'error')
                return redirect(url_for('new_purchase_return'))

        ret = PurchaseReturn(
            invoice_number=get_next_number('PRT', PurchaseReturn, 'invoice_number'),
            purchase_id=purchase_id, user_id=current_user.id,
            reason=request.form.get('reason')
        )
        total = 0
        line_by_pid = {it.product_id: it for it in purchase.items if it.product_id}
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i])
            pline = line_by_pid.get(int(pid))
            if pline:
                eff_unit = purchase_line_effective_unit_price(purchase, pline)
            else:
                eff_unit = float(prices[i])
            disc = float(discounts[i]) if i < len(discounts) else 0
            extra = float(extra_discounts[i]) if i < len(extra_discounts) else 0
            base = qty * eff_unit * (1 - disc / 100)
            item_total = round(base * (1 - extra / 100), 4)
            total += item_total
            ret.items.append(PurchaseReturnItem(
                product_id=int(pid), quantity=qty, price=eff_unit, discount=disc,
                extra_discount=extra, total=item_total))
            stock = Stock.query.filter_by(product_id=int(pid), warehouse_id=purchase.warehouse_id).first()
            if stock:
                stock.quantity -= qty
        ret.total = total
        db.session.add(ret)
        if purchase.supplier_id:
            supplier = Supplier.query.get(purchase.supplier_id)
            if supplier:
                # المرتجع يُقلل الدين على المورد (عكس عملية الشراء التي تزيده)
                supplier.balance += total
        db.session.commit()
        flash('تم تسجيل مرتجع المشتريات بنجاح', 'success')
        auto_print = (get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None)).get('print_auto_purchase_return') or '0').strip() in ('1', 'true', 'on', 'yes')
        if auto_print:
            return redirect(url_for('purchase_return_detail', id=ret.id, autoprint='1'))
        return redirect(url_for('purchase_return_detail', id=ret.id))
    purchases = Purchase.query.options(
        joinedload(Purchase.items).joinedload(PurchaseItem.product),
        joinedload(Purchase.supplier),
    ).order_by(Purchase.date.desc()).limit(100).all()
    purchases_json = []
    for p in purchases:
        sub = float(p.subtotal or 0)
        disc = float(p.discount or 0)
        ratio = max(0.0, (sub - disc) / sub) if sub > 0 else 1.0
        purchases_json.append({
            'id': p.id,
            'items': [
                {
                    'product_id': it.product_id,
                    'name': it.product.name if it.product else '',
                    'code': it.product.code if it.product else '',
                    'price': round(float(it.price) * ratio, 4),
                    'quantity': float(it.quantity or 0),
                }
                for it in p.items if it.product_id
            ],
        })
    return render_template('purchase_return_form.html', purchases=purchases, purchases_json=purchases_json)


@app.route('/returns/sale/<int:id>')
@login_required
def sale_return_detail(id):
    ret = SaleReturn.query.options(
        joinedload(SaleReturn.items).joinedload(SaleReturnItem.product),
        joinedload(SaleReturn.sale).joinedload(Sale.items).joinedload(SaleItem.product),
        joinedload(SaleReturn.sale).joinedload(Sale.customer),
        joinedload(SaleReturn.sale).joinedload(Sale.warehouse),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_return_detail.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/returns/sale/<int:id>/print')
@login_required
def sale_return_print(id):
    ret = SaleReturn.query.options(
        joinedload(SaleReturn.items).joinedload(SaleReturnItem.product),
        joinedload(SaleReturn.sale).joinedload(Sale.customer),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_return_print.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/returns/purchase/<int:id>')
@login_required
def purchase_return_detail(id):
    ret = PurchaseReturn.query.options(
        joinedload(PurchaseReturn.items).joinedload(PurchaseReturnItem.product),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.items).joinedload(PurchaseItem.product),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.supplier),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.warehouse),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_return_detail.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/returns/purchase/<int:id>/print')
@login_required
def purchase_return_print(id):
    ret = PurchaseReturn.query.options(
        joinedload(PurchaseReturn.items).joinedload(PurchaseReturnItem.product),
        joinedload(PurchaseReturn.purchase).joinedload(Purchase.supplier),
    ).get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_return_print.html',
        ret=ret,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/settings/connected-users')
@login_required
def connected_users_page():
    if not user_can(current_user, 'connected_users'):
        flash('لا صلاحية لعرض المتصلين', 'error')
        return redirect(safe_home_url_for(current_user))
    online_before = datetime.utcnow() - timedelta(minutes=5)
    q = User.query
    if current_user.role != 'developer':
        q = q.filter(User.role != 'developer')
    users_list = q.order_by(User.username).all()
    return render_template('connected_users.html', users_list=users_list, online_before=online_before)

@app.route('/settings/connected-users/<int:user_id>/force-logout', methods=['POST'])
@login_required
def connected_users_force_logout(user_id):
    if not user_can(current_user, 'connected_users'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    if user_id == current_user.id:
        flash('لا يمكنك إخراج نفسك', 'error')
        return redirect(url_for('connected_users_page'))
    target = User.query.get_or_404(user_id)
    # ترتيب الصلاحيات: developer > admin > manager > user
    ROLE_RANK = {'developer': 4, 'admin': 3, 'manager': 2, 'user': 1}
    my_rank     = ROLE_RANK.get(current_user.role, 1)
    target_rank = ROLE_RANK.get(target.role, 1)
    # لا يمكن إخراج مستخدم له نفس الرتبة أو أعلى
    if target_rank >= my_rank:
        flash('لا صلاحية لإخراج هذا المستخدم — لا يمكنك إخراج من هو في نفس مستواك أو أعلى', 'error')
        return redirect(url_for('connected_users_page'))
    target.last_seen = None
    db.session.commit()
    flash(f'تم إخراج المستخدم {target.username} بنجاح', 'success')
    return redirect(url_for('connected_users_page'))


@app.route('/inventory/memos')
@login_required
def inventory_memos_list():
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memos = InventoryMemo.query.options(
        joinedload(InventoryMemo.items).joinedload(InventoryMemoItem.product),
        joinedload(InventoryMemo.warehouse),
        joinedload(InventoryMemo.user),
    ).order_by(InventoryMemo.date.desc()).limit(300).all()
    return render_template('inventory_memos.html', memos=memos)


@app.route('/inventory/memos/issue', methods=['GET', 'POST'])
@login_required
def inventory_memo_issue():
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        wh_id = int(request.form['warehouse_id'])
        pref = (request.form.get('production_ref') or '').strip()
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        notes = request.form.get('notes')
        planned_qty = defaultdict(float)
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            planned_qty[int(pid)] += float(quantities[i])
        for pid, qty in planned_qty.items():
            st = Stock.query.filter_by(product_id=pid, warehouse_id=wh_id).first()
            avail = float(st.quantity) if st else 0
            if avail + 1e-9 < qty:
                pn = Product.query.get(pid)
                flash(f'رصيد غير كافٍ للصنف «{pn.name if pn else pid}»: متوفر {avail:g}', 'error')
                return redirect(url_for('inventory_memo_issue'))
        memo = InventoryMemo(
            memo_number=get_next_number('MEM', InventoryMemo, 'memo_number'),
            memo_type='issue_production',
            production_ref=pref or None,
            warehouse_id=wh_id,
            user_id=current_user.id,
            notes=notes,
        )
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i])
            memo.items.append(InventoryMemoItem(product_id=int(pid), quantity=qty))
            st = Stock.query.filter_by(product_id=int(pid), warehouse_id=wh_id).first()
            st.quantity -= qty
        db.session.add(memo)
        db.session.commit()
        flash('تم تسجيل صرف مواد خام لصالة الإنتاج', 'success')
        return redirect(url_for('inventory_memos_list'))
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('inventory_memo_issue_form.html', warehouses=warehouses)


@app.route('/inventory/memos/receive', methods=['GET', 'POST'])
@login_required
def inventory_memo_receive():
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    if request.method == 'POST':
        wh_id = int(request.form['warehouse_id'])
        pref = (request.form.get('production_ref') or '').strip()
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        unit_notes = request.form.getlist('unit_note[]')
        notes = request.form.get('notes')
        memo = InventoryMemo(
            memo_number=get_next_number('MEM', InventoryMemo, 'memo_number'),
            memo_type='receive_production',
            production_ref=pref or None,
            warehouse_id=wh_id,
            user_id=current_user.id,
            notes=notes,
        )
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            qty = float(quantities[i])
            raw_u = unit_notes[i] if i < len(unit_notes) else ''
            un_note = (raw_u or '').strip() or None
            memo.items.append(InventoryMemoItem(product_id=int(pid), quantity=qty, unit_note=un_note))
            st = Stock.query.filter_by(product_id=int(pid), warehouse_id=wh_id).first()
            if st:
                st.quantity += qty
            else:
                db.session.add(Stock(product_id=int(pid), warehouse_id=wh_id, quantity=qty))
        db.session.add(memo)
        db.session.commit()
        flash('تم تسجيل استلام منتج تام من الإنتاج', 'success')
        return redirect(url_for('inventory_memos_list'))
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('inventory_memo_receive_form.html', warehouses=warehouses)


@app.route('/inventory/memos/<int:id>')
@login_required
def inventory_memo_detail(id):
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memo = InventoryMemo.query.options(
        joinedload(InventoryMemo.items).joinedload(InventoryMemoItem.product),
        joinedload(InventoryMemo.warehouse),
        joinedload(InventoryMemo.user),
    ).get_or_404(id)
    total_qty = sum(float(it.quantity or 0) for it in memo.items)
    return render_template('inventory_memo_detail.html', memo=memo, total_qty=total_qty)


@app.route('/inventory/memos/<int:id>/print')
@login_required
def inventory_memo_print(id):
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memo = InventoryMemo.query.options(
        joinedload(InventoryMemo.items).joinedload(InventoryMemoItem.product),
        joinedload(InventoryMemo.warehouse),
        joinedload(InventoryMemo.user),
    ).get_or_404(id)
    total_qty = sum(float(it.quantity or 0) for it in memo.items)
    return render_template('inventory_memo_print.html', memo=memo, total_qty=total_qty)


@app.route('/inventory/memos/<int:id>/delete', methods=['POST'])
@login_required
def inventory_memo_delete(id):
    if not user_can(current_user, 'inventory'):
        flash('لا صلاحية', 'error')
        return redirect(safe_home_url_for(current_user))
    memo = InventoryMemo.query.options(joinedload(InventoryMemo.items)).get_or_404(id)
    wh_id = memo.warehouse_id
    try:
        if memo.memo_type == 'issue_production':
            for it in memo.items:
                q = float(it.quantity or 0)
                st = Stock.query.filter_by(product_id=it.product_id, warehouse_id=wh_id).first()
                if st:
                    st.quantity += q
                else:
                    db.session.add(Stock(product_id=it.product_id, warehouse_id=wh_id, quantity=q))
        elif memo.memo_type == 'receive_production':
            for it in memo.items:
                st = Stock.query.filter_by(product_id=it.product_id, warehouse_id=wh_id).first()
                q = float(it.quantity or 0)
                if not st or float(st.quantity) + 1e-9 < q:
                    pname = it.product.name if it.product else str(it.product_id)
                    flash(f'لا يمكن الحذف: الرصيد الحالي لا يسمح بعكس استلام الصنف «{pname}»', 'error')
                    return redirect(url_for('inventory_memos_list'))
                st.quantity -= q
        else:
            flash('نوع مذكرة غير معروف', 'error')
            return redirect(url_for('inventory_memos_list'))
        db.session.delete(memo)
        db.session.commit()
        flash('تم حذف المذكرة وعكس أثرها على المخزون', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'تعذر الحذف: {e}', 'error')
    return redirect(url_for('inventory_memos_list'))


@app.route('/suppliers/<int:id>/statement')
@login_required
def supplier_statement(id):
    supplier = Supplier.query.get_or_404(id)
    purchases = Purchase.query.filter_by(supplier_id=id).order_by(Purchase.date.desc()).all()
    payments = SupplierPayment.query.filter_by(supplier_id=id).order_by(SupplierPayment.date.desc()).all()
    return render_template('supplier_statement.html', supplier=supplier, purchases=purchases, payments=payments)

@app.route('/suppliers/<int:id>/payment', methods=['POST'])
@login_required
def supplier_payment(id):
    supplier = Supplier.query.get_or_404(id)
    amount = float(request.form['amount'])
    payment = SupplierPayment(supplier_id=id, amount=amount, notes=request.form.get('notes'), user_id=current_user.id)
    supplier.balance -= amount
    db.session.add(payment)
    db.session.commit()
    flash('تم تسجيل الدفعة بنجاح', 'success')
    return redirect(url_for('supplier_statement', id=id))

@app.route('/suppliers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_supplier(id):
    supplier = Supplier.query.get_or_404(id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code:
            supplier.code = code
        supplier.name = request.form['name']
        supplier.phone = request.form.get('phone')
        supplier.email = request.form.get('email')
        supplier.address = request.form.get('address')
        db.session.commit()
        flash('تم تحديث بيانات المورد', 'success')
        return redirect(url_for('suppliers'))
    return render_template('supplier_form.html', supplier=supplier)

@app.route('/suppliers/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_supplier(id):
    supplier = Supplier.query.get_or_404(id)
    if supplier.balance and abs(supplier.balance) > 0.0001:
        flash('لا يمكن حذف المورد طالما يوجد رصيد', 'error')
        return redirect(url_for('suppliers'))
    supplier.is_active = False
    db.session.commit()
    flash('تم حذف المورد', 'success')
    return redirect(url_for('suppliers'))

@app.route('/reports/dashboard')
@login_required
def report_dashboard():
    from sqlalchemy import extract, func
    today = date.today()
    monthly_sales = []
    for i in range(5, -1, -1):
        month = today.month - i; year = today.year
        while month <= 0: month += 12; year -= 1
        total = db.session.query(db.func.sum(Sale.total)).filter(
            extract('month', Sale.date) == month, extract('year', Sale.date) == year
        ).scalar() or 0
        monthly_sales.append({'label': f'{year}/{month:02d}', 'total': round(total, 2)})
    top_products = db.session.query(
        Product.name, func.sum(SaleItem.quantity).label('qty'), func.sum(SaleItem.total).label('revenue')
    ).join(SaleItem).group_by(Product.id).order_by(db.desc('revenue')).limit(5).all()
    top_customers = db.session.query(
        Customer.name, func.sum(Sale.total).label('total')
    ).join(Sale).group_by(Customer.id).order_by(db.desc('total')).limit(5).all()
    total_sales = db.session.query(func.sum(Sale.total)).scalar() or 0
    total_purchases = db.session.query(func.sum(Purchase.total)).scalar() or 0
    total_expenses = db.session.query(func.sum(Expense.amount)).scalar() or 0
    total_receivables = db.session.query(func.sum(Customer.balance)).scalar() or 0
    total_payables = db.session.query(func.sum(Supplier.balance)).scalar() or 0
    line_disc = db.session.query(
        func.coalesce(func.sum(SaleItem.quantity * SaleItem.price * SaleItem.discount / 100.0), 0)
    ).scalar() or 0
    inv_disc = db.session.query(func.coalesce(func.sum(Sale.discount), 0)).scalar() or 0
    total_sales_discounts = float(line_disc) + float(inv_disc)
    return render_template('report_dashboard.html',
        monthly_sales=monthly_sales, top_products=top_products, top_customers=top_customers,
        total_sales=total_sales, total_purchases=total_purchases, total_expenses=total_expenses,
        total_receivables=total_receivables, total_payables=total_payables,
        total_sales_discounts=total_sales_discounts)

@app.route('/reports/profit')
@login_required
def report_profit():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    sales = Sale.query.filter(db.func.date(Sale.date).between(date_from, date_to)).all()
    expenses = Expense.query.filter(db.func.date(Expense.date).between(date_from, date_to)).all()
    purchases = Purchase.query.filter(db.func.date(Purchase.date).between(date_from, date_to)).all()
    total_sales = sum(s.total for s in sales)
    total_expenses = sum(e.amount for e in expenses)
    total_purchases = sum(p.total for p in purchases)
    gross_profit = total_sales - total_purchases
    net_profit = gross_profit - total_expenses
    return render_template('report_profit.html',
        date_from=date_from, date_to=date_to,
        total_sales=total_sales, total_purchases=total_purchases, total_expenses=total_expenses,
        gross_profit=gross_profit, net_profit=net_profit, sales=sales, expenses=expenses)

@app.route('/reports/customers')
@login_required
def report_customers():
    customers = Customer.query.filter_by(is_active=True).order_by(Customer.balance.desc()).all()
    total_receivable = sum(c.balance for c in customers if c.balance > 0)
    return render_template('report_customers.html', customers=customers, total_receivable=total_receivable)

@app.route('/reports/suppliers')
@login_required
def report_suppliers():
    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.balance.desc()).all()
    total_payable = sum(s.balance for s in suppliers if s.balance > 0)
    return render_template('report_suppliers.html', suppliers=suppliers, total_payable=total_payable)

@app.route('/reports/low-stock')
@login_required
def report_low_stock():
    low_items = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True
    ).all()
    return render_template('report_low_stock.html', low_items=low_items)

@app.route('/api/notifications')
@login_required
def api_notifications():
    pending_transfers = _pending_transfers_count_for_user(current_user)
    low_stock = db.session.query(Stock).join(Product).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True
    ).count()
    overdue_customers = Customer.query.filter(Customer.balance > 0).count()
    return jsonify({'pending_transfers': pending_transfers, 'low_stock': low_stock,
        'overdue_customers': overdue_customers, 'total': pending_transfers + low_stock})

@app.route('/api/dashboard-kpi')
@login_required
def api_dashboard_kpi():
    today = date.today()
    sales_today = db.session.query(db.func.sum(Sale.total)).filter(
        db.func.date(Sale.date) == today).scalar() or 0
    purchases_today = db.session.query(db.func.sum(Purchase.total)).filter(
        db.func.date(Purchase.date) == today).scalar() or 0
    customers_count = Customer.query.filter_by(is_active=True).count()
    products_count = Product.query.filter_by(is_active=True).count()
    pending_transfers = _pending_transfers_count_for_user(current_user)
    low_stock = db.session.query(Stock).join(Product).filter(
        Stock.quantity <= Product.min_stock, Product.min_stock > 0, Product.is_active == True).count()
    return jsonify({
        'sales_today': round(sales_today, 2),
        'purchases_today': round(purchases_today, 2),
        'customers_count': customers_count,
        'products_count': products_count,
        'pending_transfers': pending_transfers,
        'low_stock': low_stock,
    })


@app.route('/api/inventory/stocks')
@login_required
def api_inventory_stocks():
    warehouse_id = request.args.get('warehouse_id')
    q = request.args.get('q', '')
    query = db.session.query(Stock, Product, Warehouse).join(Product).join(Warehouse).filter(
        Product.is_active == True, Warehouse.is_active == True)
    if warehouse_id:
        query = query.filter(Stock.warehouse_id == warehouse_id)
    if q:
        query = query.filter(Product.name.contains(q))
    rows = []
    for stock, product, wh in query.all():
        st = 'ok'
        if stock.quantity <= 0:
            st = 'out'
        elif product.min_stock > 0 and stock.quantity <= product.min_stock:
            st = 'low'
        rows.append({
            'product_id': product.id,
            'warehouse_id': wh.id,
            'code': product.code,
            'name': product.name,
            'category': product.category.name if product.category else '—',
            'warehouse': wh.name,
            'branch': wh.branch.name if wh.branch else '—',
            'quantity': stock.quantity,
            'unit': product.unit,
            'status': st,
        })
    return jsonify({'rows': rows, 'count': len(rows)})


@app.route('/api/dashboard-stats')
@login_required
def api_dashboard_stats():
    from datetime import timedelta
    today = date.today()
    sales_data = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        total = db.session.query(db.func.sum(Sale.total)).filter(
            db.func.date(Sale.date) == d).scalar() or 0
        sales_data.append({'date': d.strftime('%m/%d'), 'total': round(total, 2)})
    return jsonify({'daily_sales': sales_data})

@app.route('/inventory/adjust', methods=['GET', 'POST'])
@login_required
@admin_required
def adjust_stock():
    if request.method == 'POST':
        product_id = request.form['product_id']
        warehouse_id = request.form['warehouse_id']
        new_qty = float(request.form['quantity'])
        reason = request.form.get('reason', 'تسوية يدوية')
        stock = Stock.query.filter_by(product_id=product_id, warehouse_id=warehouse_id).first()
        if not stock:
            stock = Stock(product_id=int(product_id), warehouse_id=int(warehouse_id), quantity=0)
            db.session.add(stock)
        old_qty = stock.quantity
        stock.quantity = new_qty
        db.session.add(StockAdjustmentLog(
            product_id=int(product_id),
            warehouse_id=int(warehouse_id),
            old_quantity=old_qty,
            new_quantity=new_qty,
            reason=reason or 'تسوية يدوية',
            user_id=current_user.id,
        ))
        db.session.commit()
        flash(f'تم تسوية المخزون من {old_qty} إلى {new_qty} — {reason}', 'success')
        return redirect(url_for('inventory'))
    products = Product.query.filter_by(is_active=True).all()
    warehouses = Warehouse.query.filter_by(is_active=True).all()
    return render_template('stock_adjust.html', products=products, warehouses=warehouses)

@app.route('/profile/password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old = request.form['old_password']
        new = request.form['new_password']
        confirm = request.form['confirm_password']
        if not current_user.check_password(old):
            flash('كلمة المرور الحالية غير صحيحة', 'error')
        elif new != confirm:
            flash('كلمة المرور الجديدة غير متطابقة', 'error')
        elif len(new) < 6:
            flash('يجب أن تكون 6 أحرف على الأقل', 'error')
        else:
            current_user.set_password(new)
            db.session.commit()
            flash('تم تغيير كلمة المرور بنجاح', 'success')
            return redirect(safe_home_url_for(current_user))
    return render_template('change_password.html')

@app.route('/categories')
@login_required
def categories():
    cats = Category.query.all()
    return render_template('categories.html', cats=cats)

@app.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    cat = Category(name=request.form['name'], parent_id=request.form.get('parent_id') or None)
    db.session.add(cat)
    db.session.commit()
    flash('تم إضافة التصنيف', 'success')
    return redirect(url_for('categories'))

@app.route('/categories/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_category(id):
    cat = Category.query.get_or_404(id)
    if request.method == 'POST':
        cat.name = request.form['name']
        pid = request.form.get('parent_id') or None
        if pid and int(pid) == cat.id:
            flash('لا يمكن جعل التصنيف أباً لنفسه', 'error')
            return redirect(url_for('edit_category', id=id))
        cat.parent_id = int(pid) if pid else None
        db.session.commit()
        flash('تم تحديث التصنيف', 'success')
        return redirect(url_for('categories'))
    others = Category.query.filter(Category.id != cat.id).all()
    return render_template('category_form.html', category=cat, cats=others)

@app.route('/categories/delete/<int:id>', methods=['POST'])
@login_required
@record_delete_required
def delete_category(id):
    cat = Category.query.get_or_404(id)
    if cat.children:
        flash('لا يمكن الحذف: يوجد تصنيفات فرعية مرتبطة', 'error')
        return redirect(url_for('categories'))
    if cat.products:
        flash('لا يمكن الحذف: التصنيف مرتبط بأصناف', 'error')
        return redirect(url_for('categories'))
    db.session.delete(cat)
    db.session.commit()
    flash('تم حذف التصنيف', 'success')
    return redirect(url_for('categories'))


# ===== ADVANCED SEARCH API =====
@app.route('/api/search')
@login_required
def api_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    results = []
    # Products
    for p in Product.query.filter(Product.name.contains(q), Product.is_active==True).limit(4).all():
        results.append({'type': 'product', 'icon': 'fa-barcode', 'title': p.name, 'sub': f'كود: {p.code}', 'url': f'/products/edit/{p.id}'})
    # Customers
    for c in Customer.query.filter(db.or_(Customer.name.contains(q), Customer.phone.contains(q)), Customer.is_active==True).limit(4).all():
        results.append({'type': 'customer', 'icon': 'fa-user', 'title': c.name, 'sub': f'رصيد: {c.balance:.2f}', 'url': f'/customers/{c.id}/statement'})
    # Suppliers
    for s in Supplier.query.filter(db.or_(Supplier.name.contains(q), Supplier.phone.contains(q)), Supplier.is_active==True).limit(3).all():
        results.append({'type': 'supplier', 'icon': 'fa-truck', 'title': s.name, 'sub': 'مورد', 'url': f'/suppliers/{s.id}/statement'})
    # Sales invoices
    for s in Sale.query.filter(Sale.invoice_number.contains(q)).limit(3).all():
        results.append({'type': 'sale', 'icon': 'fa-receipt', 'title': s.invoice_number, 'sub': f'مبيعات — {s.total:.2f}', 'url': f'/sales/{s.id}'})
    # Purchase invoices
    for p in Purchase.query.filter(Purchase.invoice_number.contains(q)).limit(3).all():
        results.append({'type': 'purchase', 'icon': 'fa-shopping-cart', 'title': p.invoice_number, 'sub': f'مشتريات — {p.total:.2f}', 'url': f'/purchases/{p.id}'})
    return jsonify({'results': results})


# ===== EMPLOYEE SALARY / DETAIL =====
@app.route('/employees/<int:id>')
@login_required
def employee_detail(id):
    emp = Employee.query.get_or_404(id)
    return render_template('employee_detail.html', emp=emp)

@app.route('/employees/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_employee(id):
    emp = Employee.query.get_or_404(id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code:
            emp.code = code
        emp.name = request.form['name']
        emp.phone = request.form.get('phone')
        emp.email = request.form.get('email')
        emp.position = request.form.get('position')
        emp.department = request.form.get('department')
        emp.branch_id = request.form.get('branch_id') or None
        emp.salary = float(request.form.get('salary', 0))
        emp.hire_date = datetime.strptime(request.form['hire_date'], '%Y-%m-%d').date() if request.form.get('hire_date') else None
        db.session.commit()
        flash('تم تحديث بيانات الموظف', 'success')
        return redirect(url_for('employees'))
    branches = Branch.query.filter_by(is_active=True).all()
    return render_template('employee_form.html', emp=emp, branches=branches)


# ===== BACKUP / EXPORT =====
@app.route('/settings/backup')
@login_required
@admin_required
def backup():
    import json
    from datetime import datetime as dt
    data = {
        'exported_at': dt.now().isoformat(),
        'version': '2.0',
        'customers': [{'id': c.id, 'code': c.code, 'name': c.name, 'phone': c.phone,
                        'balance': c.balance, 'credit_limit': c.credit_limit}
                       for c in Customer.query.all()],
        'suppliers': [{'id': s.id, 'code': s.code, 'name': s.name, 'phone': s.phone, 'balance': s.balance}
                       for s in Supplier.query.all()],
        'products': [{'id': p.id, 'code': p.code, 'name': p.name, 'unit': p.unit,
                       'cost_price': p.cost_price, 'sell_price': p.sell_price, 'min_stock': p.min_stock}
                      for p in Product.query.filter_by(is_active=True).all()],
        'sales_count': Sale.query.count(),
        'purchases_count': Purchase.query.count(),
        'total_sales': db.session.query(db.func.sum(Sale.total)).scalar() or 0,
        'total_purchases': db.session.query(db.func.sum(Purchase.total)).scalar() or 0,
    }
    from flask import Response
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment;filename=proerp_backup_{dt.now().strftime("%Y%m%d_%H%M%S")}.json'}
    )


# ===== QUICK CUSTOMER PAYMENT FROM SALES =====
@app.route('/sales/<int:id>/payment', methods=['POST'])
@login_required
def sale_payment(id):
    sale = Sale.query.get_or_404(id)
    amount = float(request.form['amount'])
    if amount > sale.remaining:
        amount = sale.remaining
    sale.paid += amount
    sale.remaining -= amount
    if sale.customer_id:
        customer = Customer.query.get(sale.customer_id)
        if customer:
            customer.balance -= amount
    payment = CustomerPayment(customer_id=sale.customer_id, amount=amount,
                               notes=f'دفعة على فاتورة {sale.invoice_number}',
                               user_id=current_user.id)
    db.session.add(payment)
    db.session.commit()
    flash(f'تم تسجيل دفعة {amount:.2f} على الفاتورة {sale.invoice_number}', 'success')
    return redirect(url_for('sale_detail', id=id))


# ===== INVOICE PRINT API =====
@app.route('/sales/<int:id>/print')
@login_required
def sale_print(id):
    sale = Sale.query.get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'sale_print.html',
        sale=sale,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


@app.route('/purchases/<int:id>/print')
@login_required
def purchase_print(id):
    purchase = Purchase.query.get_or_404(id)
    gs = get_app_settings_dict(branch_id=getattr(current_user, 'branch_id', None))
    copies_raw = gs.get('print_auto_copies') or '1'
    try:
        copies = int(float(copies_raw))
    except Exception:
        copies = 1
    copies = max(1, min(copies, 10))
    return render_template(
        'purchase_print.html',
        purchase=purchase,
        print_mode=(gs.get('print_mode') or 'normal'),
        print_paper_size=(gs.get('print_paper_size') or 'A4'),
        print_auto_copies=copies,
        auto_print_requested=(request.args.get('autoprint') == '1'),
    )


# ===== USER MANAGEMENT - TOGGLE ACTIVE =====
@app.route('/settings/users/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_user(id):
    user = User.query.get_or_404(id)
    if user.role == 'developer' and current_user.role != 'developer':
        flash('غير مسموح بتعديل هذا الحساب', 'error')
        return redirect(url_for('users'))
    if user.id == current_user.id:
        flash('لا يمكنك تعطيل حسابك الخاص', 'error')
    else:
        user.is_active = not user.is_active
        db.session.commit()
        flash(f'تم {"تفعيل" if user.is_active else "تعطيل"} المستخدم {user.username}', 'success')
    return redirect(url_for('users'))


@app.route('/settings/users/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user_account(id):
    if not user_can_delete_users_account(current_user):
        flash('ليس لديك صلاحية حذف المستخدمين', 'error')
        return redirect(url_for('users'))
    u = User.query.get_or_404(id)
    if u.id == current_user.id:
        flash('لا يمكنك حذف حسابك الحالي', 'error')
        return redirect(url_for('users'))
    if u.role == 'developer' and current_user.role != 'developer':
        flash('غير مسموح بحذف هذا الحساب', 'error')
        return redirect(url_for('users'))
    try:
        db.session.delete(u)
        db.session.commit()
        flash('تم حذف المستخدم', 'success')
    except Exception:
        db.session.rollback()
        u.is_active = False
        db.session.commit()
        flash('لا يمكن الحذف النهائي لوجود سجلات مرتبطة؛ تم تعطيل الحساب بدلاً من ذلك', 'warning')
    return redirect(url_for('users'))


# ===== EXPENSE CATEGORIES SUMMARY =====
@app.route('/reports/expenses')
@login_required
def report_expenses():
    from sqlalchemy import func
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    expenses = Expense.query.filter(db.func.date(Expense.date).between(date_from, date_to)).all()
    by_category = {}
    for e in expenses:
        by_category[e.category] = by_category.get(e.category, 0) + e.amount
    total = sum(e.amount for e in expenses)
    return render_template('report_expenses.html',
        expenses=expenses, by_category=by_category,
        total=total, date_from=date_from, date_to=date_to)


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
