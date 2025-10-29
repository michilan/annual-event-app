"""
Simple MVP for the Rotary annual conference management system.

This Flask-based prototype implements:
  * QR code check‑in: visiting `/checkin/<int:attendee_id>` marks an attendee as checked in.
  * Voting page: after check‑in, attendees can vote on performance items (scores from 1–5).
  * Scoreboard: displays average scores per performance and updates automatically via client‑side polling.
  * Raffle draw: a privileged endpoint `/raffle/draw` picks a random eligible attendee (checked in & not drawn yet).
    The front‑end page `/raffle` shows a 5‑second animation before revealing the winner.

Database schema uses SQLite via SQLAlchemy for demonstration.  See models definitions below.

Note: Flask and SQLAlchemy are not installed in this environment; this code is provided as a template.
To run locally, install dependencies:
    pip install flask flask_sqlalchemy

Then execute:
    python app.py

The application will create a SQLite database `event.db` in the project directory.
"""

from datetime import datetime
import csv
import os
import random
from uuid import uuid4

import io
from io import TextIOWrapper

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import inspect, text, or_
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import OperationalError
from urllib.parse import urlencode


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'replace-with-a-secure-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///event.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Admin login decorator
from functools import wraps

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# Prize image upload configuration
PRIZE_IMAGE_SUBDIR = 'uploads/prizes'
app.config['PRIZE_IMAGE_SUBDIR'] = PRIZE_IMAGE_SUBDIR
app.config['PRIZE_IMAGE_UPLOAD_FOLDER'] = os.path.join(app.static_folder, PRIZE_IMAGE_SUBDIR)
os.makedirs(app.config['PRIZE_IMAGE_UPLOAD_FOLDER'], exist_ok=True)
ALLOWED_PRIZE_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

def parse_bool(value):
    """Utility to interpret form/CSV boolean inputs consistently."""
    if value is None:
        return False
    normalized = str(value).strip().lower()
    truthy_markers = {'1', 'true', 'yes', 'y', 't', 'on', '是', '有'}
    return normalized in truthy_markers



def allowed_prize_image(filename):
    """Check whether the uploaded prize image has an allowed file extension."""
    if not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_PRIZE_IMAGE_EXTENSIONS


def save_prize_image(file_storage):
    """Persist an uploaded prize image and return its relative static path."""
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if not filename:
        raise ValueError('invalid-filename')
    if not allowed_prize_image(filename):
        raise ValueError('invalid-extension')
    ext = os.path.splitext(filename)[1].lower()
    unique_name = f"{uuid4().hex}{ext}"
    destination = os.path.join(app.config['PRIZE_IMAGE_UPLOAD_FOLDER'], unique_name)
    file_storage.save(destination)
    relative_path = f"{app.config['PRIZE_IMAGE_SUBDIR']}/{unique_name}"
    return relative_path.replace('\\', '/')


def delete_prize_image(image_path):
    """Delete a previously uploaded prize image from disk."""
    if not image_path:
        return
    absolute_path = os.path.join(app.static_folder, image_path.replace('/', os.sep))
    if os.path.exists(absolute_path):
        try:
            os.remove(absolute_path)
        except OSError:
            pass


def get_raffle_eligible_attendees():
    """Return a list of attendees eligible for raffle draws."""
    drawn_ids = db.session.query(RaffleResult.attendee_id).distinct()
    return (
        Attendee.query.filter(
            Attendee.event_checked_in.is_(True),
            Attendee.show_checked_in.is_(True),
            Attendee.has_voted.is_(True),
            Attendee.has_drawn.is_(False),
            ~Attendee.id.in_(drawn_ids)
        )
        .order_by(Attendee.id)
        .all()
    )





def perform_draw_for_prize(prize):
    """Draw a winner for a specific prize. Returns (result, winner) or (None, None)."""
    if prize.awarded_count >= prize.quantity:
        return None, None
    eligible = get_raffle_eligible_attendees()
    if not eligible:
        return None, None
    winner = random.choice(eligible)
    winner.has_drawn = True
    prize.awarded_count += 1
    result = RaffleResult(
        attendee_id=winner.id,
        performance_id=prize.performance_id,
        prize_id=prize.id,
        phase=prize.phase,
        prize_name=prize.name,
        prize_description=prize.description
    )
    db.session.add(result)
    db.session.flush()
    return result, winner


def perform_phase_draw(phase):
    """Draw a winner for the given phase. Returns (result, winner, prize, error_key)."""
    available_prizes = Prize.query.filter(
        Prize.phase == phase,
        Prize.awarded_count < Prize.quantity
    ).order_by(Prize.id).all()
    if not available_prizes:
        return None, None, None, 'no_prizes'
    selected_prize = available_prizes[0]
    result, winner = perform_draw_for_prize(selected_prize)
    if not result:
        return None, None, selected_prize, 'no_eligible_attendee'
    return result, winner, selected_prize, None


def get_next_phase_with_available_prizes():
    """Return the lowest phase number that still has prizes remaining."""
    row = db.session.query(Prize.phase).filter(Prize.awarded_count < Prize.quantity).order_by(Prize.phase).first()
    return row[0] if row else None


def snapshot_raffle_result(result):
    """Capture essential data for a raffle result so it can be restored."""
    return {
        'attendee_id': result.attendee_id,
        'performance_id': result.performance_id,
        'phase': result.phase,
        'prize_id': result.prize_id,
        'prize_name': result.prize_name,
        'prize_description': result.prize_description,
        'drawn_at': result.drawn_at
    }


def release_raffle_result(result, flush_only=False):
    """Remove an existing raffle result, returning its snapshot and associated prize."""
    snapshot = snapshot_raffle_result(result)
    prize = result.prize
    if result.attendee:
        result.attendee.has_drawn = False
    if prize and prize.awarded_count > 0:
        prize.awarded_count -= 1
    db.session.delete(result)
    if flush_only:
        db.session.flush()
    else:
        db.session.commit()
    return snapshot, prize


def restore_raffle_result(snapshot):
    """Re-create a raffle result from a snapshot if needed."""
    restored = RaffleResult(
        attendee_id=snapshot['attendee_id'],
        performance_id=snapshot['performance_id'],
        prize_id=snapshot['prize_id'],
        phase=snapshot['phase'],
        prize_name=snapshot['prize_name'],
        prize_description=snapshot['prize_description']
    )
    if snapshot['drawn_at']:
        restored.drawn_at = snapshot['drawn_at']
    db.session.add(restored)
    attendee = Attendee.query.get(snapshot['attendee_id'])
    if attendee:
        attendee.has_drawn = True
    if snapshot['prize_id']:
        prize = Prize.query.get(snapshot['prize_id'])
        if prize:
            prize.awarded_count += 1
    db.session.flush()
    return restored


