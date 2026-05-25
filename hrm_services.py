"""خدمات وحدة الموارد البشرية — رواتب، حضور، محاسبة، إشعارات."""
from datetime import datetime, date, time, timedelta
from calendar import monthrange
import csv
import io
import os


# حسابات افتراضية للقيود المحاسبية
ACC_SALARY_EXPENSE = ('6100', 'مصروف الرواتب')
ACC_SALARIES_PAYABLE = ('2100', 'رواتب مستحقة الدفع')
ACC_CASH = ('1100', 'الصندوق')
ACC_BANK = ('1200', 'البنك')
ACC_EMPLOYEE_LOANS = ('1300', 'سلف الموظفين')
ACC_BONUSES_EXPENSE = ('6110', 'مكافآت الموظفين')
ACC_DEDUCTIONS = ('2190', 'خصومات مستحقة')


def hrm_employee_count(models, db, active_only=True):
    from app import Employee
    q = Employee.query
    if active_only:
        q = q.filter_by(is_active=True)
    return q.count()


def department_employee_count(dept_id, models):
    from app import Employee
    return Employee.query.filter_by(department_id=dept_id, is_active=True).count()


def calc_working_hours(check_in, check_out):
    if not check_in or not check_out:
        return 0.0
    d = date.today()
    t1 = datetime.combine(d, check_in)
    t2 = datetime.combine(d, check_out)
    if t2 < t1:
        t2 += timedelta(days=1)
    return round((t2 - t1).total_seconds() / 3600, 2)


def attendance_dashboard_today(models, db):
    HrmAttendance = models['HrmAttendance']
    from app import Employee
    today = date.today()
    active_ids = {e.id for e in Employee.query.filter_by(is_active=True).all()}
    records = HrmAttendance.query.filter_by(att_date=today).all()
    present = sum(1 for r in records if r.status in ('present', 'late') and r.employee_id in active_ids)
    on_leave = sum(1 for r in records if r.status == 'leave')
    late = sum(1 for r in records if r.status == 'late' or (r.delay_minutes or 0) > 0)
    absent = max(0, len(active_ids) - present - on_leave)
    return {'present': present, 'absent': absent, 'late': late, 'leave': on_leave}


def create_journal_entry(db, models, user_id, ref_type, ref_id, description, lines):
    """lines: list of (account_code, account_name, debit, credit)"""
    HrmJournalEntry = models['HrmJournalEntry']
    HrmJournalLine = models['HrmJournalLine']
    total_d = sum(l[2] for l in lines)
    total_c = sum(l[3] for l in lines)
    entry = HrmJournalEntry(
        reference_type=ref_type,
        reference_id=ref_id,
        description=description,
        total_debit=total_d,
        total_credit=total_c,
        created_by=user_id,
    )
    db.session.add(entry)
    db.session.flush()
    for code, name, debit, credit in lines:
        db.session.add(HrmJournalLine(
            entry_id=entry.id,
            account_code=code,
            account_name=name,
            debit=debit,
            credit=credit,
        ))
    return entry


def accrue_payroll_journal(db, models, payroll, user_id):
    """اعتماد الرواتب: مدين مصروف الرواتب — دائن رواتب مستحقة."""
    amount = payroll.total_net or payroll.total_gross or 0
    if amount <= 0:
        return None
    entry = create_journal_entry(
        db, models, user_id, 'payroll_accrual', payroll.id,
        f'استحقاق رواتب {payroll.period_year}/{payroll.period_month:02d}',
        [
            (*ACC_SALARY_EXPENSE, amount, 0),
            (*ACC_SALARIES_PAYABLE, 0, amount),
        ],
    )
    payroll.journal_accrual_id = entry.id
    return entry


