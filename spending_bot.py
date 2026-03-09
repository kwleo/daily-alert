"""
텔레그램 가계부 + 대출 관리 봇

지출 입력:
  카드: "농협 카드 4500"  (카드사 + 카드 + 금액)
  현금: "현금 3000"

명령어:
  /summary   이번 달 지출 요약
  /history   최근 10건 내역
  /delete    내 마지막 항목 삭제
  /loan      대출 현황 조회
  /setrate   금리 변경 (예: /setrate 4.50)
  /help      도움말
"""
import asyncpg
import calendar
import os
import re
import ssl
import threading
from datetime import datetime, date, time as dt_time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL     = os.environ["DATABASE_URL"]
ALLOWED_CHAT_IDS = set(map(int, os.environ["ALLOWED_CHAT_IDS"].split(",")))
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")

MEMBER_NAMES = {
    7182419728: "건우",
    7706672156: "혜연",
}

# ── 대출 상수 ────────────────────────────────────────────────────
LOAN_PRINCIPAL          = 600_000_000
LOAN_TERM_MONTHS        = 360
LOAN_START_DATE         = date(2026, 3, 6)
LOAN_PAYMENT_DAY        = 11
LOAN_INITIAL_RATE       = 4.18
LOAN_RATE_CHANGE_MONTHS = 6


# ── 유틸 ─────────────────────────────────────────────────────────
def clean_db_url(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("sslmode", None)
    params.pop("channel_binding", None)
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=clean_query))


def add_months(d, months):
    """날짜에 N개월 더하기"""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def calc_monthly_payment(remaining_balance, annual_rate_pct, remaining_months):
    """원리금 균등상환 월 납부액"""
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return round(remaining_balance / remaining_months)
    m = remaining_balance * r / (1 - (1 + r) ** -remaining_months)
    return round(m)


def calc_prorated_interest(principal, annual_rate_pct, days):
    """일할 이자"""
    return round(principal * annual_rate_pct / 100 / 365 * days)