def admin_required(f):
    """Decorator to ensure the current user is logged in as admin."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def operator_required(f):
    """Decorator to ensure the current user is logged in as a club operator."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('operator_id'):
            return redirect(url_for('operator_login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def current_operator():
    operator_id = session.get('operator_id')
    if operator_id:
        return Operator.query.get(operator_id)
    return None


@app.route('/operator/login', methods=['GET', 'POST'])
def operator_login():
    """Allow club operators to authenticate and access their dashboard."""
    error = None
    if request.method == 'POST':
        account = (request.form.get('account') or '').strip()
        password = request.form.get('password') or ''
        if not account or not password:
            error = '請輸入帳號與密碼'
        else:
            operator = Operator.query.filter_by(login_account=account).first()
            if operator is None or not operator.check_password(password):
                error = '帳號或密碼錯誤，請再試一次'
            else:
                session['operator_id'] = operator.id
                next_url = request.args.get('next')
                return redirect(next_url or url_for('operator_dashboard'))
    return render_template('operator/login.html', error=error)


@app.route('/operator/logout')
def operator_logout():
    session.pop('operator_id', None)
    return redirect(url_for('operator_login'))


@app.route('/operator')
@operator_required
def operator_dashboard():
    operator = current_operator()
    if operator is None:
        return redirect(url_for('operator_login'))

    attendees = (
        Attendee.query.filter(Attendee.club_name == operator.club_name)
        .order_by(Attendee.id)
        .all()
    )

    raffle_results = (
        RaffleResult.query.join(Attendee)
        .options(joinedload(RaffleResult.attendee), joinedload(RaffleResult.prize))
        .filter(Attendee.club_name == operator.club_name)
        .order_by(RaffleResult.drawn_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        'operator/dashboard.html',
        operator=operator,
        attendees=attendees,
        raffle_results=raffle_results
    )


@app.route('/operator/checkin/<stage>/<int:attendee_id>', methods=['POST'])
@operator_required
def operator_checkin(stage, attendee_id):
    operator = current_operator()
    attendee = Attendee.query.get_or_404(attendee_id)
    if operator and attendee.club_name != operator.club_name:
        flash('無法操作其他社的參加者。', 'warning')
        return redirect(url_for('operator_dashboard'))

    stage = stage.lower()
    if stage not in {'event', 'show', 'reset'}:
        flash('未知的操作類型。', 'danger')
        return redirect(url_for('operator_dashboard'))

    if stage == 'event':
        if not attendee.event_checked_in:
            attendee.event_checked_in = True
            flash(f'{attendee.name} 已完成第一階段報到。', 'success')
        else:
            flash(f'{attendee.name} 已完成第一階段報到。', 'info')
    elif stage == 'show':
        if not attendee.event_checked_in:
            flash('請先完成第一階段報到，才能進行節目報到。', 'warning')
        elif attendee.show_checked_in:
            flash(f'{attendee.name} 已完成節目報到。', 'info')
        else:
            attendee.show_checked_in = True
            flash(f'{attendee.name} 已完成節目報到。', 'success')
    else:  # reset
        attendee.event_checked_in = False
        attendee.show_checked_in = False
        flash(f'{attendee.name} 的報到狀態已重置。', 'info')

    db.session.commit()
    return redirect(url_for('operator_dashboard'))


@app.route('/vote/<int:attendee_id>', methods=['GET', 'POST'])
def vote(attendee_id):
    attendee = Attendee.query.get_or_404(attendee_id)
    if session.get('participant_attendee_id') != attendee.id:
        flash('請先登入後再進行節目評分。', 'warning')
        return redirect(url_for('index', attendee_id=attendee_id))
    if not attendee.show_checked_in:
        flash('請完成節目報到後再進行評分。', 'warning')
        return redirect(url_for('index', attendee_id=attendee.id))

    performances = Performance.query.order_by(Performance.id).all()
    existing_votes = {
        vote.performance_id: vote.score
        for vote in Vote.query.filter_by(attendee_id=attendee.id).all()
    }

    if request.method == 'POST':
        form_scores: dict[int, int] = {}
        errors = []
        for performance in performances:
            raw_value = request.form.get(f'perf_{performance.id}')
            if raw_value is None:
                errors.append('請為所有節目給分後再送出。')
                break
            try:
                score = int(raw_value)
            except ValueError:
                errors.append('評分必須介於 1 到 5 分之間。')
                break
            if score < 1 or score > 5:
                errors.append('評分必須介於 1 到 5 分之間。')
                break
            form_scores[performance.id] = score

        if errors:
            for message in errors:
                flash(message, 'warning')
            existing_votes.update(form_scores)
        else:
            for performance in performances:
                score = form_scores[performance.id]
                vote_row = Vote.query.filter_by(
                    attendee_id=attendee.id,
                    performance_id=performance.id
                ).first()
                if vote_row:
                    vote_row.score = score
                else:
                    db.session.add(Vote(
                        attendee_id=attendee.id,
                        performance_id=performance.id,
                        score=score
                    ))
            attendee.has_voted = True
            db.session.commit()
            flash('評分已成功送出。', 'success')
            return redirect(url_for('vote_thankyou', attendee_id=attendee.id))

    return render_template(
        'vote.html',
        attendee=attendee,
        performances=performances,
        existing_votes=existing_votes
    )


@app.route('/vote/<int:attendee_id>/thankyou')
def vote_thankyou(attendee_id):
    attendee = Attendee.query.get_or_404(attendee_id)
    if session.get('participant_attendee_id') != attendee.id:
        flash('請先登入後再查看評分結果。', 'warning')
        return redirect(url_for('index', attendee_id=attendee_id))

    votes = (
        Vote.query.filter_by(attendee_id=attendee.id)
        .options(joinedload(Vote.performance))
        .order_by(Vote.performance_id)
        .all()
    )
    votes_summary = [
        {
            'title': vote.performance.title if vote.performance else '未命名節目',
            'score': vote.score
        }
        for vote in votes
    ]
    raffle_result = (
        RaffleResult.query.filter_by(attendee_id=attendee.id)
        .order_by(RaffleResult.drawn_at.desc())
        .first()
    )
    return render_template(
        'thankyou.html',
        attendee=attendee,
        votes_summary=votes_summary,
        raffle_result=raffle_result
    )


def apply_attendee_filters(query, params):
    """Apply search and status filters to an attendee query."""
    search = (params.get('q') or '').strip()
    if search:
        like = f"%{search}%"
        conditions = [
            Attendee.login_account.ilike(like),
            Attendee.name.ilike(like),
            Attendee.call_name.ilike(like),
            Attendee.club_name.ilike(like),
            Attendee.district.ilike(like)
        ]
        if search.isdigit():
            conditions.append(Attendee.id == int(search))
        query = query.filter(or_(*conditions))
    status_flags = {}
    status_columns = {
        'event': Attendee.event_checked_in,
        'show': Attendee.show_checked_in,
        'voted': Attendee.has_voted,
        'drawn': Attendee.has_drawn
    }
    for key, column in status_columns.items():
        if params.get(key) == '1':
            query = query.filter(column.is_(True))
            status_flags[key] = True
        else:
            status_flags[key] = False
    return query, search, status_flags


class Attendee(db.Model):
    """Attendee model representing registered participants.

    Fields:
        id: primary key (int)
        name: full name
        call_name: nickname
        club_name: club or affiliation
        district: district/area
        event_checked_in: boolean indicating completion of general conference check-in
        show_checked_in: boolean indicating completion of program hall check-in
        has_drawn: boolean indicating if this attendee has already won the raffle
        created_at: timestamp
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    call_name = db.Column(db.String(100))
    club_name = db.Column(db.String(100))
    district = db.Column(db.String(20))
    event_checked_in = db.Column('checked_in', db.Boolean, default=False)
    show_checked_in = db.Column(db.Boolean, default=False)
    login_account = db.Column(db.String(64), unique=True)
    password_hash = db.Column(db.String(255))
    has_drawn = db.Column(db.Boolean, default=False)
    # 新增欄位：是否已投票，用於抽獎資格判斷
    has_voted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    votes = db.relationship('Vote', backref='attendee', lazy=True)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)


class Operator(db.Model):
    """Operator (secretary) responsible for check-in of a specific club."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    club_name = db.Column(db.String(120), nullable=False)
    login_account = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Performance(db.Model):
    """Performance model representing a performance item in the schedule.

    Fields:
        id: primary key (int)
        title: performance title
        description: performance description
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)

    votes = db.relationship('Vote', backref='performance', lazy=True)


class Prize(db.Model):
    """Prize model representing a raffle prize assigned to a performance.

    Each prize can have a quantity >1 (e.g., two prizes for each performance).  When
    ``awarded_count`` reaches ``quantity``, the prize is fully distributed.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    performance_id = db.Column(db.Integer, db.ForeignKey('performance.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    awarded_count = db.Column(db.Integer, nullable=False, default=0)
    # Phase of the raffle this prize belongs to (1 or 2). Default is 1.
    phase = db.Column(db.Integer, nullable=False, default=1)
    image_path = db.Column(db.String(255))

    performance = db.relationship('Performance', backref='prizes')


class Vote(db.Model):
    """Vote model linking attendees to performances with a score (1–5).

    Fields:
        id: primary key (int)
        attendee_id: foreign key to Attendee
        performance_id: foreign key to Performance
        score: integer from 1–5
        created_at: timestamp
    """
    id = db.Column(db.Integer, primary_key=True)
    attendee_id = db.Column(db.Integer, db.ForeignKey('attendee.id'), nullable=False)
    performance_id = db.Column(db.Integer, db.ForeignKey('performance.id'), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RaffleResult(db.Model):
    """Model for storing raffle winners.

    Fields:
        id: primary key (int)
        attendee_id: foreign key to Attendee
        drawn_at: timestamp
    """
    id = db.Column(db.Integer, primary_key=True)
    attendee_id = db.Column(db.Integer, db.ForeignKey('attendee.id'), nullable=False)
    performance_id = db.Column(db.Integer, db.ForeignKey('performance.id'), nullable=False)
    prize_id = db.Column(db.Integer, db.ForeignKey('prize.id'))
    phase = db.Column(db.Integer, nullable=False, default=1)
    prize_name = db.Column(db.String(100))
    prize_description = db.Column(db.Text)
    drawn_at = db.Column(db.DateTime, default=datetime.utcnow)

    # relationships to fetch related objects easily
    performance = db.relationship('Performance', backref='raffle_results')
    attendee = db.relationship('Attendee')
    prize = db.relationship('Prize')


def ensure_raffle_result_columns():
    """Ensure new raffle result columns exist for legacy databases."""
    inspector = inspect(db.engine)
    if not inspector.has_table('raffle_result'):
        return
    existing_columns = {col['name'] for col in inspector.get_columns('raffle_result')}
    statements = []
    if 'prize_name' not in existing_columns:
        statements.append("ALTER TABLE raffle_result ADD COLUMN prize_name VARCHAR(100)")
    if 'prize_description' not in existing_columns:
        statements.append("ALTER TABLE raffle_result ADD COLUMN prize_description TEXT")
    if 'prize_id' not in existing_columns:
        statements.append("ALTER TABLE raffle_result ADD COLUMN prize_id INTEGER")
    if not statements:
        return
    with db.engine.connect() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except OperationalError:
                # Column likely already exists; ignore to keep startup resilient.
                pass


def ensure_attendee_columns():
    """Ensure attendee table has the expected two-stage check-in columns."""
    inspector = inspect(db.engine)
    if not inspector.has_table('attendee'):
        return
    existing_columns = {col['name'] for col in inspector.get_columns('attendee')}
    statements = []
    if 'checked_in' not in existing_columns:
        statements.append("ALTER TABLE attendee ADD COLUMN checked_in BOOLEAN DEFAULT 0")
    if 'show_checked_in' not in existing_columns:
        statements.append("ALTER TABLE attendee ADD COLUMN show_checked_in BOOLEAN DEFAULT 0")
    if 'login_account' not in existing_columns:
        statements.append("ALTER TABLE attendee ADD COLUMN login_account VARCHAR(64)")
    if 'password_hash' not in existing_columns:
        statements.append("ALTER TABLE attendee ADD COLUMN password_hash VARCHAR(255)")
    if not statements:
        return
    with db.engine.connect() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except OperationalError:
                pass


def ensure_prize_columns():
    """Ensure legacy prize tables include the image_path column."""
    inspector = inspect(db.engine)
    if not inspector.has_table('prize'):
        return
    existing_columns = {col['name'] for col in inspector.get_columns('prize')}
    if 'image_path' in existing_columns:
        return
    statement = "ALTER TABLE prize ADD COLUMN image_path VARCHAR(255)"
    with db.engine.connect() as conn:
        try:
            conn.execute(text(statement))
        except OperationalError:
            pass


def seed_sample_attendees(count=100):
    """Populate the attendee table with sample data when empty."""
    if Attendee.query.count() > 0:
        return
    first_names = ['建宏', '淑芬', '志強', '怡君', '玉霞', '俊宏', '雅婷', '國華', '惠美', '志玲']
    nicknames = ['Ming', 'Lily', 'David', 'Ray', 'Jane', 'John', 'Henry', 'Emily', 'Alan', 'Ivy']
    clubs = [
        '台中港扶輪社', '台中中區扶輪社', '台中大都扶輪社', '台中黎明扶輪社', '彰化扶輪社',
        '大里扶輪社', '豐原扶輪社', '潭子扶輪社'
    ]
    districts = ['D-1', 'D-2', 'D-3', 'D-4']
    attendees = []
    for i in range(1, count + 1):
        name = f"模擬參加者{i:03d}"
        call_name = random.choice(nicknames)
        club = random.choice(clubs)
        district = random.choice(districts)
        voted = (i % 2 == 0)
        drawn = voted and (i % 10 == 0)
        attendee = Attendee(
            name=name,
            call_name=call_name,
            club_name=club,
            district=district,
            event_checked_in=(i % 3 == 0),
            show_checked_in=(i % 5 == 0),
            has_voted=voted if (i % 5 == 0) else False,
            has_drawn=drawn if (i % 10 == 0) else False
        )
        attendee.login_account = f"user{i:03d}"
        attendee.set_password(attendee.login_account)
        attendees.append(attendee)
    db.session.add_all(attendees)
    db.session.commit()
    # create sample operators for seeded clubs
    existing_clubs = sorted(set(clubs))
    operators = []
    for idx, club in enumerate(existing_clubs, start=1):
        account = f"club{idx:03d}"
        operator = Operator(name=f'{club} 執秘', club_name=club, login_account=account)
        operator.set_password('club123')
        operators.append(operator)
    db.session.add_all(operators)
    db.session.commit()


def initialize_database():
    """
    Create database tables and seed initial data if not present.

    This function is no longer registered as a ``before_first_request`` handler,
    because newer versions of Flask removed that decorator. Instead, it should
    be invoked manually once during application startup.
    """
    db.create_all()
    ensure_attendee_columns()
    ensure_raffle_result_columns()
    ensure_prize_columns()
    if Attendee.query.count() == 0:
        sample_count = int(os.environ.get('SAMPLE_ATTENDEE_COUNT', 100))
        if sample_count > 0:
            seed_sample_attendees(sample_count)
    # Seed performances only if not existing
    if Performance.query.count() == 0:
        # 五個示例節目，每個節目將配置兩份獎品
        sample_performances = [
            Performance(title='開場舞', description='熱情的開場舞蹈'),
            Performance(title='爵士樂演奏', description='精緻的爵士樂表演'),
            Performance(title='戲劇演出', description='社友自編自演劇本'),
            Performance(title='魔術表演', description='神秘的魔術秀'),
            Performance(title='合唱團表演', description='和諧的合唱演出')
        ]
        db.session.add_all(sample_performances)
        db.session.commit()
    # Seed attendees only if not existing
    if Attendee.query.count() == 0:
        sample_attendees = [
            Attendee(name='王小明', call_name='Ming', club_name='台中港扶輪社', district='D-3'),
            Attendee(name='陳美麗', call_name='Lily', club_name='台中港扶輪社', district='D-3'),
            Attendee(name='李大勇', call_name='David', club_name='台中港都扶青社', district='D-3'),
            Attendee(name='張耀宏', call_name='Ray', club_name='台中港都扶輪社', district='D-3'),
            Attendee(name='吳淑珍', call_name='Jane', club_name='台中港都扶青社', district='D-3'),
            Attendee(name='林志明', call_name='John', club_name='台中港扶輪社', district='D-3'),
            Attendee(name='蔡宏仁', call_name='Henry', club_name='台中港扶輪社', district='D-3'),
            Attendee(name='鄭明慧', call_name='Emily', club_name='台中港都扶青社', district='D-3')
        ]
        db.session.add_all(sample_attendees)
        db.session.commit()
    # Seed prizes only if not existing
    if Prize.query.count() == 0:
        # Create two prizes per performance (10 prizes total)
        prizes = []
        performances = Performance.query.order_by(Performance.id).all()
        prize_names = [
            '早鳥禮券', '精品筆記本', '咖啡券', '精美水壺', '文創禮品',
            '電影票', '美食折價券', '運動毛巾', '紀念杯墊', '驚喜福袋'
        ]
        # Assign two prizes per performance: one for phase 1 and one for phase 2
        i = 0
        for perf in performances:
            for j in range(2):
                name = prize_names[i % len(prize_names)]
                phase = j + 1  # j=0 => phase1, j=1 => phase2
                prizes.append(Prize(name=name, description=f'{perf.title} 專屬禮品', performance_id=perf.id, phase=phase))
                i += 1
        db.session.add_all(prizes)
        db.session.commit()
    if Operator.query.count() == 0:
        clubs = [row[0] for row in db.session.query(Attendee.club_name).filter(Attendee.club_name.isnot(None)).distinct()]
        for idx, club in enumerate(clubs, start=1):
            base = ''.join(ch for ch in (club or '') if ch.isascii() and ch.isalnum())
            if not base:
                base = f'club{idx:03d}'
            account = base.lower()
            suffix = 1
            while Operator.query.filter_by(login_account=account).first():
                account = f"{base.lower()}{suffix}"
                suffix += 1
            operator = Operator(name=f'{club} 執秘' if club else f'操作員 {idx:03d}',
                                club_name=club or f'未命名社 {idx}',
                                login_account=account)
            operator.set_password(os.environ.get('OPERATOR_DEFAULT_PASSWORD', 'club123'))
            db.session.add(operator)
        db.session.commit()


# -----------------------------
# Admin authentication routes
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """
    Render and process the admin login form.  The admin password is configured via
    the ADMIN_PASSWORD environment variable.  Successful login sets session['admin_logged_in'].
    """
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            next_url = request.args.get('next') or url_for('admin_dashboard')
            return redirect(next_url)
        else:
            error = '密碼錯誤，請再試一次。'
    return render_template('admin/login.html', error=error)

@app.route('/admin/logout')
@admin_required
def admin_logout():
    """Log out the current admin user by clearing the session."""
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    """
    Admin dashboard page summarizing counts of attendees, performances, and prizes.
    """
    attendee_count = Attendee.query.count()
    performance_count = Performance.query.count()
    prize_count = Prize.query.count()
    return render_template('admin/dashboard.html', attendee_count=attendee_count,
                           performance_count=performance_count, prize_count=prize_count)


# -----------------------------
# Admin CRUD routes for Attendees

@app.route('/admin/attendees')
@admin_required
def admin_attendees():
    query = Attendee.query.order_by(Attendee.id)
    query, search, status_flags = apply_attendee_filters(query, request.args)
    page = request.args.get('page', default=1, type=int)
    if not page or page < 1:
        page = 1
    per_page = request.args.get('per_page', type=int)
    per_page_options = [25, 50, 100, 200, 300]
    if per_page not in per_page_options:
        per_page = 25
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    attendees = pagination.items
    query_params = request.args.to_dict(flat=True)
    query_params['per_page'] = str(per_page)
    pagination_params = {k: v for k, v in query_params.items() if k != 'page'}
    export_params = {k: v for k, v in pagination_params.items() if v not in (None, '')}
    export_url = url_for('admin_attendees_export')
    if export_params:
        export_url = f"{export_url}?{urlencode(export_params)}"
    def build_page_url(page_value):
        params = dict(pagination_params)
        params['page'] = page_value
        return url_for('admin_attendees', **params)
    return render_template(
        'admin/attendees/list.html',
        attendees=attendees,
        search=search,
        status_flags=status_flags,
        query_params=query_params,
        pagination=pagination,
        per_page=per_page,
        per_page_options=per_page_options,
        pagination_params=pagination_params,
        export_url=export_url,
        pagination_url=build_page_url
    )

@app.route('/admin/attendees/new', methods=['GET', 'POST'])
@admin_required
def admin_attendees_new():
    if request.method == 'POST':
        name = request.form.get('name')
        call_name = request.form.get('call_name')
        club_name = request.form.get('club_name')
        district = request.form.get('district')
        event_checked_in = bool(request.form.get('event_checked_in'))
        show_checked_in = bool(request.form.get('show_checked_in'))
        has_voted = bool(request.form.get('has_voted'))
        has_drawn = bool(request.form.get('has_drawn'))
        login_account = request.form.get('login_account', '').strip() or None
        password = request.form.get('password', '').strip()
        if login_account:
            existing = Attendee.query.filter_by(login_account=login_account).first()
            if existing:
                flash('登入帳號已存在，請使用其他帳號。', 'danger')
                return redirect(url_for('admin_attendees_new'))
        attendee = Attendee(name=name, call_name=call_name, club_name=club_name, district=district,
                            event_checked_in=event_checked_in, show_checked_in=show_checked_in,
                            has_voted=has_voted, has_drawn=has_drawn, login_account=login_account)
        db.session.add(attendee)
        db.session.flush()
        if not attendee.login_account:
            attendee.login_account = str(attendee.id)
        if password:
            attendee.set_password(password)
        elif attendee.login_account and not attendee.password_hash:
            attendee.set_password(attendee.login_account)
        db.session.commit()
        flash(f'已新增參加者：{attendee.name}（帳號：{attendee.login_account}）', 'success')
        return redirect(url_for('admin_attendees'))
    return render_template('admin/attendees/form.html', attendee=None)

@app.route('/admin/attendees/edit/<int:attendee_id>', methods=['GET', 'POST'])
@admin_required
def admin_attendees_edit(attendee_id):
    attendee = Attendee.query.get_or_404(attendee_id)
    if request.method == 'POST':
        attendee.name = request.form.get('name')
        attendee.call_name = request.form.get('call_name')
        attendee.club_name = request.form.get('club_name')
        attendee.district = request.form.get('district')
        attendee.event_checked_in = bool(request.form.get('event_checked_in'))
        attendee.show_checked_in = bool(request.form.get('show_checked_in'))
        attendee.has_voted = bool(request.form.get('has_voted'))
        attendee.has_drawn = bool(request.form.get('has_drawn'))
        login_account = request.form.get('login_account', '').strip()
        password = request.form.get('password', '').strip()
        if login_account:
            existing = Attendee.query.filter(Attendee.login_account == login_account, Attendee.id != attendee.id).first()
            if existing:
                flash('登入帳號已存在，請改用其他帳號。', 'danger')
                return redirect(url_for('admin_attendees_edit', attendee_id=attendee_id))
            attendee.login_account = login_account
        elif not attendee.login_account:
            attendee.login_account = str(attendee.id)
        if password:
            attendee.set_password(password)
        db.session.commit()
        flash(f'已更新參加者：{attendee.name}', 'success')
        return redirect(url_for('admin_attendees'))
    return render_template('admin/attendees/form.html', attendee=attendee)

@app.route('/admin/attendees/delete/<int:attendee_id>', methods=['POST'])
@admin_required
def admin_attendees_delete(attendee_id):
    attendee = Attendee.query.get_or_404(attendee_id)
    db.session.delete(attendee)
    db.session.commit()
    return redirect(url_for('admin_attendees'))


@app.route('/admin/attendees/bulk', methods=['POST'])
@admin_required
def admin_attendees_bulk():
    selected_ids = request.form.getlist('selected_ids')
    action = request.form.get('action')
    return_url = request.form.get('return_url') or url_for('admin_attendees')
    if not selected_ids or not action:
        flash('請選擇參加者與操作項目。', 'warning')
        return redirect(return_url)
    try:
        ids = [int(i) for i in selected_ids]
    except ValueError:
        flash('選取的參加者編號有誤。', 'danger')
        return redirect(return_url)
    attendees = Attendee.query.filter(Attendee.id.in_(ids)).all()
    if not attendees:
        flash('未找到選取的參加者。', 'warning')
        return redirect(return_url)
    updated = 0
    for attendee in attendees:
        if action == 'event_on':
            if not attendee.event_checked_in:
                attendee.event_checked_in = True
                updated += 1
        elif action == 'show_on':
            if attendee.event_checked_in and not attendee.show_checked_in:
                attendee.show_checked_in = True
                updated += 1
        elif action == 'vote_on':
            if not attendee.has_voted:
                attendee.has_voted = True
                updated += 1
        elif action == 'reset_flags':
            attendee.event_checked_in = False
            attendee.show_checked_in = False
            attendee.has_voted = False
            attendee.has_drawn = False
            updated += 1
    db.session.commit()
    flash(f'已更新 {updated} 位參加者。', 'success')
    return redirect(return_url)



@app.route('/raffle')
def raffle_page():
    return render_template('raffle.html')



@app.route('/api/raffle/latest')
def api_raffle_latest():
    latest = RaffleResult.query.options(
        joinedload(RaffleResult.attendee),
        joinedload(RaffleResult.performance),
        joinedload(RaffleResult.prize)
    ).order_by(RaffleResult.drawn_at.desc()).first()
    results = RaffleResult.query.options(
        joinedload(RaffleResult.attendee),
        joinedload(RaffleResult.performance),
        joinedload(RaffleResult.prize)
    ).order_by(RaffleResult.drawn_at.asc()).all()

    def serialize(result: RaffleResult):
        return {
            'id': result.id,
            'attendee_id': result.attendee_id,
            'name': result.attendee.name if result.attendee else None,
            'call_name': result.attendee.call_name if result.attendee else None,
            'club_name': result.attendee.club_name if result.attendee else None,
            'performance': result.performance.title if result.performance else None,
            'performance_id': result.performance_id,
            'prize_id': result.prize_id,
            'prize_name': result.prize_name,
            'prize_description': result.prize_description,
            'phase': result.phase,
            'drawn_at': result.drawn_at.isoformat() if result.drawn_at else None
        }

    return jsonify({
        'latest': serialize(latest) if latest else None,
        'results': [serialize(item) for item in results]
    })


def _raffle_error_message(error_key, phase=None):
    messages = {
        'no_prizes': '\u6c92\u6709\u53ef\u7528\u7684\u734e\u9805\u53ef\u4f9b\u62bd\u51fa\u3002',
        'no_available_slot': '\u734e\u9805\u7684\u540d\u984d\u5df2\u767c\u5b8c\uff0c\u7121\u6cd5\u518d\u62bd\u3002',
        'no_eligible_attendee': '\u76ee\u524d\u6c92\u6709\u7b26\u5408\u62bd\u734e\u8cc7\u683c\u7684\u53c3\u52a0\u8005\u3002'
    }
    message = messages.get(error_key, '\u62bd\u734e\u6642\u767c\u751f\u672a\u77e5\u932f\u8aa4\u3002')
    if phase is not None and error_key == 'no_prizes':
        return '\u7b2c ' + str(phase) + ' \u968e\u6bb5\u6c92\u6709\u53ef\u7528\u7684\u734e\u9805\u3002'
    return message


def _serialize_draw(result, winner, prize):
    performance_title = None
    if prize and getattr(prize, 'performance', None):
        performance_title = prize.performance.title
    elif getattr(result, 'performance', None):
        performance_title = result.performance.title
    return {
        'result_id': result.id,
        'attendee_id': winner.id if winner else None,
        'name': winner.name if winner else None,
        'call_name': winner.call_name if winner else None,
        'login_account': winner.login_account if winner else None,
        'club_name': winner.club_name if winner else None,
        'district': winner.district if winner else None,
        'performance_id': result.performance_id,
        'performance_title': performance_title,
        'prize_id': prize.id if prize else result.prize_id,
        'prize_name': prize.name if prize else result.prize_name,
        'prize_description': prize.description if prize else result.prize_description,
        'phase': result.phase,
        'drawn_at': result.drawn_at.isoformat() if result.drawn_at else None
    }


@app.route('/api/raffle/draw', methods=['POST'])
def api_raffle_draw():
    current_phase = get_next_phase_with_available_prizes()
    if current_phase is None:
        return jsonify({'error': '\u76ee\u524d\u6c92\u6709\u53ef\u7528\u7684\u734e\u9805\u53ef\u4f9b\u62bd\u51fa\u3002'}), 400
    result, winner, prize, error = perform_phase_draw(current_phase)
    if error:
        db.session.rollback()
        return jsonify({'error': _raffle_error_message(error, current_phase)}), 400
    db.session.commit()
    payload = _serialize_draw(result, winner, prize)
    return jsonify({
        'winner_id': payload['attendee_id'],
        'name': payload['name'],
        'call_name': payload['call_name'],
        'club_name': payload['club_name'],
        'performance': payload['performance_title'],
        'prize_id': payload['prize_id'],
        'prize_name': payload['prize_name'],
        'phase': payload['phase'],
        'result': payload
    })


@app.route('/api/raffle/draw_phase/<int:phase>', methods=['POST'])
@admin_required
def api_raffle_draw_phase(phase):
    result, winner, prize, error = perform_phase_draw(phase)
    if error:
        db.session.rollback()
        return jsonify({'error': _raffle_error_message(error, phase)}), 400
    db.session.commit()
    payload = _serialize_draw(result, winner, prize)
    return jsonify({
        'winner_id': payload['attendee_id'],
        'name': payload['name'],
        'call_name': payload['call_name'],
        'club_name': payload['club_name'],
        'performance': payload['performance_title'],
        'prize_id': payload['prize_id'],
        'prize_name': payload['prize_name'],
        'phase': payload['phase'],
        'result': payload
    })


@app.route('/api/raffle/draw_batch', methods=['POST'])
@admin_required
def api_raffle_draw_batch():
    data = request.get_json(silent=True) or {}
    try:
        phase = int(data.get('phase', 1))
    except (TypeError, ValueError):
        return jsonify({'error': '\u62bd\u734e\u968e\u6bb5\u5fc5\u9808\u662f\u6709\u6548\u7684\u6578\u5b57\u3002'}), 400
    try:
        count = int(data.get('count', 1))
    except (TypeError, ValueError):
        return jsonify({'error': '\u62bd\u51fa\u4eba\u6578\u5fc5\u9808\u662f\u6709\u6548\u7684\u6578\u5b57\u3002'}), 400
    if phase < 1:
        return jsonify({'error': '\u62bd\u734e\u968e\u6bb5\u5fc5\u9808\u5927\u65bc\u6216\u7b49\u65bc 1\u3002'}), 400
    if count < 1:
        return jsonify({'error': '\u62bd\u51fa\u4eba\u6578\u81f3\u5c11\u9700\u8981 1 \u4f4d\u3002'}), 400
    if count > 100:
        return jsonify({'error': '\u70ba\u907f\u514d\u64cd\u4f5c\u5931\u8aa4\uff0c\u4e00\u6b21\u6700\u591a\u53ea\u80fd\u62bd\u51fa 100 \u4f4d\u5f97\u734e\u8005\u3002'}), 400

    results_payload = []
    warning = None
    for _ in range(count):
        result, winner, prize, error = perform_phase_draw(phase)
        if error:
            if results_payload:
                warning = _raffle_error_message(error, phase)
                break
            db.session.rollback()
            return jsonify({'error': _raffle_error_message(error, phase)}), 400
        results_payload.append(_serialize_draw(result, winner, prize))

    if not results_payload:
        return jsonify({'error': '\u62bd\u734e\u672a\u7522\u751f\u4efb\u4f55\u5f97\u734e\u8005\u3002'}), 400

    db.session.commit()
    response = {
        'phase': phase,
        'results': results_payload,
        'count': len(results_payload)
    }
    if warning:
        response['warning'] = warning
    return jsonify(response)


@app.route('/admin/attendees/export')
@admin_required
def admin_attendees_export():
    query = Attendee.query.order_by(Attendee.id)
    query, search, status_flags = apply_attendee_filters(query, request.args)
    attendees = query.all()
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['ID', '登入帳號', '姓名', '暱稱', '扶輪社', '地區', '大會報到', '節目報到', '已評分', '已中獎'])
    for attendee in attendees:
        writer.writerow([
            attendee.id,
            attendee.login_account or '',
            attendee.name,
            attendee.call_name or '',
            attendee.club_name or '',
            attendee.district or '',
            '是' if attendee.event_checked_in else '否',
            '是' if attendee.show_checked_in else '否',
            '是' if attendee.has_voted else '否',
            '是' if attendee.has_drawn else '否'
        ])
    output.seek(0)
    response = Response(output.getvalue(), mimetype='text/csv; charset=utf-8')
    filename = f"attendees_export.csv"
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


# -----------------------------
# Admin CRUD routes for Operators

@app.route('/admin/operators')
@admin_required
def admin_operators():
    operators = Operator.query.order_by(Operator.id).all()
    return render_template('admin/operators/list.html', operators=operators)


@app.route('/admin/operators/new', methods=['GET', 'POST'])
@admin_required
def admin_operators_new():
    if request.method == 'POST':
        name = request.form.get('name')
        club_name = request.form.get('club_name')
        login_account = request.form.get('login_account', '').strip()
        password = request.form.get('password', '').strip()
        if not login_account:
            flash('請輸入登入帳號。', 'danger')
            return redirect(url_for('admin_operators_new'))
        if Operator.query.filter_by(login_account=login_account).first():
            flash('登入帳號已存在，請改用其他帳號。', 'danger')
            return redirect(url_for('admin_operators_new'))
        operator = Operator(name=name, club_name=club_name, login_account=login_account or club_name or name or 'operator')
        operator.set_password(password or login_account)
        db.session.add(operator)
        db.session.commit()
        flash(f'已新增操作員：{operator.name or operator.login_account}', 'success')
        return redirect(url_for('admin_operators'))
    return render_template('admin/operators/form.html', operator=None)


@app.route('/admin/operators/edit/<int:operator_id>', methods=['GET', 'POST'])
@admin_required
def admin_operators_edit(operator_id):
    operator = Operator.query.get_or_404(operator_id)
    if request.method == 'POST':
        operator.name = request.form.get('name')
        operator.club_name = request.form.get('club_name')
        login_account = request.form.get('login_account', '').strip()
        password = request.form.get('password', '').strip()
        if login_account:
            existing = Operator.query.filter(Operator.login_account == login_account, Operator.id != operator.id).first()
            if existing:
                flash('登入帳號已存在，請改用其他帳號。', 'danger')
                return redirect(url_for('admin_operators_edit', operator_id=operator_id))
            operator.login_account = login_account
        if password:
            operator.set_password(password)
        db.session.commit()
        flash('已更新操作員資料。', 'success')
        return redirect(url_for('admin_operators'))
    return render_template('admin/operators/form.html', operator=operator)


@app.route('/admin/operators/delete/<int:operator_id>', methods=['POST'])
@admin_required
def admin_operators_delete(operator_id):
    operator = Operator.query.get_or_404(operator_id)
    db.session.delete(operator)
    db.session.commit()
    flash('已刪除操作員。', 'info')
    return redirect(url_for('admin_operators'))


# -----------------------------
# Admin CRUD routes for Performances

@app.route('/admin/performances')
@admin_required
def admin_performances():
    performances = Performance.query.order_by(Performance.id).all()
    return render_template('admin/performances/list.html', performances=performances)

@app.route('/admin/performances/new', methods=['GET', 'POST'])
@admin_required
def admin_performances_new():
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        performance = Performance(title=title, description=description)
        db.session.add(performance)
        db.session.commit()
        return redirect(url_for('admin_performances'))
    return render_template('admin/performances/form.html', performance=None)

@app.route('/admin/performances/edit/<int:performance_id>', methods=['GET', 'POST'])
@admin_required
def admin_performances_edit(performance_id):
    performance = Performance.query.get_or_404(performance_id)
    if request.method == 'POST':
        performance.title = request.form.get('title')
        performance.description = request.form.get('description')
        db.session.commit()
        return redirect(url_for('admin_performances'))
    return render_template('admin/performances/form.html', performance=performance)

@app.route('/admin/performances/delete/<int:performance_id>', methods=['POST'])
@admin_required
def admin_performances_delete(performance_id):
    performance = Performance.query.get_or_404(performance_id)
    db.session.delete(performance)
    db.session.commit()
    return redirect(url_for('admin_performances'))


# -----------------------------
# Admin CRUD routes for Prizes

@app.route('/admin/prizes')
@admin_required
def admin_prizes():
    prizes = Prize.query.order_by(Prize.phase, Prize.id).all()
    return render_template('admin/prizes/list.html', prizes=prizes)

@app.route('/admin/prizes/new', methods=['GET', 'POST'])
@admin_required
def admin_prizes_new():
    performances = Performance.query.all()
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        performance_id = int(request.form.get('performance_id'))
        quantity = int(request.form.get('quantity') or 1)
        phase = int(request.form.get('phase') or 1)
        image_file = request.files.get('image')
        image_path = None
        if image_file and image_file.filename:
            try:
                image_path = save_prize_image(image_file)
            except ValueError as exc:
                error_key = str(exc)
                if error_key == 'invalid-extension':
                    flash('圖片格式僅支援 JPG、PNG、GIF、WEBP。', 'warning')
                else:
                    flash('圖片檔名不合法，請重新命名後再上傳。', 'warning')
                return redirect(url_for('admin_prizes_new', phase=phase))
        prize = Prize(name=name, description=description, performance_id=performance_id, quantity=quantity, phase=phase, image_path=image_path)
        db.session.add(prize)
        db.session.commit()
        flash('已新增獎品。', 'success')
        return redirect(url_for('admin_prizes'))
    default_phase = request.args.get('phase', type=int) or 1
    if default_phase not in (1, 2):
        default_phase = 1
    return render_template('admin/prizes/form.html', prize=None, performances=performances, default_phase=default_phase)

@app.route('/admin/prizes/edit/<int:prize_id>', methods=['GET', 'POST'])
@admin_required
def admin_prizes_edit(prize_id):
    prize = Prize.query.get_or_404(prize_id)
    performances = Performance.query.all()
    if request.method == 'POST':
        prize.name = request.form.get('name')
        prize.description = request.form.get('description')
        prize.performance_id = int(request.form.get('performance_id'))
        prize.quantity = int(request.form.get('quantity') or 1)
        prize.phase = int(request.form.get('phase') or 1)
        remove_image = request.form.get('remove_image') == '1'
        image_file = request.files.get('image')
        if remove_image:
            delete_prize_image(prize.image_path)
            prize.image_path = None
        if image_file and image_file.filename:
            try:
                new_path = save_prize_image(image_file)
            except ValueError as exc:
                error_key = str(exc)
                if error_key == 'invalid-extension':
                    flash('圖片格式僅支援 JPG、PNG、GIF、WEBP。', 'warning')
                else:
                    flash('圖片檔名不合法，請重新命名後再上傳。', 'warning')
                return redirect(url_for('admin_prizes_edit', prize_id=prize_id))
            old_path = prize.image_path
            prize.image_path = new_path
            if old_path and old_path != new_path:
                delete_prize_image(old_path)
        db.session.commit()
        flash('已更新獎品。', 'success')
        return redirect(url_for('admin_prizes'))
    return render_template('admin/prizes/form.html', prize=prize, performances=performances, default_phase=prize.phase)

@app.route('/admin/prizes/delete/<int:prize_id>', methods=['POST'])
@admin_required
def admin_prizes_delete(prize_id):
    prize = Prize.query.get_or_404(prize_id)
    delete_prize_image(prize.image_path)
    db.session.delete(prize)
    db.session.commit()
    flash('已刪除獎品。', 'info')
    return redirect(url_for('admin_prizes'))


# -----------------------------
# Admin import routes

@app.route('/admin/import/attendees', methods=['GET', 'POST'])
@admin_required
def admin_import_attendees():
    """
    Upload and import attendees from a CSV file.  Expected columns: name,call_name,club_name,district
    Optional columns: checked_in,event_checked_in,show_checked_in,has_voted,has_drawn,login_account,password
    """
    message = None
    if request.method == 'POST':
        file = request.files.get('file')
        if file and file.filename.endswith('.csv'):
            csv_file = TextIOWrapper(file.stream, encoding='utf-8')
            reader = csv.DictReader(csv_file)
            count = 0
            for row in reader:
                name = row.get('name')
                if not name:
                    continue
                login_account = (row.get('login_account') or row.get('account') or '').strip()
                password_value = (row.get('password') or row.get('initial_password') or '').strip()
                attendee = Attendee(
                    name=name,
                    call_name=row.get('call_name'),
                    club_name=row.get('club_name'),
                    district=row.get('district'),
                    event_checked_in=parse_bool(row.get('event_checked_in', row.get('checked_in'))),
                    show_checked_in=parse_bool(row.get('show_checked_in')),
                    has_voted=parse_bool(row.get('has_voted')),
                    has_drawn=parse_bool(row.get('has_drawn'))
                )
                if login_account:
                    attendee.login_account = login_account
                db.session.add(attendee)
                db.session.flush()
                base_account = attendee.login_account or str(attendee.id)
                unique_account = base_account
                suffix = 1
                while Attendee.query.filter(Attendee.login_account == unique_account, Attendee.id != attendee.id).first():
                    unique_account = f"{base_account}{suffix}"
                    suffix += 1
                attendee.login_account = unique_account
                if password_value:
                    attendee.set_password(password_value)
                elif not attendee.password_hash:
                    attendee.set_password(attendee.login_account)
                count += 1
            db.session.commit()
            message = f'已匯入 {count} 位參加者'
        else:
            message = '請選擇 CSV 格式的檔案'
    return render_template('admin/import_attendees.html', message=message)

@app.route('/admin/import/prizes', methods=['GET', 'POST'])
@admin_required
def admin_import_prizes():
    """
    Upload and import prizes from a CSV file.  Expected columns: name,performance_id,quantity,phase,description
    """
    performances = Performance.query.all()
    message = None
    if request.method == 'POST':
        file = request.files.get('file')
        if file and file.filename.endswith('.csv'):
            csv_file = TextIOWrapper(file.stream, encoding='utf-8')
            reader = csv.DictReader(csv_file)
            count = 0
            for row in reader:
                name = row.get('name')
                if not name:
                    continue
                performance_id = row.get('performance_id')
                # allow performance title to be used instead of id
                if performance_id and performance_id.isdigit():
                    perf_id = int(performance_id)
                else:
                    # find performance by title
                    perf = Performance.query.filter_by(title=row.get('performance_id')).first()
                    perf_id = perf.id if perf else None
                if perf_id is None:
                    continue
                quantity = int(row.get('quantity') or 1)
                phase = int(row.get('phase') or 1)
                description = row.get('description')
                prize = Prize(name=name, description=description, performance_id=perf_id, quantity=quantity, phase=phase)
                db.session.add(prize)
                count += 1
            db.session.commit()
            message = f'已成功匯入 {count} 個獎品'
        else:
            message = '請選擇 CSV 格式的檔案'
    return render_template('admin/import_prizes.html', message=message, performances=performances)


# -----------------------------
# Admin raffle management


@app.route('/admin/raffle')
@admin_required
def admin_raffle():
    """Display raffle management UI with dynamic phase support."""
    search = (request.args.get('q') or '').strip()
    phase_filter = request.args.get('phase', 'all')
    return_url = request.full_path if request.query_string else request.path

    # Collect all phases that currently have prizes or historical results.
    phase_values = {
        row[0] for row in db.session.query(Prize.phase).distinct() if row[0] is not None
    }
    phase_values.update(
        row[0] for row in db.session.query(RaffleResult.phase).distinct() if row[0] is not None
    )
    if not phase_values:
        phase_values = {1}
    phase_numbers = sorted(phase_values)

    # Determine which phases should be displayed based on the filter.
    if phase_filter == 'all':
        active_phases = phase_numbers
    else:
        try:
            selected_phase = int(phase_filter)
        except (TypeError, ValueError):
            selected_phase = None
        if selected_phase in phase_numbers:
            active_phases = [selected_phase]
        else:
            active_phases = phase_numbers
            phase_filter = 'all'

    # Group prizes by phase for quick lookups.
    prizes_by_phase: dict[int, list[Prize]] = {}
    for prize in Prize.query.order_by(Prize.phase, Prize.id).all():
        prizes_by_phase.setdefault(prize.phase, []).append(prize)

    def build_results(phase: int):
        query = RaffleResult.query.options(
            joinedload(RaffleResult.attendee),
            joinedload(RaffleResult.performance),
            joinedload(RaffleResult.prize)
        ).filter(RaffleResult.phase == phase)
        if search:
            like = f"%{search}%"
            query = query.join(Attendee)
            query = query.outerjoin(Performance)
            query = query.outerjoin(Prize)
            filters = [
                Attendee.name.ilike(like),
                Attendee.call_name.ilike(like),
                Attendee.login_account.ilike(like),
                Attendee.club_name.ilike(like),
                RaffleResult.prize_name.ilike(like),
                Performance.title.ilike(like)
            ]
            if search.isdigit():
                filters.append(RaffleResult.attendee_id == int(search))
            query = query.filter(or_(*filters)).distinct()
        return query.order_by(RaffleResult.drawn_at.desc()).all()

    def build_stats(prize_list: list[Prize]):
        total_quantity = sum(p.quantity for p in prize_list)
        total_awarded = sum(p.awarded_count for p in prize_list)
        remaining = max(total_quantity - total_awarded, 0)
        return {
            'total_quantity': total_quantity,
            'total_awarded': total_awarded,
            'remaining': remaining
        }

    phase_overview = [
        {
            'phase': phase,
            'stats': build_stats(prizes_by_phase.get(phase, []))
        }
        for phase in phase_numbers
    ]

    phase_sections = []
    for phase in active_phases:
        prize_list = prizes_by_phase.get(phase, [])
        phase_sections.append({
            'phase': phase,
            'prizes': prize_list,
            'stats': build_stats(prize_list),
            'results': build_results(phase)
        })

    performances = Performance.query.order_by(Performance.title).all()
    eligible_count = len(get_raffle_eligible_attendees())
    return render_template(
        'admin/raffle.html',
        search=search,
        phase_filter=phase_filter,
        phase_numbers=phase_numbers,
        phase_sections=phase_sections,
        phase_overview=phase_overview,
        performances=performances,
        eligible_count=eligible_count,
        return_url=return_url
    )


@app.route('/admin/raffle/manual', methods=['POST'])
@admin_required
def admin_raffle_manual():
    phase = request.form.get('phase', type=int) or 1
    attendee_identifier = (request.form.get('attendee_identifier') or '').strip()
    prize_id = request.form.get('prize_id', type=int)
    performance_id = request.form.get('performance_id', type=int)
    prize_name_manual = (request.form.get('prize_name') or '').strip()
    prize_description_manual = (request.form.get('prize_description') or '').strip()
    return_url = request.form.get('return_url') or url_for('admin_raffle')

    if phase < 1:
        phase = 1
    if not attendee_identifier:
        flash('請輸入參加者編號或帳號。', 'warning')
        return redirect(return_url)

    attendee = None
    if attendee_identifier.isdigit():
        attendee = Attendee.query.get(int(attendee_identifier))
    if attendee is None:
        attendee = Attendee.query.filter_by(login_account=attendee_identifier).first()
    if attendee is None:
        attendee = Attendee.query.filter_by(name=attendee_identifier).first()
    if attendee is None:
        flash('找不到對應的參加者。', 'danger')
        return redirect(return_url)
    if attendee.has_drawn:
        flash('此參加者已有得獎紀錄，請先釋出原紀錄後再操作。', 'warning')
        return redirect(return_url)

    prize = None
    if prize_id:
        prize = Prize.query.get(prize_id)
        if not prize:
            flash('選擇的獎項不存在。', 'danger')
            return redirect(return_url)
        if prize.phase != phase:
            flash('選擇的獎項與階段不符。', 'warning')
            return redirect(return_url)
        if prize.awarded_count >= prize.quantity:
            flash('該獎項名額已滿，請先釋出或改選其他獎項。', 'warning')
            return redirect(return_url)

    if prize:
        performance_id = prize.performance_id
        prize_name = prize.name
        prize_description = prize.description
    else:
        performance = Performance.query.get(performance_id) if performance_id else None
        if performance is None:
            flash('請選擇節目。', 'warning')
            return redirect(return_url)
        prize_name = prize_name_manual or '手動指定獎項'
        prize_description = prize_description_manual

    attendee.has_drawn = True
    if prize:
        prize.awarded_count += 1
    result = RaffleResult(
        attendee_id=attendee.id,
        performance_id=performance_id,
        prize_id=prize.id if prize else None,
        phase=phase,
        prize_name=prize_name,
        prize_description=prize_description
    )
    db.session.add(result)
    db.session.commit()
    flash(f'已新增 {attendee.name} 為第 {phase} 階段得獎者。', 'success')
    return redirect(return_url)

@app.route('/admin/raffle/delete/<int:result_id>', methods=['POST'])
@admin_required
def admin_raffle_delete(result_id):
    result = RaffleResult.query.get_or_404(result_id)
    return_url = request.form.get('return_url') or url_for('admin_raffle')
    release_raffle_result(result, flush_only=False)
    flash('已刪除得獎紀錄，獎項名額已釋出。', 'info')
    return redirect(return_url)


@app.route('/admin/raffle/redraw/<int:result_id>', methods=['POST'])
@admin_required
def admin_raffle_redraw(result_id):
    result = RaffleResult.query.options(
        joinedload(RaffleResult.prize),
        joinedload(RaffleResult.attendee)
    ).get_or_404(result_id)
    return_url = request.form.get('return_url') or url_for('admin_raffle')
    snapshot = snapshot_raffle_result(result)
    prize = result.prize
    phase = result.phase
    release_raffle_result(result, flush_only=False)
    if prize:
        prize = Prize.query.get(prize.id)

    if prize:
        new_result, winner = perform_draw_for_prize(prize)
        if not new_result:
            restore_raffle_result(snapshot)
            db.session.commit()
            flash('沒有符合資格的參加者可以重新抽出，已還原原得獎紀錄。', 'warning')
            return redirect(return_url)
        db.session.commit()
        flash(f'已重抽 {prize.name}，新得獎者：{winner.name}。', 'success')
        return redirect(return_url)

    new_result, winner, new_prize, error = perform_phase_draw(phase)
    if error:
        restore_raffle_result(snapshot)
        db.session.commit()
        error_messages = {
            'no_prizes': f'第 {phase} 階段已無可抽獎項，已還原原得獎紀錄。',
            'no_available_slot': '符合條件的獎項名額已滿，已還原原得獎紀錄。',
            'no_eligible_attendee': '沒有符合資格的參加者可以重抽，已還原原得獎紀錄。'
        }
        flash(error_messages.get(error, '重抽失敗，已還原原得獎紀錄。'), 'warning')
        return redirect(return_url)

    db.session.commit()
    prize_name = new_prize.name if new_prize else '獎項'
    flash(f'已重抽第 {phase} 階段 {prize_name}，新得獎者：{winner.name}。', 'success')
    return redirect(return_url)


@app.route('/', methods=['GET', 'POST'])
def index():
    attendee = None
    lookup_error = None
    raffle_result = None
    prefill_attendee_id = request.args.get('attendee_id', '').strip()

    if request.method == 'POST':
        attendee_id_raw = (request.form.get('attendee_id') or '').strip()
        password = request.form.get('password', '')
        prefill_attendee_id = attendee_id_raw
        if not attendee_id_raw:
            lookup_error = '請輸入參加者編號'
        elif not attendee_id_raw.isdigit():
            lookup_error = '參加者編號需為數字'
        else:
            attendee = Attendee.query.get(int(attendee_id_raw))
            if attendee is None:
                lookup_error = '查無此參加者'
            elif not attendee.check_password(password):
                lookup_error = '密碼不正確，請再試一次'
                attendee = None
            else:
                session['participant_attendee_id'] = attendee.id
                return redirect(url_for('index'))

    session_attendee_id = session.get('participant_attendee_id')
    if session_attendee_id:
        attendee = Attendee.query.get(session_attendee_id)
        if attendee:
            raffle_result = (
                RaffleResult.query.filter_by(attendee_id=attendee.id)
                .order_by(RaffleResult.drawn_at.desc())
                .first()
            )
        else:
            session.pop('participant_attendee_id', None)

    attendee_id_value = prefill_attendee_id if prefill_attendee_id else ''

    return render_template(
        'index.html',
        attendee=attendee,
        attendee_id=attendee_id_value,
        lookup_error=lookup_error,
        raffle_result=raffle_result
    )


@app.route('/logout')
def participant_logout():
    session.pop('participant_attendee_id', None)
    return redirect(url_for('index'))

    if request.method == 'POST':
        attendee_id_raw = (request.form.get('attendee_id') or '').strip()
        password = request.form.get('password', '')
        if not attendee_id_raw:
            lookup_error = '請輸入參加者編號。'
        else:
            try:
                attendee_id = int(attendee_id_raw)
            except ValueError:
                lookup_error = '參加者編號必須為數字。'
            else:
                attendee = Attendee.query.get(attendee_id)
                if attendee is None:
                    lookup_error = '找不到對應的參加者。'
                    attendee_id = None
                elif not attendee.check_password(password):
                    lookup_error = '密碼驗證失敗，請再次確認。'
                    attendee = None
                else:
                    raffle_result = (
                        RaffleResult.query.filter_by(attendee_id=attendee.id)
                        .order_by(RaffleResult.drawn_at.desc())
                        .first()
                    )
    else:
        attendee_id = request.args.get('attendee_id', type=int)
        if attendee_id:
            attendee = Attendee.query.get(attendee_id)
            if attendee is None:
                lookup_error = '找不到對應的參加者。'
            else:
                raffle_result = (
                    RaffleResult.query.filter_by(attendee_id=attendee.id)
                    .order_by(RaffleResult.drawn_at.desc())
                    .first()
                )

    return render_template(
        'index.html',
        attendee=attendee,
        attendee_id=attendee_id,
        lookup_error=lookup_error,
        raffle_result=raffle_result
    )


@app.route('/attendees/<int:attendee_id>/raffle_result')
def attendee_raffle_result(attendee_id):
    attendee = Attendee.query.get_or_404(attendee_id)
    results = RaffleResult.query.filter_by(attendee_id=attendee.id).order_by(RaffleResult.drawn_at.desc()).all()
    return render_template('raffle_result.html', attendee=attendee, results=results)


@app.route('/scoreboard')
def scoreboard():
    performances = Performance.query.order_by(Performance.id).all()
    results = []
    for performance in performances:
        votes = Vote.query.filter_by(performance_id=performance.id).all()
        total = len(votes)
        average = sum(v.score for v in votes) / total if total else 0
        results.append({
            'performance': performance,
            'vote_count': total,
            'average_score': round(average, 2)
        })
    return render_template('scoreboard.html', results=results)


@app.errorhandler(404)
def not_found(error):
    return '頁面不存在。', 404


if __name__ == '__main__':
    with app.app_context():
        initialize_database()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
