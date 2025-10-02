
# Telegram бот для доступу до групи поліції UKRAINE GTA

## Налаштування

1. Встановіть залежності:
```bash
pip install -r requirements.txt
```

2. Замініть `YOUR_BOT_TOKEN` на ваш токен бота

3. Запустіть бота:
```bash
python bot.py
```

## Функціонал

- Підтвердження членства в групі поліції UKRAINE GTA
- Надання доступу до приватної групи
- Перевірка статусу користувача

## Вимоги

- Python 3.6+
- aiogram 3.x
- asyncio

## Інструкція

1. Створіть нового бота через @BotFather
2. Додайте бота до групи поліції як адміністратора
3. Встановіть необхідні права для бота
4. Замініть `GROUP_ID` на ID групи поліції
5. Запустіть скрипт

## Код бота

```python
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
import asyncio

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Токен вашого бота
BOT_TOKEN = "7652276422:AAGC-z7Joic3m7cFKXVdafvKvaqTZ3VZsBo"

# ID групи поліції (потрібно замінити на реальний ID)
POLICE_GROUP_ID = -1001234567890

# Створення об'єктів бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_command(message: Message):
    await message.answer("Привіт! Я бот для надання доступу до групи поліції UKRAINE GTA.\n"
                         "Для отримання доступу введіть команду /access")

@dp.message(Command("access"))
async def access_command(message: Message):
    user_id = message.from_user.id
    
    try:
        # Перевірка членства в групі поліції
        member = await bot.get_chat_member(POLICE_GROUP_ID, user_id)
        
        if member.status in ['member', 'administrator', 'creator']:
            # Користувач є членом групи
            await message.answer("Ви успішно підтвердили членство в групі поліції!\n"
                                 "Вам надано доступ до приватної групи.")
            
            # Тут можна додати код для надання доступу до інших груп/каналів
            # Наприклад, додавання користувача до приватної групи
            
        else:
            await message.answer("Ви не є членом групи поліції UKRAINE GTA.\n"
                                 "Будь ласка, приєднайтеся до групи та спробуйте ще раз.")
    
    except Exception as e:
        logging.error(f"Помилка перевірки членства: {e}")
        await message.answer("Сталася помилка під час перевірки вашого членства.\n"
                             "Спробуйте ще раз або зверніться до адміністратора.")

if __name__ == "__main__":
    # Запуск бота
    asyncio.run(dp.start_polling(bot))
```

## Потенційні покращення

1. Додати перевірку на чинність членства (не тільки наявність)
2. Реалізувати систему прав доступу
3. Додати логування дій користувачів
4. Впровадити систему відстеження активності
5. Додати можливість відкликання доступу

## Безпека

- Не розголошуйте токен бота
- Використовуйте SSL для захисту даних
- Регулярно оновлюйте бота
- Обмежте права бота до мінімально необхідних

## Підтримка

Якщо у вас виникнуть питання або проблеми з роботою бота, звертайтесь до адміністрації.
