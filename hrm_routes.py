"""مسارات وواجهات API لوحدة الموارد البشرية."""
from datetime import datetime, date, time, timedelta
from functools import wraps
import os
import json

from flask import render_template, request, redirect, url_for, flash, jsonify, send_file, current_app, send_from_directory
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

    def photo_url(photo_path):
        if not photo_path:
            return None
        fn = photo_path.split('/')[-1] if '/' in photo_path else photo_path
        return url_for('hrm_upload', filename=fn)

    def enrich_employees(items):
        """إرفاق بيانات العرض لكل موظف."""
        enriched = []
        for e in items:
            info = svc.resolve_employee_display(e, M)
            enriched.append({'emp': e, 'dept': info['dept_name'], 'position': info['position'], 'status_label': info['status_label']})
        return enriched

    @app.route('/hrm/uploads/<path:filename>')
    @login_required
    def hrm_upload(filename):
        return send_from_directory(HRM_UPLOAD_DIR, filename)

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
        abs_labels, abs_rates = svc.absence_rate_chart_data(db, M)
        pending_leaves = M['HrmLeaveRequest'].query.filter_by(status='pending').count()
        return render_template(
            'hrm/dashboard.html',
            stats=stats,
            dept_labels=dept_labels, dept_values=dept_vals,
            payroll_labels=pay_labels, payroll_values=pay_vals,
            att_labels=att_labels, att_present=att_present, att_absent=att_absent,
            abs_labels=abs_labels, abs_rates=abs_rates,
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
        sort = request.args.get('sort', 'id')
        order = request.args.get('order', 'desc')
        if search:
            q = q.filter(
                db.or_(
                    Employee.name.contains(search),
                    Employee.code.contains(search),
                    Employee.phone.contains(search),
                    Employee.national_id.contains(search),
                    Employee.department.contains(search),
                )
            )
        if dept_id:
            q = q.filter_by(department_id=dept_id)
        if status == 'active':
            q = q.filter_by(is_active=True)
        elif status == 'inactive':
            q = q.filter_by(is_active=False)
        elif status == 'on_leave':
            today = date.today()
            leave_ids = db.session.query(M['HrmLeaveRequest'].employee_id).filter(
                M['HrmLeaveRequest'].status == 'approved',
                M['HrmLeaveRequest'].date_from <= today,
                M['HrmLeaveRequest'].date_to >= today,
            ).distinct()
            q = q.filter(Employee.id.in_(leave_ids))
        sort_map = {
            'name': Employee.name, 'code': Employee.code,
            'salary': Employee.salary, 'id': Employee.id,
        }
        col = sort_map.get(sort, Employee.id)
        q = q.order_by(col.asc() if order == 'asc' else col.desc())
        pagination = paginate_query(q, default=20)
        departments = M['HrmDepartment'].query.filter_by(is_active=True).all()
        emp_stats = svc.employee_list_stats(db, M)
        rows = enrich_employees(pagination.items)
        return render_template(
            'hrm/employees.html',
            employees=rows,
            pagination=pagination,
            departments=departments,
            emp_stats=emp_stats,
            search=search, dept_id=dept_id, status=status,
            sort=sort, order=order,
            photo_url=photo_url,
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

    @app.route('/hrm/employees/export/pdf')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employees_export_pdf():
        rows = []
        for e in Employee.query.order_by(Employee.name).all():
            info = svc.resolve_employee_display(e, M)
            rows.append([
                e.code, e.name, info['dept_name'], info['position'],
                e.phone or '', f'{e.salary or 0:,.2f}', info['status_label'],
            ])
        return svc.export_print_html(
            'قائمة الموظفين',
            ['الكود', 'الاسم', 'القسم', 'الوظيفة', 'الهاتف', 'الراتب', 'الحالة'],
            rows,
        )

    @app.route('/hrm/employees/bulk', methods=['POST'])
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employees_bulk():
        action = request.form.get('action')
        ids = request.form.getlist('employee_ids')
        if not ids:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': False, 'message': 'لم يتم اختيار موظفين'})
            flash('لم يتم اختيار موظفين', 'warning')
            return redirect(url_for('hrm_employees'))
        count = 0
        for eid in ids:
            emp = Employee.query.get(int(eid))
            if not emp:
                continue
            if action == 'deactivate':
                emp.is_active = False
                emp.employment_status = 'inactive'
                count += 1
            elif action == 'activate':
                emp.is_active = True
                emp.employment_status = 'active'
                count += 1
        db.session.commit()
        msg = f'تم تطبيق العملية على {count} موظف(ين)'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True, 'message': msg, 'count': count})
        flash(msg, 'success')
        return redirect(url_for('hrm_employees'))

    @app.route('/hrm/employees/<int:id>/activity')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employee_activity(id):
        emp = Employee.query.get_or_404(id)
        timeline = svc.employee_activity_timeline(db, M, id)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'employee': emp.name, 'timeline': timeline})
        return render_template('hrm/employee_activity.html', emp=emp, timeline=timeline)

    def _employee_qr_row(emp):
        info = svc.resolve_employee_display(emp, M)
        payload = svc.employee_qr_payload(emp)
        return {
            'emp': emp,
            'dept': info['dept_name'],
            'position': info['position'],
            'payload': payload,
            'qr_svg': svc.employee_qr_svg(payload),
        }

    @app.route('/hrm/employees/<int:id>/qr')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employee_qr(id):
        emp = Employee.query.get_or_404(id)
        return render_template(
            'hrm/employee_qr_print.html',
            employees=[_employee_qr_row(emp)],
            single=True,
        )

    @app.route('/hrm/employees/qr/print')
    @login_required
    @hrm_required('hrm_employees')
    def hrm_employees_qr_bulk():
        ids = request.args.getlist('ids', type=int)
        q = Employee.query.filter_by(is_active=True)
        if ids:
            q = q.filter(Employee.id.in_(ids))
        rows = [_employee_qr_row(emp) for emp in q.order_by(Employee.name).all()]
        return render_template('hrm/employee_qr_print.html', employees=rows, single=False)

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
            bio_id = (request.form.get('biometric_id') or '').strip() or None
            if bio_id:
                q = Employee.query.filter_by(biometric_id=bio_id)
                if emp:
                    q = q.filter(Employee.id != emp.id)
                if q.first():
                    flash('رقم البصمة مستخدم لموظف آخر', 'error')
                    return redirect(url_for('hrm_employee_form', id=id) if id else url_for('hrm_employee_form'))
            data['biometric_id'] = bio_id
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
    def _device_or_session_ok():
        data = request.get_json(silent=True) or {}
        key = (
            request.headers.get('X-HRM-Key') or request.headers.get('X-API-Key')
            or data.get('api_key') or request.args.get('api_key')
        )
        if svc.verify_device_api_key(key):
            return True
        return current_user.is_authenticated

    def _punch_from_request(source):
        data = request.get_json(silent=True) if request.is_json else {}
        if not data and request.form:
            data = request.form.to_dict()
        raw = data.get('scan') or data.get('employee_code') or data.get('code') or ''
        parsed = svc.parse_attendance_scan(raw)
        emp = svc.find_employee_for_punch(
            code=parsed.get('code') or raw,
            employee_id=data.get('employee_id') or parsed.get('employee_id'),
            biometric_id=data.get('biometric_id') or data.get('user_id') or data.get('pin'),
        )
        if not emp:
            return {'ok': False, 'error': 'employee_not_found', 'message': 'الموظف غير موجود'}, 404
        action = data.get('action')  # None = auto toggle
        result = svc.register_attendance_punch(
            db, M, emp, source=source,
            device_id=data.get('device_id'),
            force_action=action if action in ('check_in', 'check_out') else None,
        )
        if result.get('ok'):
            db.session.commit()
            return result, 200
        return result, 400

    @app.route('/hrm/attendance')
    @login_required
    @hrm_required('hrm_attendance')
    def hrm_attendance():
        att_date = request.args.get('date')
        d = datetime.strptime(att_date, '%Y-%m-%d').date() if att_date else date.today()
        records = M['HrmAttendance'].query.filter_by(att_date=d).all()
        cards = svc.attendance_dashboard_today(M, db) if d == date.today() else {}
        employees = Employee.query.filter_by(is_active=True).all()
        recent = svc.recent_attendance_logs(M, 15)
        api_key_set = bool(svc.get_biometric_api_key())
        return render_template(
            'hrm/attendance.html', records=records, att_date=d,
            cards=cards, employees=employees, recent_logs=recent,
            api_key_set=api_key_set,
        )

    @app.route('/hrm/attendance/devices', methods=['GET', 'POST'])
    @login_required
    @hrm_required('hrm_attendance')
    def hrm_attendance_devices():
        if request.method == 'POST':
            action = request.form.get('action', 'save')
            if action == 'generate_key':
                import secrets
                svc.save_biometric_api_key(db, secrets.token_urlsafe(24))
            else:
                svc.save_biometric_api_key(db, request.form.get('api_key', ''))
            db.session.commit()
            flash('تم حفظ إعدادات الأجهزة', 'success')
            return redirect(url_for('hrm_attendance_devices'))
        api_key = svc.get_biometric_api_key()
        base = request.url_root.rstrip('/')
        return render_template(
            'hrm/attendance_devices.html',
            api_key=api_key,
            punch_url=f'{base}/api/hrm/attendance/punch',
            biometric_url=f'{base}/api/hrm/biometric',
        )

    @app.route('/hrm/attendance/kiosk')
    @login_required
    @hrm_required('hrm_attendance')
    def hrm_attendance_kiosk():
        return render_template('hrm/attendance_kiosk.html')

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
            raw = (request.form.get('employee_code') or request.form.get('scan') or '').strip()
            parsed = svc.parse_attendance_scan(raw)
            emp = svc.find_employee_for_punch(
                code=parsed.get('code') or raw,
                employee_id=parsed.get('employee_id'),
            )
            if not emp:
                flash('كود الموظف غير صحيح', 'error')
                return redirect(url_for('hrm_attendance_qr'))
            result = svc.register_attendance_punch(db, M, emp, source='qr')
            db.session.commit()
            flash(result.get('message', 'تم'), 'success' if result.get('ok') else 'warning')
            return redirect(url_for('hrm_attendance_qr'))
        return render_template('hrm/attendance_qr.html')

    @app.route('/api/hrm/attendance/punch', methods=['POST'])
    def api_hrm_attendance_punch():
        """تسجيل حضور/انصراف — QR أو كود (مفتاح API أو جلسة)."""
        if not _device_or_session_ok():
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        payload = request.get_json(silent=True) or {}
        source = payload.get('source') or 'api'
        result, status = _punch_from_request(source)
        return jsonify(result), status

    @app.route('/api/hrm/attendance/recent')
    def api_hrm_attendance_recent():
        if not _device_or_session_ok():
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        return jsonify({'ok': True, 'items': svc.recent_attendance_logs(M, 25)})

    @app.route('/api/hrm/biometric', methods=['POST'])
    def api_hrm_biometric():
        """ربط أجهزة البصمة — يرسل biometric_id ويُسجّل تلقائياً."""
        if not _device_or_session_ok():
            return jsonify({'ok': False, 'error': 'unauthorized', 'message': 'مفتاح API غير صحيح'}), 401
        result, status = _punch_from_request('biometric')
        return jsonify(result), status

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
        elif step == 'hr':
            lr.hr_approved = True
            lr.hr_approved_by = current_user.id
            lr.hr_approved_at = datetime.utcnow()
        elif step == 'finance':
            lr.finance_approved = True
            lr.finance_approved_by = current_user.id
            lr.finance_approved_at = datetime.utcnow()
            if lr.manager_approved and lr.hr_approved:
                lr.status = 'approved'
                svc.dismiss_leave_notifications(db, M, employee_id=lr.employee_id, leave_id=lr.id)
        else:
            lr.hr_approved = True
            lr.hr_approved_by = current_user.id
            lr.hr_approved_at = datetime.utcnow()
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
        svc.dismiss_leave_notifications(db, M, employee_id=lr.employee_id, leave_id=lr.id)
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
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        q = (request.args.get('q') or '').strip()
        dept_id = request.args.get('department_id', type=int)
        status = request.args.get('status')
        sort = request.args.get('sort', 'name')
        order = request.args.get('order', 'asc')
        query = Employee.query
        if q:
            query = query.filter(db.or_(
                Employee.name.contains(q), Employee.code.contains(q),
                Employee.phone.contains(q), Employee.department.contains(q),
            ))
        if dept_id:
            query = query.filter_by(department_id=dept_id)
        if status == 'active':
            query = query.filter_by(is_active=True)
        elif status == 'inactive':
            query = query.filter_by(is_active=False)
        sort_map = {'name': Employee.name, 'code': Employee.code, 'salary': Employee.salary, 'id': Employee.id}
        col = sort_map.get(sort, Employee.name)
        query = query.order_by(col.asc() if order == 'asc' else col.desc())
        p = query.paginate(page=page, per_page=per_page, error_out=False)
        return jsonify({
            'items': [svc.serialize_employee(e, M, photo_url) for e in p.items],
            'total': p.total, 'pages': p.pages, 'page': page, 'per_page': per_page,
            'stats': svc.employee_list_stats(db, M),
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
        al, ap, ab = svc.attendance_30_days(db, M)
        abs_l, abs_r = svc.absence_rate_chart_data(db, M)
        return jsonify({
            'stats': stats,
            'departments': {'labels': dl, 'values': dv},
            'payroll': {'labels': pl, 'values': pv},
            'attendance': {'labels': al, 'present': ap, 'absent': ab},
            'absence_rate': {'labels': abs_l, 'values': abs_r},
        })

    @app.route('/api/hrm/notifications')
    @login_required
    def api_hrm_notifications():
        feed = svc.collect_hrm_notification_feed(db, M, current_user)
        return jsonify({
            'items': feed.get('items', []) + feed.get('stored', []),
            'pending_leaves': feed.get('pending_leaves', 0),
            'count': len(feed.get('items', [])) + len(feed.get('stored', [])),
        })

    return {
        'hrm_can': hrm_can,
        'seed_hrm': lambda: svc.seed_leave_types(db, M),
    }
