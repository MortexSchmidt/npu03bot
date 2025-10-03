import os
import logging
import re
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)
from db import init_db, upsert_profile, update_profile_fields, get_profile
from db import replace_profile_images
from db import (
    insert_warning,
    insert_neaktyv_request,
    decide_neaktyv_request,
    insert_access_application,
    decide_access_application,
)
from db import init_db, upsert_profile, update_profile_fields, get_profile
try:
    from db import get_profile_by_username, search_profiles
except ImportError:
    get_profile_by_username = None  # type: ignore
    search_profiles = None  # type: ignore

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфігурація
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не знайдено змінну оточення BOT_TOKEN. Перевірте налаштування на хостингу.")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "1648720935")
ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',')]

GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # ID групи для створення запрошень
GROUP_INVITE_LINK = "https://t.me/+RItcaiRa-KU5ZThi"  # Основне посилання (резервне)

# Додаткові налаштування для відправки в теми (forum topics)
def _int_or_none(val: str | None):
    try:
        return int(val) if val is not None and val != "" else None
    except Exception:
        return None

# Основний чат для звітів/тем: беремо з REPORTS_CHAT_ID, інакше GROUP_CHAT_ID, інакше з посилань на теми
REPORTS_CHAT_ID = _int_or_none(os.getenv("REPORTS_CHAT_ID")) or _int_or_none(os.getenv("GROUP_CHAT_ID")) or -1003191532549

# ID тем за замовчуванням з ваших посилань
WARNINGS_TOPIC_ID = _int_or_none(os.getenv("WARNINGS_TOPIC_ID")) or 146
AFK_TOPIC_ID = _int_or_none(os.getenv("AFK_TOPIC_ID")) or 152

# Стани користувача
PENDING_REQUESTS = {}
USER_APPLICATIONS = {}  # Зберігання даних заявок користувачів
 
# Тимчасовий рефіл профілю (стани діалогу)
REFILL_NAME, REFILL_NPU, REFILL_RANK, REFILL_IMAGES = range(4)

# Підрозділи НПУ (UKRAINE GTA) з описами
NPU_DEPARTMENTS = {
    # 1. НАВС / ХНУВС
    "navs": {
        "title": "Національна академія внутрішніх справ (НАВС / ХНУВС)",
        "tag": "[ХНУВС]",
        "location": "перебуває при ГУНП м. Харкова",
        "eligibility": "вступ до НПУ — автоматичне зарахування",
        "desc": "Провідний навчальний заклад МВС для підготовки, перепідготовки та підвищення кваліфікації працівників поліції; наукові дослідження, міжнародна співпраця.",
    },
    # 2. КОРД
    "kord": {
        "title": "Корпус Оперативно-Раптових Дій (КОРД)",
        "tag": "[КОРД]",
        "location": "перебуває в УНПУ м. Дніпра",
        "eligibility": "з 4-го порядкового звання",
        "desc": "Елітний спецпідрозділ: штурмові/антитерористичні операції, звільнення заручників, нейтралізація озброєних злочинців, взаємодія з іншими підрозділами.",
    },
    # 3. ДПП
    "dpp": {
        "title": "Департамент Патрульної Поліції (ДПП)",
        "tag": "[ДПП]",
        "location": "перебуває в УНПУ м. Харкова",
        "eligibility": "з 3-го порядкового звання",
        "desc": "Патрулювання, реагування на виклики, профілактика правопорушень, ПДР, оформлення адмінправопорушень, перша допомога при ДТП.",
    },
    # 4. ГСУ
    "gsu": {
        "title": "Головне Слідче Управління (ГСУ)",
        "tag": "[ГСУ]",
        "location": "перебуває в ГУНПУ м. Києва",
        "eligibility": "офіцерський склад, спец. у кримінальному процесі",
        "desc": "Досудове розслідування особливо тяжких злочинів, координація регіональних слідчих, взаємодія з прокуратурою та спецслужбами.",
    },
    # 5. ДВБ
    "dvb": {
        "title": "Департамент Внутрішньої Безпеки (ДВБ)",
        "tag": "[ДВБ]",
        "location": "перебуває в ГУНПУ м. Києва",
        "eligibility": "відбір у спеціалізований підрозділ",
        "desc": "Протидія корупції та злочинам у поліції, службові розслідування, оперативні заходи, взаємодія з антикорупційними органами.",
    },
    # 6. НЦУП
    "ncup": {
        "title": "Національний Центр Управління Поліцією (НЦУП)",
        "tag": "[НЦУП]",
        "location": "перебуває в ГУНПУ м. Києва",
        "eligibility": "центральний оперативно-аналітичний підрозділ",
        "desc": "Координація підрозділів у реальному часі, диспетчеризація 102, аналітика, підтримка інформаційних систем і кібербезпека.",
    },
}

# Список звань НПУ для UKRAINE GTA (по порядку)
NPU_RANKS = [
    "Рядовий",
    "Капрал",
    "Сержант",
    "Старший сержант",
    "Молодший лейтенант",
    "Лейтенант",
    "Старший лейтенант",
    "Капітан",
    "Майор",
    "Підполковник",
    "Полковник",
    "Генерал",
]

def parse_ranked_name(text: str) -> tuple[str | None, str]:
    """Виділяє звання на початку рядка, якщо воно є, та повертає (rank, name).
    Якщо звання не знайдено — повертає (None, original_text).
    Порівняння нечутливе до регістру.
    """
    s = text.strip()
    lower = s.lower()
    for rank in NPU_RANKS:
        r = rank.lower()
        if lower.startswith(r + " "):
            name = s[len(rank):].strip()
            return rank, name
    return None, s

def is_ukrainian_name(text: str) -> bool:
    """Перевіряє, чи містить текст українські ім'я та прізвище"""
    # Українські літери
    ukrainian_pattern = re.compile(r'^[А-ЯІЇЄа-яіїє\'\-\s]+$', re.UNICODE)
    
    # Перевіряємо базовий формат
    if not ukrainian_pattern.match(text.strip()):
        return False
    
    # Розділяємо на слова та перевіряємо що є мінімум 2 слова (ім'я та прізвище)
    words = text.strip().split()
    if len(words) < 2:
        return False
    
    # Перевіряємо що кожне слово має мінімум 2 символи
    for word in words:
        if len(word) < 2:
            return False
    
    return True

def display_ranked_name(rank: str | None, name: str) -> str:
    """Повертає відформатоване ім'я з опціональним званням."""
    return f"{rank} {name}".strip() if rank else name

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

