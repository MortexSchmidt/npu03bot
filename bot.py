import os
import logging
import re
import time
import traceback
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
    ApplicationHandlerStop,
)
from db import init_db, upsert_profile, update_profile_fields, get_profile, get_profile_images
from db import replace_profile_images
from db import (
    insert_warning,
    insert_neaktyv_request,
    decide_neaktyv_request,
    insert_access_application,
    decide_access_application,
    insert_promotion_request,
    get_promotion_request,
    get_pending_promotion_requests,
    decide_promotion_request,
)
from db import log_action, log_profile_update, log_antispam_event
from db import (
    log_action,
    log_profile_update,
    log_antispam_event,
    query_action_logs,
    query_antispam_top,
    export_table_csv,
    logs_stats,
    log_error,
)
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

# ===== Антиспам (тротлінг) =====
# Налаштування лімітів (у секундах)
RATE_LIMITS = {
    "message": {"window": 5.0, "max": 5, "min_interval": 0.5},   # Не більше 5 повідомлень за 5с, інтервал >= 0.5с
    "callback": {"window": 10.0, "max": 8, "min_interval": 0.4}, # Не більше 8 кліків за 10с, інтервал >= 0.4с
}

def _rl_storage(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("_rate_limits", {})

def _rl_get_user_bucket(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    storage = _rl_storage(context)
    if user_id not in storage:
        storage[user_id] = {
            "message": deque(),
            "callback": deque(),
            "last_event_time": {"message": 0.0, "callback": 0.0},
            "last_warn": 0.0,
        }
    return storage[user_id]

def _rate_limited(context: ContextTypes.DEFAULT_TYPE, user_id: int, kind: str) -> tuple[bool, float]:
    """Повертає (is_limited, retry_after_sec). Обрізає старі події; застосовує min_interval та вікно."""
    now = time.time()
    cfg = RATE_LIMITS[kind]
    bucket = _rl_get_user_bucket(context, user_id)
    dq: deque = bucket[kind]
    # Видалити старі події поза вікном
    window = cfg["window"]
    while dq and (now - dq[0]) > window:
        dq.popleft()
    # Перевірка інтервалу між подіями
    last_t = bucket["last_event_time"].get(kind, 0.0)
    min_i = cfg["min_interval"]
    if now - last_t < min_i:
        retry = max(0.1, min_i - (now - last_t))
        return True, retry
    # Перевірка кількості у вікні
    if len(dq) >= cfg["max"]:
        # Коли мине ліміт?
        retry = max(0.1, window - (now - dq[0]))
        return True, retry
    # Додаємо подію
    dq.append(now)
    bucket["last_event_time"][kind] = now
    return False, 0.0

def _should_warn(context: ContextTypes.DEFAULT_TYPE, user_id: int, cooldown: float = 10.0) -> bool:
    now = time.time()
    bucket = _rl_get_user_bucket(context, user_id)
    if now - bucket.get("last_warn", 0.0) >= cooldown:
        bucket["last_warn"] = now
        return True
    return False

async def anti_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pre-handler для повідомлень: відсікає спам. Перериває подальшу обробку при ліміті."""
    if not update.effective_user:
        return
    user_id = update.effective_user.id
    # Не обмежуємо адміністраторів
    if user_id in ADMIN_IDS:
        return
    limited, retry = _rate_limited(context, user_id, "message")
    if limited:
        # Лог події антиспаму (повідомлення)
        try:
            if update.effective_user:
                log_antispam_event(update.effective_user.id, "message", retry_after=retry)
        except Exception:
            pass
        if _should_warn(context, user_id):
            try:
                await update.effective_message.reply_text(
                    f"⏳ Занадто часто. Зачекайте приблизно {int(retry)+1} сек.")
            except Exception:
                pass
        # Перериваємо подальшу обробку усіх хендлерів
        raise ApplicationHandlerStop()
    return

async def anti_spam_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pre-handler для кліків по кнопках: відсікає спам. Перериває подальшу обробку при ліміті."""
    query = update.callback_query
    if not query or not query.from_user:
        return
    user_id = query.from_user.id
    if user_id in ADMIN_IDS:
        return
    limited, retry = _rate_limited(context, user_id, "callback")
    if limited:
        # Лог події антиспаму (клік)
        try:
            log_antispam_event(user_id, "callback", retry_after=retry)
        except Exception:
            pass
        try:
            await query.answer(f"⏳ Повільніше, зачекайте ~{int(retry)+1} сек.", show_alert=False)
        except Exception:
            pass
        raise ApplicationHandlerStop()
    return

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

# Система рангов НПУ (от низших к высшим)
NPU_RANKS = [
    # Младший состав
    "Курсант поліції",
    "Поліцейський",
    "Старший поліцейський",
    "Молодший сержант поліції",
    "Сержант поліції",
    "Старший сержант поліції",
    "Старшина поліції",
    # Средний командный состав
    "Молодший лейтенант поліції",
    "Лейтенант поліції", 
    "Старший лейтенант поліції",
    "Капітан поліції",
    # Старший командный состав
    "Майор поліції",
    "Підполковник поліції",
    "Полковник поліції",
    # Высший командный состав
    "Генерал-майор поліції",
    "Генерал-лейтенант поліції",
    "Генерал-полковник поліції",
    "Генерал поліції України",
]

def get_next_ranks(current_rank: str) -> list:
    """Получить доступные ранги для повышения (только следующие)."""
    try:
        current_index = NPU_RANKS.index(current_rank)
        # Возвращаем только следующий ранг (или несколько следующих если нужно)
        return NPU_RANKS[current_index + 1:current_index + 3]  # следующие 1-2 ранга
    except (ValueError, IndexError):
        return []

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
    context.user_data["refill_images_received"] = []
    await query.edit_message_text(
        f"✅ Звання обрано: {rank}\n\n"
        "🔸 Крок 4 з 4: Надішліть 2 фотографії (посвідчення та трудову книжку).\n\n"
        "Надішліть фотографії прямо в чат (по одній за раз).")
    return REFILL_IMAGES

async def refill_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка фотографий для /refill"""
    user = update.effective_user
    
    if not update.message.photo:
        await update.message.reply_text("❌ Будь ласка, надішліть фотографію (не файл).")
        return REFILL_IMAGES
    
    photo = update.message.photo[-1]
    file_id = photo.file_id
    
    try:
        if "refill_images_received" not in context.user_data:
            context.user_data["refill_images_received"] = []
        
        context.user_data["refill_images_received"].append(file_id)
        images_count = len(context.user_data["refill_images_received"])
        
        if images_count < 2:
            await update.message.reply_text(
                f"✅ Фото {images_count}/2 отримано.\n\n"
                "📸 Надішліть ще одне фото."
            )
            return REFILL_IMAGES
        else:
            form = context.user_data.get("refill_form", {})
            file_ids = context.user_data["refill_images_received"]
            
            update_profile_fields(
                user.id,
                in_game_name=form.get("in_game_name"),
                npu_department=form.get("npu_department"),
                rank=form.get("rank"),
            )
            replace_profile_images(user.id, file_ids)
            
            log_profile_update(
                user_id=user.id,
                fields={
                    "in_game_name": form.get("in_game_name"),
                    "npu_department": form.get("npu_department"),
                    "rank": form.get("rank"),
                },
                images_count=len(file_ids),
                source="refill",
            )
            log_action(
                actor_id=user.id,
                actor_username=user.username,
                action="profile_refill",
                target_user_id=user.id,
                target_username=user.username,
                details=f"images={len(file_ids)}",
            )

            summary = (
                "✅ <b>Профіль оновлено</b>\n\n"
                "<blockquote>"
                f"Ім'я у грі: {form.get('in_game_name')}\n"
                f"Підрозділ: {form.get('npu_department')}\n"
                f"Звання: {form.get('rank')}\n"
                f"Фото: {len(file_ids)} зображення"
                "</blockquote>\n\n"
                "Дякуємо! Ця команда є <i>тимчасовою</i> і буде видалена після міграції."
            )
            await update.message.reply_text(summary, parse_mode="HTML", disable_web_page_preview=True)

            context.user_data.pop("refill_form", None)
            context.user_data.pop("refill_images_received", None)
            return ConversationHandler.END
    
    except Exception as e:
        logger.error(f"Error processing refill photo: {e}", exc_info=True)
        await update.message.reply_text("❌ Помилка обробки фотографії. Спробуйте ще раз.")
        return REFILL_IMAGES

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
    # Лог події старту та знімок оновлення профілю
    try:
        log_profile_update(
            user_id=user.id,
            fields={
                "username": user.username or None,
                "full_name_tg": tg_fullname or None,
                "role": ('admin' if user.id in ADMIN_IDS else 'user'),
            },
            images_count=None,
            source="start",
        )
        log_action(
            actor_id=user.id,
            actor_username=user.username,
            action="start",
            target_user_id=user.id,
            target_username=user.username,
            details=None,
        )
    except Exception:
        pass

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
        keyboard_rows = [
            ["📝 Заява на неактив", "📈 Заява на підвищення"]
        ]
        if is_admin:
            keyboard_rows.append(["⚡ Адмін-команди"])  # Перемикач у адмін-меню
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


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Детальна довідка для адміністраторів (доступ лише адмінам)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    text = (
        "🛡️ <b>Адмін-довідка</b>\n\n"
        "<b>Адмінські команди</b>:\n"
        "• /admin — коротка статистика заяв\n"
        "• /dogana — оформлення догани (5 кроків, збереження у БД)\n"
        "• /user &lt;id|@username&gt; — показати профіль користувача\n"
        "• /find &lt;текст&gt; — пошук профілів; з повідомлення додаються кнопки дій (kick/догана)\n"
        "• /broadcast_fill — розсилка інструкції щодо заповнення профілю\n"
        "• /logs [limit] [action=...] [actor_id=...] [actor=@...] [from=YYYY-MM-DD] [to=YYYY-MM-DD] — останні дії з фільтрами\n"
        "• /antispam_top [days=7] [kind=message|callback] [limit=10] — топ за антиспам-подіями\n"
        "• /export_csv &lt;table&gt; [days=N] — експорт таблиці у CSV (profiles, action_logs, warnings, ... )\n"
        "• /log_stats [days=7] — сводка (дії за типами, антиспам підсумки)\n\n"
        "<b>Модерація неактиву</b>: у приват приходять картки з кнопками; після рішення — публікація у темі з атрибуцією.\n"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def show_pending_promotions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать активные заявки на повышение (только для админов)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    
    # Получаем все активные заявки
    pending_requests = get_pending_promotion_requests()
    
    if not pending_requests:
        await update.message.reply_text(
            "📈 <b>Рапорти на підвищення</b>\n\n"
            "🔍 Немає активних заявок на розгляд.",
            parse_mode="HTML"
        )
        return
    
    # Создаем сообщение со списком заявок
    message_text = f"📈 <b>Рапорти на підвищення</b>\n\nАктивних заявок: {len(pending_requests)}\n\n"
    
    # Создаем кнопки для каждой заявки
    keyboard = []
    for i, req in enumerate(pending_requests, 1):
        created_date = req['created_at'][:10] if req['created_at'] else 'N/A'  # YYYY-MM-DD
        
        message_text += (
            f"<b>{i}.</b> {req['requester_name']}\n"
            f"   📈 {req['current_rank']} → {req['target_rank']}\n"
            f"   📅 {created_date}\n\n"
        )
        
        # Кнопка для просмотра конкретной заявки
        keyboard.append([
            InlineKeyboardButton(
                f"📋 Заявка №{req['id']} ({req['requester_name']})",
                callback_data=f"view_promotion_{req['id']}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        message_text,
        parse_mode="HTML",
        reply_markup=reply_markup
    )

############################
# ДОГАН (адміністраторам)
############################

# Стани для діалогу 'догана'
DOGANA_OFFENSE, DOGANA_DATE, DOGANA_TO, DOGANA_BY, DOGANA_PUNISH = range(5)

# Стани для заявки на підвищення
PROMOTION_CURRENT_RANK, PROMOTION_TARGET_RANK, PROMOTION_WORKBOOK, PROMOTION_EVIDENCE, PROMOTION_FINISH = range(5)

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
    # Підтримка попереднього вибору через /find (за замовчуванням)
    prefill_to = context.user_data.get("dogana_prefill_to")
    hint = (f"\nЗа замовчуванням: <code>{prefill_to}</code>\n"
            "Введіть 'за замовчуванням' або напишіть інше ім'я") if prefill_to else ""
    await update.message.reply_text(
        "📝 <b>ОФОРМЛЕННЯ ДОГАНИ</b>\n\n"
        "🔸 <b>Крок 3 з 5:</b> Порушник\n\n"
        "Введіть ім'я та прізвище особи, якій видається догана:\n"
        "<i>(українською мовою, повне ім'я та прізвище)</i>" + hint,
        parse_mode="HTML"
    )
    return DOGANA_TO

async def dogana_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    # Дозволяємо 'за замовчуванням' для використання префіла з /find
    if raw.lower() == "за замовчуванням" and context.user_data.get("dogana_prefill_to"):
        raw = context.user_data.get("dogana_prefill_to")
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
    # Після використання префіла — приберемо його
    context.user_data.pop("dogana_prefill_to", None)
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
            try:
                log_action(
                    actor_id=query.from_user.id if query and query.from_user else None,
                    actor_username=query.from_user.username if query and query.from_user else None,
                    action="warning_issued",
                    target_user_id=None,
                    target_username=None,
                    details=f"kind={kind}; to={form.get('to_whom')}; rank={form.get('rank_to')}; date={form.get('date')}",
                )
            except Exception:
                pass
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
            "✅ Капрал Марія Коваленко\n"
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
            [[meta["title"]] for meta in NPU_DEPARTMENTS.values()],
            one_time_keyboard=True,
            resize_keyboard=True
        )
    )
    return NEAKTYV_DEPARTMENT

async def neaktyv_dept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Приймаємо як код (navs/kord/...) так і повну назву; зберігаємо завжди повну назву
    inp = update.message.text.strip()
    dept_title = None
    # Пряме співпадіння по коду
    if inp in NPU_DEPARTMENTS:
        dept_title = NPU_DEPARTMENTS[inp]["title"]
    else:
        # Пошук по назві (без регістру)
        low = inp.lower()
        for meta in NPU_DEPARTMENTS.values():
            if meta["title"].lower() == low:
                dept_title = meta["title"]
                break
    context.user_data["neaktyv_form"]["department"] = dept_title or inp
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
        # Загальний лог створення заявки
        try:
            log_action(
                actor_id=user_id,
                actor_username=username,
                action="neaktyv_request_created",
                target_user_id=None,
                target_username=None,
                details=f"request_id={request_id}; to={form.get('to_whom')}; duration={form.get('duration')}; dept={form.get('department')}",
            )
        except Exception:
            pass
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
# ЗАЯВКА НА ПІДВИЩЕННЯ
############################

async def promotion_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начать подачу заявки на повышение."""
    user = update.effective_user
    user_id = user.id
    
    # Проверяем, что у пользователя есть профиль с именем
    profile = get_profile(user_id)
    if not profile or not profile.get('in_game_name'):
        await update.message.reply_text(
            "❌ Помилка!\n\n"
            "Для подачі заяви на підвищення у вас повинно бути заповнено ім'я в грі.\n"
            "Будь ласка, спочатку подайте заявку на вступ або оновіть профіль через /refill."
        )
        return ConversationHandler.END
    
    # Инициализируем форму заявки
    context.user_data["promotion_form"] = {
        "requester_name": profile.get('in_game_name'),
        "requester_username": user.username,
    }
    
    # Создаем кнопки с рангами
    keyboard = []
    for rank in NPU_RANKS:
        keyboard.append([rank])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        "📈 <b>Заява на підвищення</b>\n\n"
        f"Ім'я в грі: <b>{profile.get('in_game_name')}</b>\n\n"
        "Крок 1: Оберіть ваш поточний ранг:",
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    
    return PROMOTION_CURRENT_RANK

async def promotion_current_rank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработать выбор текущего ранга."""
    current_rank = update.message.text.strip()
    
    if current_rank not in NPU_RANKS:
        await update.message.reply_text(
            "❌ Неправильний ранг. Оберіть зі списку нижче:",
            reply_markup=ReplyKeyboardMarkup([[rank] for rank in NPU_RANKS], resize_keyboard=True)
        )
        return PROMOTION_CURRENT_RANK
    
    # Сохраняем текущий ранг
    context.user_data["promotion_form"]["current_rank"] = current_rank
    
    # Получаем доступные ранги для повышения
    next_ranks = get_next_ranks(current_rank)
    
    if not next_ranks:
        await update.message.reply_text(
            "❌ Ви вже маєте найвищий ранг або система не може визначити наступний ранг.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    # Создаем кнопки с доступными рангами для повышения
    keyboard = [[rank] for rank in next_ranks]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        f"Поточний ранг: <b>{current_rank}</b>\n\n"
        "Крок 2: Оберіть ранг, на який хочете підвищення:",
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    
    return PROMOTION_TARGET_RANK

async def promotion_target_rank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработать выбор целевого ранга."""
    target_rank = update.message.text.strip()
    current_rank = context.user_data["promotion_form"]["current_rank"]
    next_ranks = get_next_ranks(current_rank)
    
    if target_rank not in next_ranks:
        await update.message.reply_text(
            "❌ Неправильний ранг. Оберіть зі списку доступних для підвищення:",
            reply_markup=ReplyKeyboardMarkup([[rank] for rank in next_ranks], resize_keyboard=True)
        )
        return PROMOTION_TARGET_RANK
    
    # Сохраняем целевой ранг
    context.user_data["promotion_form"]["target_rank"] = target_rank
    
    await update.message.reply_text(
        f"Підвищення: <b>{current_rank}</b> → <b>{target_rank}</b>\n\n"
        "Крок 3: Надішліть скриншот вашої трудової книги (зображення).\n"
        "📋 Трудова книга повинна бути чітко видно.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    
    return PROMOTION_WORKBOOK

async def promotion_workbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработать скриншот трудовой книги."""
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Будь ласка, надішліть зображення трудової книги."
        )
        return PROMOTION_WORKBOOK
    
    photo = update.message.photo[-1]
    
    try:
        context.user_data["promotion_form"]["workbook_image_id"] = photo.file_id
        context.user_data["promotion_form"]["work_evidence_image_ids"] = [] # Инициализируем список для фото доказательств
        
        await update.message.reply_text(
            "✅ Скриншот трудової книги прийнято.\n\n"
            "Крок 4: Надішліть одне або декілька фото з доказом виконаної роботи.\n"
            "📸 Це можуть бути скріншоти з гри, звіти, виконані завдання тощо.\n\n"
            "<b>Коли закінчите, натисніть кнопку 'Завершити'.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Завершити та відправити", callback_data="promotion_finish")]])
        )
        
        return PROMOTION_EVIDENCE
        
    except Exception as e:
        logger.error(f"Error processing workbook image: {e}")
        await update.message.reply_text(
            "❌ Помилка обробки зображення. Спробуйте ще раз."
        )
        return PROMOTION_WORKBOOK

async def promotion_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработать скриншоты проделанной работы."""
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Будь ласка, надішліть зображення."
        )
        return PROMOTION_EVIDENCE
    
    photo = update.message.photo[-1]
    
    try:
        # Добавляем file_id в список
        if "work_evidence_image_ids" not in context.user_data["promotion_form"]:
            context.user_data["promotion_form"]["work_evidence_image_ids"] = []
            
        context.user_data["promotion_form"]["work_evidence_image_ids"].append(photo.file_id)
        
        count = len(context.user_data["promotion_form"]["work_evidence_image_ids"])
        await update.message.reply_text(
            f"✅ Фото доказів {count} отримано.\n"
            "Надішліть ще або натисніть 'Завершити'.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Завершити та відправити", callback_data="promotion_finish")]])
        )
        
        return PROMOTION_EVIDENCE
        
    except Exception as e:
        logger.error(f"Error processing evidence image: {e}")
        await update.message.reply_text(
            "❌ Помилка обробки зображення. Спробуйте ще раз."
        )
        return PROMOTION_EVIDENCE