# ── DB 초기화 ─────────────────────────────────────────────────────
async def init_db(pool):
    async with pool.acquire() as conn:
        # 지출 테이블
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id           SERIAL PRIMARY KEY,
                chat_id      BIGINT NOT NULL,
                user_name    TEXT,
                payment_type TEXT NOT NULL,
                card_issuer  TEXT,
                amount       INTEGER NOT NULL,
                description  TEXT,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            ALTER TABLE expenses ADD COLUMN IF NOT EXISTS card_issuer TEXT
        """)

        # 대출 설정 테이블
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS loan_config (
                id                    SERIAL PRIMARY KEY,
                principal             BIGINT NOT NULL,
                annual_rate           NUMERIC(6,4) NOT NULL,
                term_months           INTEGER NOT NULL,
                start_date            DATE NOT NULL,
                payment_day           INTEGER NOT NULL,
                remaining_balance     BIGINT NOT NULL,
                paid_months           INTEGER NOT NULL DEFAULT 0,
                next_rate_change_date DATE NOT NULL,
                updated_at            TIMESTAMP DEFAULT NOW()
            )
        """)

        # 대출 납부 기록 테이블
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS loan_payments (
                id                SERIAL PRIMARY KEY,
                payment_no        INTEGER NOT NULL,
                payment_date      DATE NOT NULL,
                interest_amount   BIGINT NOT NULL,
                principal_amount  BIGINT NOT NULL,
                total_amount      BIGINT NOT NULL,
                remaining_balance BIGINT NOT NULL,
                annual_rate       NUMERIC(6,4) NOT NULL,
                created_at        TIMESTAMP DEFAULT NOW()
            )
        """)

        # 초기 대출 데이터 삽입
        exists = await conn.fetchval("SELECT COUNT(*) FROM loan_config")
        if exists == 0:
            next_rate_change = add_months(LOAN_START_DATE, LOAN_RATE_CHANGE_MONTHS)
            await conn.execute("""
                INSERT INTO loan_config
                (principal, annual_rate, term_months, start_date, payment_day,
                 remaining_balance, paid_months, next_rate_change_date)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7)
            """, LOAN_PRINCIPAL, LOAN_INITIAL_RATE, LOAN_TERM_MONTHS,
                LOAN_START_DATE, LOAN_PAYMENT_DAY,
                LOAN_PRINCIPAL, next_rate_change)


# ── 지출 파싱 ─────────────────────────────────────────────────────
def parse_expense(text):
    text = text.strip()

    # 현금: "현금 3000" 또는 "현금 3000 편의점"
    cash = re.match(r'^현금\s+(\d[\d,]*)\s*(.*)$', text)
    if cash:
        amount = int(cash.group(1).replace(',', ''))
        description = cash.group(2).strip() or None
        return "현금", None, amount, description

    # 카드: "농협 카드 4500" 또는 "농협 카드 4500 스타벅스"
    card = re.match(r'^(.+?)\s+카드\s+(\d[\d,]*)\s*(.*)$', text)
    if card:
        card_issuer = card.group(1).strip()
        amount = int(card.group(2).replace(',', ''))
        description = card.group(3).strip() or None
        return "카드", card_issuer, amount, description

    return None


# ── 지출 핸들러 ───────────────────────────────────────────────────
async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        return

    parsed = parse_expense(update.message.text)
    if not parsed:
        return

    payment_type, card_issuer, amount, description = parsed
    user_name = update.effective_user.first_name
    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expenses
               (chat_id, user_name, payment_type, card_issuer, amount, description)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            chat_id, user_name, payment_type, card_issuer, amount, description
        )
        my_total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE chat_id = $1
               AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())""",
            chat_id
        )
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"""
        )

    emoji = "💳" if payment_type == "카드" else "💵"
    now = datetime.now()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    date_str = f"{now.month}/{now.day} ({weekdays[now.weekday()]})"
    label = f"{card_issuer} 카드" if card_issuer else "현금"
    desc_str = f" ({description})" if description else ""
    sender_name = MEMBER_NAMES.get(chat_id, user_name)

    await update.message.reply_text(
        f"{emoji} 기록 완료\n"
        f"  {date_str} {label} {amount:,}원{desc_str}\n\n"
        f"👤 {sender_name} {now.month}월 누적: {my_total:,}원\n"
        f"🏠 가계 {now.month}월 합계: {total:,}원"
    )

    other_id = next((uid for uid in MEMBER_NAMES if uid != chat_id), None)
    if other_id:
        await context.bot.send_message(
            chat_id=other_id,
            text=(
                f"{emoji} {sender_name}이 {date_str} {label} {amount:,}원 사용했어요{desc_str}\n\n"
                f"👤 {sender_name} {now.month}월 누적: {my_total:,}원\n"
                f"🏠 가계 {now.month}월 합계: {total:,}원"
            )
        )


# ── 지출 명령어 ───────────────────────────────────────────────────
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    pool = context.bot_data["pool"]
    now = datetime.now()

    async with pool.acquire() as conn:
        per_person = await conn.fetch(
            """SELECT user_name, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               GROUP BY user_name ORDER BY total DESC"""
        )
        per_type = await conn.fetch(
            """SELECT payment_type, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               GROUP BY payment_type"""
        )
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"""
        )

    if not per_person:
        await update.message.reply_text(f"📊 {now.month}월 지출 내역이 없어요.")
        return

    lines = [f"📊 {now.year}년 {now.month}월 지출 요약\n"]
    lines.append("👥 개인별")
    for row in per_person:
        lines.append(f"  👤 {row['user_name']}: {row['total']:,}원 ({row['cnt']}건)")
    lines.append("\n결제수단별")
    for row in per_type:
        emoji = "💳" if row["payment_type"] == "카드" else "💵"
        lines.append(f"  {emoji} {row['payment_type']}: {row['total']:,}원 ({row['cnt']}건)")
    lines.append(f"\n💰 가계 합계: {total:,}원")

    await update.message.reply_text("\n".join(lines))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_name, payment_type, card_issuer, amount, description, created_at
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               ORDER BY created_at DESC LIMIT 10"""
        )

    if not rows:
        await update.message.reply_text("이번 달 내역이 없어요.")
        return

    lines = ["📋 이번 달 최근 10건\n"]
    for row in rows:
        emoji = "💳" if row["payment_type"] == "카드" else "💵"
        dt = row["created_at"].strftime("%m/%d")
        label = f"{row['card_issuer']} 카드" if row["card_issuer"] else "현금"
        desc = row["description"] or "-"
        lines.append(f"{dt} {emoji} {row['amount']:,}원  {label}  {desc}  ({row['user_name']})")

    await update.message.reply_text("\n".join(lines))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    chat_id = update.effective_chat.id
    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, payment_type, card_issuer, amount, description
               FROM expenses WHERE chat_id = $1
               ORDER BY created_at DESC LIMIT 1""",
            chat_id
        )
        if not row:
            await update.message.reply_text("삭제할 내역이 없어요.")
            return
        await conn.execute("DELETE FROM expenses WHERE id = $1", row["id"])

    label = f"{row['card_issuer']} 카드" if row["card_issuer"] else "현금"
    await update.message.reply_text(
        f"🗑 삭제 완료\n{label} {row['amount']:,}원 ({row['description'] or '-'})"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return
    await update.message.reply_text(
        "💬 사용법\n\n"
        "지출 입력:\n"
        "  농협 카드 45000\n"
        "  신한 카드 12000 스타벅스\n"
        "  현금 8000\n"
        "  📸 결제 알림 캡처 이미지 전송 (자동 인식)\n\n"
        "지출 명령어:\n"
        "  /summary  이번 달 요약\n"
        "  /history  최근 10건 내역\n"
        "  /delete   내 마지막 항목 삭제\n\n"
        "대출 명령어:\n"
        "  /loan          대출 현황 조회\n"
        "  /setrate 4.50  금리 변경\n\n"
        "  /help     도움말"
    )


# ── 이미지 OCR ────────────────────────────────────────────────────
def parse_gemini_response(text):
    """Gemini 응답 텍스트에서 결제 정보 파싱"""
    data = {}
    for line in text.strip().split('\n'):
        if ':' in line:
            key, _, val = line.partition(':')
            data[key.strip()] = val.strip()

    try:
        amount_str = data.get('금액', '').replace(',', '').replace('원', '').strip()
        amount = int(amount_str)
    except (ValueError, AttributeError):
        return None

    payment_type = data.get('결제수단', '').strip()
    card_issuer  = data.get('카드사', '').strip() or None
    description  = data.get('가맹점', '').strip() or None

    if card_issuer in ('없음', 'N/A', '-', ''):
        card_issuer = None
    if description in ('없음', 'N/A', '-', ''):
        description = None

    if payment_type not in ('카드', '현금'):
        payment_type = '카드' if card_issuer else '현금'

    return payment_type, card_issuer, amount, description


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        return

    if not GEMINI_API_KEY:
        await update.message.reply_text("❌ Gemini API 키가 설정되지 않았어요.")
        return

    await update.message.reply_text("🔍 이미지 분석 중...")

    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        prompt = (
            "이 이미지는 카드 결제 알림, 은행 앱 푸시 알림, 또는 SMS 결제 문자입니다.\n"
            "다음 정보를 추출해주세요.\n"
            "반드시 아래 형식으로만 응답하고 다른 설명은 하지 마세요:\n\n"
            "결제수단: 카드 또는 현금\n"
            "카드사: (카드 결제인 경우 카드사/은행명, 없으면 없음)\n"
            "금액: (숫자만, 원 단위)\n"
            "가맹점: (가맹점 또는 상호명, 없으면 없음)"
        )
        image_blob = {"mime_type": "image/jpeg", "data": bytes(photo_bytes)}
        response = model.generate_content([prompt, image_blob])
        parsed = parse_gemini_response(response.text.strip())

        if not parsed:
            await update.message.reply_text(
                "❌ 결제 정보를 인식하지 못했어요.\n\n직접 입력해주세요:\n예) 농협 카드 45000 스타벅스"
            )
            return

        payment_type, card_issuer, amount, description = parsed
        label    = f"{card_issuer} 카드" if card_issuer else "현금"
        desc_str = f" ({description})" if description else ""

        context.user_data["pending_expense"] = {
            "payment_type": payment_type,
            "card_issuer":  card_issuer,
            "amount":       amount,
            "description":  description,
            "user_name":    update.effective_user.first_name,
        }

        keyboard = [[
            InlineKeyboardButton("✅ 기록", callback_data="ocr_confirm"),
            InlineKeyboardButton("❌ 취소", callback_data="ocr_cancel"),
        ]]
        await update.message.reply_text(
            f"📸 인식된 내용:\n\n{label} {amount:,}원{desc_str}\n\n기록할까요?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        await update.message.reply_text(f"❌ 오류 발생: {str(e)[:120]}")


async def ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = update.effective_chat.id
    await query.answer()

    if query.data == "ocr_cancel":
        context.user_data.pop("pending_expense", None)
        await query.edit_message_text("❌ 취소되었어요.")
        return

    if query.data != "ocr_confirm":
        return

    pending = context.user_data.pop("pending_expense", None)
    if not pending:
        await query.edit_message_text("❌ 기록할 내용이 없어요. 이미지를 다시 보내주세요.")
        return

    payment_type = pending["payment_type"]
    card_issuer  = pending["card_issuer"]
    amount       = pending["amount"]
    description  = pending["description"]
    user_name    = pending["user_name"]

    pool = context.bot_data["pool"]
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expenses
               (chat_id, user_name, payment_type, card_issuer, amount, description)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            chat_id, user_name, payment_type, card_issuer, amount, description
        )
        my_total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE chat_id = $1
               AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())""",
            chat_id
        )
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"""
        )

    emoji   = "💳" if payment_type == "카드" else "💵"
    now     = datetime.now()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    date_str = f"{now.month}/{now.day} ({weekdays[now.weekday()]})"
    label    = f"{card_issuer} 카드" if card_issuer else "현금"
    desc_str = f" ({description})" if description else ""
    sender_name = MEMBER_NAMES.get(chat_id, user_name)

    await query.edit_message_text(
        f"{emoji} 기록 완료\n"
        f"  {date_str} {label} {amount:,}원{desc_str}\n\n"
        f"👤 {sender_name} {now.month}월 누적: {my_total:,}원\n"
        f"🏠 가계 {now.month}월 합계: {total:,}원"
    )

    other_id = next((uid for uid in MEMBER_NAMES if uid != chat_id), None)
    if other_id:
        await context.bot.send_message(
            chat_id=other_id,
            text=(
                f"{emoji} {sender_name}이 {date_str} {label} {amount:,}원 사용했어요{desc_str}\n\n"
                f"👤 {sender_name} {now.month}월 누적: {my_total:,}원\n"
                f"🏠 가계 {now.month}월 합계: {total:,}원"
            )
        )


