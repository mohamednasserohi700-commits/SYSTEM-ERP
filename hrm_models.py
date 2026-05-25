"""نماذج وحدة الموارد البشرية HRM — تُستورد بعد تهيئة db في app.py"""
from datetime import datetime, date


def init_hrm_models(db):
    """تسجيل نماذج HRM على نفس db الخاص بالتطبيق."""

    class HrmDepartment(db.Model):
        __tablename__ = 'hrm_department'
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(120), nullable=False)
        manager_id = db.Column(db.Integer, db.ForeignKey('employee.id'))
        description = db.Column(db.Text)
        is_active = db.Column(db.Boolean, default=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        manager = db.relationship('Employee', foreign_keys=[manager_id], backref='managed_departments')
        designations = db.relationship('HrmDesignation', backref='department', lazy=True)

    class HrmDesignation(db.Model):
        __tablename__ = 'hrm_designation'
        id = db.Column(db.Integer, primary_key=True)
        title = db.Column(db.String(120), nullable=False)
        department_id = db.Column(db.Integer, db.ForeignKey('hrm_department.id'))
        description = db.Column(db.Text)
        is_active = db.Column(db.Boolean, default=True)

    class HrmLeaveType(db.Model):
        __tablename__ = 'hrm_leave_type'
        id = db.Column(db.Integer, primary_key=True)
        code = db.Column(db.String(30), unique=True, nullable=False)
        name = db.Column(db.String(80), nullable=False)
        days_per_year = db.Column(db.Float, default=0)
        is_paid = db.Column(db.Boolean, default=True)
        is_active = db.Column(db.Boolean, default=True)

    class HrmAttendance(db.Model):
        __tablename__ = 'hrm_attendance'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        att_date = db.Column(db.Date, nullable=False, index=True)
        check_in = db.Column(db.Time)
        check_out = db.Column(db.Time)
        working_hours = db.Column(db.Float, default=0)
        overtime_hours = db.Column(db.Float, default=0)
        delay_minutes = db.Column(db.Integer, default=0)
        status = db.Column(db.String(20), default='present')  # present, absent, leave, late
        source = db.Column(db.String(20), default='manual')  # manual, qr, biometric
        notes = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        employee = db.relationship('Employee', backref='attendance_records')
        __table_args__ = (db.UniqueConstraint('employee_id', 'att_date'),)

    class HrmAttendanceLog(db.Model):
        __tablename__ = 'hrm_attendance_log'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        log_time = db.Column(db.DateTime, default=datetime.utcnow)
        action = db.Column(db.String(20))  # check_in, check_out
        source = db.Column(db.String(20), default='manual')
        device_id = db.Column(db.String(80))
        employee = db.relationship('Employee', backref='attendance_logs')

    class HrmLeaveRequest(db.Model):
        __tablename__ = 'hrm_leave_request'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        leave_type_id = db.Column(db.Integer, db.ForeignKey('hrm_leave_type.id'), nullable=False)
        date_from = db.Column(db.Date, nullable=False)
        date_to = db.Column(db.Date, nullable=False)
        days_count = db.Column(db.Float, default=1)
        reason = db.Column(db.Text)
        status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
        manager_approved = db.Column(db.Boolean, default=False)
        manager_approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        manager_approved_at = db.Column(db.DateTime)
        hr_approved = db.Column(db.Boolean, default=False)
        hr_approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        hr_approved_at = db.Column(db.DateTime)
        rejected_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        rejected_at = db.Column(db.DateTime)
        rejection_reason = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        employee = db.relationship('Employee', backref='leave_requests')
        leave_type = db.relationship('HrmLeaveType')

    class HrmPayroll(db.Model):
        __tablename__ = 'hrm_payroll'
        id = db.Column(db.Integer, primary_key=True)
        period_month = db.Column(db.Integer, nullable=False)
        period_year = db.Column(db.Integer, nullable=False)
        title = db.Column(db.String(120))
        status = db.Column(db.String(20), default='draft')  # draft, approved, paid
        total_gross = db.Column(db.Float, default=0)
        total_net = db.Column(db.Float, default=0)
        total_deductions = db.Column(db.Float, default=0)
        approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        approved_at = db.Column(db.DateTime)
        paid_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        paid_at = db.Column(db.DateTime)
        payment_method = db.Column(db.String(30))  # cash, bank
        journal_accrual_id = db.Column(db.Integer, db.ForeignKey('hrm_journal_entry.id'))
        journal_payment_id = db.Column(db.Integer, db.ForeignKey('hrm_journal_entry.id'))
        expense_id = db.Column(db.Integer, db.ForeignKey('expense.id'))
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        details = db.relationship('HrmPayrollDetail', backref='payroll', lazy=True, cascade='all, delete-orphan')
        __table_args__ = (db.UniqueConstraint('period_month', 'period_year'),)

    class HrmPayrollDetail(db.Model):
        __tablename__ = 'hrm_payroll_detail'
        id = db.Column(db.Integer, primary_key=True)
        payroll_id = db.Column(db.Integer, db.ForeignKey('hrm_payroll.id'), nullable=False)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        basic_salary = db.Column(db.Float, default=0)
        allowances = db.Column(db.Float, default=0)
        bonuses = db.Column(db.Float, default=0)
        overtime = db.Column(db.Float, default=0)
        deductions = db.Column(db.Float, default=0)
        loans = db.Column(db.Float, default=0)
        taxes = db.Column(db.Float, default=0)
        insurance = db.Column(db.Float, default=0)
        net_salary = db.Column(db.Float, default=0)
        is_paid = db.Column(db.Boolean, default=False)
        paid_at = db.Column(db.DateTime)
        payment_method = db.Column(db.String(30))
        expense_id = db.Column(db.Integer, db.ForeignKey('expense.id'))
        employee = db.relationship('Employee')

    class HrmEmployeeLoan(db.Model):
        __tablename__ = 'hrm_employee_loan'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        amount = db.Column(db.Float, nullable=False)
        remaining = db.Column(db.Float, default=0)
        monthly_deduction = db.Column(db.Float, default=0)
        start_date = db.Column(db.Date)
        status = db.Column(db.String(20), default='active')  # active, closed
        notes = db.Column(db.Text)
        journal_id = db.Column(db.Integer, db.ForeignKey('hrm_journal_entry.id'))
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        employee = db.relationship('Employee', backref='loans')

    class HrmEmployeeDeduction(db.Model):
        __tablename__ = 'hrm_employee_deduction'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        title = db.Column(db.String(120), nullable=False)
        amount = db.Column(db.Float, nullable=False)
        deduction_date = db.Column(db.Date, default=date.today)
        is_recurring = db.Column(db.Boolean, default=False)
        status = db.Column(db.String(20), default='active')
        journal_id = db.Column(db.Integer, db.ForeignKey('hrm_journal_entry.id'))
        employee = db.relationship('Employee', backref='deductions')

    class HrmEmployeeBonus(db.Model):
        __tablename__ = 'hrm_employee_bonus'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        title = db.Column(db.String(120), nullable=False)
        amount = db.Column(db.Float, nullable=False)
        bonus_date = db.Column(db.Date, default=date.today)
        status = db.Column(db.String(20), default='approved')
        journal_id = db.Column(db.Integer, db.ForeignKey('hrm_journal_entry.id'))
        employee = db.relationship('Employee', backref='bonuses')

    class HrmEmployeeDocument(db.Model):
        __tablename__ = 'hrm_employee_document'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        title = db.Column(db.String(200), nullable=False)
        doc_type = db.Column(db.String(60))
        file_path = db.Column(db.String(300))
        expiry_date = db.Column(db.Date)
        notes = db.Column(db.Text)
        uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
        employee = db.relationship('Employee', backref='documents')

    class HrmContract(db.Model):
        __tablename__ = 'hrm_contract'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        contract_type = db.Column(db.String(40))  # permanent, temporary, probation
        start_date = db.Column(db.Date, nullable=False)
        end_date = db.Column(db.Date)
        salary = db.Column(db.Float, default=0)
        status = db.Column(db.String(20), default='active')
        notes = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        employee = db.relationship('Employee', backref='contracts')

    class HrmPerformanceReview(db.Model):
        __tablename__ = 'hrm_performance_review'
        id = db.Column(db.Integer, primary_key=True)
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
        review_date = db.Column(db.Date, nullable=False)
        period_label = db.Column(db.String(80))
        score = db.Column(db.Float, default=0)
        strengths = db.Column(db.Text)
        weaknesses = db.Column(db.Text)
        goals = db.Column(db.Text)
        reviewer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
        status = db.Column(db.String(20), default='draft')
        employee = db.relationship('Employee', backref='performance_reviews')

    class HrmJournalEntry(db.Model):
        """قيود محاسبية لربط HR بالحسابات."""
        __tablename__ = 'hrm_journal_entry'
        id = db.Column(db.Integer, primary_key=True)
        entry_date = db.Column(db.DateTime, default=datetime.utcnow)
        reference_type = db.Column(db.String(40))  # payroll_accrual, payroll_payment, loan, bonus, deduction
        reference_id = db.Column(db.Integer)
        description = db.Column(db.Text)
        total_debit = db.Column(db.Float, default=0)
        total_credit = db.Column(db.Float, default=0)
        created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        lines = db.relationship('HrmJournalLine', backref='entry', lazy=True, cascade='all, delete-orphan')

    class HrmJournalLine(db.Model):
        __tablename__ = 'hrm_journal_line'
        id = db.Column(db.Integer, primary_key=True)
        entry_id = db.Column(db.Integer, db.ForeignKey('hrm_journal_entry.id'), nullable=False)
        account_code = db.Column(db.String(40), nullable=False)
        account_name = db.Column(db.String(120))
        debit = db.Column(db.Float, default=0)
        credit = db.Column(db.Float, default=0)

    class HrmNotification(db.Model):
        __tablename__ = 'hrm_notification'
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
        employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'))
        ntype = db.Column(db.String(40))
        title = db.Column(db.String(200))
        message = db.Column(db.Text)
        link = db.Column(db.String(200))
        is_read = db.Column(db.Boolean, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    return {
        'HrmDepartment': HrmDepartment,
        'HrmDesignation': HrmDesignation,
        'HrmLeaveType': HrmLeaveType,
        'HrmAttendance': HrmAttendance,
        'HrmAttendanceLog': HrmAttendanceLog,
        'HrmLeaveRequest': HrmLeaveRequest,
        'HrmPayroll': HrmPayroll,
        'HrmPayrollDetail': HrmPayrollDetail,
        'HrmEmployeeLoan': HrmEmployeeLoan,
        'HrmEmployeeDeduction': HrmEmployeeDeduction,
        'HrmEmployeeBonus': HrmEmployeeBonus,
        'HrmEmployeeDocument': HrmEmployeeDocument,
        'HrmContract': HrmContract,
        'HrmPerformanceReview': HrmPerformanceReview,
        'HrmJournalEntry': HrmJournalEntry,
        'HrmJournalLine': HrmJournalLine,
        'HrmNotification': HrmNotification,
    }
