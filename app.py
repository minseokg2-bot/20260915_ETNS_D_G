"""이트너스몰 배송운영 관리 대시보드.

실제 이트너스몰과 연동하지 않는다. 주문 현황을 바탕으로 미주문자를 정리하고,
개인별 주문 마감일을 계산하며, 1차 안내메일 작성·발송을 시뮬레이션하는
사내 운영 지원 도구다.

핵심 원칙: 계산(마감일·D-day·안내 대상 선별)은 시스템이 하고, 문안 작성은
mailwriter가 하고, 실제 보낼지 최종 판단은 관리자가 화면에서 직접 누른다.
"""

import functools
import os
from datetime import date, datetime

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash

from config import Config, POSTGRES_SCHEMA
from mailwriter import generate_email
from models import EMAIL_RE, DestinationLeadTime, EmployeeOrder, Event, Notification, User, db
from seed import seed_if_empty

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 서버리스에서는 작업 디렉터리가 달라질 수 있어 절대 경로로 고정한다.
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.config.from_object(Config)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "로그인이 필요합니다."
login_manager.login_message_category = "error"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(view):
    """관리자 전용 라우트에 붙인다. 조직원이 URL을 직접 쳐도 막는다."""

    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@app.errorhandler(403)
def forbidden(exc):
    return render_template("error.html", code=403, message="접근 권한이 없습니다."), 403


@app.errorhandler(404)
def not_found(exc):
    return render_template("error.html", code=404, message="요청하신 자료를 찾을 수 없습니다."), 404


# --------------------------------------------------------------------------
# 인증
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_home_for(current_user))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user is None or not check_password_hash(user.password_hash, password):
            flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
            return render_template("login.html", username=username)

        login_user(user)
        return redirect(_home_for(user))

    return render_template("login.html", username="")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def root():
    return redirect(_home_for(current_user))


def _home_for(user):
    return url_for("admin_dashboard") if user.is_admin else url_for("employee_dashboard")


# --------------------------------------------------------------------------
# 공통 헬퍼
# --------------------------------------------------------------------------
def _current_event(event_id=None):
    """행사 선택 헬퍼. 지정이 없으면 진행 중인 행사, 없으면 가장 최근 행사."""
    if event_id:
        ev = db.session.get(Event, event_id)
        if ev:
            return ev
    ongoing = Event.query.filter_by(status=Event.STATUS_ONGOING).order_by(Event.id.desc()).first()
    return ongoing or Event.query.order_by(Event.id.desc()).first()


def _orders_query(event_id, department=None, destination=None, ordered=None, q=None):
    query = EmployeeOrder.query.filter_by(event_id=event_id).join(User)
    if department:
        query = query.filter(User.department == department)
    if destination:
        query = query.filter(EmployeeOrder.destination == destination)
    if ordered is not None:
        query = query.filter(EmployeeOrder.ordered == ordered)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.name.like(like), EmployeeOrder.email.like(like)))
    return query


# --------------------------------------------------------------------------
# 관리자 — 대시보드
# --------------------------------------------------------------------------
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    events = Event.query.order_by(Event.id.desc()).all()
    event = _current_event(request.args.get("event_id", type=int))
    if event is None:
        return render_template("admin/dashboard.html", events=events, event=None)

    orders = EmployeeOrder.query.filter_by(event_id=event.id).all()
    summary = _summary_for(orders)

    rows = sorted(
        [o for o in orders if not o.ordered],
        key=lambda o: (o.dday() is None, o.dday() if o.dday() is not None else 0),
    )

    return render_template(
        "admin/dashboard.html",
        events=events,
        event=event,
        summary=summary,
        rows=rows[:15],
        today=date.today().isoformat(),
    )


def _summary_for(orders):
    today = date.today()
    total = len(orders)
    ordered = sum(1 for o in orders if o.ordered)
    unordered = total - ordered
    notify_today = sum(1 for o in orders if o.is_notify_target(today))
    notified = sum(1 for o in orders if o.notice_status == "1차 안내 완료")
    errors = sum(1 for o in orders if o.validation_errors())
    return {
        "total": total,
        "ordered": ordered,
        "unordered": unordered,
        "notify_today": notify_today,
        "notified": notified,
        "errors": errors,
    }