def pay_payroll_journal(db, models, payroll, user_id, payment_method='cash'):
    """صرف الرواتب: مدين رواتب مستحقة — دائن صندوق/بنك + مصروف في النظام."""
    from app import Expense
    amount = payroll.total_net or 0
    if amount <= 0:
        return None
    credit_acc = ACC_BANK if payment_method == 'bank' else ACC_CASH
    entry = create_journal_entry(
        db, models, user_id, 'payroll_payment', payroll.id,
        f'صرف رواتب {payroll.period_year}/{payroll.period_month:02d}',
        [
            (*ACC_SALARIES_PAYABLE, amount, 0),
            (*credit_acc, 0, amount),
        ],
    )
    payroll.journal_payment_id = entry.id
    exp = Expense(
        category='رواتب',
        description=f'صرف مسير رواتب {payroll.period_year}/{payroll.period_month:02d}',
        amount=amount,
        user_id=user_id,
    )
    db.session.add(exp)
    db.session.flush()
    payroll.expense_id = exp.id
    return entry


def loan_journal(db, models, loan, user_id):
    amount = loan.amount or 0
    if amount <= 0:
        return None
    entry = create_journal_entry(
        db, models, user_id, 'loan', loan.id,
        f'سلفة موظف #{loan.employee_id}',
        [
            (*ACC_EMPLOYEE_LOANS, amount, 0),
            (*ACC_CASH, 0, amount),
        ],
    )
    loan.journal_id = entry.id
    return entry


def bonus_journal(db, models, bonus, user_id):
    amount = bonus.amount or 0
    if amount <= 0:
        return None
    entry = create_journal_entry(
        db, models, user_id, 'bonus', bonus.id,
        bonus.title or 'مكافأة موظف',
        [
            (*ACC_BONUSES_EXPENSE, amount, 0),
            (*ACC_SALARIES_PAYABLE, 0, amount),
        ],
    )
    bonus.journal_id = entry.id
    return entry


def deduction_journal(db, models, ded, user_id):
    amount = ded.amount or 0
    if amount <= 0:
        return None
    entry = create_journal_entry(
        db, models, user_id, 'deduction', ded.id,
        ded.title or 'خصم موظف',
        [
            (*ACC_SALARIES_PAYABLE, amount, 0),
            (*ACC_DEDUCTIONS, 0, amount),
        ],
    )
    ded.journal_id = entry.id
    return entry


HRM_STATUTORY_KEYS = (
    'hrm_tax_percent',
    'hrm_insurance_employee_percent',
    'hrm_insurance_employer_percent',
    'hrm_health_insurance_percent',
)


def get_hrm_statutory_rates():
    """نسب الضرائب والتأمينات — قراءة مباشرة من AppSetting."""
    from app import AppSetting
    defaults = {
        'hrm_tax_percent': '0',
        'hrm_insurance_employee_percent': '11',
        'hrm_insurance_employer_percent': '18.75',
        'hrm_health_insurance_percent': '0',
    }
    stored = dict(defaults)
    for row in AppSetting.query.filter(AppSetting.key.in_(HRM_STATUTORY_KEYS)).all():
        if row.key and row.value is not None and str(row.value).strip() != '':
            stored[row.key] = str(row.value).strip()
    return {
        'tax_percent': float(stored.get('hrm_tax_percent') or 0),
        'insurance_employee_percent': float(stored.get('hrm_insurance_employee_percent') or 0),
        'insurance_employer_percent': float(stored.get('hrm_insurance_employer_percent') or 0),
        'health_insurance_percent': float(stored.get('hrm_health_insurance_percent') or 0),
    }


def save_hrm_statutory_rates(db, rates: dict):
    from app import AppSetting
    mapping = {
        'tax_percent': 'hrm_tax_percent',
        'insurance_employee_percent': 'hrm_insurance_employee_percent',
        'insurance_employer_percent': 'hrm_insurance_employer_percent',
        'health_insurance_percent': 'hrm_health_insurance_percent',
    }
    for key, setting_key in mapping.items():
        val = str(rates.get(key, 0))
        row = AppSetting.query.filter_by(key=setting_key).first()
        if not row:
            row = AppSetting(key=setting_key)
            db.session.add(row)
        row.value = val