# ── 대출 명령어 ───────────────────────────────────────────────────
async def loan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    pool = context.bot_data["pool"]
    async with pool.acquire() as conn:
        cfg = await conn.fetchrow("SELECT * FROM loan_config LIMIT 1")

    if not cfg:
        await update.message.reply_text("대출 정보가 없어요.")
        return

    annual_rate       = float(cfg['annual_rate'])
    remaining_balance = cfg['remaining_balance']
    paid_months       = cfg['paid_months']
    term_months       = cfg['term_months']
    remaining_months  = term_months - paid_months

    monthly_payment = calc_monthly_payment(remaining_balance, annual_rate, remaining_months)
    interest  = round(remaining_balance * annual_rate / 100 / 12)
    principal = monthly_payment - interest

    now = datetime.now()
    payoff = add_months(date(now.year, now.month, 1), remaining_months)
    progress_pct = paid_months / term_months * 100

    await update.message.reply_text(
        f"🏠 대출 현황\n\n"
        f"잔여 원금: {remaining_balance:,}원\n"
        f"현재 금리: {annual_rate:.2f}%\n"
        f"월 납부액: {monthly_payment:,}원\n"
        f"  └ 이자: {interest:,}원\n"
        f"  └ 원금: {principal:,}원\n\n"
        f"납부 진행: {paid_months} / {term_months}회 ({progress_pct:.1f}%)\n"
        f"완납 예정: {payoff.year}년 {payoff.month}월\n"
        f"다음 금리 변동: {cfg['next_rate_change_date'].strftime('%Y년 %m월')}"
    )


