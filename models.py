"""데이터 모델.

계산이 필요한 값(주문 마감일, D-day, 안내 우선순위, 자료 오류)은 컬럼으로
저장하지 않고 그때그때 계산한다. 저장해 두면 원본 값(수령 희망일, 배송
소요일)이 바뀌었을 때 갱신을 깜빡해 오래된 값이 남을 수 있기 때문이다.
"""

import re
from datetime import date, datetime, timedelta

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData

# 모든 테이블에 스키마 하나를 일괄 지정한다. "dm"은 실제 스키마명이 아니라
# 자리표시자이고, config.py가 엔진을 만들 때 schema_translate_map으로 실행
# 시점에 실제 값으로 바꿔 끼운다 — Postgres 배포에서는 "delivery"로, 로컬
# SQLite에서는 스키마 없음(None)으로. 여기서 한 곳에만 지정해 두면 모델 안의
# `db.ForeignKey("events.id")` 같은 스키마 없는 참조도 같은 기본 스키마를
# 따라가므로 테이블마다 스키마를 따로 적을 필요가 없다.
#
# 이렇게 나눈 이유: 다른 앱과 같은 Supabase 프로젝트(같은 Postgres 데이터베이스)를
# 함께 쓰기로 했는데, 그 앱도 "users" 테이블을 쓴다. 스키마로 구역을 나눠 두면
# 테이블 이름이 겹쳐도 서로 침범하지 않는다.
db = SQLAlchemy(metadata=MetaData(schema="dm"))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120))
    department = db.Column(db.String(50))
    position = db.Column(db.String(50))
    role = db.Column(db.String(20), nullable=False, default="employee")  # admin | employee
    created_at = db.Column(db.DateTime, default=datetime.now)

    orders = db.relationship("EmployeeOrder", backref="user", lazy=True)

    @property
    def is_admin(self):
        return self.role == "admin"


class DestinationLeadTime(db.Model):
    """주재지별 배송 소요일. 관리자가 사전에 설정해 두는 값."""

    __tablename__ = "destination_lead_times"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    lead_time_days = db.Column(db.Integer, nullable=False)


class Event(db.Model):
    __tablename__ = "events"

    STATUS_UPCOMING = "진행 예정"
    STATUS_ONGOING = "진행 중"
    STATUS_CLOSED = "종료"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    order_start_date = db.Column(db.Date, nullable=False)
    order_end_date = db.Column(db.Date, nullable=False)
    delivery_date = db.Column(db.Date, nullable=False)  # 기본 수령 희망일
    status = db.Column(db.String(20), nullable=False, default=STATUS_ONGOING)
    created_at = db.Column(db.DateTime, default=datetime.now)

    orders = db.relationship(
        "EmployeeOrder", backref="event", lazy=True, cascade="all, delete-orphan"
    )
    notifications = db.relationship(
        "Notification", backref="event", lazy=True, cascade="all, delete-orphan"
    )


