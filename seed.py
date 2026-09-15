"""데모용 초기 데이터.

앱을 처음 실행했을 때 테이블이 비어 있으면 한 번만 실행된다. 관리자 1명,
조직원 10명, 진행 중인 행사 1건과 그 주문 데이터, 그리고 발송 이력을
확인할 수 있도록 지난 행사 1건을 함께 만든다.
"""

from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from mailwriter import classify_reply, generate_email, generate_reply_response
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
    ("정지호", "마케팅팀", "런던", True, "no_email"),
    ("강수아", "영업팀", "싱가포르", False, "bad_email"),
    ("윤예준", "인사팀", "서울", True, None),
    ("임지우", "마케팅팀", "상파울루", False, None),
    ("한서현", "영업팀", "뉴욕", False, None),
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
    current_orders = {}
    for (u, dest, ordered, defect), offset in zip(employee_users, desired_offsets):
        desired = today + timedelta(days=offset)
        order = _make_order(current, u, dest, ordered, defect, desired)
        db.session.add(order)
        current_orders[u.username] = order
    db.session.flush()

    # 진행 중인 행사에도 회신 메일 관리 화면을 바로 확인할 수 있도록 안내메일 발송 +
    # 조직원 회신을 몇 건 미리 만들어 둔다 (관리자가 발송 버튼을 누르기 전부터
    # 회신 상세·AI 답변 초안을 시연할 수 있게).
    current_reply_cases = [
        # (계정, 회신 내용, AI 답변까지 발송해 둘지)
        ("employee01", "네, 이번 주 안에 주문하겠습니다.", False),  # AI 답변 초안만 (처리 중 데모)
        ("employee02", "배송지를 다른 곳으로 옮기고 싶어요. 가능할까요?", True),  # 처리 완료 데모
        ("employee04", "이미 다른 채널로 구매했습니다.", False),  # 회신만, AI 답변 대기 (미처리 데모)
    ]
    for username, reply, respond in current_reply_cases:
        order = current_orders[username]
        order.notice_status = "1차 안내 완료"
        subject, content = generate_email(current, order)
        n = Notification(
            event_id=current.id,
            user_id=order.user_id,
            order_id=order.id,
            subject=subject,
            content=content,
            status=Notification.STATUS_SENT,
            sent_at=datetime.now() - timedelta(hours=6),
            reply_content=reply,
            reply_category=classify_reply(reply),
            reply_at=datetime.now() - timedelta(hours=2),
            process_status=Notification.PROCESS_DONE if respond else Notification.PROCESS_UNHANDLED,
        )
        db.session.add(n)
        db.session.flush()

        r_subject, r_content = generate_reply_response(current, order, n)
        response = Notification(
            event_id=current.id,
            user_id=order.user_id,
            order_id=order.id,
            in_reply_to_id=n.id,
            notification_type="회신 답변",
            subject=r_subject,
            content=r_content,
            status=Notification.STATUS_SENT if respond else Notification.STATUS_PENDING,
        )
        if respond:
            response.sent_at = datetime.now() - timedelta(hours=1)
        db.session.add(response)

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

    # 각 건마다 발송·회신·AI답변 상태를 다르게 줘서, 화면을 처음 열었을 때부터
    # 발송실패→재발송, 회신→AI답변 초안→발송 같은 기능을 바로 만져볼 수 있게 한다.
    # 회신 유형은 실제 분류기(classify_reply)로 매겨서, 화면에서 보는 로직과
    # 시드 데이터가 항상 같은 규칙을 따르게 한다.
    past_cases = [
        # (발송상태, 회신 내용, AI 답변까지 발송해 둘지)
        (Notification.STATUS_SENT, "이미 다른 채널로 주문했습니다. 확인 부탁드립니다.", True),
        (Notification.STATUS_SENT, None, False),  # 회신 대기
        (Notification.STATUS_FAILED, None, False),  # 발송 실패 데모 (재발송 버튼용)
        (Notification.STATUS_SENT, "배송지를 변경하고 싶습니다. 연락 가능한 시간이 언제일까요?", False),  # AI 답변 초안만
    ]
    for (u, dest, _, _), (status, reply, respond) in zip(employee_users[:4], past_cases):
        order = _make_order(past, u, dest, True, None, today - timedelta(days=30))
        order.notice_status = "1차 안내 완료"
        db.session.add(order)
        db.session.flush()
        subject, content = generate_email(past, order)
        n = Notification(
            event_id=past.id,
            user_id=u.id,
            order_id=order.id,
            subject=subject,
            content=content,
            status=status,
            process_status=Notification.PROCESS_DONE if respond else Notification.PROCESS_UNHANDLED,
        )
        if status == Notification.STATUS_SENT:
            n.sent_at = datetime.now() - timedelta(days=40)
        else:
            n.fail_reason = "임시 발송 오류 (시뮬레이션)"
        if reply:
            n.reply_content = reply
            n.reply_category = classify_reply(reply)
            n.reply_at = datetime.now() - timedelta(days=39)
        db.session.add(n)
        db.session.flush()

        if reply:
            r_subject, r_content = generate_reply_response(past, order, n)
            response = Notification(
                event_id=past.id,
                user_id=u.id,
                order_id=order.id,
                in_reply_to_id=n.id,
                notification_type="회신 답변",
                subject=r_subject,
                content=r_content,
                status=Notification.STATUS_SENT if respond else Notification.STATUS_PENDING,
            )
            if respond:
                response.sent_at = datetime.now() - timedelta(days=38)
            db.session.add(response)

    db.session.commit()