def calc_statutory_deductions(gross, basic, rates=None):
    """حساب ضريبة كسب العمل + تأمينات + تأمين صحي على الأساس الشهري."""
    rates = rates or get_hrm_statutory_rates()
    taxable = max(0.0, float(gross or 0))
    basic_base = max(0.0, float(basic or 0))
    taxes = taxable * (rates['tax_percent'] / 100.0)
    social = basic_base * (rates['insurance_employee_percent'] / 100.0)
    health = basic_base * (rates['health_insurance_percent'] / 100.0)
    insurance = social + health
    return round(taxes, 2), round(insurance, 2)


def recalc_payroll_detail_net(detail, rates=None):
    gross = (
        (detail.basic_salary or 0) + (detail.allowances or 0) +
        (detail.bonuses or 0) + (detail.overtime or 0)
    )
    taxes, insurance = calc_statutory_deductions(gross, detail.basic_salary, rates)
    detail.taxes = taxes
    detail.insurance = insurance
    detail.net_salary = max(0, round(
        gross - (detail.deductions or 0) - (detail.loans or 0) - taxes - insurance, 2
    ))
    return detail


def recalc_payroll_totals(payroll):
    """إعادة حساب إجماليات المسير من تفاصيل الموظفين."""
    total_gross = total_net = total_ded = 0.0
    for d in payroll.details:
        g = (d.basic_salary or 0) + (d.allowances or 0) + (d.bonuses or 0) + (d.overtime or 0)
        total_gross += g
        total_net += d.net_salary or 0
        total_ded += (d.deductions or 0) + (d.loans or 0) + (d.taxes or 0) + (d.insurance or 0)
    payroll.total_gross = round(total_gross, 2)
    payroll.total_net = round(total_net, 2)
    payroll.total_deductions = round(total_ded, 2)
    return payroll


def apply_statutory_to_draft_payrolls(db, models):
    """تطبيق نسب الضرائب والتأمينات على مسيرات المسودة."""
    HrmPayroll = models['HrmPayroll']
    rates = get_hrm_statutory_rates()
    updated = 0
    for payroll in HrmPayroll.query.filter_by(status='draft').all():
        for detail in payroll.details:
            recalc_payroll_detail_net(detail, rates)
        recalc_payroll_totals(payroll)
        updated += 1
    return updated


def payroll_payment_summary(payroll):
    paid = sum(1 for d in payroll.details if d.is_paid)
    total = len(payroll.details)
    paid_amount = sum(d.net_salary or 0 for d in payroll.details if d.is_paid)
    pending_amount = sum(d.net_salary or 0 for d in payroll.details if not d.is_paid)
    return {
        'paid_count': paid,
        'total_count': total,
        'paid_amount': round(paid_amount, 2),
        'pending_amount': round(pending_amount, 2),
    }


def pay_payroll_detail(db, models, detail, user_id, payment_method='cash'):
    """صرف راتب موظف واحد وتسجيل مصروف."""
    from app import Expense, Employee
    if detail.is_paid:
        return False, 'تم صرف راتب هذا الموظف مسبقاً'
    amount = detail.net_salary or 0
    if amount <= 0:
        return False, 'مبلغ الراتب غير صالح'
    emp = Employee.query.get(detail.employee_id)
    emp_name = emp.name if emp else str(detail.employee_id)
    payroll = detail.payroll
    exp = Expense(
        category='رواتب',
        description=f'صرف راتب {emp_name} — {payroll.period_year}/{payroll.period_month:02d}',
        amount=amount,
        user_id=user_id,
    )
    db.session.add(exp)
    db.session.flush()
    detail.is_paid = True
    detail.paid_at = datetime.utcnow()
    detail.payment_method = payment_method
    detail.expense_id = exp.id
    summary = payroll_payment_summary(payroll)
    if summary['paid_count'] >= summary['total_count'] and summary['total_count'] > 0:
        payroll.status = 'paid'
        payroll.paid_at = datetime.utcnow()
        payroll.paid_by = user_id
        payroll.payment_method = payment_method
    return True, None


