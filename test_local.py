import logging
import re
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфігурація для локального тестування
# ВАЖЛИВО: НЕ ВИКОРИСТОВУЙТЕ ЦЕ НА ПРОДАКШЕНІ!
BOT_TOKEN = "7652276422:AAGC-z7Joic3m7cFKXVdafvKvaqTZ3VZsBo"  # Ваш токен для тестування
ADMIN_IDS = [1648720935]  # Ваш ID адміністратора
GROUP_CHAT_ID = None  # Для локального тесту - отключено создание ссылок
GROUP_INVITE_LINK = "https://t.me/+RItcaiRa-KU5ZThi"  # Основна ссылка для тестів

# Стани користувача
PENDING_REQUESTS = {}
USER_APPLICATIONS = {}  # Зберігання даних заявок користувачів

def is_valid_image_url(url: str) -> bool:
    """Перевіряє, чи є URL валідним посиланням на зображення"""
    # Базова перевірка формату URL
    url_pattern = re.compile(
        r'^https?://'  # http:// або https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # домен
        r'localhost|'  # localhost
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # IP
        r'(?::\d+)?'  # опціональний порт
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    
    if not url_pattern.match(url):
        return False
    
    # Перевіряємо розширення файлу
    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')
    if any(url.lower().endswith(ext) for ext in image_extensions):
        return True
    
    # Перевіряємо популярні хостинги зображень
    image_hosts = ['imgbb.com', 'imgur.com', 'postimg.cc', 'ibb.co', 'imageban.ru', 'radikal.ru']
    if any(host in url.lower() for host in image_hosts):
        return True
    
    # Додаткова перевірка через HTTP HEAD запит
    try:
        response = requests.head(url, timeout=5)
        content_type = response.headers.get('content-type', '')
        return content_type.startswith('image/')
    except:
        return False

def validate_image_urls(urls: list) -> tuple:
    """Валідує список URL зображень"""
    valid_urls = []
    invalid_urls = []
    
    for url in urls:
        url = url.strip()
        if is_valid_image_url(url):
            valid_urls.append(url)
        else:
            invalid_urls.append(url)
    
    return valid_urls, invalid_urls

async def create_invite_link(context: ContextTypes.DEFAULT_TYPE, user_name: str) -> str:
    """Створює одноразове посилання-запрошення для користувача (ТЕСТ РЕЖИМ)"""
    # В локальному тесті не створюємо реальні посилання
    logger.info(f"[ТЕСТ] Створено б одноразове посилання для {user_name}")
    return GROUP_INVITE_LINK

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
        "Щоб отримати доступ до групи, натисніть кнопку нижче та заповніть заявку.\n\n"
        "🔧 ЛОКАЛЬНИЙ ТЕСТ РЕЖИМ"
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

async def handle_application_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Универсальный обработчик текстовых сообщений для заявок"""
    user = update.effective_user
    user_id = user.id
    
    # Если пользователь не в процессе подачи заявки
    if not context.user_data.get('awaiting_application'):
        return
    
    # Если это первое сообщение - обрабатываем как текст заявки
    if user_id not in USER_APPLICATIONS:
        await handle_text_application(update, context)
    else:
        user_data = USER_APPLICATIONS[user_id]
        if user_data.get('step') == 'waiting_text':
            await handle_text_application(update, context)
        elif user_data.get('step') == 'waiting_image_urls':
            await handle_image_urls_application(update, context)

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
            'image_urls': [],
            'step': 'waiting_text'
        }

    USER_APPLICATIONS[user_id]['text'] = update.message.text
    USER_APPLICATIONS[user_id]['step'] = 'waiting_image_urls'

    await update.message.reply_text(
        "✅ Текст заявки отримано!\n\n"
        "Тепер надішліть посилання на скріншоти:\n"
    "1. Скріншот посвідчення\n"
    "2. Скріншот трудової книжки\n\n"
        "📋 Інструкція:\n"
        "• Завантажте скріншоти на imgbb.com, imgur.com або подібний сервіс\n"
        "• Скопіюйте прямі посилання на зображення\n"
        "• Надішліть їх одним повідомленням, кожне з нового рядка\n\n"
        "Приклад:\n"
        "https://i.ibb.co/example1.jpg\n"
        "https://i.ibb.co/example2.png"
    )

async def handle_image_urls_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник посилань на зображення для заявок"""
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
    
    # Перевіряємо чи очікуємо посилання на зображення
    if user_data.get('step') != 'waiting_image_urls':
        await update.message.reply_text(
            "❌ Будь ласка, спочатку введіть текст заявки."
        )
        return

    # Отримуємо текст повідомлення та розділяємо на рядки
    message_text = update.message.text.strip()
    urls = [url.strip() for url in message_text.split('\n') if url.strip()]
    
    if len(urls) < 2:
        await update.message.reply_text(
            "❌ Будь ласка, надішліть мінімум 2 посилання на зображення:\n"
            "1. Скріншот посвідчення\n"
            "2. Скріншот трудової книжки\n\n"
            "Кожне посилання з нового рядка."
        )
        return
    
    # Валідуємо URL
    valid_urls, invalid_urls = validate_image_urls(urls)
    
    if invalid_urls:
        invalid_list = '\n'.join(f"• {url}" for url in invalid_urls)
        await update.message.reply_text(
            f"❌ Деякі посилання некоректні:\n\n{invalid_list}\n\n"
            "Будь ласка, перевірте посилання та надішліть тільки валідні URL зображень.\n"
            "Підтримуються: imgbb.com, imgur.com, postimg.cc та інші."
        )
        return
    
    if len(valid_urls) < 2:
        await update.message.reply_text(
            "❌ Потрібно мінімум 2 валідних посилання на зображення.\n"
            "Будь ласка, завантажте скріншоти на imgbb.com або imgur.com та надішліть прямі посилання."
        )
        return

    # Зберігаємо посилання
    user_data['image_urls'] = valid_urls
    
    await finalize_application(update, context, user_id)

def get_image_info(url: str) -> str:
    """Отримуємо інформацію про зображення для адміністратора"""
    return f"🔗 Посилання: {url}"

async def finalize_application(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Завершуємо обробку заявки"""
    user_data = USER_APPLICATIONS[user_id]
    user = user_data['user']

    # Створюємо заявку для обробки
    PENDING_REQUESTS[user_id] = {
        'user': user,
        'application': user_data['text'],
        'image_urls': user_data['image_urls']
    }

    # Відправляємо підтвердження користувачу
    await update.message.reply_text(
        "✅ Вашу заявку повністю отримано!\n\n"
        f"📝 Текст: отримано\n"
        f"🔗 Посилання на зображення: {len(user_data['image_urls'])}\n\n"
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

    # Формуємо список зображень для адміністратора
    images_list = "\n".join([f"{i+1}. {url}" for i, url in enumerate(user_data['image_urls'])])

    admin_message = (
        "🆕 Нова заявка на доступ! (ЛОКАЛЬНИЙ ТЕСТ)\n\n"
        f"👤 Користувач: {user.first_name} {user.last_name or ''}\n"
        f"🆔 ID: {user.id}\n"
        f"📱 Нікнейм: @{user.username or 'немає'}\n\n"
        f"📝 Заявка:\n{user_data['text']}\n\n"
        f"🔗 Зображення ({len(user_data['image_urls'])}):\n{images_list}"
    )

    for admin_id in ADMIN_IDS:
        try:
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
    
    # Створюємо індивідуальне одноразове посилання (в тесті - імітація)
    try:
        user_display_name = f"{user.first_name} {user.last_name or ''}".strip()
        if user.username:
            user_display_name += f" (@{user.username})"
        
        invite_link = await create_invite_link(context, user_display_name)
        
        # Відправляємо персональне посилання користувачу
        invite_message = (
            "🎉 Вітаємо! (ЛОКАЛЬНИЙ ТЕСТ)\n\n"
            "Вашу заявку схвалено! Ви отримали персональне запрошення до групи поліції UKRAINE GTA.\n\n"
            f"🔗 Ваше особисте посилання:\n{invite_link}\n\n"
            "⚠️ ВАЖЛИВО (в реальній версії):\n"
            "• Це посилання створено спеціально для вас\n"
            "• Воно одноразове - може використати тільки одна людина\n"
            "• Не передавайте його іншим\n"
            "• Після вступу посилання стане недійсним\n\n"
            "🔧 ТЕСТ: В реальній версії буде створено унікальне посилання"
        )
        
        await context.bot.send_message(
            chat_id=user_id,
            text=invite_message
        )
        
        # Повідомляємо адміністратору про успіх
        await query.edit_message_text(
            f"✅ Заявку користувача {user.first_name} ({user.id}) схвалено! (ТЕСТ)\n\n"
            f"👤 Користувач: {user_display_name}\n"
            f"🔗 В реальній версії створено б персональне посилання\n"
            f"📊 Ліміт використань: 1 раз\n\n"
            f"Користувачу надіслано тестове повідомлення."
        )
        
        logger.info(f"[ТЕСТ] Заявку користувача {user_display_name} ({user.id}) схвалено")
        
    except Exception as e:
        logger.error(f"Помилка при схваленні заявки користувача {user.id}: {e}")
        await query.edit_message_text(
            f"⚠️ Помилка в тесті: {str(e)}"
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
        f"📊 Статистика (ЛОКАЛЬНИЙ ТЕСТ):\n\n"
        f"Заявок в очікуванні: {pending_count}"
    )

def main() -> None:
    """Запуск бота"""
    print("🔧 ЗАПУСК ЛОКАЛЬНОГО ТЕСТУ БОТА")
    print(f"🤖 Токен: {BOT_TOKEN[:10]}...")
    print(f"👮 Адміни: {ADMIN_IDS}")
    print("=" * 50)
    
    # Створюємо додаток
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Додаємо обробники
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_application_text))
    
    # Запускаємо бота
    logger.info("🔧 Локальний тест бота запущено!")
    print("🔧 Локальний тест бота запущено! Натисніть Ctrl+C для зупинки.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()