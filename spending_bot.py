"""
텔레그램 지출 관리 봇
사용법:
  카드: "농협 카드 4500"  (카드사 + 카드 + 금액)
  현금: "현금 3000"       (금액)
  메모 추가도 가능: "농협 카드 4500 스타벅스"
명령어: /summary, /history, /delete, /help
"""
import asyncpg
import calendar
import os
import re
import ssl
import threading
from datetime import datetime, time as dt_time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL     = os.environ["DATABASE_URL"]
ALLOWED_CHAT_IDS = set(map(int, os.environ["ALLOWED_CHAT_IDS"].split(",")))

MEMBER_NAMES = {
    7182419728: "LEO",
    7706672156: "JANE",
}


def clean_db_url(url):
    """asyncpg용으로 sslmode, channel_binding 파라미터 제거"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("sslmode", None)
    params.pop("channel_binding", None)
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=clean_query))


async def init_db(pool):
    async with pool.acquire() as conn:
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
        # 기존 테이블에 card_issuer 컬럼 없으면 추가
        await conn.execute("""
            ALTER TABLE expenses ADD COLUMN IF NOT EXISTS card_issuer TEXT
        """)


def parse_expense(text):
    """
    카드: "농협 카드 4500" 또는 "농협 카드 4500 스타벅스"
    현금: "현금 3000"     또는 "현금 3000 편의점"
    금액에 쉼표 허용: "농협 카드 4,500"
    """
    text = text.strip()

    # 현금
    cash = re.match(r'^현금\s+(\d[\d,]*)\s*(.*)$', text)
    if cash:
        amount = int(cash.group(1).replace(',', ''))
        description = cash.group(2).strip() or None
        return "현금", None, amount, description

    # 카드: "카드사 카드 금액 [메모]"
    card = re.match(r'^(.+?)\s+카드\s+(\d[\d,]*)\s*(.*)$', text)
    if card:
        card_issuer = card.group(1).strip()
        amount = int(card.group(2).replace(',', ''))
        description = card.group(3).strip() or None
        return "카드", card_issuer, amount, description

    return None


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
        # 개인 이번 달 누적
        my_total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE chat_id = $1
               AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())""",
            chat_id
        )
        # 가계 이번 달 합계
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"""
        )

    emoji = "💳" if payment_type == "카드" else "💵"
    now = datetime.now()
    label = f"{card_issuer} 카드" if card_issuer else "현금"
    desc_str = f" ({description})" if description else ""
    sender_name = MEMBER_NAMES.get(chat_id, user_name)

    # 보내는 사람에게 확인 메시지
    await update.message.reply_text(
        f"{emoji} 기록 완료\n"
        f"  {label} {amount:,}원{desc_str}\n\n"
        f"👤 {sender_name} {now.month}월 누적: {my_total:,}원\n"
        f"🏠 가계 {now.month}월 합계: {total:,}원"
    )

    # 상대방에게 알림
    other_id = next((uid for uid in MEMBER_NAMES if uid != chat_id), None)
    if other_id:
        other_name = MEMBER_NAMES[other_id]
        await context.bot.send_message(
            chat_id=other_id,
            text=(
                f"{emoji} {sender_name}이 {label} {amount:,}원 사용했어요{desc_str}\n\n"
                f"👤 {sender_name} {now.month}월 누적: {my_total:,}원\n"
                f"🏠 가계 {now.month}월 합계: {total:,}원"
            )
        )


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
               GROUP BY user_name
               ORDER BY total DESC"""
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
               ORDER BY created_at DESC
               LIMIT 10"""
        )

    if not rows:
        await update.message.reply_text("이번 달 내역이 없어요.")
        return

    lines = ["📋 이번 달 최근 10건\n"]
    for row in rows:
        emoji = "💳" if row["payment_type"] == "카드" else "💵"
        date = row["created_at"].strftime("%m/%d")
        label = f"{row['card_issuer']} 카드" if row["card_issuer"] else "현금"
        desc = row["description"] or "-"
        lines.append(f"{date} {emoji} {row['amount']:,}원  {label}  {desc}  ({row['user_name']})")

    await update.message.reply_text("\n".join(lines))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    chat_id = update.effective_chat.id
    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, payment_type, card_issuer, amount, description
               FROM expenses
               WHERE chat_id = $1
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
        "  현금 5000 편의점\n\n"
        "명령어:\n"
        "  /summary  이번 달 요약\n"
        "  /history  최근 10건 내역\n"
        "  /delete   내 마지막 항목 삭제\n"
        "  /help     도움말"
    )


async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    """매월 말일 21:00 KST (12:00 UTC)에 월간 리포트 발송"""
    now = datetime.now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day != last_day:
        return

    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        per_person = await conn.fetch(
            """SELECT user_name, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               GROUP BY user_name
               ORDER BY total DESC"""
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

    report = "\n".join(lines)
    for chat_id in MEMBER_NAMES:
        await context.bot.send_message(chat_id=chat_id, text=report)


async def post_init(application: Application):
    db_url = clean_db_url(DATABASE_URL)
    ssl_ctx = ssl.create_default_context()
    pool = await asyncpg.create_pool(db_url, ssl=ssl_ctx)
    await init_db(pool)
    application.bot_data["pool"] = pool
    # 매일 21:00 KST (12:00 UTC) 말일 체크
    application.job_queue.run_daily(monthly_report_job, time=dt_time(12, 0, 0))
    print("DB 연결 완료, 봇 시작!")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # 헬스체크 로그 억제


def run_health_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))
    app.run_polling()


if __name__ == "__main__":
    main()