def generate_monthly_payroll(db, models, year, month):
    """إنشاء مسير رواتب شهري مع تفاصيل لكل موظف نشط."""
    from app import Employee
    HrmPayroll = models['HrmPayroll']
    HrmPayrollDetail = models['HrmPayrollDetail']
    HrmEmployeeLoan = models['HrmEmployeeLoan']
    HrmEmployeeDeduction = models['HrmEmployeeDeduction']
    HrmEmployeeBonus = models['HrmEmployeeBonus']
    HrmAttendance = models['HrmAttendance']

    existing = HrmPayroll.query.filter_by(period_year=year, period_month=month).first()
    if existing:
        return existing, False

    payroll = HrmPayroll(
        period_year=year,
        period_month=month,
        title=f'رواتب {year}/{month:02d}',
        status='draft',
    )
    db.session.add(payroll)
    db.session.flush()

    employees = Employee.query.filter_by(is_active=True).all()
    total_gross = total_net = total_ded = 0.0

    for emp in employees:
        basic = float(emp.salary or 0)
        allowances = float(getattr(emp, 'allowances', 0) or 0)
        month_start = date(year, month, 1)
        _, last_day = monthrange(year, month)
        month_end = date(year, month, last_day)

        bonuses = sum(
            b.amount for b in HrmEmployeeBonus.query.filter(
                HrmEmployeeBonus.employee_id == emp.id,
                HrmEmployeeBonus.bonus_date >= month_start,
                HrmEmployeeBonus.bonus_date <= month_end,
                HrmEmployeeBonus.status == 'approved',
            ).all()
        )
        deductions = sum(
            d.amount for d in HrmEmployeeDeduction.query.filter(
                HrmEmployeeDeduction.employee_id == emp.id,
                HrmEmployeeDeduction.status == 'active',
            ).all() if d.is_recurring or (
                d.deduction_date and month_start <= d.deduction_date <= month_end
            )
        )
        loans = sum(
            l.monthly_deduction for l in HrmEmployeeLoan.query.filter_by(
                employee_id=emp.id, status='active'
            ).all() if (l.monthly_deduction or 0) > 0
        )
        overtime = sum(
            a.overtime_hours or 0 for a in HrmAttendance.query.filter(
                HrmAttendance.employee_id == emp.id,
                HrmAttendance.att_date >= month_start,
                HrmAttendance.att_date <= month_end,
            ).all()
        )
        ot_pay = overtime * (basic / 200) if basic else 0
        gross = basic + allowances + bonuses + ot_pay
        taxes, insurance = calc_statutory_deductions(gross, basic)
        net = gross - deductions - loans - taxes - insurance

        db.session.add(HrmPayrollDetail(
            payroll_id=payroll.id,
            employee_id=emp.id,
            basic_salary=basic,
            allowances=allowances,
            bonuses=bonuses,
            overtime=ot_pay,
            deductions=deductions,
            loans=loans,
            taxes=taxes,
            insurance=insurance,
            net_salary=max(0, net),
            is_paid=False,
        ))
        total_gross += gross
        total_net += max(0, net)
        total_ded += deductions + loans + taxes + insurance

    payroll.total_gross = round(total_gross, 2)
    payroll.total_net = round(total_net, 2)
    payroll.total_deductions = round(total_ded, 2)
    return payroll, True


def hard_delete_employee(db, models, employee_id):
    """حذف موظف نهائياً مع السجلات المرتبطة."""
    from app import Employee
    emp = Employee.query.get(employee_id)
    if not emp:
        return False, 'الموظف غير موجود'
    eid = emp.id
    for model_name in (
        'HrmAttendance', 'HrmAttendanceLog', 'HrmLeaveRequest',
        'HrmEmployeeLoan', 'HrmEmployeeDeduction', 'HrmEmployeeBonus',
        'HrmEmployeeDocument', 'HrmContract', 'HrmPerformanceReview',
    ):
        M = models[model_name]
        M.query.filter_by(employee_id=eid).delete(synchronize_session=False)
    models['HrmPayrollDetail'].query.filter_by(employee_id=eid).delete(synchronize_session=False)
    models['HrmNotification'].query.filter_by(employee_id=eid).delete(synchronize_session=False)
    Employee.query.filter_by(manager_id=eid).update({'manager_id': None}, synchronize_session=False)
    models['HrmDepartment'].query.filter_by(manager_id=eid).update({'manager_id': None}, synchronize_session=False)
    db.session.delete(emp)
    return True, None


