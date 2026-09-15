"""앱 설정.

SECRET_KEY는 세션 쿠키 서명에 쓰인다. 환경변수가 없으면 프로세스마다 임의 값을
쓰므로, 재시작되면 로그인이 풀린다 — 로컬 데모 용도로는 괜찮지만 서버리스에
올릴 때는 인스턴스가 수시로 재시작되므로 반드시 환경변수로 고정값을 넣어야 한다.

DATABASE_URL이 있으면 그 값을(Supabase Postgres), 없으면 로컬 SQLite 파일을
쓴다. 두 백엔드 모두 SQLAlchemy가 흡수하므로 모델·쿼리 코드는 손댈 필요 없다.
"""

import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

# Vercel 등 서버리스 환경은 프로젝트 디렉터리가 읽기 전용이라 /tmp만 쓸 수 있다.
IS_SERVERLESS = bool(os.environ.get("VERCEL"))

if not IS_SERVERLESS:
    os.makedirs(INSTANCE_DIR, exist_ok=True)


def _database_uri():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        db_dir = "/tmp" if IS_SERVERLESS else INSTANCE_DIR
        return f"sqlite:///{os.path.join(db_dir, 'delivery.db')}"

    # Supabase(Postgres)는 SSL을 요구한다. sqlite:// 등 다른 스킴에는 의미 없는
    # 파라미터라 postgres 계열일 때만 붙인다.
    if url.startswith("postgres") and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


# 실제로 쓸 Postgres 스키마 이름. 같은 Supabase 프로젝트를 다른 앱과 공유하기로
# 했으므로, "public"이 아닌 별도 구역에 이 앱의 테이블만 모아 둔다.
POSTGRES_SCHEMA = "delivery"


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SECRET_KEY_IS_EPHEMERAL = not os.environ.get("SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    _IS_POSTGRES = "sqlite" not in SQLALCHEMY_DATABASE_URI

    # 서버리스는 연결이 수시로 끊기고 재활용된다. pool_pre_ping으로 죽은 연결을
    # 자동 감지해 재연결하고, pool_recycle로 pgbouncer가 끊기 전에 먼저 갱신한다.
    #
    # schema_translate_map: models.py는 모든 테이블에 "dm"이라는 자리표시자
    # 스키마를 붙여 둔다. 여기서 그 자리표시자를 실제 값으로 바꿔 끼운다 —
    # Postgres에서는 진짜 스키마 "delivery"로, SQLite에서는 None(스키마 없음)으로.
    # SQLite는 스키마 개념이 달라 그대로 두면 에러가 나므로 반드시 None이어야 한다.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {
            "pool_pre_ping": True,
            "pool_recycle": 280,
            "execution_options": {"schema_translate_map": {"dm": POSTGRES_SCHEMA}},
        }
        if _IS_POSTGRES
        else {"execution_options": {"schema_translate_map": {"dm": None}}}
    )

    @staticmethod
    def backend_name():
        return "sqlite" if "sqlite" in Config.SQLALCHEMY_DATABASE_URI else "postgres"

    @staticmethod
    def describe_target():
        """비밀번호를 뺀 접속 대상만 돌려준다. 진단 화면에 노출해도 안전하다."""
        uri = Config.SQLALCHEMY_DATABASE_URI
        if "sqlite" in uri:
            return "local sqlite file"
        import re

        m = re.search(r"@([^/\s]+)/([^?\s]+)", uri)
        return f"{m.group(1)}/{m.group(2)}" if m else "unparsed"