async def setrate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    if not context.args:
        await update.message.reply_text("사용법: /setrate 4.50")
        return

    try:
        new_rate = float(context.args[0])
        if not (0 < new_rate < 20):
            raise ValueError
    except ValueError:
        await update.message.reply_text("올바른 금리를 입력해주세요. 예: /setrate 4.50")
        return

    pool = context.bot_data["pool"]
    async with pool.acquire() as conn:
        cfg = await conn.fetchrow("SELECT * FROM loan_config LIMIT 1")
        if not cfg:
            await update.message.reply_text("대출 정보가 없어요.")
            return

        old_rate     = float(cfg['annual_rate'])
        next_change  = add_months(cfg['next_rate_change_date'], LOAN_RATE_CHANGE_MONTHS)

        await conn.execute("""
            UPDATE loan_config SET
                annual_rate = $1,
                next_rate_change_date = $2,
                updated_at = NOW()
            WHERE id = $3
        """, new_rate, next_change, cfg['id'])

    remaining_balance = cfg['remaining_balance']
    remaining_months  = cfg['term_months'] - cfg['paid_months']
    new_monthly = calc_monthly_payment(remaining_balance, new_rate, remaining_months)

    msg = (
        f"✅ 금리 변경 완료\n\n"
        f"이전 금리: {old_rate:.2f}%\n"
        f"새 금리:   {new_rate:.2f}%\n\n"
        f"잔여 원금: {remaining_balance:,}원\n"
        f"새 월 납부액: {new_monthly:,}원\n"
        f"다음 금리 변동: {next_change.strftime('%Y년 %m월')}"
    )
    for chat_id in MEMBER_NAMES:
        await context.bot.send_message(chat_id=chat_id, text=msg)