async def promotion_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершить подачу заявки и отправить на модерацию."""
    query = update.callback_query
    if query:
        await query.answer()
        user = query.from_user
        chat = query.message.chat
    else: # Если вызвано не кнопкой, а, например, командой
        user = update.effective_user
        chat = update.effective_chat

    form = context.user_data.get("promotion_form", {})

    if not form.get("work_evidence_image_ids"):
        await chat.send_message("❌ Ви не додали жодного фото з доказом роботи.")
        return PROMOTION_EVIDENCE

    try:
        # Сохраняем заявку в базу данных
        request_id = insert_promotion_request(
            requester_id=user.id,
            requester_username=user.username or "",
            requester_name=form["requester_name"],
            current_rank=form["current_rank"],
            target_rank=form["target_rank"],
            workbook_image_id=form["workbook_image_id"],
            # Преобразуем список ID в строку для хранения в БД
            work_evidence_image_ids=",".join(form["work_evidence_image_ids"])
        )
        
        log_action(
            actor_id=user.id,
            actor_username=user.username,
            action="create_promotion_request",
            details=f"Current: {form['current_rank']}, Target: {form['target_rank']}, Images: {len(form['work_evidence_image_ids'])}"
        )
        
        await send_promotion_to_admins(context, request_id, form, user)
        
        final_message = (
            "✅ <b>Заявка на підвищення подана!</b>\n\n"
            f"📋 Заявка №{request_id}\n"
            f"👤 Заявник: {form['requester_name']}\n"
            f"📈 Підвищення: {form['current_rank']} → {form['target_rank']}\n"
            f"📸 Додано доказів: {len(form['work_evidence_image_ids'])}\n\n"
            "Ваша заявка відправлена адміністраторам на розгляд."
        )
        
        if query:
            await query.edit_message_text(final_message, parse_mode="HTML", reply_markup=None)
        else:
            await chat.send_message(final_message, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

        context.user_data.pop("promotion_form", None)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error processing promotion request: {e}", exc_info=True)
        await chat.send_message("❌ Помилка створення заявки. Спробуйте пізніше.")
        return ConversationHandler.END

async def promotion_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменить подачу заявки на повышение."""
    context.user_data.pop("promotion_form", None)
    await update.message.reply_text("Подача заявки на підвищення скасована.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def send_promotion_to_admins(context: ContextTypes.DEFAULT_TYPE, request_id: int, form: dict, user):
    """Отправить заявку на повышение админам для модерации."""
    
    workbook_image_id = form.get("workbook_image_id")
    work_evidence_image_ids = form.get("work_evidence_image_ids", [])

    admin_message_text = (
        "📈 <b>НОВА ЗАЯВКА НА ПІДВИЩЕННЯ</b>\n\n"
        f"📋 Заявка №{request_id}\n\n"
        f"👤 Заявник: {form['requester_name']}\n"
        f"🆔 Telegram: @{user.username or 'немає'} (ID: {user.id})\n"
        f"📊 Поточний ранг: {form['current_rank']}\n"
        f"📈 Бажаний ранг: {form['target_rank']}\n\n"
        "<i>Докази надіслано окремими повідомленнями.</i>"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Одобрити", callback_data=f"approve_promotion_{request_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_promotion_{request_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    for admin_id in ADMIN_IDS:
        try:
            media_group = []
            # Добавляем фото трудовой книги первым
            if workbook_image_id:
                media_group.append(InputMediaPhoto(media=workbook_image_id, caption="Трудова книга"))

            # Добавляем фото доказательств
            for i, evidence_id in enumerate(work_evidence_image_ids):
                caption = f"Доказ роботи {i+1}" if len(media_group) > 0 else "Докази роботи"
                media_group.append(InputMediaPhoto(media=evidence_id, caption=caption))

            # Отправляем медиагруппу, если есть фото
            if media_group:
                # Ограничение Telegram - до 10 фото в медиагруппе
                for i in range(0, len(media_group), 10):
                    chunk = media_group[i:i+10]
                    await context.bot.send_media_group(chat_id=admin_id, media=chunk)

            # Отправляем основное сообщение с кнопками
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_message_text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to send promotion request to admin {admin_id}: {e}", exc_info=True)

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
        disp_name = display_ranked_name(form.get('rank'), form.get('to_whom') or form.get('full_name_tg') or '')
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
                    try:
                        log_action(
                            actor_id=update.effective_user.id,
                            actor_username=update.effective_user.username,
                            action="neaktyv_approved",
                            target_user_id=user_id,
                            target_username=None,
                            details=f"request_id={req_id}; moderator={name}",
                        )
                    except Exception:
                        pass
            except Exception as dbe:
                logger.error(f"DB decide neaktyv approve failed: {dbe}")
            await update.message.reply_text(f"✅ Заяву одобрено та опубліковано в групі!")
        except Exception as e:
            logger.error(f"Помилка при обробці заяви: {e}")
            await update.message.reply_text("❌ Помилка при обробці заяви.")
    else:
        # Відхилення - редагуємо повідомлення адміністратора
        disp = form.get('to_whom') or form.get('full_name_tg') or ''
        admin_edit_message = (
            "❌ ЗАЯВА ВІДХИЛЕНА\n\n"
            "<blockquote>"
            f"1. Кому надається: {disp}\n"
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
                    try:
                        log_action(
                            actor_id=update.effective_user.id,
                            actor_username=update.effective_user.username,
                            action="neaktyv_rejected",
                            target_user_id=user_id,
                            target_username=None,
                            details=f"request_id={req_id}; moderator={name}",
                        )
                    except Exception:
                        pass
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

############################
# МОДЕРАЦІЯ ЗАЯВОК НА ПІДВИЩЕННЯ
############################

async def handle_promotion_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка модерації заявок на підвищення."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Немає доступу.")
        return
    
    # Парсинг callback_data
    if query.data.startswith("approve_promotion_"):
        request_id = int(query.data.split("_")[2])
        await approve_promotion_request(update, context, request_id)
    elif query.data.startswith("reject_promotion_"):
        request_id = int(query.data.split("_")[2])
        await start_reject_promotion_request(update, context, request_id)

async def approve_promotion_request(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: int):
    """Одобрити заявку на підвищення."""
    query = update.callback_query
    admin = query.from_user
    
    # Получаем профиль админа для ранга
    admin_profile = get_profile(admin.id)
    admin_rank = admin_profile.get('rank', 'Адміністратор') if admin_profile else 'Адміністратор'
    
    # Получаем заявку из БД
    request = get_promotion_request(request_id)
    if not request:
        await query.edit_message_text("❌ Заявка не знайдена.")
        return
    
    if request["status"] != "pending":
        await query.edit_message_text("❌ Заявка вже оброблена.")
        return
    
    # Одобряем заявку в БД
    success = decide_promotion_request(
        request_id=request_id,
        moderator_id=admin.id,
        moderator_username=admin.username or "",
        moderator_rank=admin_rank,
        approved=True
    )
    
    if not success:
        await query.edit_message_text("❌ Помилка одобрення заявки.")
        return
    
    # Логируем действие
    log_action(
        actor_id=admin.id,
        actor_username=admin.username,
        action="approve_promotion",
        target_user_id=request["requester_id"],
        target_username=request["requester_username"],
        details=f"request_id={request_id}; {request['current_rank']}->{request['target_rank']}"
    )
    
    # Обновляем сообщение
    await query.edit_message_text(
        f"✅ <b>ПІДВИЩЕННЯ ОДОБРЕНО</b>\n\n"
        f"📋 Заявка №{request_id}\n"
        f"👤 Заявник: {request['requester_name']}\n"
        f"📈 Підвищення: {request['current_rank']} → {request['target_rank']}\n"
        f"👔 Модератор: @{admin.username or 'невідомо'} ({admin_rank})\n\n"
        f"✅ Одобрено та відправлено в канал.",
        parse_mode="HTML"
    )
    
    # Отправляем в канал
    await send_promotion_to_channel(context, request, admin_rank, admin.username or admin.first_name)
    
    # Уведомляем заявителя
    try:
        await context.bot.send_message(
            chat_id=request["requester_id"],
            text=f"🎉 <b>Вітаємо!</b>\n\n"
                 f"Ваша заявка на підвищення №{request_id} одобрена!\n"
                 f"📈 {request['current_rank']} → {request['target_rank']}\n\n"
                 f"Інформація про підвищення відправлена в офіційний канал.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify user about promotion approval: {e}")

async def start_reject_promotion_request(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: int):
    """Начать процесс отклонения заявки (запросить причину)."""
    query = update.callback_query
    
    # Сохраняем ID заявки для последующей обработки
    context.user_data["reject_promotion_id"] = request_id
    context.user_data["original_promotion_message_id"] = query.message.message_id
    
    await query.edit_message_text(
        f"❌ <b>Відхилення заявки №{request_id}</b>\n\n"
        "Введіть причину відхилення заявки на підвищення:",
        parse_mode="HTML"
    )
    
    # Ждем ввода причины
    context.user_data["awaiting_reject_reason"] = True

async def process_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать причину отклонения заявки."""
    if not context.user_data.get("awaiting_reject_reason"):
        return
    
    reject_reason = update.message.text.strip()
    request_id = context.user_data.get("reject_promotion_id")
    original_message_id = context.user_data.get("original_promotion_message_id")
    
    if not request_id:
        await update.message.reply_text("❌ Помилка: ID заявки не знайдено.")
        return
    
    admin = update.effective_user
    admin_profile = get_profile(admin.id)
    admin_rank = admin_profile.get('rank', 'Адміністратор') if admin_profile else 'Адміністратор'
    
    # Получаем заявку
    request = get_promotion_request(request_id)
    if not request:
        await update.message.reply_text("❌ Заявка не знайдена.")
        return
    
    # Отклоняем заявку в БД
    success = decide_promotion_request(
        request_id=request_id,
        moderator_id=admin.id,
        moderator_username=admin.username or "",
        moderator_rank=admin_rank,
        approved=False,
        reject_reason=reject_reason
    )
    
    if not success:
        await update.message.reply_text("❌ Помилка відхилення заявки.")
        return
    
    # Логируем действие
    log_action(
        actor_id=admin.id,
        actor_username=admin.username,
        action="reject_promotion",
        target_user_id=request["requester_id"],
        target_username=request["requester_username"],
        details=f"request_id={request_id}; reason={reject_reason}"
    )
    
    # Обновляем оригинальное сообщение
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=original_message_id,
            text=f"❌ <b>ПІДВИЩЕННЯ ВІДХИЛЕНО</b>\n\n"
                 f"📋 Заявка №{request_id}\n"
                 f"👤 Заявник: {request['requester_name']}\n"
                 f"📈 Підвищення: {request['current_rank']} → {request['target_rank']}\n"
                 f"👔 Модератор: @{admin.username or 'невідомо'} ({admin_rank})\n\n"
                 f"❌ Причина відхилення: {reject_reason}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to edit promotion message: {e}")
    
    # Уведомляем заявителя
    try:
        await context.bot.send_message(
            chat_id=request["requester_id"],
            text=f"❌ <b>Заявка відхилена</b>\n\n"
                 f"Ваша заявка на підвищення №{request_id} була відхилена.\n"
                 f"📈 {request['current_rank']} → {request['target_rank']}\n\n"
                 f"📝 Причина відхилення: {reject_reason}\n\n"
                 f"Ви можете подати нову заявку після усунення зауважень.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify user about promotion rejection: {e}")
    
    await update.message.reply_text("✅ Заявку відхилено. Заявник отримав повідомлення.")
    
    # Очищаем данные
    context.user_data.pop("awaiting_reject_reason", None)
    context.user_data.pop("reject_promotion_id", None)
    context.user_data.pop("original_promotion_message_id", None)

async def send_promotion_to_channel(context: ContextTypes.DEFAULT_TYPE, request: dict, admin_rank: str, admin_name: str):
    """Отправить одобренную заявку в канал."""
    
    channel_message = (
        "🔺 <b>ПІДВИЩЕННЯ В ЗВАННІ</b>\n\n"
        f"👤 <b>Підвищено:</b> {request['requester_name']}\n"
        f"📈 <b>Підвищення:</b> {request['current_rank']} → {request['target_rank']}\n\n"
        f"✅ <b>Одобрив:</b> {admin_name} ({admin_rank})\n\n"
        f"📋 <b>Вимога для старшого складу:</b>\n"
        f"Підвищити у званні {request['requester_name']} "
        f"з {request['current_rank']} до {request['target_rank']}."
    )
    
    try:
        await context.bot.send_message(
            chat_id=REPORTS_CHAT_ID,
            text=channel_message,
            parse_mode="HTML"
        )
        logger.info(f"Promotion request {request['id']} sent to channel")
    except Exception as e:
        logger.error(f"Failed to send promotion to channel: {e}")

async def view_promotion_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показати деталі конкретної заявки на підвищення."""
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Немає доступу.")
        return

    try:
        # Извлекаем ID заявки из callback_data (может быть view_promotion_{id} или back_to_promotions_list)
        if query.data.startswith("view_promotion_"):
            request_id = int(query.data.split("_")[-1])
        else: # back_to_promotions_list
            await show_pending_promotions(update, context)
            return
    except (ValueError, IndexError):
        await query.edit_message_text("Помилка: невірний ID заявки.")
        return

    request = get_promotion_request(request_id)
    if not request:
        await query.edit_message_text("❌ Заявка не знайдена.")
        return

    # Удаляем предыдущее сообщение (список заявок)
    await query.delete_message()

    # Формируем детальное сообщение с заявкой
    created_date = request['created_at'][:19] if request['created_at'] else 'N/A'
    
    message_text = (
        f"📈 <b>ЗАЯВКА НА ПІДВИЩЕННЯ #{request_id}</b>\n\n"
        f"👤 <b>Заявник:</b> {request['requester_name']}\n"
        f"🆔 <b>Telegram:</b> @{request['requester_username'] or 'немає'} (ID: {request['requester_id']})\n"
        f"📊 <b>Поточний ранг:</b> {request['current_rank']}\n"
        f"📈 <b>Бажаний ранг:</b> {request['target_rank']}\n"
        f"📅 <b>Дата подачі:</b> {created_date}\n\n"
        f"⏳ <b>Статус:</b> {request['status']}"
    )

    # Отправляем фото
    if request.get("workbook_image_id"):
        await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=request["workbook_image_id"],
            caption="Трудова книга"
        )
    
    evidence_ids_str = request.get("work_evidence_image_ids", "")
    if evidence_ids_str:
        evidence_ids = evidence_ids_str.split(',')
        media_group = [InputMediaPhoto(media=file_id) for file_id in evidence_ids]
        
        if media_group:
            # Добавляем подпись к первому элементу, если это возможно
            media_group[0].caption = "Докази роботи"
            
            # Отправляем медиагруппу (до 10 фото за раз)
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                await context.bot.send_media_group(
                    chat_id=query.from_user.id,
                    media=chunk
                )

    # Кнопки для модерации
    keyboard = []
    if request['status'] == 'pending':
        keyboard.append([
            InlineKeyboardButton("✅ Одобрити", callback_data=f"approve_promotion_{request_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_promotion_{request_id}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("🔙 Назад до списку", callback_data="list_pending_promotions")
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=message_text,
        parse_mode="HTML",
        reply_markup=reply_markup
    )

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
                try:
                    log_profile_update(user_id=user_id, fields={"rank": rank}, images_count=None, source="apply")
                except Exception:
                    pass
                await query.edit_message_text(
                    f"✅ Звання обрано: {rank}\n\n"
                    "📝 Крок 3: Надішліть скріншоти (2 фото)\n\n"
                    "Потрібні: посвідчення та трудова книжка. Надішліть фотографії прямо в чат (по одній за раз)."
                )
                context.user_data['step'] = 'waiting_images'
                USER_APPLICATIONS[user_id]['step'] = 'waiting_images'
    
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
    message_text = update.message.text
    
    # Логируем все входящие текстовые сообщения
    logger.info(f"handle_application_text: User {user_id} sent: '{message_text}'")
    
    # Обработка причины отклонения заявки на повышение (для админов)
    if context.user_data.get("awaiting_reject_reason"):
        await process_reject_reason(update, context)
        return
    
    # Якщо користувач не в процесі подачі заявки
    if not context.user_data.get('awaiting_application'):
        logger.info(f"User {user_id} not in application process, ignoring text: '{message_text}'")
        return
    
    step = context.user_data.get('step', 'waiting_name')
    
    if step == 'waiting_name':
        await handle_name_input(update, context)
    # waiting_images обрабатывается в отдельном handler для фотографий

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
    try:
        log_profile_update(user_id=user_id, fields={"in_game_name": name_input}, images_count=None, source="apply")
    except Exception:
        pass
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
    try:
        log_profile_update(user_id=user_id, fields={"npu_department": NPU_DEPARTMENTS[npu_code]["title"]}, images_count=None, source="apply")
    except Exception:
        pass
    USER_APPLICATIONS[user_id]['step'] = 'waiting_rank'
    context.user_data['step'] = 'waiting_rank'

    # Показати вибір звання
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

async def handle_photo_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник фотографій для заявок на доступ"""
    user = update.effective_user
    user_id = user.id

    if not context.user_data.get('awaiting_application') or context.user_data.get('step') != 'waiting_images':
        return

    if user_id not in USER_APPLICATIONS:
        await update.message.reply_text("❌ Помилка: дані заявки не знайдено. Почніть з /start")
        return

    if not update.message.photo:
        await update.message.reply_text("❌ Будь ласка, надішліть фотографію (не файл).")
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    try:
        if 'image_file_ids' not in USER_APPLICATIONS[user_id]:
            USER_APPLICATIONS[user_id]['image_file_ids'] = []
        
        USER_APPLICATIONS[user_id]['image_file_ids'].append(file_id)
        images_count = len(USER_APPLICATIONS[user_id]['image_file_ids'])

        if images_count < 2:
            await update.message.reply_text(f"✅ Фото {images_count}/2 отримано. Надішліть ще одне.")
        else:
            # Отримали всі фото, завершуємо заявку
            application = USER_APPLICATIONS[user_id]
            replace_profile_images(user_id, application['image_file_ids'])
            
            # Логування
            log_profile_update(
                user_id=user_id,
                fields=None,
                images_count=len(application['image_file_ids']),
                source="apply"
            )
            
            # Формуємо повідомлення для адмінів
            admin_message = (
                "📝 <b>НОВА ЗАЯВКА НА ДОСТУП</b>\n\n"
                f"👤 <b>Користувач:</b> @{user.username or 'немає'} (ID: {user_id})\n"
                f"<b>Ім'я в грі:</b> {application['name']}\n"
                f"<b>Підрозділ:</b> {application['npu_department']}\n"
                f"<b>Звання:</b> {application.get('rank', 'не вказано')}\n\n"
                "<i>Фото-докази надіслано окремими повідомленнями.</i>"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Одобрити", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{user_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Зберігаємо заявку в БД
            db_app_id = insert_access_application(
                user_id=user_id,
                username=user.username,
                in_game_name=application['name'],
                npu_department=application['npu_department'],
                rank=application.get('rank'),
                images=",".join(application['image_file_ids']) # Зберігаємо ID як рядок
            )
            log_action(
                actor_id=user_id, actor_username=user.username, action="access_application_created",
                details=f"app_id={db_app_id}"
            )

            # Відправляємо адмінам
            for admin_id in ADMIN_IDS:
                try:
                    # Спочатку фото
                    for img_id in application['image_file_ids']:
                        await context.bot.send_photo(chat_id=admin_id, photo=img_id)
                    # Потім текст з кнопками
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=admin_message,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Не вдалося відправити заявку адміну {admin_id}: {e}")

            await update.message.reply_text(
                "✅ <b>Заявку відправлено!</b>\n\n"
                "Ваша заявка на доступ відправлена на розгляд адміністрації. "
                "Очікуйте на рішення.",
                reply_markup=ReplyKeyboardRemove()
            )
            
            # Очищуємо стан
            del USER_APPLICATIONS[user_id]
            context.user_data.pop('awaiting_application', None)
            context.user_data.pop('step', None)

    except Exception as e:
        logger.error(f"Помилка обробки фото для заявки: {e}", exc_info=True)
        await update.message.reply_text("❌ Сталася помилка під час обробки фото. Спробуйте ще раз.")

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
        # Загальний лог рішення
        try:
            log_action(
                actor_id=update.effective_user.id,
                actor_username=update.effective_user.username,
                action="access_approved",
                target_user_id=user.id,
                target_username=user.username,
                details=f"invite_link={invite_link}",
            )
        except Exception:
            pass
        
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
        try:
            log_action(
                actor_id=update.effective_user.id,
                actor_username=update.effective_user.username,
                action="access_approved_fallback",
                target_user_id=user.id,
                target_username=user.username,
                details=f"invite_link={GROUP_INVITE_LINK}",
            )
        except Exception:
            pass
    
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
    try:
        log_action(
            actor_id=update.effective_user.id,
            actor_username=update.effective_user.username,
            action="access_rejected",
            target_user_id=user.id,
            target_username=user.username,
            details=None,
        )
    except Exception:
        pass
    
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

async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Адм-команда: останні N дій, фільтри по даті/актору/дії.\n
    Використання: /logs [limit] [action=<x>] [actor_id=<id>] [actor=@name] [from=YYYY-MM-DD] [to=YYYY-MM-DD]
    """
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    args = context.args or []
    limit = 50
    kw = {"actor_id": None, "actor_username": None, "action": None, "date_from": None, "date_to": None}
    for a in args:
        if a.isdigit():
            limit = max(1, min(500, int(a)))
        elif a.startswith("action="):
            kw["action"] = a.split("=",1)[1]
        elif a.startswith("actor_id="):
            try:
                kw["actor_id"] = int(a.split("=",1)[1])
            except Exception:
                kw["actor_id"] = None
        elif a.startswith("actor="):
            kw["actor_username"] = a.split("=",1)[1]
        elif a.startswith("from="):
            kw["date_from"] = a.split("=",1)[1]
        elif a.startswith("to="):
            kw["date_to"] = a.split("=",1)[1]
    rows = query_action_logs(limit=limit, **kw)
    if not rows:
        await update.message.reply_text("Порожньо.")
        return
    lines = []
    for r in rows:
        actor = f"{r['actor_id']} (@{r['actor_username']})" if r.get('actor_username') else str(r.get('actor_id'))
        target = (f" -> {r['target_user_id']} (@{r['target_username']})" if r.get('target_user_id') else "")
        det = f" | {r['details']}" if r.get('details') else ""
        lines.append(f"[{r['created_at']}] {actor}: {r['action']}{target}{det}")
    text = "\n".join(lines[:1000])
    await update.message.reply_text(f"<b>Останні дії ({len(rows)}):</b>\n\n<code>{text}</code>", parse_mode="HTML", disable_web_page_preview=True)

async def antispam_top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Адм-команда: топ по антиспаму.\n
    Использование: /antispam_top [days=7] [kind=message|callback] [limit=10]
    """
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    days = 7
    kind = None
    limit = 10
    for a in context.args or []:
        if a.startswith("days="):
            try: days = max(1, int(a.split("=",1)[1]));
            except Exception: pass
        elif a.startswith("kind="):
            k = a.split("=",1)[1]
            if k in ("message","callback"): kind = k
        elif a.startswith("limit="):
            try: limit = max(1, min(50, int(a.split("=",1)[1])));
            except Exception: pass
    rows = query_antispam_top(days=days, kind=kind, limit=limit)
    if not rows:
        await update.message.reply_text("За період порожньо.")
        return
    lines = [f"{i+1}. {r['user_id']} — {r['count']}" for i,r in enumerate(rows)]
    await update.message.reply_text("<b>Топ антиспаму</b>\n"+"\n".join(lines), parse_mode="HTML")

async def export_csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Адм-команда: експорта CSV.\n
    Использование: /export_csv <table> [days=N]
    Допустимые таблицы: profiles, profile_images, warnings, neaktyv_requests, access_applications, action_logs, profile_updates, antispam_events, error_logs
    """
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /export_csv <table> [days=N]")
        return
    table = context.args[0]
    days = None
    if len(context.args) > 1 and context.args[1].startswith("days="):
        try:
            days = int(context.args[1].split("=",1)[1])
        except Exception:
            days = None
    try:
        filename, content = export_table_csv(table, days=days)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
        return
    await update.message.reply_document(document=(filename, content), caption=f"Експорт {table}{' за ' + str(days) + ' дн.' if days else ''}")

async def log_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Адм-команда: сводні показники.\n
    Использование: /log_stats [days=7]
    """
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    days = 7
    for a in context.args or []:
        if a.startswith("days="):
            try: days = max(1, int(a.split("=",1)[1]));
            except Exception: pass
    stats = logs_stats(days=days)
    parts = ["<b>Сводка</b>"]
    parts.append("\nДії по типам:")
    for k,v in stats.get("actions_by_type", []):
        parts.append(f"• {k}: {v}")
    parts.append(f"\nАнтиспам (всього): {stats.get('antispam_total', 0)}")
    if stats.get("antispam_by_kind"):
        parts.append("Антиспам по типам:")
        for k,v in stats["antispam_by_kind"]:
            parts.append(f"• {k}: {v}")
    await update.message.reply_text("\n".join(parts), parse_mode="HTML")

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
        "3) Надішліть 2 фотографії прямо в чат (посвідчення та трудову книжку)\n\n"
        "Дякуємо за оперативність!"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def open_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Відкрити адмін-меню (тільки для адмінів)."""
    user_id = update.effective_user.id
    logger.info(f"open_admin_menu called by user {user_id}")
    
    if user_id not in ADMIN_IDS:
        logger.warning(f"Non-admin user {user_id} tried to access admin menu")
        await update.message.reply_text("❌ Немає доступу.")
        return
    
    logger.info(f"Opening admin menu for admin {user_id}")
    kb = ReplyKeyboardMarkup([
        ["📝 Оформити догану", "/admin_help"],
        ["📈 Рапорти на підвищення"],
        ["🔙 Звичайні команди"],
    ], resize_keyboard=True)
    await update.message.reply_text("🛡️ Адмін-меню відкрито.", reply_markup=kb)

async def open_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Повернутись до звичайного меню (у всіх користувачів)."""
    user_id = update.effective_user.id
    logger.info(f"open_user_menu called by user {user_id}")
    
    kb_rows = [
        ["📝 Заява на неактив", "📈 Заява на підвищення"]
    ]
    if user_id in ADMIN_IDS:
        kb_rows.append(["⚡ Адмін-команди"])
        logger.info(f"Added admin button for admin {user_id}")
    
    kb = ReplyKeyboardMarkup(kb_rows, resize_keyboard=True)
    await update.message.reply_text("🔙 Повернувся до звичайного меню.", reply_markup=kb)

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
    # Кожен профіль — окремим повідомленням з кнопками дій (тільки для адмінів)
    for p in results:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚫 Обмежити доступ (вигнати)", callback_data=f"admin_kick_{p['telegram_id']}"),
            ],
            [
                InlineKeyboardButton("⚠️ Догана", callback_data=f"admin_warn_{p['telegram_id']}")
            ]
        ])
        await update.message.reply_text(_format_profile(p), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

async def handle_admin_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка адмінських дій з /find: вигнати з групи або підготувати догану."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Немає доступу.")
        return
    data = query.data
    if data.startswith("admin_kick_"):
        target_id = int(data.split("_")[2])
        chat_id = REPORTS_CHAT_ID
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_id)
            await query.edit_message_text(f"🚫 Користувача {target_id} вигнано з групи.")
            try:
                log_action(
                    actor_id=query.from_user.id,
                    actor_username=query.from_user.username,
                    action="kick_from_group",
                    target_user_id=target_id,
                    target_username=None,
                    details=f"chat_id={chat_id}",
                )
            except Exception:
                pass
        except Exception as e:
            await query.edit_message_text(f"⚠️ Не вдалося вигнати користувача {target_id}: {e}")
    elif data.startswith("admin_warn_"):
        target_id = int(data.split("_")[2])
        # Отримаємо профіль, щоб підставити Ім'я та, за наявності, звання
        prof = get_profile(target_id)
        disp = None
        if prof:
            disp = display_ranked_name(prof.get('rank'), prof.get('to_whom') or prof.get('full_name_tg') or '')
        if disp:
            context.user_data["dogana_prefill_to"] = disp
            await query.edit_message_text(
                "⚠️ Підготовка догани\n\n"
                f"За замовчуванням: <code>{disp}</code>\n"
                "Натисніть /dogana, і на кроці 'Порушник' введіть 'за замовчуванням' або вкажіть інше ім'я.",
                parse_mode="HTML"
            )
            try:
                log_action(
                    actor_id=query.from_user.id,
                    actor_username=query.from_user.username,
                    action="dogana_prefill_set",
                    target_user_id=target_id,
                    target_username=prof.get('username') if prof else None,
                    details=f"prefill={disp}",
                )
            except Exception:
                pass
        else:
            await query.edit_message_text(
                "⚠️ Не знайдено профіль для префілу. Натисніть /dogana та вкажіть ім'я вручну.")

async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показати власний профіль."""
    user = update.effective_user
    profile = get_profile(user.id)
    if not profile:
        await update.message.reply_text("❌ Ваш профіль ще не створено. Почніть з /start.")
        return

    # Отримуємо зображення
    image_file_ids = get_profile_images(user.id)

    text = (
        f"👤 <b>ВАШ ПРОФІЛЬ</b>\n\n"
        f"<b>Ім'я в Telegram:</b> {profile.get('full_name_tg', 'не вказано')}\n"
        f"<b>Username:</b> @{profile.get('username', 'немає')}\n"
        f"<b>ID:</b> <code>{user.id}</code>\n\n"
        f"<b>Ім'я в грі:</b> {profile.get('in_game_name', 'не вказано')}\n"
        f"<b>Звання:</b> {profile.get('rank', 'не вказано')}\n"
        f"<b>Підрозділ:</b> {profile.get('npu_department', 'не вказано')}\n\n"
        f"<b>Роль:</b> {profile.get('role', 'user')}\n"
        f"<b>Створено:</b> {profile.get('created_at', 'N/A')[:19]}\n"
        f"<b>Оновлено:</b> {profile.get('updated_at', 'N/A')[:19]}\n"
    )
    
    await update.message.reply_text(text, parse_mode="HTML")

    if image_file_ids:
        await update.message.reply_text("<b>Збережені фото:</b>", parse_mode="HTML")
        for i, file_id in enumerate(image_file_ids):
            try:
                await context.bot.send_photo(
                    chat_id=user.id,
                    photo=file_id,
                    caption=f"Фото {i+1}"
                )
            except Exception as e:
                logger.error(f"Failed to send profile photo {file_id} for user {user.id}: {e}")
    else:
        await update.message.reply_text("<i>Збережених фото немає.</i>", parse_mode="HTML")


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/user <id|@username> — показати профіль користувача."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /user <id|@username>")
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