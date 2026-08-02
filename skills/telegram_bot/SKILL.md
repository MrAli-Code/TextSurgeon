---
name: telegram_bot
title: 🤖 Telegram Bot Builder
description: Build interactive Telegram bots using python-telegram-bot or pyTelegramBotAPI (telebot) with commands, inline buttons, state handlers, and error resilience.
keywords: [telegram, bot, telegram bot, telebot, python-telegram-bot, ربات تلگرام, بات]
packages: [python-telegram-bot]
---

# Telegram Bot Skill

Creates Telegram automation bots with command handlers and interactive keyboards.

## Core Guidelines & Best Practices

```python
import os
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Status", callback_data="status"),
         InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Hello! I am your automated bot built with Text Surgeon Agent.",
        reply_markup=reply_markup
    )

def main():
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ Please set TELEGRAM_BOT_TOKEN in your .env or environment.")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("🤖 Bot started polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
```