# ── 스케줄 작업 ───────────────────────────────────────────────────
async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    """매월 말일 21:00 KST (12:00 UTC) 가계 지출 결산"""
    now = datetime.utcnow()
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day != last_day:
        return

    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        per_person = await conn.fetch(
            """SELECT user_name, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               GROUP BY user_name ORDER BY total DESC"""
        )
        per_type = await conn.fetch(
            """SELECT payment_type, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               GROUP BY payment_type"""
        )
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"""
        )
        prev_total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW() - INTERVAL '1 month')"""
        )

    if not per_person:
        return

    lines = [f"📅 {now.year}년 {now.month}월 결산 리포트\n"]
    lines.append("👥 개인별")
    for row in per_person:
        lines.append(f"  👤 {row['user_name']}: {row['total']:,}원 ({row['cnt']}건)")
    lines.append("\n결제수단별")
    for row in per_type:
        emoji = "💳" if row["payment_type"] == "카드" else "💵"
        lines.append(f"  {emoji} {row['payment_type']}: {row['total']:,}원 ({row['cnt']}건)")
    lines.append(f"\n💰 이달 총 지출: {total:,}원")
    if prev_total > 0:
        diff  = total - prev_total
        pct   = diff / prev_total * 100
        arrow = "▲" if diff > 0 else "▼"
        lines.append(f"📊 전월 대비: {arrow} {abs(diff):,}원 ({pct:+.1f}%)")
    else:
        lines.append("📊 전월 대비: 전월 데이터 없음")

    report = "\n".join(lines)
    for chat_id in MEMBER_NAMES:
        await context.bot.send_message(chat_id=chat_id, text=report)


async def loan_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    """매월 12일 09:00 KST (00:00 UTC) 대출 납부 브리핑"""
    now = datetime.utcnow()
    if now.day != 12:
        return

    pool = context.bot_data["pool"]
    async with pool.acquire() as conn:
        cfg = await conn.fetchrow("SELECT * FROM loan_config LIMIT 1")
        if not cfg:
            return

        # 이번 달 11일에 이미 기록됐는지 확인
        payment_date = date(now.year, now.month, LOAN_PAYMENT_DAY)
        already = await conn.fetchval(
            "SELECT COUNT(*) FROM loan_payments WHERE payment_date = $1", payment_date
        )
        if already:
            return

        # 마지막 납부 번호 확인
        last = await conn.fetchrow(
            "SELECT payment_no FROM loan_payments ORDER BY payment_no DESC LIMIT 1"
        )

    annual_rate       = float(cfg['annual_rate'])
    remaining_balance = cfg['remaining_balance']
    term_months       = cfg['term_months']
    paid_months       = cfg['paid_months']

    if last is None:
        # 첫 납부: 일할 이자 (대출 실행일 ~ 첫 납부일)
        first_payment_date = date(
            cfg['start_date'].year, cfg['start_date'].month, LOAN_PAYMENT_DAY
        )
        days     = (first_payment_date - cfg['start_date']).days
        interest = calc_prorated_interest(LOAN_PRINCIPAL, annual_rate, days)
        principal_paid = 0
        total          = interest
        new_balance    = LOAN_PRINCIPAL
        payment_no     = 0
        is_first       = True
    else:
        # 원리금 균등상환
        remaining_months = term_months - paid_months
        monthly_payment  = calc_monthly_payment(remaining_balance, annual_rate, remaining_months)
        interest         = round(remaining_balance * annual_rate / 100 / 12)
        principal_paid   = monthly_payment - interest
        new_balance      = remaining_balance - principal_paid
        total            = monthly_payment
        payment_no       = paid_months + 1
        is_first         = False

    # DB 기록
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO loan_payments
            (payment_no, payment_date, interest_amount, principal_amount,
             total_amount, remaining_balance, annual_rate)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, payment_no, payment_date, interest, principal_paid,
            total, new_balance, annual_rate)

        if is_first:
            # 일할: 잔액/회차 변동 없음
            await conn.execute(
                "UPDATE loan_config SET updated_at = NOW() WHERE id = $1", cfg['id']
            )
        else:
            await conn.execute("""
                UPDATE loan_config SET
                    remaining_balance = $1,
                    paid_months = paid_months + 1,
                    updated_at = NOW()
                WHERE id = $2
            """, new_balance, cfg['id'])

    # 메시지 작성
    header = f"🏠 {now.month}월 대출 납부 내역 ({now.month}/{LOAN_PAYMENT_DAY})\n"
    lines  = [header]

    if is_first:
        days = (date(cfg['start_date'].year, cfg['start_date'].month, LOAN_PAYMENT_DAY)
                - cfg['start_date']).days
        regular_monthly = calc_monthly_payment(LOAN_PRINCIPAL, annual_rate, term_months)
        lines.append(f"첫 납부 (대출 실행 후 {days}일치 일할 이자)")
        lines.append(f"납부액: {total:,}원")
        lines.append(f"  └ 이자: {interest:,}원")
        lines.append(f"  └ 원금: 0원")
        lines.append(f"\n대출 잔액: {new_balance:,}원")
        lines.append(f"다음 달부터 월 납부액: {regular_monthly:,}원 (1/360회)")
    else:
        remaining_after = term_months - payment_no
        payoff = add_months(date(now.year, now.month, 1), remaining_after)
        lines.append(f"납부액: {total:,}원")
        lines.append(f"  └ 이자: {interest:,}원")
        lines.append(f"  └ 원금: {principal_paid:,}원")
        lines.append(f"\n대출 잔액: {new_balance:,}원")
        lines.append(f"납부 회차: {payment_no} / {term_months}회")
        lines.append(f"완납 예정: {payoff.year}년 {payoff.month}월")

    msg = "\n".join(lines)
    for chat_id in MEMBER_NAMES:
        await context.bot.send_message(chat_id=chat_id, text=msg)


