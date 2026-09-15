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


# ---------------------------------------------------------------------------
# 회신 자동 분류 + 답변 초안 작성
#
# 조직원이 회신을 보내면 (1) 내용을 보고 유형을 자동으로 분류하고,
# (2) 그 유형에 맞는 답변 초안을 바로 만들어 둔다. 관리자는 그걸 훑어보고
# 발송 버튼만 누르면 된다 — "회신을 읽고 매번 답장을 손으로 쓰는" 일이
# 사라지는 게 이 기능의 핵심이다. 분류는 키워드 매칭이라 완벽하지 않을 수
# 있어서, 실제 발송 전에는 반드시 사람이 한 번 보게 한다.
# ---------------------------------------------------------------------------

# (분류명, 이 안에 하나라도 있으면 그 분류로 판단할 키워드들)
# 위에서부터 순서대로 검사한다 — "취소"처럼 더 구체적인 신호를 "주문"보다
# 먼저 봐야, "주문을 취소하고 싶어요"가 엉뚱하게 "주문 예정"으로 잡히지 않는다.
_CLASSIFY_RULES = (
    ("주문 취소", ("취소", "캔슬")),
    ("배송지 변경 요청", ("배송지", "주소", "이사", "받을 곳", "수령지")),
    ("주문 불필요", ("필요없", "필요 없", "안 살", "안살", "괜찮습니다", "괜찮아요", "이미 주문", "이미 구매", "다른 채널")),
    ("배송 관련 문의", ("배송", "언제 오", "도착", "발송일")),
    ("주문 예정", ("예정", "하겠습니다", "할게요", "할 예정", "곧 주문", "주문하겠", "구매하겠")),
)


def classify_reply(text):
    """회신 본문을 보고 REPLY_CATEGORIES 중 하나로 분류한다. 못 정하면 '기타 문의'."""
    if not text:
        return "기타 문의"
    lowered = text.replace(" ", "")
    for category, keywords in _CLASSIFY_RULES:
        for kw in keywords:
            if kw.replace(" ", "") in lowered:
                return category
    return "기타 문의"


_RESPONSE_BODY = {
    "주문 예정": (
        "회신 감사합니다.\n\n"
        "{name}님께서 주문하실 예정이라는 점 확인했습니다.\n"
        "수령 희망일에 맞춰 받으시려면 {deadline}까지는 주문을 완료해 주시면 됩니다.\n\n"
        "따로 문의사항 있으시면 언제든 말씀해 주세요."
    ),
    "주문 불필요": (
        "회신 감사합니다.\n\n"
        "{name}님은 이번 건 주문이 필요하지 않은 것으로 확인했습니다.\n"
        "앞으로 이번 건에 대한 추가 안내는 발송하지 않겠습니다.\n\n"
        "혹시 착오가 있었다면 언제든 다시 말씀해 주세요."
    ),
    "주문 취소": (
        "회신 감사합니다.\n\n"
        "{name}님의 주문 취소 요청을 확인했습니다. 담당자가 취소 처리를 진행하겠습니다.\n"
        "처리에 1~2일 정도 소요될 수 있는 점 양해 부탁드립니다.\n\n"
        "다시 주문을 원하실 경우 마감일({deadline}) 전까지는 언제든 가능합니다."
    ),
    "배송 관련 문의": (
        "회신 감사합니다.\n\n"
        "{name}님께서 문의하신 배송 관련 내용을 확인했습니다. 담당자가 구체적인 일정을\n"
        "확인한 뒤 다시 안내드리겠습니다.\n\n"
        "빠르게 답변드리도록 하겠습니다."
    ),
    "배송지 변경 요청": (
        "회신 감사합니다.\n\n"
        "{name}님의 배송지 변경 요청을 확인했습니다. 변경하실 새 주소를 회신으로\n"
        "다시 알려주시면 담당자가 반영해 드리겠습니다.\n\n"
        "마감일({deadline}) 전까지 처리해 드리겠습니다."
    ),
    "기타 문의": (
        "회신 감사합니다.\n\n"
        "{name}님께서 보내주신 내용을 확인했습니다. 담당자가 확인한 뒤 다시\n"
        "안내드리겠습니다.\n\n"
        "빠르게 답변드리도록 하겠습니다."
    ),
}


def generate_reply_response(event, order, source_notification):
    """조직원의 회신에 대한 답변 초안을 (제목, 본문)으로 돌려준다.

    source_notification.reply_category(자동 분류된 값)에 맞는 문구를 고른다.
    분류가 REPLY_CATEGORIES 중 하나가 아니면(방어적으로) 기타 문의 문구를 쓴다.
    """
    name = order.user.name
    deadline = (
        f"{order.deadline_date.month}월 {order.deadline_date.day}일"
        if order.deadline_date
        else "미정"
    )
    category = source_notification.reply_category
    template = _RESPONSE_BODY.get(category, _RESPONSE_BODY["기타 문의"])
    body = template.format(name=name, deadline=deadline)

    subject = f"[{event.name}] 회신 확인 — {category or '기타 문의'}"
    return subject, body
