"""
텔레그램 지출 관리 봇
사용법: "카드 45000 스타벅스" 또는 "현금 12000 편의점"
명령어: /summary, /history, /delete, /help
"""
import asyncpg
import os
import re
import ssl
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL     = os.environ["DATABASE_URL"]
ALLOWED_CHAT_IDS = set(map(int, os.environ["ALLOWED_CHAT_IDS"].split(",")))


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
                amount       INTEGER NOT NULL,
                description  TEXT,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)


def parse_expense(text):
    match = re.match(r'^(카드|현금)\s+(\d+)\s*(.*)$', text.strip())
    if match:
        return match.group(1), int(match.group(2)), match.group(3).strip() or None
    return None


async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        return

    parsed = parse_expense(update.message.text)
    if not parsed:
        return

    payment_type, amount, description = parsed
    user_name = update.effective_user.first_name
    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO expenses (chat_id, user_name, payment_type, amount, description) VALUES ($1, $2, $3, $4, $5)",
            chat_id, user_name, payment_type, amount, description
        )
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"
        )

    emoji = "💳" if payment_type == "카드" else "💵"
    now = datetime.now()
    await update.message.reply_text(
        f"{emoji} 기록 완료\n"
        f"  {payment_type} {amount:,}원 ({description or '-'})\n\n"
        f"📊 {now.month}월 누적 지출: {total:,}원"
    )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    pool = context.bot_data["pool"]
    now = datetime.now()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT payment_type, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
               GROUP BY payment_type"""
        )
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())"
        )

    if not rows:
        await update.message.reply_text(f"📊 {now.month}월 지출 내역이 없어요.")
        return

    lines = [f"📊 {now.year}년 {now.month}월 지출 요약\n"]
    for row in rows:
        emoji = "💳" if row["payment_type"] == "카드" else "💵"
        lines.append(f"{emoji} {row['payment_type']}: {row['total']:,}원 ({row['cnt']}건)")
    lines.append(f"\n💰 합계: {total:,}원")

    await update.message.reply_text("\n".join(lines))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_name, payment_type, amount, description, created_at
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
        desc = row["description"] or "-"
        lines.append(f"{date} {emoji} {row['amount']:,}원  {desc}  ({row['user_name']})")

    await update.message.reply_text("\n".join(lines))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    pool = context.bot_data["pool"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, payment_type, amount, description FROM expenses ORDER BY created_at DESC LIMIT 1"
        )
        if not row:
            await update.message.reply_text("삭제할 내역이 없어요.")
            return
        await conn.execute("DELETE FROM expenses WHERE id = $1", row["id"])

    await update.message.reply_text(
        f"🗑 삭제 완료\n{row['payment_type']} {row['amount']:,}원 ({row['description'] or '-'})"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return
    await update.message.reply_text(
        "💬 사용법\n\n"
        "지출 입력:\n"
        "  카드 45000 스타벅스\n"
        "  현금 12000 편의점\n\n"
        "명령어:\n"
        "  /summary  이번 달 요약\n"
        "  /history  최근 10건 내역\n"
        "  /delete   마지막 항목 삭제\n"
        "  /help     도움말"
    )


async def post_init(application: Application):
    db_url = clean_db_url(DATABASE_URL)
    ssl_ctx = ssl.create_default_context()
    pool = await asyncpg.create_pool(db_url, ssl=ssl_ctx)
    await init_db(pool)
    application.bot_data["pool"] = pool
    print("DB 연결 완료, 봇 시작!")


def main():
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
