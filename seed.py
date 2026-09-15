"""데모용 초기 데이터.

앱을 처음 실행했을 때 테이블이 비어 있으면 한 번만 실행된다. 관리자 1명,
조직원 10명, 진행 중인 행사 1건과 그 주문 데이터, 그리고 발송 이력을
확인할 수 있도록 지난 행사 1건을 함께 만든다.
"""

from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from mailwriter import generate_email
from models import DestinationLeadTime, EmployeeOrder, Event, Notification, User, db

DEFAULT_LEAD_TIMES = {
    "상파울루": 14,
    "뉴욕": 7,
    "도쿄": 5,
    "베트남": 7,
    "런던": 10,
    "싱가포르": 6,
    "서울": 2,
}

# (이름, 부서, 주재지, 주문완료 여부, 이메일/연락처 결함 여부)
EMPLOYEES = [
    ("김민서", "영업팀", "상파울루", False, None),
    ("이서준", "마케팅팀", "뉴욕", False, None),
    ("박도윤", "영업팀", "도쿄", True, None),
    ("최하윤", "인사팀", "베트남", False, None),
    ("정지호", "마케팅팀", "런던", True, None),
    ("강수아", "영업팀", "싱가포르", False, None),
    ("윤예준", "인사팀", "서울", True, None),
    ("임지우", "마케팅팀", "상파울루", False, "no_email"),
    ("한서현", "영업팀", "뉴욕", False, "bad_email"),
    ("오태윤", "인사팀", "도쿄", False, None),
]


def _make_order(event, user, destination, ordered, defect, base_desired_date):
    lead = DEFAULT_LEAD_TIMES.get(destination)
    order = EmployeeOrder(
        event_id=event.id,
        user_id=user.id,
        destination=destination,
        delivery_days=lead,
        desired_delivery_date=base_desired_date,
        ordered=ordered,
        ordered_at=datetime.now() - timedelta(days=2) if ordered else None,
        phone="010-0000-0000",
        email=user.email,
    )
    if defect == "no_email":
        order.email = None
    elif defect == "bad_email":
        order.email = "잘못된메일주소"
    order.recalc_deadline()
    return order


def seed_if_empty():
    if User.query.first() is not None:
        return  # 이미 데이터가 있으면 손대지 않는다

    for name, days in DEFAULT_LEAD_TIMES.items():
        db.session.add(DestinationLeadTime(name=name, lead_time_days=days))

    admin = User(
        username="admin",
        password_hash=generate_password_hash("admin1234"),
        name="관리자",
        email="admin@etners.example",
        department="운영팀",
        position="매니저",
        role="admin",
    )
    db.session.add(admin)

    employee_users = []
    for i, (name, dept, dest, ordered, defect) in enumerate(EMPLOYEES, start=1):
        u = User(
            username=f"employee{i:02d}",
            password_hash=generate_password_hash("employee1234"),
            name=name,
            email=f"{name}@etners.example" if defect != "no_email" else "",
            department=dept,
            position="사원",
            role="employee",
        )
        employee_users.append((u, dest, ordered, defect))
        db.session.add(u)

    db.session.flush()  # id 확보

    # 진행 중인 행사 — 오늘 기준으로 D-day가 다양하게 분포하도록 수령희망일을 조금씩 다르게 준다
    today = date.today()
    current = Event(
        name="2026 추석 선물",
        order_start_date=today - timedelta(days=10),
        order_end_date=today + timedelta(days=5),
        delivery_date=today + timedelta(days=14),
        status=Event.STATUS_ONGOING,
    )
    db.session.add(current)
    db.session.flush()

    desired_offsets = [14, 14, 20, 8, 20, 25, 20, 14, 7, 20]  # 마감일 D-day가 섞이도록
    for (u, dest, ordered, defect), offset in zip(employee_users, desired_offsets):
        desired = today + timedelta(days=offset)
        order = _make_order(current, u, dest, ordered, defect, desired)
        db.session.add(order)

    # 지난 행사 — 발송 이력이 비어 있지 않도록 안내 완료 기록을 남겨 둔다
    past = Event(
        name="2026 여름 휴가 선물",
        order_start_date=today - timedelta(days=60),
        order_end_date=today - timedelta(days=45),
        delivery_date=today - timedelta(days=30),
        status=Event.STATUS_CLOSED,
    )
    db.session.add(past)
    db.session.flush()

    for u, dest, _, _ in employee_users[:4]:
        order = _make_order(
            past, u, dest, True, None, today - timedelta(days=30)
        )
        order.notice_status = "1차 안내 완료"
        db.session.add(order)
        db.session.flush()
        subject, content = generate_email(past, order)
        db.session.add(
            Notification(
                event_id=past.id,
                user_id=u.id,
                order_id=order.id,
                subject=subject,
                content=content,
                status=Notification.STATUS_SENT,
                sent_at=datetime.now() - timedelta(days=40),
            )
        )

    db.session.commit()