async def rate_change_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """6개월마다 금리 변동 알림"""
    today = date.today()
    pool  = context.bot_data["pool"]

    async with pool.acquire() as conn:
        cfg = await conn.fetchrow("SELECT * FROM loan_config LIMIT 1")

    if not cfg or today != cfg['next_rate_change_date']:
        return

    months_elapsed = (
        (today.year - LOAN_START_DATE.year) * 12
        + (today.month - LOAN_START_DATE.month)
    )
    change_no = months_elapsed // LOAN_RATE_CHANGE_MONTHS

    msg = (
        f"⚠️ 금리 변동 시점 (대출 실행 {change_no * LOAN_RATE_CHANGE_MONTHS}개월)\n\n"
        f"현재 금리: {float(cfg['annual_rate']):.2f}%\n"
        f"새 금리를 입력해주세요:\n\n"
        f"/setrate 4.50"
    )
    for chat_id in MEMBER_NAMES:
        await context.bot.send_message(chat_id=chat_id, text=msg)


# ── 앱 초기화 ─────────────────────────────────────────────────────
async def post_init(application: Application):
    db_url  = clean_db_url(DATABASE_URL)
    ssl_ctx = ssl.create_default_context()
    pool    = await asyncpg.create_pool(db_url, ssl=ssl_ctx)
    await init_db(pool)
    application.bot_data["pool"] = pool

    jq = application.job_queue
    # 매일 00:00 UTC (09:00 KST) 실행
    jq.run_daily(monthly_report_job,    time=dt_time(12, 0, 0))  # 말일 체크
    jq.run_daily(loan_briefing_job,     time=dt_time(0, 0, 0))   # 12일 체크
    jq.run_daily(rate_change_reminder_job, time=dt_time(0, 0, 0))  # 금리 변동일 체크

    print("DB 연결 완료, 봇 시작!")


# ── 헬스체크 HTTP 서버 ────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def run_health_server():
    HTTPServer(("0.0.0.0", 8080), HealthHandler).serve_forever()


# ── 메인 ─────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("summary",  summary_command))
    app.add_handler(CommandHandler("history",  history_command))
    app.add_handler(CommandHandler("delete",   delete_command))
    app.add_handler(CommandHandler("loan",     loan_command))
    app.add_handler(CommandHandler("setrate",  setrate_command))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(ocr_callback, pattern="^ocr_"))
    app.run_polling()


if __name__ == "__main__":
    main()
