
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфігурація
BOT_TOKEN = "7652276422:AAGC-z7Joic3m7cFKXVdafvKvaqTZ3VZsBo"
ADMIN_IDS = [1648720935]  # Додайте ID адміністраторів, наприклад: [123456789, 987654321]
GROUP_INVITE_LINK = ""  # Додайте посилання на групу

# Стани користувача
PENDING_REQUESTS = {}
USER_APPLICATIONS = {}  # Зберігання даних заявок користувачів

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник команди /start"""
    user = update.effective_user
    
    keyboard = [
        [InlineKeyboardButton("📝 Подати заявку на доступ", callback_data="request_access")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_message = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        "Це бот для отримання доступу до групи поліції UKRAINE GTA.\n\n"
        "Щоб отримати доступ до групи, натисніть кнопку нижче та заповніть заявку."
    )
    
    await update.message.reply_text(welcome_message, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник натискань на кнопки"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "request_access":
        await query.edit_message_text(
            "📝 Будь ласка, надішліть наступну інформацію:\n\n"
            "1. Ваше ім'я та прізвище\n"
            "2. НПУ міста\n\n"
            "Надішліть текст одним повідомленням."
        )
        context.user_data['awaiting_application'] = True
    
    elif query.data.startswith("approve_"):
        user_id = int(query.data.split("_")[1])
        await approve_request(update, context, user_id)
    
    elif query.data.startswith("reject_"):
        user_id = int(query.data.split("_")[1])
        await reject_request(update, context, user_id)

async def handle_text_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник текстових заявок від користувачів"""
    if not context.user_data.get('awaiting_application'):
        return

    user = update.effective_user
    user_id = user.id

    # Ініціалізуємо дані користувача, якщо ще не існує
    if user_id not in USER_APPLICATIONS:
        USER_APPLICATIONS[user_id] = {
            'user': user,
            'text': None,
            'photos': [],
            'step': 'waiting_text'
        }

    USER_APPLICATIONS[user_id]['text'] = update.message.text
    USER_APPLICATIONS[user_id]['step'] = 'waiting_photos'

    await update.message.reply_text(
        "✅ Текст заявки отримано!\n\n"
        "Тепер надішліть скріншоти:\n"
        "1. Скріншот посвідчення\n"
        "2. Скріншот планшету\n\n"
        "Надішліть їх окремими повідомленнями або разом."
    )

