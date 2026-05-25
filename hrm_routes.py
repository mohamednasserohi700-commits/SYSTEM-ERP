"""مسارات وواجهات API لوحدة الموارد البشرية."""
from datetime import datetime, date, time, timedelta
from functools import wraps
import os
import json

from flask import render_template, request, redirect, url_for, flash, jsonify, send_file, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

HRM_UPLOAD_DIR = None
_models = None
_db = None


def register_hrm(app, db, models, helpers):
    """تسجيل جميع مسارات HRM على التطبيق."""
    global HRM_UPLOAD_DIR, _models, _db
    _models = models
    _db = db
    HRM_UPLOAD_DIR = os.path.join(app.instance_path, 'hrm_uploads')
    os.makedirs(HRM_UPLOAD_DIR, exist_ok=True)

    user_can = helpers['user_can']
    record_delete_required = helpers['record_delete_required']
    allocate_entity_code = helpers['allocate_entity_code']
    get_next_number = helpers.get('get_next_number')

    def hrm_can(perm):
        return user_can(current_user, perm) or user_can(current_user, 'hrm')

    def hrm_required(perm='hrm'):
        def deco(f):
            @wraps(f)
            def inner(*args, **kwargs):
                if not hrm_can(perm):
                    flash('ليس لديك صلاحية للوصول لوحدة الموارد البشرية', 'error')
                    from flask import url_for as uf
                    return redirect(helpers['safe_home_url_for'](current_user))
                return f(*args, **kwargs)
            return inner
        return deco

    def hrm_approve_required(f):
        @wraps(f)
        def inner(*args, **kwargs):
            if not (hrm_can('hrm_approve') or current_user.role in (
                'developer', 'admin', 'hr_manager', 'department_manager'
            )):
                flash('ليس لديك صلاحية الاعتماد', 'error')
                return redirect(url_for('hrm_dashboard'))
            return f(*args, **kwargs)
        return inner

    from app import Employee, Branch, User, Expense
    import hrm_services as svc

    M = models

    def paginate_query(q, default=20):
        page = request.args.get('page', 1, type=int)
        per = request.args.get('per_page', default, type=int)
        return q.paginate(page=page, per_page=min(per, 100), error_out=False)

    # ── Dashboard ─────────────────────────────────────────
    @app.route('/hrm')
    @app.route('/hrm/dashboard')
    @login_required
    @hrm_required('hrm_dashboard')
    def hrm_dashboard():
        stats = svc.hr_dashboard_stats(db, M)
        dept_labels, dept_vals = svc.department_chart_data(db, M)
        pay_labels, pay_vals = svc.payroll_chart_data(db, M)
        att_labels, att_present, att_absent = svc.attendance_30_days(db, M)
        pending_leaves = M['HrmLeaveRequest'].query.filter_by(status='pending').count()
        return render_template(
            'hrm/dashboard.html',
            stats=stats,
            dept_labels=dept_labels, dept_values=dept_vals,
            payroll_labels=pay_labels, payroll_values=pay_vals,
            att_labels=att_labels, att_present=att_present, att_absent=att_absent,
            pending_leaves=pending_leaves,
        )

    # ── Employees ───────────────────────────────────────────
    @app.route('/hrm/employees')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employees():
        q = Employee.query
        search = (request.args.get('q') or '').strip()
        dept_id = request.args.get('department_id', type=int)
        status = request.args.get('status')
        if search:
            q = q.filter(
                db.or_(
                    Employee.name.contains(search),
                    Employee.code.contains(search),
                    Employee.phone.contains(search),
                    Employee.national_id.contains(search),
                )
            )
        if dept_id:
            q = q.filter_by(department_id=dept_id)
        if status == 'active':
            q = q.filter_by(is_active=True)
        elif status == 'inactive':
            q = q.filter_by(is_active=False)
        q = q.order_by(Employee.id.desc())
        pagination = paginate_query(q)
        departments = M['HrmDepartment'].query.filter_by(is_active=True).all()
        return render_template(
            'hrm/employees.html',
            employees=pagination.items,
            pagination=pagination,
            departments=departments,
            search=search, dept_id=dept_id, status=status,
        )

    @app.route('/hrm/employees/export')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employees_export():
        rows = []
        for e in Employee.query.order_by(Employee.name).all():
            dept = M['HrmDepartment'].query.get(e.department_id) if e.department_id else None
            des = M['HrmDesignation'].query.get(e.designation_id) if getattr(e, 'designation_id', None) else None
            rows.append([
                e.code, e.name, e.national_id or '', e.phone or '', e.email or '',
                dept.name if dept else (e.department or ''),
                des.title if des else (e.position or ''),
                e.salary, e.employment_status or '', 'نشط' if e.is_active else 'غير نشط',
            ])
        return svc.export_csv_response(
            rows,
            ['الكود', 'الاسم', 'الرقم القومي', 'الهاتف', 'البريد', 'القسم', 'الوظيفة', 'الراتب', 'الحالة', 'نشط'],
            'employees.csv',
        )

    @app.route('/hrm/employees/add', methods=['GET', 'POST'])
    @app.route('/hrm/employees/<int:id>/edit', methods=['GET', 'POST'])
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employee_form(id=None):
        emp = Employee.query.get(id) if id else None
        if request.method == 'POST':
            code = (request.form.get('code') or '').strip()
            if not code:
                code = allocate_entity_code('E', Employee)
            data = dict(
                code=code,
                name=request.form['name'],
                phone=request.form.get('phone'),
                email=request.form.get('email'),
                national_id=request.form.get('national_id'),
                address=request.form.get('address'),
                department_id=request.form.get('department_id') or None,
                designation_id=request.form.get('designation_id') or None,
                manager_id=request.form.get('manager_id') or None,
                branch_id=request.form.get('branch_id') or None,
                salary=float(request.form.get('salary', 0) or 0),
                allowances=float(request.form.get('allowances', 0) or 0),
                employment_status=request.form.get('employment_status') or 'active',
                contract_type=request.form.get('contract_type') or 'permanent',
                position=request.form.get('position'),
                department=request.form.get('department_legacy'),
            )
            hd = request.form.get('hire_date')
            data['hire_date'] = datetime.strptime(hd, '%Y-%m-%d').date() if hd else None
            if data.get('department_id'):
                dept = M['HrmDepartment'].query.get(int(data['department_id']))
                if dept:
                    data['department'] = dept.name
            if data.get('designation_id'):
                des = M['HrmDesignation'].query.get(int(data['designation_id']))
                if des:
                    data['position'] = des.title
            if emp:
                for k, v in data.items():
                    setattr(emp, k, v)
            else:
                emp = Employee(**data, is_active=True)
                db.session.add(emp)
            db.session.flush()
            f = request.files.get('photo')
            if f and f.filename:
                fn = secure_filename(f'emp_{emp.id}_{f.filename}')
                path = os.path.join(HRM_UPLOAD_DIR, fn)
                f.save(path)
                emp.photo = f'hrm_uploads/{fn}'
            db.session.commit()
            flash('تم حفظ بيانات الموظف', 'success')
            return redirect(url_for('hrm_employees'))
        departments = M['HrmDepartment'].query.filter_by(is_active=True).all()
        designations = M['HrmDesignation'].query.filter_by(is_active=True).all()
        branches = Branch.query.filter_by(is_active=True).all()
        managers = Employee.query.filter_by(is_active=True).all()
        suggested = allocate_entity_code('E', Employee) if not emp else emp.code
        return render_template(
            'hrm/employee_form.html', emp=emp, departments=departments,
            designations=designations, branches=branches, managers=managers,
            suggested_code=suggested,
        )

    @app.route('/hrm/employees/<int:id>/delete', methods=['POST'])
    @login_required
    @record_delete_required
    @hrm_required('hrm_employees')
    def hrm_employee_delete(id):
        emp = Employee.query.get_or_404(id)
        ok, err = svc.hard_delete_employee(db, M, emp.id)
        if not ok:
            flash(err or 'تعذر الحذف', 'error')
            return redirect(url_for('hrm_employees'))
        try:
            db.session.commit()
            flash('تم حذف الموظف نهائياً من النظام', 'success')
        except Exception as ex:
            db.session.rollback()
            flash(f'تعذر الحذف: قد يكون مرتبطاً بسجلات أخرى. {ex}', 'error')
        return redirect(url_for('hrm_employees'))

    # ── Departments ─────────────────────────────────────────
    @app.route('/hrm/departments')
    @login_required
    @hrm_required('hrm_departments')
    def hrm_departments():
        depts = M['HrmDepartment'].query.order_by(M['HrmDepartment'].name).all()
        counts = {d.id: svc.department_employee_count(d.id, M) for d in depts}
        managers = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
        return render_template('hrm/departments.html', departments=depts, counts=counts, managers=managers)

    @app.route('/hrm/departments/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_departments')
    def hrm_department_save():
        did = request.form.get('id', type=int)
        HrmDepartment = M['HrmDepartment']
        if did:
            d = HrmDepartment.query.get_or_404(did)
        else:
            d = HrmDepartment()
            db.session.add(d)
        d.name = request.form['name']
        d.manager_id = request.form.get('manager_id') or None
        d.description = request.form.get('description')
        d.is_active = request.form.get('is_active') != '0'
        db.session.commit()
        flash('تم حفظ القسم', 'success')
        return redirect(url_for('hrm_departments'))

    @app.route('/hrm/departments/<int:id>/delete', methods=['POST'])
    @login_required
    @record_delete_required
    @hrm_required('hrm_departments')
    def hrm_department_delete(id):
        ok, err = svc.hard_delete_department(db, M, id)
        if not ok:
            flash(err or 'تعذر الحذف', 'error')
            return redirect(url_for('hrm_departments'))
        try:
            db.session.commit()
            flash('تم حذف القسم نهائياً', 'success')
        except Exception as ex:
            db.session.rollback()
            flash(f'تعذر حذف القسم: {ex}', 'error')
        return redirect(url_for('hrm_departments'))

    # ── Designations ──────────────────────────────────────────
    @app.route('/hrm/designations')
    @login_required
    @hrm_required('hrm_departments')
    def hrm_designations():
        items = M['HrmDesignation'].query.order_by(M['HrmDesignation'].title).all()
        departments = M['HrmDepartment'].query.filter_by(is_active=True).all()
        return render_template('hrm/designations.html', designations=items, departments=departments)

    @app.route('/hrm/designations/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_departments')
    def hrm_designation_save():
        did = request.form.get('id', type=int)
        HrmDesignation = M['HrmDesignation']
        if did:
            d = HrmDesignation.query.get_or_404(did)
        else:
            d = HrmDesignation()
            db.session.add(d)
        d.title = request.form['title']
        d.department_id = request.form.get('department_id') or None
        d.description = request.form.get('description')
        db.session.commit()
        flash('تم حفظ الوظيفة', 'success')
        return redirect(url_for('hrm_designations'))

    @app.route('/hrm/designations/<int:id>/delete', methods=['POST'])
    @login_required
    @record_delete_required
    @hrm_required('hrm_departments')
    def hrm_designation_delete(id):
        ok, err = svc.hard_delete_designation(db, M, id)
        if not ok:
            flash(err or 'تعذر الحذف', 'error')
            return redirect(url_for('hrm_designations'))
        try:
            db.session.commit()
            flash('تم حذف الوظيفة نهائياً', 'success')
        except Exception as ex:
            db.session.rollback()
            flash(f'تعذر الحذف: {ex}', 'error')
        return redirect(url_for('hrm_designations'))

    # ── Attendance ────────────────────────────────────────────
    @app.route('/hrm/attendance')
    @login_required
    @hrm_required('hrm_attendance')
    def hrm_attendance():
        att_date = request.args.get('date')
        d = datetime.strptime(att_date, '%Y-%m-%d').date() if att_date else date.today()
        records = M['HrmAttendance'].query.filter_by(att_date=d).all()
        cards = svc.attendance_dashboard_today(M, db) if d == date.today() else {}
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template(
            'hrm/attendance.html', records=records, att_date=d,
            cards=cards, employees=employees,
        )

    @app.route('/hrm/attendance/manual', methods=['POST'])
    @login_required
    @hrm_required('hrm_attendance')
    def hrm_attendance_manual():
        HrmAttendance = M['HrmAttendance']
        eid = request.form.get('employee_id', type=int)
        att_date = datetime.strptime(request.form['att_date'], '%Y-%m-%d').date()
        ci = request.form.get('check_in')
        co = request.form.get('check_out')
        check_in = datetime.strptime(ci, '%H:%M').time() if ci else None
        check_out = datetime.strptime(co, '%H:%M').time() if co else None
        wh = svc.calc_working_hours(check_in, check_out)
        delay = int(request.form.get('delay_minutes', 0) or 0)
        status = request.form.get('status', 'present')
        rec = HrmAttendance.query.filter_by(employee_id=eid, att_date=att_date).first()
        if not rec:
            rec = HrmAttendance(employee_id=eid, att_date=att_date, source='manual')
            db.session.add(rec)
        rec.check_in = check_in
        rec.check_out = check_out
        rec.working_hours = wh
        rec.overtime_hours = float(request.form.get('overtime_hours', 0) or 0)
        rec.delay_minutes = delay
        rec.status = status
        db.session.commit()
        flash('تم تسجيل الحضور', 'success')
        return redirect(url_for('hrm_attendance', date=att_date.isoformat()))

    @app.route('/hrm/attendance/qr', methods=['GET', 'POST'])
    @login_required
    @hrm_required('hrm_attendance')
    def hrm_attendance_qr():
        if request.method == 'POST':
            code = (request.form.get('employee_code') or '').strip()
            emp = Employee.query.filter_by(code=code, is_active=True).first()
            if not emp:
                flash('كود الموظف غير صحيح', 'error')
                return redirect(url_for('hrm_attendance_qr'))
            now = datetime.now()
            today = now.date()
            HrmAttendance = M['HrmAttendance']
            HrmAttendanceLog = M['HrmAttendanceLog']
            rec = HrmAttendance.query.filter_by(employee_id=emp.id, att_date=today).first()
            if not rec:
                rec = HrmAttendance(
                    employee_id=emp.id, att_date=today,
                    check_in=now.time(), status='present', source='qr',
                )
                db.session.add(rec)
                action = 'check_in'
            elif not rec.check_out:
                rec.check_out = now.time()
                rec.working_hours = svc.calc_working_hours(rec.check_in, rec.check_out)
                action = 'check_out'
            else:
                flash('تم تسجيل الحضور والانصراف مسبقاً', 'warning')
                return redirect(url_for('hrm_attendance_qr'))
            db.session.add(HrmAttendanceLog(
                employee_id=emp.id, action=action, source='qr',
            ))
            db.session.commit()
            flash(f'تم تسجيل {"حضور" if action == "check_in" else "انصراف"} — {emp.name}', 'success')
            return redirect(url_for('hrm_attendance_qr'))
        return render_template('hrm/attendance_qr.html')

    @app.route('/api/hrm/biometric', methods=['POST'])
    @login_required
    def api_hrm_biometric():
        """جاهز لربط أجهزة البصمة — يرسل employee_code و action."""
        data = request.get_json(silent=True) or {}
        code = (data.get('employee_code') or '').strip()
        action = data.get('action', 'check_in')
        emp = Employee.query.filter_by(code=code, is_active=True).first()
        if not emp:
            return jsonify({'ok': False, 'error': 'employee not found'}), 404
        now = datetime.now()
        today = now.date()
        HrmAttendance = M['HrmAttendance']
        HrmAttendanceLog = M['HrmAttendanceLog']
        rec = HrmAttendance.query.filter_by(employee_id=emp.id, att_date=today).first()
        if action == 'check_in':
            if not rec:
                rec = HrmAttendance(
                    employee_id=emp.id, att_date=today,
                    check_in=now.time(), status='present', source='biometric',
                )
                db.session.add(rec)
            else:
                rec.check_in = rec.check_in or now.time()
        else:
            if not rec:
                rec = HrmAttendance(
                    employee_id=emp.id, att_date=today,
                    check_out=now.time(), status='present', source='biometric',
                )
                db.session.add(rec)
            else:
                rec.check_out = now.time()
                rec.working_hours = svc.calc_working_hours(rec.check_in, rec.check_out)
        db.session.add(HrmAttendanceLog(
            employee_id=emp.id, action=action, source='biometric',
            device_id=data.get('device_id'),
        ))
        db.session.commit()
        return jsonify({'ok': True, 'employee': emp.name, 'action': action})

    # ── Leaves ──────────────────────────────────────────────────
    @app.route('/hrm/leaves')
    @login_required
    @hrm_required('hrm_leaves')
    def hrm_leaves():
        status = request.args.get('status')
        q = M['HrmLeaveRequest'].query.order_by(M['HrmLeaveRequest'].created_at.desc())
        if status:
            q = q.filter_by(status=status)
        items = q.limit(200).all()
        types = M['HrmLeaveType'].query.filter_by(is_active=True).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/leaves.html', leaves=items, leave_types=types, employees=employees)

    @app.route('/hrm/leaves/new', methods=['POST'])
    @login_required
    @hrm_required('hrm_leaves')
    def hrm_leave_new():
        df = datetime.strptime(request.form['date_from'], '%Y-%m-%d').date()
        dt = datetime.strptime(request.form['date_to'], '%Y-%m-%d').date()
        days = (dt - df).days + 1
        lr = M['HrmLeaveRequest'](
            employee_id=request.form['employee_id'],
            leave_type_id=request.form['leave_type_id'],
            date_from=df, date_to=dt, days_count=days,
            reason=request.form.get('reason'),
            status='pending',
        )
        db.session.add(lr)
        db.session.flush()
        ename = Employee.query.get(lr.employee_id)
        ename_s = ename.name if ename else f'موظف #{lr.employee_id}'
        for u in svc.notify_hr_managers(db, M):
            svc.push_notification(
                db, M, u.id, 'leave_request', 'طلب إجازة جديد',
                f'{ename_s} — طلب إجازة بانتظار الاعتماد', '/hrm/leaves?status=pending',
            )
        db.session.commit()
        flash('تم إرسال طلب الإجازة', 'success')
        return redirect(url_for('hrm_leaves'))

    @app.route('/hrm/leaves/<int:id>/approve', methods=['POST'])
    @login_required
    @hrm_approve_required
    def hrm_leave_approve(id):
        lr = M['HrmLeaveRequest'].query.get_or_404(id)
        step = request.form.get('step', 'manager')
        if step == 'manager':
            lr.manager_approved = True
            lr.manager_approved_by = current_user.id
            lr.manager_approved_at = datetime.utcnow()
        else:
            lr.hr_approved = True
            lr.hr_approved_by = current_user.id
            lr.hr_approved_at = datetime.utcnow()
            if lr.manager_approved or current_user.role in ('hr_manager', 'admin', 'developer'):
                lr.status = 'approved'
                svc.push_notification(
                    db, M, None, 'leave_approved', 'اعتماد إجازة',
                    'تم اعتماد طلب الإجازة', '/hrm/leaves', employee_id=lr.employee_id,
                )
        db.session.commit()
        flash('تمت الموافقة على المرحلة', 'success')
        return redirect(url_for('hrm_leaves'))

    @app.route('/hrm/leaves/<int:id>/reject', methods=['POST'])
    @login_required
    @hrm_approve_required
    def hrm_leave_reject(id):
        lr = M['HrmLeaveRequest'].query.get_or_404(id)
        lr.status = 'rejected'
        lr.rejected_by = current_user.id
        lr.rejected_at = datetime.utcnow()
        lr.rejection_reason = request.form.get('reason')
        db.session.commit()
        flash('تم رفض الطلب', 'warning')
        return redirect(url_for('hrm_leaves'))

    # ── Payroll ─────────────────────────────────────────────────
    @app.route('/hrm/payroll')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_payroll_list():
        items = M['HrmPayroll'].query.order_by(
            M['HrmPayroll'].period_year.desc(),
            M['HrmPayroll'].period_month.desc(),
        ).all()
        status_ar = {'draft': 'مسودة', 'approved': 'معتمد', 'paid': 'مُصرف بالكامل'}
        rows = []
        for p in items:
            sm = svc.payroll_payment_summary(p)
            rows.append({
                'payroll': p,
                'summary': sm,
                'status_label': status_ar.get(p.status, p.status),
                'partial_paid': p.status == 'approved' and sm['paid_count'] > 0 and sm['paid_count'] < sm['total_count'],
            })
        rates = svc.get_hrm_statutory_rates()
        return render_template(
            'hrm/payroll.html', payroll_rows=rows, today=date.today(), rates=rates,
        )

    @app.route('/hrm/payroll/generate', methods=['POST'])
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_payroll_generate():
        year = request.form.get('year', type=int) or date.today().year
        month = request.form.get('month', type=int) or date.today().month
        payroll, created = svc.generate_monthly_payroll(db, M, year, month)
        db.session.commit()
        flash('تم إنشاء مسير الرواتب' if created else 'المسير موجود مسبقاً', 'success' if created else 'info')
        return redirect(url_for('hrm_payroll_detail', id=payroll.id))

    @app.route('/hrm/payroll/<int:id>')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_payroll_detail(id):
        p = M['HrmPayroll'].query.get_or_404(id)
        summary = svc.payroll_payment_summary(p)
        status_ar = {'draft': 'مسودة', 'approved': 'معتمد — جاهز للصرف', 'paid': 'مُصرف'}
        return render_template(
            'hrm/payroll_detail.html',
            payroll=p, summary=summary,
            status_label=status_ar.get(p.status, p.status),
            rates=svc.get_hrm_statutory_rates(),
        )

    @app.route('/hrm/tax-insurance', methods=['GET', 'POST'])
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_tax_insurance():
        rates = svc.get_hrm_statutory_rates()
        if request.method == 'POST':
            action = request.form.get('action', 'save')
            new_rates = {
                'tax_percent': float(request.form.get('tax_percent', 0) or 0),
                'insurance_employee_percent': float(request.form.get('insurance_employee_percent', 0) or 0),
                'insurance_employer_percent': float(request.form.get('insurance_employer_percent', 0) or 0),
                'health_insurance_percent': float(request.form.get('health_insurance_percent', 0) or 0),
            }
            svc.save_hrm_statutory_rates(db, new_rates)
            db.session.commit()
            if action == 'apply_drafts':
                n = svc.apply_statutory_to_draft_payrolls(db, M)
                db.session.commit()
                flash(f'تم الحفظ وتطبيق النسب على {n} مسير(ات) في حالة مسودة', 'success')
            else:
                flash('تم حفظ إعدادات الضرائب والتأمينات بنجاح', 'success')
            return redirect(url_for('hrm_tax_insurance'))
        preview_basic = float(request.args.get('preview_salary', 5000) or 5000)
        preview_gross = preview_basic
        ptax, pins = svc.calc_statutory_deductions(preview_gross, preview_basic, rates)
        return render_template(
            'hrm/tax_insurance.html',
            rates=rates,
            preview_basic=preview_basic,
            preview_tax=ptax,
            preview_insurance=pins,
            preview_net=max(0, preview_gross - ptax - pins),
        )

    @app.route('/hrm/payroll/<int:id>/pay-employee/<int:detail_id>', methods=['POST'])
    @login_required
    @hrm_approve_required
    def hrm_payroll_pay_employee(id, detail_id):
        p = M['HrmPayroll'].query.get_or_404(id)
        if p.status not in ('approved', 'paid'):
            flash('يجب اعتماد المسير قبل صرف الرواتب', 'error')
            return redirect(url_for('hrm_payroll_detail', id=id))
        detail = M['HrmPayrollDetail'].query.filter_by(id=detail_id, payroll_id=id).first_or_404()
        method = request.form.get('payment_method', 'cash')
        ok, err = svc.pay_payroll_detail(db, M, detail, current_user.id, method)
        if not ok:
            flash(err or 'تعذر الصرف', 'error')
        else:
            db.session.commit()
            flash('تم صرف راتب الموظف وتسجيله في المصروفات', 'success')
        return redirect(url_for('hrm_payroll_detail', id=id))

    @app.route('/hrm/payroll/<int:id>/pay-selected', methods=['POST'])
    @login_required
    @hrm_approve_required
    def hrm_payroll_pay_selected(id):
        p = M['HrmPayroll'].query.get_or_404(id)
        if p.status not in ('approved', 'paid'):
            flash('يجب اعتماد المسير قبل الصرف', 'error')
            return redirect(url_for('hrm_payroll_detail', id=id))
        detail_ids = request.form.getlist('detail_id')
        method = request.form.get('payment_method', 'cash')
        paid_n = 0
        for did in detail_ids:
            detail = M['HrmPayrollDetail'].query.filter_by(id=int(did), payroll_id=id).first()
            if not detail:
                continue
            ok, _ = svc.pay_payroll_detail(db, M, detail, current_user.id, method)
            if ok:
                paid_n += 1
        db.session.commit()
        flash(f'تم صرف رواتب {paid_n} موظف(ين)', 'success' if paid_n else 'info')
        return redirect(url_for('hrm_payroll_detail', id=id))

    @app.route('/hrm/payroll/<int:id>/approve', methods=['POST'])
    @login_required
    @hrm_approve_required
    def hrm_payroll_approve(id):
        p = M['HrmPayroll'].query.get_or_404(id)
        p.status = 'approved'
        p.approved_by = current_user.id
        p.approved_at = datetime.utcnow()
        svc.accrue_payroll_journal(db, M, p, current_user.id)
        for u in svc.notify_hr_managers(db, M):
            svc.push_notification(db, M, u.id, 'payroll', 'اعتماد رواتب', p.title or 'مسير رواتب', f'/hrm/payroll/{p.id}')
        db.session.commit()
        flash('تم اعتماد المسير وإنشاء القيد المحاسبي', 'success')
        return redirect(url_for('hrm_payroll_detail', id=id))

    @app.route('/hrm/payroll/<int:id>/pay', methods=['POST'])
    @login_required
    @hrm_approve_required
    def hrm_payroll_pay(id):
        """صرف رواتب جميع الموظفين المتبقين في المسير."""
        p = M['HrmPayroll'].query.get_or_404(id)
        if p.status != 'approved':
            flash('يجب اعتماد المسير أولاً', 'error')
            return redirect(url_for('hrm_payroll_detail', id=id))
        method = request.form.get('payment_method', 'cash')
        paid_n = 0
        for detail in p.details:
            if detail.is_paid:
                continue
            ok, _ = svc.pay_payroll_detail(db, M, detail, current_user.id, method)
            if ok:
                paid_n += 1
        db.session.commit()
        flash(f'تم صرف رواتب {paid_n} موظف(ين) وتسجيل المصروفات', 'success')
        return redirect(url_for('hrm_payroll_detail', id=id))

    @app.route('/hrm/payroll/<int:id>/payslip/<int:emp_id>')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_payslip(id, emp_id):
        p = M['HrmPayroll'].query.get_or_404(id)
        detail = M['HrmPayrollDetail'].query.filter_by(payroll_id=id, employee_id=emp_id).first_or_404()
        emp = Employee.query.get_or_404(emp_id)
        return render_template('hrm/payslip_print.html', payroll=p, detail=detail, emp=emp)

    # ── Loans / Deductions / Bonuses ───────────────────────────
    @app.route('/hrm/loans')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_loans():
        items = M['HrmEmployeeLoan'].query.order_by(M['HrmEmployeeLoan'].created_at.desc()).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/loans.html', loans=items, employees=employees)

    @app.route('/hrm/loans/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_loan_save():
        loan = M['HrmEmployeeLoan'](
            employee_id=request.form['employee_id'],
            amount=float(request.form['amount']),
            remaining=float(request.form.get('remaining') or request.form['amount']),
            monthly_deduction=float(request.form.get('monthly_deduction', 0) or 0),
            notes=request.form.get('notes'),
        )
        sd = request.form.get('start_date')
        if sd:
            loan.start_date = datetime.strptime(sd, '%Y-%m-%d').date()
        db.session.add(loan)
        db.session.flush()
        svc.loan_journal(db, M, loan, current_user.id)
        db.session.commit()
        flash('تم تسجيل السلفة والقيد المحاسبي', 'success')
        return redirect(url_for('hrm_loans'))

    @app.route('/hrm/deductions')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_deductions():
        items = M['HrmEmployeeDeduction'].query.order_by(M['HrmEmployeeDeduction'].id.desc()).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/deductions.html', deductions=items, employees=employees)

    @app.route('/hrm/deductions/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_deduction_save():
        ded = M['HrmEmployeeDeduction'](
            employee_id=request.form['employee_id'],
            title=request.form['title'],
            amount=float(request.form['amount']),
            is_recurring=bool(request.form.get('is_recurring')),
        )
        db.session.add(ded)
        db.session.flush()
        svc.deduction_journal(db, M, ded, current_user.id)
        db.session.commit()
        flash('تم تسجيل الخصم', 'success')
        return redirect(url_for('hrm_deductions'))

    @app.route('/hrm/bonuses')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_bonuses():
        items = M['HrmEmployeeBonus'].query.order_by(M['HrmEmployeeBonus'].id.desc()).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/bonuses.html', bonuses=items, employees=employees)

    @app.route('/hrm/bonuses/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_bonus_save():
        bonus = M['HrmEmployeeBonus'](
            employee_id=request.form['employee_id'],
            title=request.form['title'],
            amount=float(request.form['amount']),
        )
        db.session.add(bonus)
        db.session.flush()
        svc.bonus_journal(db, M, bonus, current_user.id)
        db.session.commit()
        flash('تم تسجيل المكافأة والقيد المحاسبي', 'success')
        return redirect(url_for('hrm_bonuses'))

    # ── Contracts / Documents / Performance ─────────────────────
    @app.route('/hrm/contracts')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_contracts():
        items = M['HrmContract'].query.order_by(M['HrmContract'].start_date.desc()).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/contracts.html', contracts=items, employees=employees)

    @app.route('/hrm/contracts/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_employees')
    def hrm_contract_save():
        c = M['HrmContract'](
            employee_id=request.form['employee_id'],
            contract_type=request.form.get('contract_type', 'permanent'),
            start_date=datetime.strptime(request.form['start_date'], '%Y-%m-%d').date(),
            salary=float(request.form.get('salary', 0) or 0),
            notes=request.form.get('notes'),
        )
        ed = request.form.get('end_date')
        if ed:
            c.end_date = datetime.strptime(ed, '%Y-%m-%d').date()
        db.session.add(c)
        db.session.commit()
        flash('تم حفظ العقد', 'success')
        return redirect(url_for('hrm_contracts'))

    @app.route('/hrm/documents')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_documents():
        items = M['HrmEmployeeDocument'].query.order_by(M['HrmEmployeeDocument'].uploaded_at.desc()).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/documents.html', documents=items, employees=employees)

    @app.route('/hrm/documents/upload', methods=['POST'])
    @login_required
    @hrm_required('hrm_employees')
    def hrm_document_upload():
        doc = M['HrmEmployeeDocument'](
            employee_id=request.form['employee_id'],
            title=request.form['title'],
            doc_type=request.form.get('doc_type'),
        )
        ed = request.form.get('expiry_date')
        if ed:
            doc.expiry_date = datetime.strptime(ed, '%Y-%m-%d').date()
        f = request.files.get('file')
        if f and f.filename:
            fn = secure_filename(f'doc_{f.filename}')
            path = os.path.join(HRM_UPLOAD_DIR, fn)
            f.save(path)
            doc.file_path = f'hrm_uploads/{fn}'
        db.session.add(doc)
        db.session.commit()
        flash('تم رفع الوثيقة', 'success')
        return redirect(url_for('hrm_documents'))

    @app.route('/hrm/performance')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_performance():
        items = M['HrmPerformanceReview'].query.order_by(M['HrmPerformanceReview'].review_date.desc()).all()
        employees = Employee.query.filter_by(is_active=True).all()
        return render_template('hrm/performance.html', reviews=items, employees=employees)

    @app.route('/hrm/performance/save', methods=['POST'])
    @login_required
    @hrm_required('hrm_employees')
    def hrm_performance_save():
        r = M['HrmPerformanceReview'](
            employee_id=request.form['employee_id'],
            review_date=datetime.strptime(request.form['review_date'], '%Y-%m-%d').date(),
            period_label=request.form.get('period_label'),
            score=float(request.form.get('score', 0) or 0),
            strengths=request.form.get('strengths'),
            weaknesses=request.form.get('weaknesses'),
            goals=request.form.get('goals'),
            reviewer_id=current_user.id,
            status='completed',
        )
        db.session.add(r)
        db.session.commit()
        flash('تم حفظ التقييم', 'success')
        return redirect(url_for('hrm_performance'))

    # ── Payslips list ───────────────────────────────────────────
    @app.route('/hrm/payslips')
    @login_required
    @hrm_required('hrm_payroll')
    def hrm_payslips():
        payrolls = M['HrmPayroll'].query.filter(
            M['HrmPayroll'].status.in_(['approved', 'paid'])
        ).order_by(M['HrmPayroll'].period_year.desc()).limit(12).all()
        return render_template('hrm/payslips.html', payrolls=payrolls)

    # ── Reports ─────────────────────────────────────────────────
    @app.route('/hrm/reports')
    @login_required
    @hrm_required('hrm_reports')
    def hrm_reports():
        return render_template('hrm/reports.html')

    @app.route('/hrm/reports/<report_type>')
    @login_required
    @hrm_required('hrm_reports')
    def hrm_report_view(report_type):
        templates = {
            'attendance': 'hrm/report_attendance.html',
            'absence': 'hrm/report_absence.html',
            'payroll': 'hrm/report_payroll.html',
            'leaves': 'hrm/report_leaves.html',
            'loans': 'hrm/report_loans.html',
            'performance': 'hrm/report_performance.html',
        }
        tpl = templates.get(report_type)
        if not tpl:
            flash('تقرير غير معروف', 'error')
            return redirect(url_for('hrm_reports'))
        ctx = {'report_type': report_type}
        if report_type == 'attendance':
            ctx['records'] = M['HrmAttendance'].query.order_by(M['HrmAttendance'].att_date.desc()).limit(500).all()
        elif report_type == 'absence':
            ctx['records'] = M['HrmAttendance'].query.filter_by(status='absent').order_by(
                M['HrmAttendance'].att_date.desc()).limit(500).all()
        elif report_type == 'payroll':
            ctx['payrolls'] = M['HrmPayroll'].query.order_by(M['HrmPayroll'].period_year.desc()).all()
        elif report_type == 'leaves':
            ctx['leaves'] = M['HrmLeaveRequest'].query.order_by(M['HrmLeaveRequest'].created_at.desc()).limit(500).all()
        elif report_type == 'loans':
            ctx['loans'] = M['HrmEmployeeLoan'].query.all()
        elif report_type == 'performance':
            ctx['reviews'] = M['HrmPerformanceReview'].query.all()
        return render_template(tpl, **ctx)

    @app.route('/hrm/reports/<report_type>/export')
    @login_required
    @hrm_required('hrm_reports')
    def hrm_report_export(report_type):
        if report_type == 'attendance':
            rows = []
            for r in M['HrmAttendance'].query.limit(2000).all():
                rows.append([
                    r.employee_id, r.att_date, r.check_in, r.check_out,
                    r.working_hours, r.overtime_hours, r.delay_minutes, r.status,
                ])
            return svc.export_csv_response(
                rows,
                ['موظف', 'تاريخ', 'دخول', 'خروج', 'ساعات', 'إضافي', 'تأخير', 'حالة'],
                'attendance.csv',
            )
        return redirect(url_for('hrm_reports'))

    # ── REST API ────────────────────────────────────────────────
    @app.route('/api/hrm/employees')
    @login_required
    def api_hrm_employees():
        if not hrm_can('hrm_employees'):
            return jsonify({'error': 'forbidden'}), 403
        page = request.args.get('page', 1, type=int)
        q = request.args.get('q', '')
        query = Employee.query
        if q:
            query = query.filter(Employee.name.contains(q))
        p = query.paginate(page=page, per_page=20, error_out=False)
        return jsonify({
            'items': [{
                'id': e.id, 'code': e.code, 'name': e.name,
                'department_id': getattr(e, 'department_id', None),
                'salary': e.salary, 'is_active': e.is_active,
            } for e in p.items],
            'total': p.total, 'pages': p.pages, 'page': page,
        })

    @app.route('/api/hrm/employees', methods=['POST'])
    @login_required
    def api_hrm_employee_create():
        if not hrm_can('hrm_employees'):
            return jsonify({'error': 'forbidden'}), 403
        data = request.get_json(silent=True) or {}
        if not data.get('name'):
            return jsonify({'error': 'name required'}), 400
        code = data.get('code') or allocate_entity_code('E', Employee)
        emp = Employee(code=code, name=data['name'], salary=data.get('salary', 0))
        db.session.add(emp)
        db.session.commit()
        return jsonify({'ok': True, 'id': emp.id}), 201

    @app.route('/api/hrm/employees/<int:id>', methods=['PUT', 'DELETE'])
    @login_required
    def api_hrm_employee_item(id):
        emp = Employee.query.get_or_404(id)
        if not hrm_can('hrm_employees'):
            return jsonify({'error': 'forbidden'}), 403
        if request.method == 'DELETE':
            emp.is_active = False
            db.session.commit()
            return jsonify({'ok': True})
        data = request.get_json(silent=True) or {}
        for field in ('name', 'phone', 'email', 'salary', 'national_id', 'address'):
            if field in data:
                setattr(emp, field, data[field])
        db.session.commit()
        return jsonify({'ok': True, 'id': emp.id})

    @app.route('/api/hrm/dashboard')
    @login_required
    def api_hrm_dashboard():
        if not hrm_can('hrm_dashboard'):
            return jsonify({'error': 'forbidden'}), 403
        stats = svc.hr_dashboard_stats(db, M)
        dl, dv = svc.department_chart_data(db, M)
        pl, pv = svc.payroll_chart_data(db, M)
        return jsonify({'stats': stats, 'departments': {'labels': dl, 'values': dv},
                        'payroll': {'labels': pl, 'values': pv}})

    @app.route('/api/hrm/notifications')
    @login_required
    def api_hrm_notifications():
        q = M['HrmNotification'].query.filter_by(is_read=False)
        if current_user.role not in ('developer', 'admin', 'hr_manager'):
            q = q.filter(
                db.or_(
                    M['HrmNotification'].user_id == current_user.id,
                    M['HrmNotification'].user_id == None,
                )
            )
        items = q.order_by(M['HrmNotification'].created_at.desc()).limit(20).all()
        return jsonify({'items': [{
            'id': n.id, 'title': n.title, 'message': n.message, 'link': n.link, 'type': n.ntype,
        } for n in items], 'count': len(items)})

    return {
        'hrm_can': hrm_can,
        'seed_hrm': lambda: svc.seed_leave_types(db, M),
    }