class EmployeeOrder(db.Model):
    """행사 하나에 대한 조직원 한 명의 주문 상태.

    같은 조직원이라도 행사마다 별도의 행을 가지므로, 과거 행사의 주문 이력이
    새 행사에 영향을 주지 않는다.
    """

    __tablename__ = "employee_orders"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    destination = db.Column(db.String(50))  # 주재지
    delivery_days = db.Column(db.Integer)  # 배송 소요일 (스냅샷)
    desired_delivery_date = db.Column(db.Date)  # 수령 희망일
    deadline_date = db.Column(db.Date)  # 개인별 주문 마감일 (계산해서 저장)

    ordered = db.Column(db.Boolean, nullable=False, default=False)
    ordered_at = db.Column(db.DateTime)

    notice_status = db.Column(db.String(20), nullable=False, default="미안내")  # 미안내 | 1차 안내 완료

    phone = db.Column(db.String(30))
    email = db.Column(db.String(120))

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    notifications = db.relationship("Notification", backref="order", lazy=True)

    __table_args__ = (db.UniqueConstraint("event_id", "user_id", name="uq_event_user"),)

    # ----------------------------------------------------------------
    # 계산 값
    # ----------------------------------------------------------------
    def recalc_deadline(self):
        """수령 희망일과 배송 소요일로 마감일을 다시 계산한다.

        둘 중 하나라도 없으면 계산할 수 없으므로 None으로 둔다 — 이 상태는
        validation_errors()에서 "누락"으로 잡혀 안내 대상에서 자동 제외된다.
        """
        if self.desired_delivery_date and self.delivery_days is not None:
            self.deadline_date = self.desired_delivery_date - timedelta(days=self.delivery_days)
        else:
            self.deadline_date = None

    def dday(self, today=None):
        """오늘 기준 D-day. 마감일을 모르면 None."""
        if self.deadline_date is None:
            return None
        today = today or date.today()
        return (self.deadline_date - today).days

    def priority(self, today=None):
        """안내 우선순위. D-day 규칙 그대로:
        D-0 이하 → 즉시 확인, D-1~3 → 우선 안내, D-4~7 → 일반 안내, D-8 이상 → 안내 예정
        """
        d = self.dday(today)
        if d is None:
            return None
        if d <= 0:
            return "즉시 확인"
        if d <= 3:
            return "우선 안내"
        if d <= 7:
            return "일반 안내"
        return "안내 예정"

    def validation_errors(self):
        """발송 전에 걸러야 할 자료 문제. 하나라도 있으면 안내 대상에서 제외한다."""
        errors = []
        if not self.email:
            errors.append("이메일 누락")
        elif not EMAIL_RE.match(self.email):
            errors.append("이메일 형식 오류")
        if not self.phone:
            errors.append("연락처 누락")
        if not self.destination:
            errors.append("주재지 누락")
        if not self.desired_delivery_date:
            errors.append("수령 희망일 누락")
        if self.delivery_days is None:
            errors.append("배송 소요일 누락")
        return errors

    def is_notify_target(self, today=None):
        """오늘 안내 대상: 미주문 + 자료 문제 없음 + D-day가 급함(<=3)."""
        if self.ordered:
            return False
        if self.validation_errors():
            return False
        p = self.priority(today)
        return p in ("즉시 확인", "우선 안내")

    def latest_notification(self):
        """이 주문에 대해 가장 최근에 만들어진 안내메일 한 건 (없으면 None)."""
        if not self.notifications:
            return None
        return max(self.notifications, key=lambda n: n.id)

    def to_dict(self, today=None):
        latest = self.latest_notification()
        return {
            "id": self.id,
            "event_id": self.event_id,
            "user_id": self.user_id,
            "name": self.user.name,
            "department": self.user.department,
            "destination": self.destination,
            "desired_delivery_date": self.desired_delivery_date.isoformat()
            if self.desired_delivery_date
            else None,
            "deadline_date": self.deadline_date.isoformat() if self.deadline_date else None,
            "dday": self.dday(today),
            "priority": self.priority(today),
            "ordered": self.ordered,
            "notice_status": self.notice_status,
            "email": self.email,
            "phone": self.phone,
            "errors": self.validation_errors(),
            "send_status": latest.status if latest else None,
        }


class Notification(db.Model):
    """안내 메일 한 통.

    초안(발송 대기)으로 만들어져 → 발송 처리 중 상태를 거쳐 → 발송 완료 또는
    발송 실패로 확정된다. 그 사이 대상자가 스스로 주문을 끝내면 발송 제외로
    빠진다 (이 앱의 핵심 안전장치 — 발송 직전 재확인).

    발송 완료된 건에는 조직원의 회신이 나중에 달릴 수 있다. 회신 유형
    (reply_category)은 지금은 조직원이 회신할 때 직접 고르지만, 나중에
    AI가 회신 본문을 읽고 자동 분류하도록 바꿀 걸 염두에 두고 별도 컬럼으로
    구조화해 저장한다.
    """

    __tablename__ = "notifications"

    # 발송 상태
    STATUS_PENDING = "발송 대기"
    STATUS_SENDING = "발송 중"
    STATUS_SENT = "발송 완료"
    STATUS_FAILED = "발송 실패"
    STATUS_EXCLUDED = "발송 제외"

    # 처리 상태 (관리자가 회신을 확인하고 마무리했는지)
    PROCESS_UNHANDLED = "미처리"
    PROCESS_IN_PROGRESS = "처리 중"
    PROCESS_DONE = "처리 완료"

    REPLY_CATEGORIES = (
        "주문 예정", "주문 불필요", "주문 취소",
        "배송 관련 문의", "배송지 변경 요청", "기타 문의",
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("employee_orders.id"), nullable=False)

    notification_type = db.Column(db.String(20), nullable=False, default="1차 안내")
    subject = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING)
    fail_reason = db.Column(db.String(200))
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)

    recipient = db.relationship("User", foreign_keys=[user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "user_id": self.user_id,
            "order_id": self.order_id,
            "name": self.recipient.name,
            "department": self.recipient.department,
            "notification_type": self.notification_type,
            "subject": self.subject,
            "status": self.status,
            "fail_reason": self.fail_reason,
            "sent_at": self.sent_at.strftime("%Y-%m-%d %H:%M") if self.sent_at else None,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
        }