def hard_delete_department(db, models, dept_id):
    """حذف قسم نهائياً."""
    from app import Employee
    HrmDepartment = models['HrmDepartment']
    HrmDesignation = models['HrmDesignation']
    dept = HrmDepartment.query.get(dept_id)
    if not dept:
        return False, 'القسم غير موجود'
    Employee.query.filter_by(department_id=dept_id).update(
        {'department_id': None, 'department': None}, synchronize_session=False)
    HrmDesignation.query.filter_by(department_id=dept_id).delete(synchronize_session=False)
    db.session.delete(dept)
    return True, None


def hard_delete_designation(db, models, des_id):
    """حذف وظيفة نهائياً."""
    from app import Employee
    HrmDesignation = models['HrmDesignation']
    des = HrmDesignation.query.get(des_id)
    if not des:
        return False, 'الوظيفة غير موجودة'
    Employee.query.filter_by(designation_id=des_id).update({'designation_id': None}, synchronize_session=False)
    db.session.delete(des)
    return True, None


def collect_hrm_notification_feed(db, models, user):
    """إشعارات HR للجرس العلوي — طلبات إجازة + إشعارات مخزنة."""
    from app import user_can
    if not user or not getattr(user, 'is_authenticated', False):
        return {'pending_leaves': 0, 'items': [], 'stored': []}
    if not (
        user_can(user, 'hrm') or user_can(user, 'hrm_leaves')
        or user_can(user, 'hrm_dashboard') or user_can(user, 'hrm_approve')
    ):
        return {'pending_leaves': 0, 'items': [], 'stored': []}
    HrmLeaveRequest = models['HrmLeaveRequest']
    HrmNotification = models['HrmNotification']
    pending_q = HrmLeaveRequest.query.filter_by(status='pending').order_by(
        HrmLeaveRequest.created_at.desc()
    )
    pending = pending_q.limit(15).all()
    items = []
    for lr in pending:
        ename = lr.employee.name if lr.employee else f'موظف #{lr.employee_id}'
        ltype = lr.leave_type.name if lr.leave_type else 'إجازة'
        items.append({
            'kind': 'leave',
            'id': lr.id,
            'title': f'طلب إجازة — {ename}',
            'message': f'{ltype} | {lr.date_from} — {lr.date_to} ({lr.days_count} يوم)',
            'link': '/hrm/leaves?status=pending',
            'icon': 'calendar-alt',
            'tone': 'warn',
        })
    nq = HrmNotification.query.filter_by(is_read=False).order_by(HrmNotification.created_at.desc())
    if getattr(user, 'role', None) not in ('developer', 'admin', 'hr_manager'):
        nq = nq.filter(
            db.or_(
                HrmNotification.user_id == user.id,
                HrmNotification.user_id == None,
            )
        )
    stored_rows = nq.limit(10).all()
    stored = []
    for n in stored_rows:
        stored.append({
            'kind': 'stored',
            'id': n.id,
            'title': n.title or 'إشعار HR',
            'message': (n.message or '')[:120],
            'link': n.link or '/hrm/leaves',
            'icon': 'bell',
            'tone': 'info',
        })
    return {
        'pending_leaves': len(pending),
        'items': items,
        'stored': stored,
    }


def push_notification(db, models, user_id, ntype, title, message, link=None, employee_id=None):
    HrmNotification = models['HrmNotification']
    n = HrmNotification(
        user_id=user_id,
        employee_id=employee_id,
        ntype=ntype,
        title=title,
        message=message,
        link=link,
    )
    db.session.add(n)
    return n