@app.route("/api/dashboard")
@admin_required
def api_dashboard():
    event = _current_event(request.args.get("event_id", type=int))
    if event is None:
        return jsonify({"error": "행사가 없습니다."}), 404

    orders = EmployeeOrder.query.filter_by(event_id=event.id).all()
    summary = _summary_for(orders)

    by_destination = {}
    for o in orders:
        if not o.ordered:
            by_destination[o.destination or "미상"] = by_destination.get(o.destination or "미상", 0) + 1

    return jsonify(
        {
            "event": {"id": event.id, "name": event.name},
            "summary": summary,
            "by_destination": by_destination,
        }
    )


# --------------------------------------------------------------------------
# 관리자 — 행사 관리 (+ 주재지 배송 소요일 설정)
# --------------------------------------------------------------------------
@app.route("/admin/events", methods=["GET", "POST"])
@admin_required
def admin_events():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "create_event":
            try:
                ev = Event(
                    name=request.form["name"].strip(),
                    order_start_date=datetime.strptime(request.form["order_start_date"], "%Y-%m-%d").date(),
                    order_end_date=datetime.strptime(request.form["order_end_date"], "%Y-%m-%d").date(),
                    delivery_date=datetime.strptime(request.form["delivery_date"], "%Y-%m-%d").date(),
                    status=request.form.get("status", Event.STATUS_UPCOMING),
                )
            except (KeyError, ValueError):
                flash("입력한 날짜 형식을 확인해 주세요.", "error")
                return redirect(url_for("admin_events"))

            if not ev.name:
                flash("행사명을 입력해 주세요.", "error")
                return redirect(url_for("admin_events"))

            db.session.add(ev)
            db.session.commit()
            flash(f"'{ev.name}' 행사를 등록했습니다.", "success")

        elif action == "update_lead_time":
            name = request.form.get("name", "").strip()
            days = request.form.get("lead_time_days", type=int)
            if not name or days is None or days < 0:
                flash("주재지와 배송 소요일을 올바르게 입력해 주세요.", "error")
            else:
                row = DestinationLeadTime.query.filter_by(name=name).first()
                if row:
                    row.lead_time_days = days
                else:
                    db.session.add(DestinationLeadTime(name=name, lead_time_days=days))
                db.session.commit()
                flash(f"{name} 배송 소요일을 {days}일로 저장했습니다.", "success")

        return redirect(url_for("admin_events"))

    events = Event.query.order_by(Event.id.desc()).all()
    lead_times = DestinationLeadTime.query.order_by(DestinationLeadTime.name).all()
    return render_template("admin/events.html", events=events, lead_times=lead_times)


@app.route("/api/events")
@admin_required
def api_events():
    events = Event.query.order_by(Event.id.desc()).all()
    return jsonify(
        [
            {
                "id": e.id,
                "name": e.name,
                "status": e.status,
                "delivery_date": e.delivery_date.isoformat(),
            }
            for e in events
        ]
    )


# --------------------------------------------------------------------------
# 관리자 — 주문 현황 / 미주문자
# --------------------------------------------------------------------------
@app.route("/admin/orders")
@admin_required
def admin_orders():
    events = Event.query.order_by(Event.id.desc()).all()
    event = _current_event(request.args.get("event_id", type=int))
    departments = [d[0] for d in db.session.query(User.department).distinct() if d[0]]
    destinations = [d[0] for d in db.session.query(EmployeeOrder.destination).distinct() if d[0]]
    return render_template(
        "admin/orders.html",
        events=events,
        event=event,
        departments=departments,
        destinations=destinations,
    )


@app.route("/admin/unordered")
@admin_required
def admin_unordered():
    """예전에 따로 있던 '미주문자 관리' 메뉴. 주문 현황의 필터 하나였을 뿐이라
    화면을 합쳤다 — 옛 링크나 북마크가 있으면 필터가 걸린 채로 그대로 이동시킨다."""
    args = {"ordered": "false"}
    event_id = request.args.get("event_id", type=int)
    if event_id:
        args["event_id"] = event_id
    return redirect(url_for("admin_orders", **args))


def _parse_bool(value):
    if value in (None, ""):
        return None
    return value == "true"