# ===== Тимчасова команда для повторного заповнення профілю =====
async def refill_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Старт тимчасового майстра перезаповнення профілю для вже зареєстрованих."""
    user = update.effective_user
    profile = get_profile(user.id)
    if not profile:
        await update.message.reply_text(
            "ℹ️ Профіль ще не створено. Спочатку скористайтесь /start для первинної заявки.")
        return ConversationHandler.END

    context.user_data["refill_form"] = {}
    existing = profile.get("in_game_name") or ""
    hint = f" Поточне: {existing}" if existing else ""
    await update.message.reply_text(
        "🛠️ <b>Оновлення профілю (тимчасово)</b>\n\n"
        "🔸 Крок 1 з 4: Ім'я у грі\n\n"
        "Введіть <i>ім'я та прізвище українською</i> (повністю)." + hint,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    return REFILL_NAME

async def refill_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name_input = update.message.text.strip()
    if not is_ukrainian_name(name_input):
        await update.message.reply_text(
            "❌ Ім'я та прізвище мають бути українською мовою!\n\n"
            "Приклади: \n"
            "✅ Олександр Іваненко\n"
            "✅ Марія Петренко-Коваленко\n\n"
            "Спробуйте ще раз:")
        return REFILL_NAME
    context.user_data.setdefault("refill_form", {})["in_game_name"] = name_input

    # Крок 2: вибір підрозділу
    keyboard = []
    for code, meta in NPU_DEPARTMENTS.items():
        keyboard.append([InlineKeyboardButton(meta["title"], callback_data=f"refill_npu_{code}")])
    await update.message.reply_text(
        "🔸 Крок 2 з 4: Підрозділ НПУ\n\nОберіть ваш підрозділ:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REFILL_NPU

async def refill_select_npu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_", 2)
    # data формат: refill_npu_<code>
    if len(parts) < 3:
        await query.edit_message_text("❌ Невірні дані вибору підрозділу.")
        return ConversationHandler.END
    npu_code = parts[2]
    if npu_code not in NPU_DEPARTMENTS:
        await query.edit_message_text("❌ Невідомий підрозділ.")
        return ConversationHandler.END

    meta = NPU_DEPARTMENTS[npu_code]
    form = context.user_data.setdefault("refill_form", {})
    form["npu_department"] = meta["title"]
    form["npu_code"] = npu_code

    # Показати картку та вибір звання
    rank_buttons = []
    row = []
    for idx, rank in enumerate(NPU_RANKS):
        row.append(InlineKeyboardButton(rank, callback_data=f"refill_rank_{idx}"))
        if len(row) == 2:
            rank_buttons.append(row)
            row = []
    if row:
        rank_buttons.append(row)

    desc = (
        f"✅ Обрано підрозділ: <b>{meta['title']}</b> {meta['tag']}\n"
        f"Місце: {meta['location']}\n"
        f"Допуск: {meta['eligibility']}\n\n"
        f"{meta['desc']}\n\n"
        "🔸 Крок 3 з 4: Оберіть ваше звання"
    )
    await query.edit_message_text(desc, reply_markup=InlineKeyboardMarkup(rank_buttons), parse_mode="HTML")
    return REFILL_RANK

async def refill_select_rank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.split("_")[-1])
    except Exception:
        await query.edit_message_text("❌ Невірні дані вибору звання.")
        return ConversationHandler.END
    if not (0 <= idx < len(NPU_RANKS)):
        await query.edit_message_text("❌ Невідоме звання.")
        return ConversationHandler.END

    rank = NPU_RANKS[idx]
    context.user_data.setdefault("refill_form", {})["rank"] = rank
    await query.edit_message_text(
        f"✅ Звання обрано: {rank}\n\n"
        "🔸 Крок 4 з 4: Надішліть 2 посилання на скріншоти (посвідчення та трудову книжку).\n\n"
        "Кожен URL з нового рядка. Підтримуються imgbb/imgur/postimg та ін.")
    return REFILL_IMAGES

async def refill_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    urls = [u.strip() for u in text.splitlines() if u.strip()]
    if len(urls) < 2:
        await update.message.reply_text(
            "❌ Потрібно мінімум 2 посилання на зображення. Надішліть ще раз.")
        return REFILL_IMAGES
    valid, invalid = validate_image_urls(urls)
    if invalid or len(valid) < 2:
        bad = "\n".join(f"• {u}" for u in invalid) if invalid else ""
        await update.message.reply_text(
            ("❌ Деякі посилання некоректні:\n" + bad + "\n\n" if bad else "") +
            "Надішліть 2+ валідних URL (imgbb/imgur/postimg).")
        return REFILL_IMAGES

    form = context.user_data.get("refill_form", {})
    user = update.effective_user

    # Оновлюємо профіль та зображення в БД
    try:
        update_profile_fields(
            user.id,
            in_game_name=form.get("in_game_name"),
            npu_department=form.get("npu_department"),
            rank=form.get("rank"),
        )
        replace_profile_images(user.id, valid)
    except Exception as e:
        logger.error(f"refill save failed: {e}")
        await update.message.reply_text("⚠️ Сталася помилка при збереженні. Спробуйте ще раз пізніше.")
        return ConversationHandler.END

    # Підсумок
    summary = (
        "✅ <b>Профіль оновлено</b>\n\n"
        "<blockquote>"
        f"Ім'я у грі: {form.get('in_game_name')}\n"
        f"Підрозділ: {form.get('npu_department')}\n"
        f"Звання: {form.get('rank')}\n"
        f"Фото: {len(valid)} посилання"
        "</blockquote>\n\n"
        "Дякуємо! Ця команда є <i>тимчасовою</i> і буде видалена після міграції."
    )
    await update.message.reply_text(summary, parse_mode="HTML", disable_web_page_preview=True)

    # Очистка стану
    context.user_data.pop("refill_form", None)
    return ConversationHandler.END

async def create_invite_link(context: ContextTypes.DEFAULT_TYPE, user_name: str) -> str:
    """Створює одноразове посилання-запрошення для користувача"""
    try:
        # Якщо є ID групи, створюємо одноразове посилання
        if GROUP_CHAT_ID:
            # Створюємо запрошення, що діє 24 години та може використати тільки 1 людина
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=GROUP_CHAT_ID,
                name=f"Запрошення для {user_name}",
                expire_date=None,  # Без обмеження по часу, але з лімітом використань
                member_limit=1,  # Тільки одна людина може використати
                creates_join_request=False  # Прямий вступ без запиту
            )
            logger.info(f"Створено одноразове посилання для {user_name}: {invite_link.invite_link}")
            return invite_link.invite_link
        else:
            # Якщо немає ID групи, використовуємо основне посилання
            logger.warning("GROUP_CHAT_ID не налаштовано, використовуємо основне посилання")
            return GROUP_INVITE_LINK
    except Exception as e:
        logger.error(f"Помилка при створенні посилання-запрошення: {e}")
        # В разі помилки використовуємо основне посилання
        return GROUP_INVITE_LINK

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник команди /start: різна поведінка для членів групи та тих, хто ще не в групі"""
    user = update.effective_user

    # Оновлюємо профіль користувача в БД (TG дані)
    tg_fullname = f"{user.first_name or ''} {user.last_name or ''}".strip()
    upsert_profile(
        telegram_id=user.id,
        username=user.username or None,
        full_name_tg=tg_fullname or None,
        role='admin' if user.id in ADMIN_IDS else 'user',
    )

    # Перевірка членства у групі
    user_is_member = False
    if REPORTS_CHAT_ID:
        try:
            member = await context.bot.get_chat_member(REPORTS_CHAT_ID, user.id)
            user_is_member = member.status in {"member", "administrator", "creator"}
        except Exception as e:
            logger.warning(f"Не вдалося перевірити членство користувача {user.id}: {e}")

    if user_is_member:
        # Показуємо меню взаємодії (кнопки під полем вводу)
        is_admin = user.id in ADMIN_IDS
        keyboard_rows = [["📝 Заява на неактив"]]
        if is_admin:
            keyboard_rows.append(["📝 Оформити догану"])
        reply_kb = ReplyKeyboardMarkup(keyboard_rows, resize_keyboard=True)

        text = (
            f"<b>Вітаю, {user.first_name}!</b> 👋\n\n"
            "<i>Я готовий до роботи з вами у групі. Оберіть дію нижче:</i>"
        )
        await update.message.reply_text(text, reply_markup=reply_kb, parse_mode="HTML")
    else:
        # Користувач ще не в групі — стара логіка отримання доступу
        keyboard = [
            [InlineKeyboardButton("📝 Подати заявку на доступ", callback_data="request_access")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        welcome_message = (
            f"<b>Вітаю, {user.first_name}!</b> 👋\n\n"
            "Це бот для отримання доступу до групи <b>поліції UKRAINE GTA</b>.\n\n"
            "<i>Щоб отримати доступ до групи, натисніть кнопку нижче та заповніть заявку.</i>"
        )
        await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode="HTML")

############################
# ДОГАН (адміністраторам)
############################

# Стани для діалогу 'догана'
DOGANA_OFFENSE, DOGANA_DATE, DOGANA_TO, DOGANA_BY, DOGANA_PUNISH = range(5)

async def dogana_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає доступу до цієї дії.")
        return ConversationHandler.END
    context.user_data["dogana_form"] = {}
    await update.message.reply_text(
        "📝 <b>ОФОРМЛЕННЯ ДОГАНИ</b>\n\n"
        "🔸 <b>Крок 1 з 5:</b> Опис порушення\n\n"
        "<i>Введіть, будь ласка, детальний опис порушення:</i>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )
    return DOGANA_OFFENSE

async def dogana_offense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dogana_form"]["offense"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 <b>ОФОРМЛЕННЯ ДОГАНИ</b>\n\n"
        "🔸 <b>Крок 2 з 5:</b> Дата порушення\n\n"
        "<i>Вкажіть дату у форматі</i> <code>ДД.ММ.РРРР</code> <i>або</i> <code>ДД.ММ</code>:\n"
        "Приклад: <code>01.10.2025</code> або <code>01.10</code>",
        parse_mode="HTML"
    )
    return DOGANA_DATE

async def dogana_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_text = update.message.text.strip()
    
    # Перевірка формату дати (цифри та точки)
    if not re.match(r'^\d{1,2}\.\d{1,2}(\.\d{4})?$', date_text):
        await update.message.reply_text(
            "❌ <b>Невірний формат дати!</b>\n\n"
            "<i>Використовуйте формат:</i>\n"
            "• <code>ДД.ММ.РРРР</code> (наприклад: <code>01.10.2025</code>)\n"
            "• <code>ДД.ММ</code> (наприклад: <code>01.10</code>)\n\n"
            "Спробуйте ще раз:",
            parse_mode="HTML"
        )
        return DOGANA_DATE
    
    context.user_data["dogana_form"]["date"] = date_text
    await update.message.reply_text(
        "📝 <b>ОФОРМЛЕННЯ ДОГАНИ</b>\n\n"
        "🔸 <b>Крок 3 з 5:</b> Порушник\n\n"
        "Введіть ім'я та прізвище особи, якій видається догана:\n"
        "<i>(українською мовою, повне ім'я та прізвище)</i>",
        parse_mode="HTML"
    )
    return DOGANA_TO

async def dogana_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    rank, name_text = parse_ranked_name(raw)
    
    # Перевірка українських символів та формату імені
    if not re.match(r'^[А-ЯІЇЄа-яіїє\'\-\s\.]+$', name_text):
        await update.message.reply_text(
            "❌ <b>Ім'я та прізвище мають бути українською мовою!</b>\n\n"
            "<i>Приклади правильного формату:</i>\n"
            "✅ Олександр Іваненко\n"
            "✅ Марія Петренко-Коваленко\n"
            "✅ Анна-Марія Сидоренко\n\n"
            "Спробуйте ще раз:",
            parse_mode="HTML"
        )
        return DOGANA_TO
    
    # Перевірка що є мінімум 2 слова
    words = name_text.split()
    if len(words) < 2:
        await update.message.reply_text(
            "❌ <b>Потрібно вказати ім'я та прізвище!</b>\n\n"
            "Приклад: <code>Олександр Іваненко</code>\n\n"
            "Спробуйте ще раз:",
            parse_mode="HTML"
        )
        return DOGANA_TO
    
    context.user_data["dogana_form"]["to_whom"] = name_text
    context.user_data["dogana_form"]["rank_to"] = rank
    # Пропонуємо автозаповнення хто видав
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()
    await update.message.reply_text(
        "📝 <b>ОФОРМЛЕННЯ ДОГАНИ</b>\n\n"
        "🔸 <b>Крок 4 з 5:</b> Хто видав\n\n"
        f"За замовчуванням: <code>{admin_name}</code>\n\n"
        "<i>Введіть ім'я та прізвище особи, яка видає догану, або залиште як є:</i>",
        parse_mode="HTML"
    )
    context.user_data["dogana_form"]["default_by"] = admin_name
    return DOGANA_BY

async def dogana_by(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    by_whom = text if text and text.lower() != "за замовчуванням" else context.user_data["dogana_form"].get("default_by")
    context.user_data["dogana_form"]["by_whom"] = by_whom

    # Вибір покарання через inline кнопки
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Догана", callback_data="dogana_punish_dogana"),
            InlineKeyboardButton("Попередження", callback_data="dogana_punish_poperedzhennya"),
        ]
    ])
    await update.message.reply_text(
        "📝 <b>ОФОРМЛЕННЯ ДОГАНИ</b>\n\n"
        "🔸 <b>Крок 5 з 5:</b> Вид покарання\n\n"
        "Оберіть вид покарання:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    return DOGANA_PUNISH

async def dogana_punish_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kind = "Догана" if query.data.endswith("dogana") else "Попередження"
    form = context.user_data.get("dogana_form", {})

    text = (
        "⚠️ <b>ДОГАНА</b>\n\n"
        "<blockquote>"
        f"1. Порушення: {form.get('offense')}\n"
        f"2. Дата порушення: {form.get('date')}\n"
        f"3. Кому видано: {display_ranked_name(form.get('rank_to'), form.get('to_whom'))}\n"
        f"4. Хто видав: {form.get('by_whom')}\n"
        f"5. Покарання: {kind}\n\n"
        f"Від: @{query.from_user.username if query.from_user.username else query.from_user.first_name}"
        "</blockquote>"
    )
    try:
        # Логування в БД
        try:
            insert_warning(
                offense=form.get('offense') or '',
                date_text=form.get('date') or '',
                to_whom=form.get('to_whom') or '',
                rank_to=form.get('rank_to'),
                by_whom=form.get('by_whom') or '',
                kind=kind,
                issued_by_user_id=query.from_user.id if query and query.from_user else None,
                issued_by_username=(query.from_user.username if query and query.from_user else None),
            )
        except Exception as dbe:
            logger.error(f"DB log warning failed: {dbe}")

        await context.bot.send_message(
            chat_id=REPORTS_CHAT_ID,
            text=text,
            message_thread_id=WARNINGS_TOPIC_ID,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await query.edit_message_text("✅ Догану оформлено та відправлено у тему.")
    except Exception as e:
        logger.error(f"Помилка відправки догани: {e}")
        await query.edit_message_text("⚠️ Не вдалося відправити у тему. Перевірте права бота та ID теми.")
    finally:
        context.user_data.pop("dogana_form", None)
    return ConversationHandler.END

async def dogana_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("dogana_form", None)
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

############################
# ЗАЯВИ НА НЕАКТИВ (усі користувачі)
############################

NEAKTYV_TO, NEAKTYV_TIME, NEAKTYV_DEPARTMENT = range(3)

# Стани заявки на доступ
APP_WAITING_NAME, APP_WAITING_NPU, APP_WAITING_RANK, APP_WAITING_IMAGES = range(4)

# Константи для модерації заяв
NEAKTYV_APPROVAL_NAME = range(1)

async def neaktyv_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["neaktyv_form"] = {}
    await update.message.reply_text(
        "📝 ПОДАЧА ЗАЯВИ НА НЕАКТИВ\n\n"
        "🔸 Крок 1 з 3: Отримувач\n\n"
        "Введіть ім'я та прізвище особи, якій надається неактив:\n"
        "(Українською мовою, повне ім'я та прізвище)",
        reply_markup=ReplyKeyboardRemove()
    )
    return NEAKTYV_TO

async def neaktyv_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    rank, name = parse_ranked_name(raw)
    
    # Валідація українського імені
    if not re.match(r'^[А-ЯҐІЇЄЁ][а-яґіїєё\']*\s+[А-ЯҐІЇЄЁ][а-яґіїєё\']*$', name):
        await update.message.reply_text(
            "❌ Помилка введення!\n\n"
            "Ім'я та прізвище повинні:\n"
            "• Бути українською мовою\n"
            "• Починатися з великих літер\n"
            "• Містити лише літери українського алфавіту\n\n"
            "Приклади правильного введення (зі званням або без нього):\n"
            "✅ Рядовий Іван Петренко\n"
            "✅ Капітан Марія Коваленко\n"
            "✅ Олексій Петренко\n\n"
            "Спробуйте ще раз:"
        )
        return NEAKTYV_TO
    
    context.user_data["neaktyv_form"]["to_whom"] = name
    context.user_data["neaktyv_form"]["rank"] = rank
    # Якщо це сам користувач — оновимо звання у профілі
    if update.effective_user and update.effective_user.id:
        # Не завжди доречно, але якщо користувач вказав звання про себе — збережемо
        if rank:
            update_profile_fields(update.effective_user.id, rank=rank)
    await update.message.reply_text(
        "🔸 Крок 2 з 3: Термін неактиву\n\n"
        "Введіть термін неактиву:\n"
        "(Наприклад: 2 тижні, 1 місяць, 3 дні)"
    )
    return NEAKTYV_TIME

async def neaktyv_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["neaktyv_form"]["duration"] = update.message.text.strip()
    await update.message.reply_text(
        "🔸 Крок 3 з 3: Відділ\n\n"
        "Оберіть відділ НПУ:",
        reply_markup=ReplyKeyboardMarkup(
            [[dept] for dept in NPU_DEPARTMENTS.keys()],
            one_time_keyboard=True,
            resize_keyboard=True
        )
    )
    return NEAKTYV_DEPARTMENT

async def neaktyv_dept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["neaktyv_form"]["department"] = update.message.text.strip()
    form = context.user_data.get("neaktyv_form", {})
    
    # Формування повідомлення для адміністраторів
    username = update.message.from_user.username
    display_name = update.message.from_user.first_name
    author = f"@{username}" if username else display_name
    user_id = update.message.from_user.id
    
    admin_message = (
        "📋 НОВА ЗАЯВА НА НЕАКТИВ\n\n"
        "<blockquote>"
        f"1. Кому надається: {display_ranked_name(form.get('rank'), form.get('to_whom'))}\n"
        f"2. На скільки (час): {form.get('duration')}\n"
        f"3. Підрозділ: {form.get('department')}\n\n"
        f"Від: {author}\n"
        f"ID заявника: {user_id}"
        "</blockquote>"
    )
    
    # Клавіатура для модерації
    keyboard = [
        [
            InlineKeyboardButton("✅ Одобрити", callback_data=f"approve_neaktyv_{user_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_neaktyv_{user_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Зберігаємо дані заяви для подальшого використання
    context.bot_data[f"neaktyv_form_{user_id}"] = form.copy()
    context.bot_data[f"neaktyv_form_{user_id}"]["author"] = author
    
    # Зберігаємо заявку в БД
    try:
        request_id = insert_neaktyv_request(
            requester_id=user_id,
            requester_username=username,
            to_whom=form.get('to_whom') or '',
            rank=form.get('rank'),
            duration=form.get('duration') or '',
            department=form.get('department') or '',
        )
        # збережемо id заявки, щоб модераторське рішення оновило саме її
        context.bot_data[f"neaktyv_req_id_{user_id}"] = request_id
    except Exception as dbe:
        logger.error(f"DB insert neaktyv failed: {dbe}")

    # Відправляємо адміністраторам
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_message,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не вдалося відправити повідомлення адміністратору {admin_id}: {e}")
    
    await update.message.reply_text(
        "✅ Заяву на неактив відправлено адміністраторам для розгляду.",
        reply_markup=ReplyKeyboardRemove()
    )
    
    context.user_data.pop("neaktyv_form", None)
    return ConversationHandler.END

async def neaktyv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("neaktyv_form", None)
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

############################
# МОДЕРАЦІЯ ЗАЯВ НА НЕАКТИВ
############################

async def handle_neaktyv_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка натискання кнопок модерації заяв на неактив"""
    query = update.callback_query
    await query.answer()
    
    # Перевірка прав адміністратора
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Ця функція доступна лише адміністраторам.")
        return ConversationHandler.END
    
    # Парсинг callback_data
    if query.data.startswith("approve_neaktyv_"):
        action = "approve"
        user_id = int(query.data.split("_")[2])
    elif query.data.startswith("reject_neaktyv_"):
        action = "reject"
        user_id = int(query.data.split("_")[2])
    else:
        return ConversationHandler.END
    
    # Зберігаємо дані для обробки
    context.user_data["moderation_action"] = action
    context.user_data["moderation_user_id"] = user_id
    context.user_data["original_message_id"] = query.message.message_id
    
    # Запитуємо ім'я модератора
    action_text = "одобрення" if action == "approve" else "відхилення"
    await query.edit_message_text(
        f"📝 {action_text.capitalize()} заяви\n\n"
        f"Введіть ваше ім'я та прізвище для підтвердження {action_text}:\n"
        "(Українською мовою, повне ім'я та прізвище)"
    )
    
    return NEAKTYV_APPROVAL_NAME

async def process_neaktyv_approval_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка введення імені модератора"""
    name = update.message.text.strip()
    
    # Валідація українського імені
    if not re.match(r'^[А-ЯҐІЇЄЁ][а-яґіїєё\']*\s+[А-ЯҐІЇЄЁ][а-яґіїєё\']*$', name):
        await update.message.reply_text(
            "❌ Помилка введення!\n\n"
            "Ім'я та прізвище повинні:\n"
            "• Бути українською мовою\n"
            "• Починатися з великих літер\n"
            "• Містити лише літери українського алфавіту\n\n"
            "Приклади правильного введення:\n"
            "✅ Олексій Петренко\n"
            "✅ Марія Коваленко\n"
            "✅ Дмитро О'Коннор\n\n"
            "Спробуйте ще раз:"
        )
        return NEAKTYV_APPROVAL_NAME
    
    action = context.user_data.get("moderation_action")
    user_id = context.user_data.get("moderation_user_id")
    original_message_id = context.user_data.get("original_message_id")
    
    # Отримуємо збережені дані заяви
    form_key = f"neaktyv_form_{user_id}"
    form = context.bot_data.get(form_key)
    
    if not form:
        await update.message.reply_text("❌ Дані заяви не знайдено. Можливо, вона вже була оброблена.")
        return ConversationHandler.END
    
    if action == "approve":
        # Одобрення - редагуємо повідомлення адміністратора та публікуємо в групу
        # Отримуємо можливе звання для красивого відображення
        disp_name = display_ranked_name(form.get('rank'), form.get('to_whom'))
        admin_edit_message = (
            "✅ ЗАЯВА ОДОБРЕНА\n\n"
            "<blockquote>"
            f"1. Кому надається: {disp_name}\n"
            f"2. На скільки (час): {form.get('duration')}\n"
            f"3. Підрозділ: {form.get('department')}\n\n"
            f"Від: {form.get('author')}\n"
            f"Модератор: {name}"
            "</blockquote>"
        )
        
        group_message = (
            "🟦 ЗАЯВА НА НЕАКТИВ\n\n"
            "<blockquote>"
            f"1. Кому надається: {disp_name}\n"
            f"2. На скільки (час): {form.get('duration')}\n"
            f"3. Підрозділ: {form.get('department')}\n\n"
            f"Від: {form.get('author')}\n"
            f"Перевіряючий: {name}"
            "</blockquote>"
        )
        
        try:
            # Редагуємо оригінальне повідомлення адміністратора
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=original_message_id,
                text=admin_edit_message,
                parse_mode="HTML"
            )
            
            # Публікуємо в групу
            await context.bot.send_message(
                chat_id=REPORTS_CHAT_ID,
                text=group_message,
                message_thread_id=AFK_TOPIC_ID,
                parse_mode="HTML"
            )
            # Лог рішення в БД
            try:
                req_id = context.bot_data.get(f"neaktyv_req_id_{user_id}")
                if req_id:
                    decide_neaktyv_request(
                        request_id=req_id,
                        status='approved',
                        moderator_name=name,
                        moderator_user_id=update.effective_user.id,
                    )
            except Exception as dbe:
                logger.error(f"DB decide neaktyv approve failed: {dbe}")
            await update.message.reply_text(f"✅ Заяву одобрено та опубліковано в групі!")
        except Exception as e:
            logger.error(f"Помилка при обробці заяви: {e}")
            await update.message.reply_text("❌ Помилка при обробці заяви.")
    else:
        # Відхилення - редагуємо повідомлення адміністратора
        disp_name = display_ranked_name(form.get('rank'), form.get('to_whom'))
        admin_edit_message = (
            "❌ ЗАЯВА ВІДХИЛЕНА\n\n"
            "<blockquote>"
            f"1. Кому надається: {disp_name}\n"
            f"2. На скільки (час): {form.get('duration')}\n"
            f"3. Підрозділ: {form.get('department')}\n\n"
            f"Від: {form.get('author')}\n"
            f"Модератор: {name}"
            "</blockquote>"
        )
        
        try:
            # Редагуємо оригінальне повідомлення адміністратора
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=original_message_id,
                text=admin_edit_message,
                parse_mode="HTML"
            )
            # Лог рішення в БД
            try:
                req_id = context.bot_data.get(f"neaktyv_req_id_{user_id}")
                if req_id:
                    decide_neaktyv_request(
                        request_id=req_id,
                        status='rejected',
                        moderator_name=name,
                        moderator_user_id=update.effective_user.id,
                    )
            except Exception as dbe:
                logger.error(f"DB decide neaktyv reject failed: {dbe}")
            await update.message.reply_text(f"❌ Заяву відхилено.")
        except Exception as e:
            logger.error(f"Помилка при редагуванні повідомлення: {e}")
            await update.message.reply_text("❌ Помилка при обробці відхилення.")
    
    # Очищуємо збережені дані
    context.bot_data.pop(form_key, None)
    context.user_data.pop("moderation_action", None)
    context.user_data.pop("moderation_user_id", None)
    context.user_data.pop("original_message_id", None)
    
    return ConversationHandler.END

async def cancel_neaktyv_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Скасування модерації"""
    context.user_data.pop("moderation_action", None)
    context.user_data.pop("moderation_user_id", None)
    context.user_data.pop("original_message_id", None)
    await update.message.reply_text("❌ Модерацію скасовано.")
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник натискань на кнопки"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "request_access":
        await query.edit_message_text(
            "📝 Крок 1: Введіть ваше ім'я та прізвище\n\n"
            "<blockquote>⚠️ ВАЖЛИВО:\n"
            "• Тільки українською мовою\n"
            "• Повне ім'я та прізвище\n"
            "• Без скорочень та абревіатур\n\n"
            "Приклад: Іван Петренко</blockquote>",
            parse_mode="HTML"
        )
        context.user_data['awaiting_application'] = True
        context.user_data['step'] = 'waiting_name'
    
    elif query.data.startswith("npu_"):
        npu_code = query.data.split("_")[1]
        await select_npu_department(update, context, npu_code)
    elif query.data.startswith("rank_"):
        # Выбор ранга в анкете доступа
        rank_idx = int(query.data.split("_")[1])
        if 0 <= rank_idx < len(NPU_RANKS):
            rank = NPU_RANKS[rank_idx]
            user_id = update.effective_user.id
            if user_id in USER_APPLICATIONS:
                USER_APPLICATIONS[user_id]['rank'] = rank
                update_profile_fields(user_id, rank=rank)
                await query.edit_message_text(
                    f"✅ Звання обрано: {rank}\n\n"
                    "📝 Крок 3: Надішліть посилання на скріншоти (2 шт)\n\n"
                    "Потрібні: посвідчення та трудова книжка. Розмістіть на imgbb/imgur/postimg та надішліть прямі URL, кожен з нового рядка."
                )
                context.user_data['step'] = 'waiting_image_urls'
                USER_APPLICATIONS[user_id]['step'] = 'waiting_image_urls'
    
    elif query.data.startswith("approve_"):
        user_id = int(query.data.split("_")[1])
        await approve_request(update, context, user_id)
    
    elif query.data.startswith("reject_"):
        user_id = int(query.data.split("_")[1])
        await reject_request(update, context, user_id)

async def handle_application_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Універсальний обробник текстових повідомлень для заявок"""
    user = update.effective_user
    user_id = user.id
    
    # Якщо користувач не в процесі подачі заявки
    if not context.user_data.get('awaiting_application'):
        return
    
    step = context.user_data.get('step', 'waiting_name')
    
    if step == 'waiting_name':
        await handle_name_input(update, context)
    elif step == 'waiting_image_urls':
        await handle_image_urls_application(update, context)

async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник введення імені та прізвища"""
    user = update.effective_user
    user_id = user.id
    name_input = update.message.text.strip()
    
    # Перевіряємо чи ім'я українською
    if not is_ukrainian_name(name_input):
        await update.message.reply_text(
            "❌ Ім'я та прізвище мають бути українською мовою!\n\n"
            "Приклади правильного формату:\n"
            "✅ Олександр Іваненко\n"
            "✅ Марія Петренко-Коваленко\n"
            "✅ Анна-Марія Сидоренко\n\n"
            "❌ НЕправильно:\n"
            "• Alexander Ivanov (англійською)\n"
            "• Олександр І. (скорочення)\n"
            "• Саша (неповне ім'я)\n\n"
            "Спробуйте ще раз:"
        )
        return
    
    # Зберігаємо ім'я та показуємо вибір НПУ
    if user_id not in USER_APPLICATIONS:
        USER_APPLICATIONS[user_id] = {
            'user': user,
            'name': None,
            'npu_department': None,
            'image_urls': [],
            'step': 'waiting_name'
        }
    
    USER_APPLICATIONS[user_id]['name'] = name_input
    # Зберігаємо ім'я у грі в профіль
    update_profile_fields(user_id, in_game_name=name_input)
    context.user_data['step'] = 'waiting_npu' # FIX: Update user_data context
    
    # Створюємо кнопки для вибору НПУ
    keyboard = []
    for code, meta in NPU_DEPARTMENTS.items():
        keyboard.append([InlineKeyboardButton(meta["title"], callback_data=f"npu_{code}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ Ім'я прийнято: {name_input}\n\n"
        "📝 Крок 2: Оберіть ваше управління НПУ\n\n"
        "⚠️ Доступні тільки ці управління для UKRAINE GTA:",
        reply_markup=reply_markup
    )

async def select_npu_department(update: Update, context: ContextTypes.DEFAULT_TYPE, npu_code: str) -> None:
    """Обробник вибору управління НПУ"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_id = user.id
    
    if user_id not in USER_APPLICATIONS:
        await query.edit_message_text("❌ Помилка: дані заявки не знайдено. Почніть спочатку з /start")
        return
    
    if npu_code not in NPU_DEPARTMENTS:
        await query.edit_message_text("❌ Помилка: невідоме управління НПУ")
        return
    
    # Зберігаємо вибір НПУ
    USER_APPLICATIONS[user_id]['npu_department'] = NPU_DEPARTMENTS[npu_code]["title"]
    # Оновлюємо підрозділ у профілі
    update_profile_fields(user_id, npu_department=NPU_DEPARTMENTS[npu_code]["title"])
    USER_APPLICATIONS[user_id]['step'] = 'waiting_rank'
    context.user_data['step'] = 'waiting_rank'

    # Показать выбор звания
    rank_buttons = []
    row = []
    for idx, rank in enumerate(NPU_RANKS):
        row.append(InlineKeyboardButton(rank, callback_data=f"rank_{idx}"))
        if len(row) == 2:
            rank_buttons.append(row)
            row = []
    if row:
        rank_buttons.append(row)
    meta = NPU_DEPARTMENTS[npu_code]
    desc = (
        f"✅ Обрано підрозділ: <b>{meta['title']}</b> {meta['tag']}\n"
        f"Місце: {meta['location']}\n"
        f"Допуск: {meta['eligibility']}\n\n"
        f"{meta['desc']}\n\n"
        "📝 Крок 3: Оберіть ваше звання"
    )
    await query.edit_message_text(desc, reply_markup=InlineKeyboardMarkup(rank_buttons), parse_mode="HTML")

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
    # Сохраняем изображения в БД
    replace_profile_images(user_id, valid_urls)
    
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
        'name': user_data['name'],
        'npu_department': user_data['npu_department'],
        'image_urls': user_data['image_urls']
    }

    # Лог заявки на доступ у БД
    try:
        insert_access_application(
            user_id=user.id,
            username=user.username,
            in_game_name=user_data['name'],
            npu_department=user_data['npu_department'],
            rank=USER_APPLICATIONS[user_id].get('rank'),
            images=user_data['image_urls'],
        )
    except Exception as dbe:
        logger.error(f"DB insert access_application failed: {dbe}")

    # Відправляємо підтвердження користувачу
    await update.message.reply_text(
        "✅ Вашу заявку повністю отримано!\n\n"
        f"👤 Ім'я: {user_data['name']}\n"
        f"🏛️ НПУ: {user_data['npu_department']}\n"
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
        "🆕 Нова заявка на доступ!\n\n"
        f"👤 Користувач: {user.first_name} {user.last_name or ''}\n"
        f"🆔 ID: {user.id}\n"
        f"📱 Нікнейм: @{user.username or 'немає'}\n\n"
        f"📝 Заявка:\n"
        f"👤 Ім'я: {user_data['name']}\n"
        f"🏛️ НПУ: {user_data['npu_department']}\n\n"
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
    
    # Створюємо індивідуальне одноразове посилання
    try:
        user_display_name = f"{user.first_name} {user.last_name or ''}".strip()
        if user.username:
            user_display_name += f" (@{user.username})"
        
        invite_link = await create_invite_link(context, user_display_name)
        
        # Відправляємо персональне посилання користувачу
        invite_message = (
            "🎉 Вітаємо!\n\n"
            "Вашу заявку схвалено! Ви отримали персональне запрошення до групи поліції UKRAINE GTA.\n\n"
            "<blockquote>🔗 Ваше особисте посилання:\n"
            f"{invite_link}</blockquote>\n\n"
            "<blockquote>⚠️ ВАЖЛИВО:\n"
            "• Це посилання створено спеціально для вас\n"
            "• Воно одноразове - може використати тільки одна людина\n"
            "• Не передавайте його іншим\n"
            "• Після вступу посилання стане недійсним</blockquote>"
        )
        
        await context.bot.send_message(
            chat_id=user_id,
            text=invite_message,
            disable_web_page_preview=True,
            parse_mode="HTML"
        )
        
    # (Оновлення інтерфейсу командою /start прибрано за вимогою)
        
        # Повідомляємо адміністратору про успіх
        await query.edit_message_text(
            f"✅ Заявку користувача {user.first_name} ({user.id}) схвалено!\n\n"
            f"👤 Користувач: {user_display_name}\n"
            f"🔗 Створено персональне посилання: {invite_link[:50]}...\n"
            f"📊 Ліміт використань: 1 раз\n\n"
            f"Користувачу надіслано персональне запрошення."
        )
        # Лог рішення по заявці у БД
        try:
            decide_access_application(
                user_id=user.id,
                decision='approved',
                decided_by_admin_id=update.effective_user.id,
                decided_by_username=update.effective_user.username,
                invite_link=invite_link,
            )
        except Exception as dbe:
            logger.error(f"DB decide access approve failed: {dbe}")
        
        logger.info(f"Заявку користувача {user_display_name} ({user.id}) схвалено, створено посилання: {invite_link}")
        
    except Exception as e:
        logger.error(f"Помилка при схваленні заявки користувача {user.id}: {e}")
        await query.edit_message_text(
            f"⚠️ Заявку схвалено, але виникла помилка при створенні персонального посилання.\n"
            f"Помилка: {str(e)}\n\n"
            f"Користувачу надіслано основне посилання на групу."
        )
        
        # Відправляємо основне посилання як резерв
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🎉 Вітаємо!\n\n"
                    "Вашу заявку схвалено! Ви можете приєднатися до групи за основним посиланням:\n\n"
                    f"<blockquote>🔗 {GROUP_INVITE_LINK}</blockquote>"
                ),
                disable_web_page_preview=True,
                parse_mode="HTML"
            )
        except Exception as e2:
            logger.error(f"Не вдалося відправити навіть основне посилання користувачу {user.id}: {e2}")
        # Лог рішення по заявці у БД (approve без персонального інвайту)
        try:
            decide_access_application(
                user_id=user.id,
                decision='approved',
                decided_by_admin_id=update.effective_user.id,
                decided_by_username=update.effective_user.username,
                invite_link=GROUP_INVITE_LINK,
            )
        except Exception as dbe:
            logger.error(f"DB decide access approve(fallback) failed: {dbe}")
    
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
    # Лог рішення по заявці у БД
    try:
        decide_access_application(
            user_id=user.id,
            decision='rejected',
            decided_by_admin_id=update.effective_user.id,
            decided_by_username=update.effective_user.username,
            invite_link=None,
        )
    except Exception as dbe:
        logger.error(f"DB decide access reject failed: {dbe}")
    
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

async def broadcast_fill_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для адмінів: попросити заповнити профілі (інструкція)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return
    text = (
        "📣 <b>Шановні учасники!</b>\n\n"
        "Для оновлення бази, просимо кожного заповнити дані через бота:\n\n"
        "1) Відкрийте діалог з ботом та натисніть /start\n"
        "2) Пройдіть анкету доступу: введіть ім'я та прізвище, оберіть управління НПУ, оберіть <b>своє звання</b>\n"
    "3) Надішліть 2 посилання на скріншоти (посвідчення та трудову книжку) з imgbb/imgur/postimg (прямі URL)\n\n"
        "Дякуємо за оперативність!"
    )
    await update.message.reply_text(text, parse_mode="HTML")

def _format_profile(profile: dict) -> str:
    return (
        "👤 <b>Профіль</b>\n\n"
        f"🆔 Telegram ID: <code>{profile['telegram_id']}</code>\n"
        f"📱 Username: @{profile['username'] or 'немає'}\n"
        f"Ім'я в Telegram: {profile['full_name_tg'] or '—'}\n"
        f"Ім'я у грі: {profile['in_game_name'] or '—'}\n"
        f"Звання: {profile['rank'] or '—'}\n"
        f"Підрозділ: {profile['npu_department'] or '—'}\n"
        f"Роль: {profile['role'] or 'user'}\n"
        f"Оновлено: {profile['updated_at'] or '—'}\n"
    )

async def user_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/user <id|@username> — показать профиль (только админам)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    if get_profile_by_username is None:
        await update.message.reply_text("⚠️ Функція пошуку за username тимчасово недоступна на цьому деплої.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /user <telegram_id | @username>")
        return
    arg = context.args[0]
    profile = None
    if arg.isdigit():
        profile = get_profile(int(arg))
    else:
        profile = get_profile_by_username(arg)
    if not profile:
        await update.message.reply_text("Не знайдено профіль.")
        return
    await update.message.reply_text(_format_profile(profile), parse_mode="HTML", disable_web_page_preview=True)

async def find_profiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/find <текст> — пошук профілів по username/Ім'я TG/Ім'я у грі (тільки адмінам)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    if search_profiles is None:
        await update.message.reply_text("⚠️ Пошук профілів тимчасово недоступний на цьому деплої.")
        return
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Використання: /find <текст>")
        return
    results = search_profiles(q, limit=10)
    if not results:
        await update.message.reply_text("Нічого не знайдено.")
        return
    chunks = [
        _format_profile(p) for p in results
    ]
    await update.message.reply_text("\n\n".join(chunks), parse_mode="HTML", disable_web_page_preview=True)

async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показати збережений профіль користувача."""
    user = update.effective_user
    profile = get_profile(user.id)
    if not profile:
        await update.message.reply_text("ℹ️ Профіль ще не збережено. Натисніть /start і спробуйте знову.")
        return
    text = (
        "👤 <b>Ваш профіль</b>\n\n"
        f"TG: @{profile['username'] or 'немає'}\n"
        f"Ім'я в Telegram: {profile['full_name_tg'] or '—'}\n"
        f"Ім'я у грі: {profile['in_game_name'] or '—'}\n"
        f"Звання: {profile['rank'] or '—'}\n"
        f"Підрозділ: {profile['npu_department'] or '—'}\n"
        f"Роль: {profile['role'] or 'user'}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показати довідку по командам та діям бота."""
    is_admin = update.effective_user.id in ADMIN_IDS
    text = (
        "ℹ️ <b>Довідка</b>\n\n"
        "<b>Основні команди</b>:\n"
        "• /start — запустити бота та показати меню\n"
        "• /help — ця довідка\n"
        "• /me — показати ваш збережений профіль\n"
    "• /neaktyv — подати <i>заяву на неактив</i> (також є кнопка в меню)\n"
    "• /refill — <i>тимчасово</i>: перезаповнити ваш профіль для оновлень БД\n\n"
        "<b>Заява на доступ у групу</b>:\n"
        "1) Натисніть /start і дотримуйтесь інструкцій\n"
        "2) Введіть <i>ім'я та прізвище українською</i> (повністю)\n"
        "3) Оберіть <i>управління НПУ</i> і <i>своє звання</i> зі списку\n"
    "4) Надішліть <i>2 посилання</i> на скріншоти (посвідчення і трудову книжку) з imgbb/imgur/postimg\n\n"
        "<blockquote>Порада: надсилайте <b>прямі URL</b> зображень, кожне з нового рядка.</blockquote>\n\n"
    )
    if is_admin:
        text += (
            "<b>Адмінські команди</b>:\n"
            "• /admin — коротка статистика заяв\n"
            "• /dogana — оформлення догани\n"
            "• /user &lt;id|@username&gt; — показати профіль користувача\n"
            "• /find &lt;текст&gt; — пошук профілів (username/ім'я TG/ім'я у грі)\n"
            "• /broadcast_fill — надіслати інструкцію для заповнення профілів\n\n"
        )
    text += (
        "<b>Модерація заяв на неактив</b> (адміни):\n"
        "• У приват повідомлення приходить карточка з кнопками <b>Одобрити/Відхилити</b>\n"
        "• Після кліку бот попросить <i>ім'я та прізвище модератора</i> для підпису\n"
        "• Результат публікується у групі з атрибуцією <i>Перевіряючий</i>\n\n"
        "<b>Формат імені</b>: лише українські літери, повне ім'я та прізвище.\n"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

def main() -> None:
    """Запуск бота"""
    # Створюємо додаток
    application = Application.builder().token(BOT_TOKEN).build()
    # Ініціалізуємо БД
    init_db()
    
    # Додаємо обробники
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("me", me_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("broadcast_fill", broadcast_fill_profiles))
    application.add_handler(CommandHandler("user", user_lookup_command))
    application.add_handler(CommandHandler("find", find_profiles_command))

    # Попередньо обробляємо вибір покарання (inline) до загального кнопкового хендлера
    application.add_handler(CallbackQueryHandler(dogana_punish_selected, pattern=r"^dogana_punish_"))
    # Ограничиваем общий обработчик кнопок, чтобы не перехватывать approve_neaktyv_/reject_neaktyv_
    application.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(request_access|npu_.+|approve_\d+|reject_\d+)$"))

    # Діалоги: Догани (адміністраторам)
    dogana_conv = ConversationHandler(
        entry_points=[CommandHandler("dogana", dogana_start), MessageHandler(filters.Regex("^📝 Оформити догану$"), dogana_start)],
        states={
            DOGANA_OFFENSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, dogana_offense)],
            DOGANA_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, dogana_date)],
            DOGANA_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, dogana_to)],
            DOGANA_BY: [MessageHandler(filters.TEXT & ~filters.COMMAND, dogana_by)],
            DOGANA_PUNISH: [CallbackQueryHandler(dogana_punish_selected, pattern=r"^dogana_punish_")],
        },
        fallbacks=[CommandHandler("cancel", dogana_cancel)],
        allow_reentry=True,
    )
    application.add_handler(dogana_conv)

    # Діалоги: Заява на неактив (всі)
    neaktyv_conv = ConversationHandler(
        entry_points=[CommandHandler("neaktyv", neaktyv_start), MessageHandler(filters.Regex("^📝 Заява на неактив$"), neaktyv_start)],
        states={
            NEAKTYV_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, neaktyv_to)],
            NEAKTYV_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, neaktyv_time)],
            NEAKTYV_DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, neaktyv_dept)],
        },
        fallbacks=[CommandHandler("cancel", neaktyv_cancel)],
        allow_reentry=True,
    )
    application.add_handler(neaktyv_conv)

    # Діалог модерації заяв на неактив
    neaktyv_moderation_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_neaktyv_moderation, pattern=r"^(approve|reject)_neaktyv_\d+$")],
        states={
            NEAKTYV_APPROVAL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_neaktyv_approval_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel_neaktyv_moderation)],
        allow_reentry=True,
    )
    application.add_handler(neaktyv_moderation_conv)

    # Діалог тимчасового перезаповнення профілю
    refill_conv = ConversationHandler(
        entry_points=[CommandHandler("refill", refill_start)],
        states={
            REFILL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, refill_name)],
            REFILL_NPU: [CallbackQueryHandler(refill_select_npu, pattern=r"^refill_npu_.+")],
            REFILL_RANK: [CallbackQueryHandler(refill_select_rank, pattern=r"^refill_rank_\d+")],
            REFILL_IMAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, refill_images)],
        },
        fallbacks=[CommandHandler("cancel", neaktyv_cancel)],
        allow_reentry=True,
    )
    application.add_handler(refill_conv)

    # Існуючі текстові повідомлення анкети
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_application_text))
    
    # Запускаємо бота
    logger.info("Бот запущено!")
    
    # Додаємо обробку помилок для конфліктів
    async def error_handler(update, context):
        logger.error(f"Помилка оброблена: {context.error}")
    
    application.add_error_handler(error_handler)
    
    # Запускаємо з обробкою конфліктів
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # Ігноруємо старі повідомлення
        )
    except Exception as e:
        logger.error(f"Критична помилка при запуску: {e}")
        raise

if __name__ == '__main__':
    main()