def notify_hr_managers(db, models):
    """إرسال إشعار لمديري HR."""
    from app import User
    users = User.query.filter(
        User.is_active == True,
        User.role.in_(['admin', 'developer', 'hr_manager', 'hr_officer']),
    ).all()
    return users


def check_contract_expiry(db, models, days_ahead=30):
    HrmContract = models['HrmContract']
    limit = date.today() + timedelta(days=days_ahead)
    return HrmContract.query.filter(
        HrmContract.status == 'active',
        HrmContract.end_date != None,
        HrmContract.end_date <= limit,
        HrmContract.end_date >= date.today(),
    ).all()


def hr_dashboard_stats(db, models):
    from app import Employee
    HrmLeaveRequest = models['HrmLeaveRequest']
    today = date.today()
    total = Employee.query.count()
    active = Employee.query.filter_by(is_active=True).count()
    month_start = today.replace(day=1)
    new_hires = Employee.query.filter(
        Employee.hire_date != None,
        Employee.hire_date >= month_start,
    ).count()
    on_leave = HrmLeaveRequest.query.filter(
        HrmLeaveRequest.status == 'approved',
        HrmLeaveRequest.date_from <= today,
        HrmLeaveRequest.date_to >= today,
    ).count()
    att = attendance_dashboard_today(models, db)
    return {
        'total_employees': total,
        'active_employees': active,
        'new_employees': new_hires,
        'on_leave': on_leave,
        'attendance_today': att,
    }


def department_chart_data(db, models):
    from app import Employee
    HrmDepartment = models['HrmDepartment']
    labels, values = [], []
    for d in HrmDepartment.query.filter_by(is_active=True).all():
        c = Employee.query.filter_by(department_id=d.id, is_active=True).count()
        labels.append(d.name)
        values.append(c)
    unassigned = Employee.query.filter(
        Employee.is_active == True,
        (Employee.department_id == None) | (Employee.department_id == 0),
    ).count()
    if unassigned:
        labels.append('غير محدد')
        values.append(unassigned)
    return labels, values


def payroll_chart_data(db, models, months=6):
    HrmPayroll = models['HrmPayroll']
    rows = HrmPayroll.query.filter(
        HrmPayroll.status.in_(['approved', 'paid'])
    ).order_by(HrmPayroll.period_year.desc(), HrmPayroll.period_month.desc()).limit(months).all()
    rows = list(reversed(rows))
    labels = [f'{r.period_year}/{r.period_month:02d}' for r in rows]
    values = [r.total_net or 0 for r in rows]
    return labels, values


def attendance_30_days(db, models):
    HrmAttendance = models['HrmAttendance']
    start = date.today() - timedelta(days=29)
    labels, present, absent = [], [], []
    for i in range(30):
        d = start + timedelta(days=i)
        labels.append(d.strftime('%m/%d'))
        recs = HrmAttendance.query.filter_by(att_date=d).all()
        present.append(sum(1 for r in recs if r.status in ('present', 'late')))
        absent.append(sum(1 for r in recs if r.status == 'absent'))
    return labels, present, absent


def export_csv_response(rows, headers, filename):
    from flask import Response
    output = io.StringIO()
    output.write('\ufeff')
    w = csv.writer(output)
    w.writerow(headers)
    for row in rows:
        w.writerow(row)
    return Response(
        output.getvalue(),
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


def seed_leave_types(db, models):
    HrmLeaveType = models['HrmLeaveType']
    defaults = [
        ('annual', 'سنوية', 21, True),
        ('sick', 'مرضية', 14, True),
        ('emergency', 'طارئة', 5, True),
        ('unpaid', 'بدون راتب', 0, False),
    ]
    for code, name, days, paid in defaults:
        if not HrmLeaveType.query.filter_by(code=code).first():
            db.session.add(HrmLeaveType(code=code, name=name, days_per_year=days, is_paid=paid))