@app.route("/api/orders")
@admin_required
def api_orders():
    event_id = request.args.get("event_id", type=int)
    event = _current_event(event_id)
    if event is None:
        return jsonify({"rows": []})

    ordered = _parse_bool(request.args.get("ordered"))
    if request.args.get("mode") == "unordered":
        ordered = False

    query = _orders_query(
        event.id,
        department=request.args.get("department") or None,
        destination=request.args.get("destination") or None,
        ordered=ordered,
        q=request.args.get("q") or None,
    )

    notice_status = request.args.get("notice_status")
    if notice_status:
        query = query.filter(EmployeeOrder.notice_status == notice_status)

    priority_filter = request.args.get("priority")
    rows = [o.to_dict() for o in query.all()]
    if priority_filter:
        rows = [r for r in rows if r["priority"] == priority_filter]

    rows.sort(key=lambda r: (r["dday"] is None, r["dday"] if r["dday"] is not None else 999999))
    return jsonify({"rows": rows, "event": {"id": event.id, "name": event.name}})


@app.route("/api/order/<int:order_id>", methods=["PUT"])
@admin_required
def api_update_order(order_id):
    """조직원이 실제로 주문을 완료했다는 것을 관리자가 대신 반영하는 데모용 API.

    실제 서비스라면 이트너스몰 쪽 데이터를 그대로 받아 채우겠지만, 이 MVP는
    가상 데이터만 다루므로 관리자가 화면에서 '주문완료 처리'를 눌러 상태를
    바꿀 수 있게 해 두었다. 그래야 대시보드 실시간 갱신과, 발송 직전
    재확인 로직을 실제로 눈으로 확인할 수 있다.
    """
    order = db.session.get(EmployeeOrder, order_id)
    if order is None:
        return jsonify({"error": "주문 정보를 찾을 수 없습니다."}), 404

    data = request.get_json(silent=True) or {}
    if "ordered" in data:
        order.ordered = bool(data["ordered"])
        order.ordered_at = datetime.now() if order.ordered else None
    db.session.commit()
    return jsonify(order.to_dict())


# --------------------------------------------------------------------------
# 관리자 — 안내메일 (생성 → 재확인 → 발송)
# --------------------------------------------------------------------------
@app.route("/admin/notifications", methods=["GET", "POST"])
@admin_required
def admin_notifications():
    events = Event.query.order_by(Event.id.desc()).all()
    event = _current_event(request.args.get("event_id", type=int) or request.form.get("event_id", type=int))

    if event is None:
        return render_template("admin/notifications.html", events=events, event=None)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "generate":
            target_ids = request.form.getlist("order_ids")
            if target_ids:
                targets = EmployeeOrder.query.filter(EmployeeOrder.id.in_(target_ids)).all()
            else:
                # 체크박스로 고르지 않았으면 '오늘 안내 대상'을 자동으로 잡는다
                targets = [
                    o
                    for o in EmployeeOrder.query.filter_by(event_id=event.id).all()
                    if o.is_notify_target()
                ]

            created = 0
            for order in targets:
                if order.ordered or order.validation_errors():
                    continue  # 이미 주문했거나 자료에 문제가 있으면 초안조차 만들지 않는다
                exists = Notification.query.filter_by(
                    order_id=order.id, status=Notification.STATUS_PENDING
                ).first()
                if exists:
                    continue
                subject, content = generate_email(event, order)
                db.session.add(
                    Notification(
                        event_id=event.id,
                        user_id=order.user_id,
                        order_id=order.id,
                        subject=subject,
                        content=content,
                        status=Notification.STATUS_PENDING,
                    )
                )
                created += 1
            db.session.commit()
            flash(f"안내메일 초안 {created}건을 생성했습니다.", "success")

        elif action == "recheck":
            pending = Notification.query.filter_by(
                event_id=event.id, status=Notification.STATUS_PENDING
            ).all()
            excluded = 0
            for n in pending:
                if n.order.ordered:
                    n.status = Notification.STATUS_EXCLUDED
                    excluded += 1
            db.session.commit()
            if excluded:
                flash(f"그새 주문을 완료한 {excluded}명을 발송 대상에서 제외했습니다.", "success")
            else:
                flash("발송 대상 전원이 아직 미주문 상태입니다. 그대로 발송할 수 있습니다.", "success")

        elif action == "send":
            pending = Notification.query.filter_by(
                event_id=event.id, status=Notification.STATUS_PENDING
            ).all()
            sent = 0
            excluded = 0
            for n in pending:
                if n.order.ordered:
                    n.status = Notification.STATUS_EXCLUDED
                    excluded += 1
                    continue
                n.status = Notification.STATUS_SENT
                n.sent_at = datetime.now()
                n.order.notice_status = "1차 안내 완료"
                sent += 1
            db.session.commit()
            msg = f"{sent}건을 발송 완료했습니다."
            if excluded:
                msg += f" (발송 직전 주문을 완료한 {excluded}명은 자동 제외)"
            flash(msg, "success")

        elif action == "discard":
            note_id = request.form.get("notification_id", type=int)
            n = db.session.get(Notification, note_id)
            if n and n.status == Notification.STATUS_PENDING:
                db.session.delete(n)
                db.session.commit()

        return redirect(url_for("admin_notifications", event_id=event.id))

    orders = EmployeeOrder.query.filter_by(event_id=event.id).all()
    notify_candidates = sorted(
        [o for o in orders if not o.ordered and not o.validation_errors()],
        key=lambda o: (o.dday() is None, o.dday()),
    )
    error_orders = [o for o in orders if o.validation_errors()]
    drafts = (
        Notification.query.filter_by(event_id=event.id, status=Notification.STATUS_PENDING)
        .order_by(Notification.id)
        .all()
    )

    return render_template(
        "admin/notifications.html",
        events=events,
        event=event,
        notify_candidates=notify_candidates,
        error_orders=error_orders,
        drafts=drafts,
    )