async def handle_photo_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник фото для заявок"""
    user = update.effective_user
    user_id = user.id

    # Перевіряємо чи користувач в процесі подачі заявки
    if not context.user_data.get('awaiting_application'):
        await update.message.reply_text(
            "❌ Будь ласка, спочатку введіть команду /start та почніть подачу заявки."
        )
        return

    # Перевіряємо чи користувач вже надіслав текст
    if user_id not in USER_APPLICATIONS:
        await update.message.reply_text(
            "❌ Будь ласка, спочатку введіть текст заявки.\n"
            "Використайте команду /start для початку."
        )
        return

    user_data = USER_APPLICATIONS[user_id]

    # Додаємо фото до списку
    if update.message.photo:
        photo = update.message.photo[-1]  # Беремо фото найвищої якості
        user_data['photos'].append(photo)

        current_count = len(user_data['photos'])
        
        # Перевіряємо чи є вже 2 фото
        if current_count >= 2:
            await finalize_application(update, context, user_id)
        else:
            photos_needed = 2 - current_count
            await update.message.reply_text(
                f"✅ Фото {current_count} з 2 отримано! Залишилось надіслати ще {photos_needed} фото."
            )

def get_photo_info(photo) -> str:
    """Отримуємо інформацію про фото для адміністратора"""
    file_id = photo.file_id
    width = photo.width
    height = photo.height
    file_size = photo.file_size

    return f"📷 Фото {width}x{height}, {file_size} bytes (ID: {file_id[:20]}...)"

async def finalize_application(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Завершуємо обробку заявки"""
    user_data = USER_APPLICATIONS[user_id]
    user = user_data['user']

    # Створюємо заявку для обробки
    PENDING_REQUESTS[user_id] = {
        'user': user,
        'application': user_data['text'],
        'photos': user_data['photos']
    }

    # Відправляємо підтвердження користувачу
    await update.message.reply_text(
        "✅ Вашу заявку повністю отримано!\n\n"
        f"📝 Текст: отримано\n"
        f"📷 Фото: {len(user_data['photos'])} з 2\n\n"
        "Очікуйте на розгляд адміністратором. "
        "Ви отримаєте повідомлення, коли заявку буде розглянуто."
    )

    # Відправляємо заявку адміністраторам
    keyboard = [
        [
            InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{user_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    admin_message = (
        "🆕 Нова заявка на доступ!\n\n"
        f"👤 Користувач: {user.first_name} {user.last_name or ''}\n"
        f"🆔 ID: {user.id}\n"
        f"📱 Нікнейм: @{user.username or 'немає'}\n\n"
        f"📝 Заявка:\n{user_data['text']}\n\n"
        f"📷 Отримано {len(user_data['photos'])} скріншотів"
    )

    for admin_id in ADMIN_IDS:
        try:
            # Створюємо медіагрупу з фото
            if user_data['photos']:
                media_group = []
                for i, photo in enumerate(user_data['photos']):
                    caption = f"Скріншот {i+1} від {user.first_name} ({user.id})" if i == 0 else f"Скріншот {i+1}"
                    media_group.append(
                        InputMediaPhoto(
                            media=photo.file_id,
                            caption=caption
                        )
                    )

                # Надсилаємо медіагрупу
                await context.bot.send_media_group(
                    chat_id=admin_id,
                    media=media_group
                )

            # Надсилаємо текстове повідомлення з кнопками
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_message,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Не вдалося відправити повідомлення адміністратору {admin_id}: {e}")

    # Очищуємо дані користувача
    context.user_data['awaiting_application'] = False
    del USER_APPLICATIONS[user_id]

async def approve_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Схвалення заявки"""
    query = update.callback_query
    
    if user_id not in PENDING_REQUESTS:
        await query.edit_message_text("❌ Заявку вже оброблено або не знайдено.")
        return
    
    user_data = PENDING_REQUESTS[user_id]
    user = user_data['user']
    
    # Відправляємо посилання користувачу
    try:
        invite_message = (
            "🎉 Вітаємо!\n\n"
            "Вашу заявку схвалено! Ви можете приєднатися до групи за посиланням нижче:\n\n"
        )
        
        if GROUP_INVITE_LINK:
            invite_message += f"🔗 {GROUP_INVITE_LINK}"
            await context.bot.send_message(
                chat_id=user_id,
                text=invite_message
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=invite_message + "⚠️ Зверніться до адміністратора для отримання посилання."
            )
        
        await query.edit_message_text(
            f"✅ Заявку користувача {user.first_name} ({user.id}) схвалено!\n"
            f"Користувачу надіслано посилання на групу."
        )
    except Exception as e:
        await query.edit_message_text(
            f"⚠️ Заявку схвалено, але не вдалося відправити повідомлення користувачу: {e}"
        )
    
    # Видаляємо заявку зі списку очікування
    del PENDING_REQUESTS[user_id]

async def reject_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Відхилення заявки"""
    query = update.callback_query
    
    if user_id not in PENDING_REQUESTS:
        await query.edit_message_text("❌ Заявку вже оброблено або не знайдено.")
        return
    
    user_data = PENDING_REQUESTS[user_id]
    user = user_data['user']
    
    # Повідомляємо користувача
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "😔 На жаль, вашу заявку відхилено.\n\n"
                "Ви можете спробувати подати заявку ще раз пізніше, "
                "використавши команду /start."
            )
        )
        
        await query.edit_message_text(
            f"❌ Заявку користувача {user.first_name} ({user.id}) відхилено.\n"
            f"Користувача повідомлено."
        )
    except Exception as e:
        await query.edit_message_text(
            f"⚠️ Заявку відхилено, але не вдалося відправити повідомлення користувачу: {e}"
        )
    
    # Видаляємо заявку зі списку очікування
    del PENDING_REQUESTS[user_id]

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для адміністраторів"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return
    
    pending_count = len(PENDING_REQUESTS)
    await update.message.reply_text(
        f"📊 Статистика:\n\n"
        f"Заявок в очікуванні: {pending_count}"
    )

def main() -> None:
    """Запуск бота"""
    # Створюємо додаток
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Додаємо обробники
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_application))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_application))
    
    # Запускаємо бота
    logger.info("Бот запущено!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
