"""안내메일 초안 작성.

실제 LLM API를 호출하지 않는다. MVP는 외부 API 키 없이도 시연 가능해야 하므로,
행사·수신자·마감일 정보를 반영해 사람이 쓴 것처럼 자연스러운 문장을 조합하는
규칙 기반 생성기를 대신 둔다. 나중에 실제 API로 바꾸더라도 이 함수의 반환 형태
(제목, 본문 문자열)만 유지하면 호출부는 손댈 필요가 없다.
"""


def _closing_line(dday):
    """D-day에 따라 문장의 다급함을 조절한다. 기계적으로 같은 말을 반복하지 않도록."""
    if dday is None:
        return "빠른 시일 내에 주문을 완료해 주시기 바랍니다."
    if dday <= 0:
        return "이미 마감일이 지났거나 오늘이 마감일입니다. 최대한 빨리 주문을 완료해 주시기 바랍니다."
    if dday <= 3:
        return "마감일이 얼마 남지 않았으니 서둘러 주문을 완료해 주시기 바랍니다."
    if dday <= 7:
        return "수령 희망일에 맞춰 받으시려면 마감일 전까지 주문을 완료해 주시기 바랍니다."
    return "여유가 있지만 잊지 않도록 미리 주문을 완료해 주시기 바랍니다."


def generate_email(event, order):
    """행사·주문 정보를 받아 (제목, 본문) 튜플을 돌려준다."""
    name = order.user.name
    # %-m / %-d 는 Windows의 strftime에서 지원되지 않으므로 직접 조합한다.
    deadline = (
        f"{order.deadline_date.month}월 {order.deadline_date.day}일"
        if order.deadline_date
        else "미정"
    )
    destination = order.destination or "해당 지역"
    dday = order.dday()

    subject = f"[{event.name}] 주문 마감일 안내"

    lines = [
        f"{name}님, 안녕하세요.",
        "",
        f"{event.name} 주문과 관련하여 안내드립니다.",
        "",
        "현재 아직 주문이 완료되지 않은 것으로 확인됩니다.",
        "",
        f"{name}님의 주재지({destination}) 배송 일정을 고려할 때,",
        f"수령 희망일에 맞춰 상품을 받으시려면 {deadline}까지 주문을 완료해 주시기 바랍니다.",
        "",
        _closing_line(dday),
        "",
        "감사합니다.",
    ]
    return subject, "\n".join(lines)