# --------------------------------------------------------------------------
# 관리자 — 발송 이력
# --------------------------------------------------------------------------
@app.route("/admin/history")
@admin_required
def admin_history():
    events = Event.query.order_by(Event.id.desc()).all()
    return render_template("admin/history.html", events=events)


@app.route("/api/notifications")
@admin_required
def api_notifications():
    query = Notification.query.join(User, Notification.user_id == User.id)

    event_id = request.args.get("event_id", type=int)
    if event_id:
        query = query.filter(Notification.event_id == event_id)

    department = request.args.get("department")
    if department:
        query = query.filter(User.department == department)

    status = request.args.get("status")
    if status:
        query = query.filter(Notification.status == status)

    notification_type = request.args.get("notification_type")
    if notification_type:
        query = query.filter(Notification.notification_type == notification_type)

    q = request.args.get("q")
    if q:
        query = query.filter(User.name.like(f"%{q}%"))

    rows = query.order_by(Notification.created_at.desc()).all()
    result = []
    for n in rows:
        d = n.to_dict()
        d["event_name"] = n.event.name
        result.append(d)
    return jsonify({"rows": result})


# --------------------------------------------------------------------------
# 관리자 — 조직원 관리
#
# "계정 이메일"(User.email, 로그인 계정에 붙은 값)과 "이번 행사 연락 이메일"
# (EmployeeOrder.email, 행사마다 따로 받는 값)은 서로 다른 컬럼이다. 안내메일
# 발송 전 자료 점검은 후자만 본다 — 실제로 안내가 나가는 주소이기 때문이다.
# 화면에 둘 다 보여주고, 발송을 막는 건 후자라는 걸 분명히 한다.
# --------------------------------------------------------------------------
@app.route("/admin/employees")
@admin_required
def admin_employees():
    events = Event.query.order_by(Event.id.desc()).all()
    event = _current_event(request.args.get("event_id", type=int))

    employees = User.query.filter_by(role="employee").order_by(User.department, User.name).all()

    rows = []
    if event:
        orders_by_user = {
            o.user_id: o for o in EmployeeOrder.query.filter_by(event_id=event.id).all()
        }
        for u in employees:
            rows.append({"user": u, "order": orders_by_user.get(u.id)})
    else:
        rows = [{"user": u, "order": None} for u in employees]

    return render_template("admin/employees.html", events=events, event=event, rows=rows)


@app.route("/admin/employees/orders/<int:order_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_employee_order_edit(order_id):
    order = db.session.get(EmployeeOrder, order_id)
    if order is None:
        abort(404)

    lead_times = DestinationLeadTime.query.order_by(DestinationLeadTime.name).all()
    lead_time_map = {lt.name: lt.lead_time_days for lt in lead_times}

    if request.method == "POST":
        destination = request.form.get("destination", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        desired_raw = request.form.get("desired_delivery_date", "").strip()

        error = None
        if not destination:
            error = "주재지를 선택해 주세요."
        elif destination not in lead_time_map:
            error = "등록되지 않은 주재지입니다. 행사 관리 화면에서 먼저 배송 소요일을 설정해 주세요."
        elif not phone:
            error = "연락처를 입력해 주세요."
        elif not email:
            error = "이메일을 입력해 주세요."
        elif not EMAIL_RE.match(email):
            error = "이메일 형식을 확인해 주세요. (예: name@example.com)"
        elif not desired_raw:
            error = "수령 희망일을 입력해 주세요."

        if not error:
            try:
                desired_date = datetime.strptime(desired_raw, "%Y-%m-%d").date()
            except ValueError:
                error = "수령 희망일 형식을 확인해 주세요."

        if error:
            flash(error, "error")
            return render_template(
                "admin/employee_edit.html", order=order, lead_times=lead_times, form=request.form
            )

        order.destination = destination
        order.delivery_days = lead_time_map[destination]
        order.desired_delivery_date = desired_date
        order.phone = phone
        order.email = email
        order.recalc_deadline()
        db.session.commit()

        flash(f"{order.user.name}님의 정보를 수정했습니다.", "success")
        return redirect(url_for("admin_employees", event_id=order.event_id))

    return render_template("admin/employee_edit.html", order=order, lead_times=lead_times, form=None)


# --------------------------------------------------------------------------
# 조직원
# --------------------------------------------------------------------------
@app.route("/employee/dashboard")
@login_required
def employee_dashboard():
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))

    orders = (
        EmployeeOrder.query.filter_by(user_id=current_user.id)
        .join(Event)
        .order_by(Event.id.desc())
        .all()
    )
    today = date.today()
    cards = [
        {
            "event": o.event,
            "order": o,
            "dday": o.dday(today),
            "priority": o.priority(today),
        }
        for o in orders
    ]

    my_notifications = (
        Notification.query.filter_by(user_id=current_user.id, status=Notification.STATUS_SENT)
        .order_by(Notification.sent_at.desc())
        .all()
    )

    return render_template("employee/dashboard.html", cards=cards, notifications=my_notifications)


# --------------------------------------------------------------------------
# 진단
# --------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    """저장소 연결 상태를 확인하는 진단용 엔드포인트.

    개인 데이터는 노출하지 않는다. 각 테이블의 건수만 센다.
    """
    info = {
        "backend": Config.backend_name(),
        "target": Config.describe_target(),
        "schema": POSTGRES_SCHEMA if Config._IS_POSTGRES else "(sqlite, 스키마 없음)",
        "database_url_set": bool(os.environ.get("DATABASE_URL", "").strip()),
        "secret_key_set": not Config.SECRET_KEY_IS_EPHEMERAL,
    }
    if DB_INIT_ERROR:
        info["startup_error"] = DB_INIT_ERROR

    try:
        info["users"] = User.query.count()
        info["events"] = Event.query.count()
        info["orders"] = EmployeeOrder.query.count()
        info["notifications"] = Notification.query.count()
        info["ok"] = True
        return info
    except Exception as exc:  # noqa: BLE001 - 진단 목적이라 원인을 노출
        info["ok"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info, 500


# --------------------------------------------------------------------------
# 기동
# --------------------------------------------------------------------------
# 모듈을 import 하는 순간 DB 접속을 시도하면, 접속이 실패할 때 서버리스 함수
# 전체가 뜨지 못하고 원인 불명의 FUNCTION_INVOCATION_FAILED만 반환한다.
# 그래서 실패해도 앱은 계속 뜨게 하고, 원인은 /healthz 에서 확인한다.
DB_INIT_ERROR = None
try:
    with app.app_context():
        if Config._IS_POSTGRES:
            # Postgres는 스키마를 미리 만들어 둬야 그 안에 테이블을 만들 수 있다.
            # 다른 앱과 같은 데이터베이스를 쓰기 때문에, 이 앱의 테이블은 전부
            # "public"이 아닌 이 스키마 안에서만 산다.
            from sqlalchemy import text

            db.session.execute(text(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA}"))
            db.session.commit()
        db.create_all()
        seed_if_empty()
except Exception as exc:  # noqa: BLE001 - 기동을 막지 않기 위해 전부 흡수
    DB_INIT_ERROR = f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    # use_reloader=False: 리로더를 켜면 감시용 프로세스와 실행용 프로세스가 별도로
    # 뜨는데, 같은 SQLite 파일에 두 프로세스가 동시에 seed를 시도하다 몇 번
    # 데이터가 꼬였다. 코드 수정 후에는 서버를 직접 재시작해서 반영한다.
    app.run(host="127.0.0.1", port=5050, debug=True, use_reloader=False)
