import os
import asyncio
import logging
import database as db
from datetime import datetime, timedelta
import datetime as dt
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from dotenv import load_dotenv

# Загружаем конфигурацию
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_ID', '')
TZ = ZoneInfo(os.getenv('TZ_NAME', 'Europe/Minsk'))              # часовой пояс для напоминаний
RATING_DELAY_MIN = int(os.getenv('RATING_DELAY_MIN', '60'))      # через сколько минут просить оценку (ФТ-9)
WORK_START = dt.time(10, 0)
WORK_END = dt.time(20, 0)

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Состояния
(
    GETTING_ADDRESS,
    GETTING_DATE,
    GETTING_TIME,
    GETTING_COMMENT,
    CHOOSING_PAYMENT,
    ADMIN_ADDING_PRODUCT_NAME,
    ADMIN_ADDING_PRODUCT_PRICE,
    ADMIN_ADDING_PRODUCT_DESC,
    ADMIN_ADDING_PRODUCT_TIME,
    ADMIN_EDITING_PRODUCT,
    GETTING_RATING,
    GETTING_CONSENT,
    GETTING_ADMIN_MSG,
    ADMIN_SET_CAPACITY,
    ADMIN_BROADCAST
) = range(15)

# Глобальная переменная для товаров
PRODUCTS = []

# Статусы заказов
ORDER_STATUSES = {
    "new": "🆕 Новый",
    "processing": "🔄 В обработке",
    "preparing": "👨‍🍳 Готовится",
    "ready": "✅ Готов к выдаче",
    "delivered": "🎁 Выдан",
    "cancelled": "❌ Отменен"
}

# Статусы оплаты (ФТ-8)
PAYMENT_STATUSES = ["не оплачен", "предоплата", "оплачен"]

# Хранилище данных
user_order_details = {}

CONSENT_TEXT = (
    "🔒 Согласие на обработку персональных данных\n\n"
    "Для оформления заказов бот хранит ваш Telegram ID, имя, username, "
    "историю заказов и действий в боте. Данные используются только для "
    "выполнения заказов и связи с вами.\n\n"
    "Нажимая «Согласен», вы даёте согласие на обработку этих данных."
)

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def is_admin(user_id: int) -> bool:
    if db.is_admin(user_id):
        return True
    if ADMIN_ID:
        admin_ids = [int(id.strip()) for id in ADMIN_ID.split(',') if id.strip().isdigit()]
        return user_id in admin_ids
    return False

def get_admin_ids():
    """Все администраторы: из .env и из таблицы admins"""
    ids = set(db.get_admins())
    for x in ADMIN_ID.split(','):
        if x.strip().isdigit():
            ids.add(int(x.strip()))
    return list(ids)

def ends_conv(handler):
    """Оборачивает обработчик так, чтобы он завершал активный ConversationHandler"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await handler(update, context)
        return ConversationHandler.END
    return wrapper

async def safe_edit(query, text, reply_markup=None):
    """edit_message_text, который не падает при «message is not modified»"""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            raise

def get_orders_by_status(status_filter=None):
    return db.get_all_orders(status_filter)

def format_order_for_admin(order_data, show_user_info=True):
    order = order_data['order']
    user_id = order_data['user_id']

    text = (
        f"📦 Заказ: {order['order_id']}\n"
        f"📅 Дата: {order['created_at']}\n"
        f"💰 Сумма: {order['total']} ₽\n"
        f"📊 Статус: {ORDER_STATUSES.get(order['status'], order['status'])}\n"
        f"💳 Оплата: {order.get('payment_status') or 'не оплачен'}\n"
    )

    if show_user_info:
        text += f"👤 Пользователь: ID {user_id}\n"

    text += f"\n📝 Состав:\n"
    for item in order['items']:
        text += f"• {item}\n"

    if order.get('delivery_date'):
        text += f"\n📅 Дата получения: {order['delivery_date']}\n"
    if order.get('delivery_time'):
        text += f"⏰ Время: {order['delivery_time']}\n"
    if order.get('payment_method'):
        text += f"💳 Способ: {order['payment_method']}\n"
    if order.get('comment'):
        text += f"💬 Комментарий: {order['comment']}\n"

    return text

def format_product_for_admin(product):
    return (
        f"{product['icon']} {product['name']}\n"
        f"💰 Цена: {product['price']} ₽\n"
        f"📝 Описание: {product['description']}\n"
        f"⏱ Изготовление: {product['prepTime']}\n"
        f"🎁 Сопутствующий товар: {'да' if product.get('is_addon') else 'нет'}\n"
        f"🆔 ID: {product['id']}"
    )

def get_available_addons(user_id):
    """ФТ-6: сопутствующие позиции, которых ещё нет в корзине"""
    in_cart = {i['id'] for i in db.get_cart(user_id)}
    return [p for p in PRODUCTS if p.get('is_addon') and p['id'] not in in_cart]

# ============ КЛАВИАТУРЫ ============

def get_welcome_keyboard(user_id=None):
    keyboard = [
        [InlineKeyboardButton("🍰 Посмотреть меню", callback_data='catalog')],
        [InlineKeyboardButton("🎁 Акции", callback_data='promotions')],
        [InlineKeyboardButton("📦 Мои заказы", callback_data='order_history')],
        [InlineKeyboardButton("❓ FAQ", callback_data='faq')],
        [InlineKeyboardButton("💬 Связаться с владельцем", callback_data='contact')],
    ]
    if user_id is not None:
        sub_text = "🔔 Рассылки: включены" if db.is_subscribed(user_id) else "🔕 Рассылки: выключены"
        keyboard.append([InlineKeyboardButton(sub_text, callback_data='toggle_sub')])
    return InlineKeyboardMarkup(keyboard)

def get_catalog_keyboard(user_id):
    keyboard = []
    for product in PRODUCTS:
        if product.get('is_addon'):
            continue  # сопутствующие товары предлагаются только в корзине
        keyboard.append([
            InlineKeyboardButton(
                f"{product.get('icon', '🍰')} {product['name']} - {product['price']} ₽",
                callback_data=f"product_{product['id']}"
            )
        ])

    cart_count = len(db.get_cart(user_id))
    cart_text = f"🛒 Корзина ({cart_count})" if cart_count > 0 else "🛒 Корзина"

    keyboard.append([
        InlineKeyboardButton(cart_text, callback_data='cart'),
        InlineKeyboardButton("🏠 Главная", callback_data='welcome')
    ])
    return InlineKeyboardMarkup(keyboard)

def get_product_keyboard(user_id):
    cart_count = len(db.get_cart(user_id))
    cart_text = f"🛒 Корзина ({cart_count})" if cart_count > 0 else "🛒 Корзина"

    keyboard = [
        [InlineKeyboardButton("➕ Добавить в корзину", callback_data='add_to_cart')],
        [
            InlineKeyboardButton(cart_text, callback_data='cart'),
            InlineKeyboardButton("◀️ Назад к меню", callback_data='catalog')
        ],
        [InlineKeyboardButton("🏠 Главная", callback_data='welcome')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cart_keyboard(user_id):
    cart_items = db.get_cart(user_id)
    if cart_items:
        keyboard = []
        # ФТ-6: допродажи
        for addon in get_available_addons(user_id):
            keyboard.append([InlineKeyboardButton(
                f"{addon.get('icon', '🎁')} {addon['name']} (+{addon['price']} ₽)",
                callback_data=f"addon_{addon['id']}"
            )])
        keyboard += [
            [InlineKeyboardButton("✅ Оформить заказ", callback_data='checkout_start')],
            [InlineKeyboardButton("🗑 Очистить корзину", callback_data='clear_cart')],
            [InlineKeyboardButton("🍰 Продолжить покупки", callback_data='catalog')],
            [InlineKeyboardButton("🏠 Главная", callback_data='welcome')]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("🍰 Посмотреть меню", callback_data='catalog')],
            [InlineKeyboardButton("🏠 Главная", callback_data='welcome')]
        ]
    return InlineKeyboardMarkup(keyboard)

def get_checkout_keyboard(step):
    if step == "date":
        keyboard = [
            [InlineKeyboardButton("📅 Указать дату", callback_data='set_date')],
            [InlineKeyboardButton("⏰ Указать время", callback_data='set_time')],
            [InlineKeyboardButton("💬 Добавить комментарий", callback_data='set_comment')],
            [InlineKeyboardButton("💳 Выбрать способ оплаты", callback_data='choose_payment')],
            [InlineKeyboardButton("◀️ Назад в корзину", callback_data='cart')]
        ]
    elif step == "payment":
        keyboard = [
            [
                InlineKeyboardButton("💳 Картой онлайн", callback_data='payment_card'),
                InlineKeyboardButton("💵 Наличными", callback_data='payment_cash')
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data='checkout_start')]
        ]
    elif step == "confirm":
        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить заказ", callback_data='confirm_order')],
            [InlineKeyboardButton("❌ Отменить", callback_data='cart')]
        ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📦 Новые заказы", callback_data='admin_new_orders')],
        [InlineKeyboardButton("📊 Все заказы", callback_data='admin_all_orders')],
        [InlineKeyboardButton("🍰 Управление товарами", callback_data='admin_products')],
        [InlineKeyboardButton("📅 Лимиты по датам", callback_data='admin_capacity')],
        [InlineKeyboardButton("📢 Рассылка", callback_data='admin_broadcast')],
        [InlineKeyboardButton("📈 Аналитика", callback_data='admin_stats')],
        [InlineKeyboardButton("◀️ Выйти из админки", callback_data='welcome')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_orders_keyboard(orders, current_index=0):
    keyboard = []
    if orders:
        order_data = orders[current_index]
        order = order_data['order']

        # ФТ-8: смена статуса оплаты по кругу (не оплачен → предоплата → оплачен)
        pay_status = order.get('payment_status') or PAYMENT_STATUSES[0]
        cur_idx = PAYMENT_STATUSES.index(pay_status) if pay_status in PAYMENT_STATUSES else 0
        next_idx = (cur_idx + 1) % len(PAYMENT_STATUSES)
        keyboard.append([InlineKeyboardButton(
            f"💰 Оплата → {PAYMENT_STATUSES[next_idx]}",
            callback_data=f"admin_pay_{order['order_id']}_{next_idx}"
        )])

        status_buttons = []
        for status_key, status_text in ORDER_STATUSES.items():
            if status_key != order['status']:
                status_buttons.append(
                    InlineKeyboardButton(
                        status_text,
                        callback_data=f"admin_set_status_{order['order_id']}_{status_key}"
                    )
                )

        for i in range(0, len(status_buttons), 2):
            keyboard.append(status_buttons[i:i+2])

        nav_buttons = []
        if current_index > 0:
            nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"admin_order_{current_index-1}"))
        nav_buttons.append(InlineKeyboardButton("📋 Список", callback_data='admin_all_orders'))
        if current_index < len(orders) - 1:
            nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"admin_order_{current_index+1}"))

        if nav_buttons:
            keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🏠 Админ-панель", callback_data='admin_panel')])
    return InlineKeyboardMarkup(keyboard)

def get_admin_products_keyboard():
    keyboard = []
    for product in PRODUCTS:
        mark = "🎁 " if product.get('is_addon') else ""
        keyboard.append([
            InlineKeyboardButton(
                f"✏️ {mark}{product['name'][:20]}...",
                callback_data=f"admin_edit_product_{product['id']}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton("➕ Добавить товар", callback_data='admin_add_product'),
        InlineKeyboardButton("🗑 Удалить товар", callback_data='admin_delete_product')
    ])
    keyboard.append([InlineKeyboardButton("🏠 Админ-панель", callback_data='admin_panel')])
    return InlineKeyboardMarkup(keyboard)

def get_admin_edit_product_keyboard(product_id):
    product = next((p for p in PRODUCTS if p['id'] == product_id), {})
    addon_text = "🎁 Сопутствующий: да (снять)" if product.get('is_addon') else "🎁 Сделать сопутствующим"
    keyboard = [
        [
            InlineKeyboardButton("📝 Название", callback_data=f"admin_change_name_{product_id}"),
            InlineKeyboardButton("💰 Цену", callback_data=f"admin_change_price_{product_id}")
        ],
        [
            InlineKeyboardButton("📄 Описание", callback_data=f"admin_change_desc_{product_id}"),
            InlineKeyboardButton("⏱ Время", callback_data=f"admin_change_time_{product_id}")
        ],
        [InlineKeyboardButton(addon_text, callback_data=f"admin_toggle_addon_{product_id}")],
        [
            InlineKeyboardButton("✅ Сохранить", callback_data='admin_products'),
            InlineKeyboardButton("❌ Удалить", callback_data=f"admin_confirm_delete_{product_id}")
        ],
        [InlineKeyboardButton("◀️ Назад к товарам", callback_data='admin_products')]
    ]
    return InlineKeyboardMarkup(keyboard)

# ============ ОСНОВНЫЕ ОБРАБОТЧИКИ (клиентские) ============

def welcome_text(first_name):
    return f"🍰 Привет! Меня зовут Елена Климашевская, {first_name}!\n\nЗдесь вы можете оформить заказ"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    db.upsert_user(user_id, user.username, user.first_name)

    if is_admin(user_id):
        await show_admin_panel(update, context)
        return

    db.log_event(user_id, "start")

    # Согласие на обработку персональных данных
    if not db.has_consent(user_id):
        await update.message.reply_text(
            CONSENT_TEXT,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Согласен", callback_data='consent_yes'),
                InlineKeyboardButton("❌ Не согласен", callback_data='consent_no')
            ]])
        )
        return

    await update.message.reply_text(
        welcome_text(user.first_name),
        reply_markup=get_welcome_keyboard(user_id)
    )

async def handle_consent_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    db.upsert_user(user.id, user.username, user.first_name)
    db.set_consent(user.id, 1)
    db.log_event(user.id, "consent_given")
    await query.edit_message_text(
        "Спасибо! 🙏\n\nХотите получать сообщения об акциях и новинках? "
        "Отписаться можно в любой момент на главном экране.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Да, хочу", callback_data='sub_yes'),
            InlineKeyboardButton("🔕 Нет", callback_data='sub_no')
        ]])
    )

async def handle_consent_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Без согласия на обработку данных оформить заказ нельзя.\n"
        "Если передумаете — отправьте /start."
    )

async def handle_subscribe_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    db.set_subscribed(user.id, 1 if query.data == 'sub_yes' else 0)
    await query.edit_message_text(welcome_text(user.first_name), reply_markup=get_welcome_keyboard(user.id))

async def handle_toggle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    new_value = 0 if db.is_subscribed(user.id) else 1
    db.set_subscribed(user.id, new_value)
    await query.answer("Рассылки включены" if new_value else "Рассылки выключены")
    await query.edit_message_text(welcome_text(user.first_name), reply_markup=get_welcome_keyboard(user.id))

async def handle_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    db.log_event(user_id, "view_catalog")
    await query.edit_message_text(
        "🍰 Наше меню:\n\nВыберите товар:",
        reply_markup=get_catalog_keyboard(user_id)
    )

async def handle_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split('_')[1])
    product = next((p for p in PRODUCTS if p['id'] == product_id), None)

    if product:
        context.user_data['selected_product'] = product
        product_text = (
            f"{product['name']}\n\n"
            f"💰 Цена: {product['price']} ₽\n"
            f"📝 {product['description']}\n\n"
            f"⏱ Изготовление: {product['prepTime']}"
        )
        await query.edit_message_text(
            product_text,
            reply_markup=get_product_keyboard(query.from_user.id)
        )

async def handle_add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    product = context.user_data.get('selected_product')

    if product:
        db.add_to_cart(user_id, product['id'])
        db.log_event(user_id, "add_to_cart", str(product['id']))
        cart_count = len(db.get_cart(user_id))
        await query.edit_message_text(
            f"✅ '{product['name']}' добавлен в корзину!\n\nТоваров в корзине: {cart_count}",
            reply_markup=get_product_keyboard(user_id)
        )

async def render_cart(query, user_id):
    cart_items = db.get_cart(user_id)

    if not cart_items:
        cart_text = "🛒 Ваша корзина пуста.\n\nДавайте выберем что-нибудь вкусное!"
    else:
        items_text = []
        total = 0
        for item in cart_items:
            item_total = item['price'] * item['quantity']
            total += item_total
            items_text.append(f"{item['name']}\n{item['quantity']} шт. × {item['price']} ₽ = {item_total} ₽")

        cart_text = f"🛒 Ваша корзина:\n\n{chr(10).join(items_text)}\n\n💰 Итого: {total} ₽"
        if get_available_addons(user_id):
            cart_text += "\n\n🎁 Дополните заказ:"

    await safe_edit(query, cart_text, get_cart_keyboard(user_id))

async def handle_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await render_cart(query, query.from_user.id)

async def handle_addon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ФТ-6: добавление сопутствующей позиции из корзины (сумма пересчитывается при показе корзины)"""
    query = update.callback_query
    user_id = query.from_user.id
    product_id = int(query.data.split('_')[1])
    product = next((p for p in PRODUCTS if p['id'] == product_id and p.get('is_addon')), None)
    if not product:
        await query.answer("Позиция недоступна", show_alert=True)
        return
    db.add_to_cart(user_id, product_id)
    db.log_event(user_id, "add_addon", str(product_id))
    await query.answer(f"Добавлено: {product['name']}")
    await render_cart(query, user_id)

async def handle_clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    db.clear_cart(user_id)
    await query.edit_message_text("🗑 Корзина очищена!", reply_markup=get_cart_keyboard(user_id))

async def handle_checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cart_items = db.get_cart(user_id)

    if not cart_items:
        await query.edit_message_text("Ваша корзина пуста!", reply_markup=get_cart_keyboard(user_id))
        return

    # Не затираем уже введённые данные при возврате на этот шаг
    if user_id not in user_order_details:
        user_order_details[user_id] = {'date': '', 'time': '', 'comment': '', 'payment': ''}
        db.log_event(user_id, "checkout_start")
    total = sum(item['price'] * item['quantity'] for item in cart_items)

    checkout_text = (
        f"📦 Оформление заказа\n\n"
        f"Товаров: {len(cart_items)}\n"
        f"Сумма: {total} ₽\n\n"
        f"Шаг 1 из 4: Укажите дату получения"
    )
    await query.edit_message_text(checkout_text, reply_markup=get_checkout_keyboard("date"))

async def handle_set_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📅 Введите дату получения (например: 25.12.2026):\n\nФормат: ДД.ММ.ГГГГ",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='checkout_start')]])
    )
    context.user_data['awaiting_date'] = True
    return GETTING_DATE

async def handle_set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⏰ Введите время получения (например: 14:00 или 16:30):\n\nФормат: ЧЧ:ММ (от 10:00 до 20:00)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='checkout_start')]])
    )
    context.user_data['awaiting_time'] = True
    return GETTING_TIME

async def handle_set_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💬 Введите комментарий к заказу (необязательно):\n\nНапример: 'Без орехов' или 'Позвонить за час'",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data='checkout_start')]])
    )
    context.user_data['awaiting_comment'] = True
    return GETTING_COMMENT

async def handle_choose_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    details = user_order_details.get(user_id, {})

    if not details.get('date') or not details.get('time'):
        await query.answer("Сначала укажите дату и время получения", show_alert=True)
        return
    await query.answer()

    cart_items = db.get_cart(user_id)
    total = sum(item['price'] * item['quantity'] for item in cart_items)

    summary_text = f"📋 Сводка заказа:\n\n"
    for item in cart_items:
        summary_text += f"{item['name']} - {item['quantity']} шт.\n"
    summary_text += f"\n💰 Сумма: {total} ₽\n"

    if details.get('date'):
        summary_text += f"📅 Дата: {details['date']}\n"
    if details.get('time'):
        summary_text += f"⏰ Время: {details['time']}\n"
    if details.get('comment'):
        summary_text += f"💬 Комментарий: {details['comment']}\n"

    summary_text += f"\n💳 Выберите способ оплаты:"
    await query.edit_message_text(summary_text, reply_markup=get_checkout_keyboard("payment"))

async def handle_payment_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_order_details.setdefault(user_id, {})['payment'] = 'Картой онлайн'
    await show_order_confirmation(update, context, user_id)

async def handle_payment_cash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_order_details.setdefault(user_id, {})['payment'] = 'Наличными при получении'
    await show_order_confirmation(update, context, user_id)

async def show_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    details = user_order_details.get(user_id, {})
    cart_items = db.get_cart(user_id)
    total = sum(item['price'] * item['quantity'] for item in cart_items)

    confirmation_text = f"✅ Заказ готов к оформлению!\n\n📦 Состав заказа:\n"
    for item in cart_items:
        confirmation_text += f"• {item['name']} - {item['quantity']} шт.\n"

    confirmation_text += (
        f"\n💰 Сумма: {total} ₽\n"
        f"📅 Дата: {details.get('date', 'не указана')}\n"
        f"⏰ Время: {details.get('time', 'не указано')}\n"
        f"💳 Оплата: {details.get('payment', 'не выбрана')}\n"
    )
    if details.get('comment'):
        confirmation_text += f"💬 Комментарий: {details['comment']}\n"

    confirmation_text += "\nПодтвердить заказ?"
    await query.edit_message_text(confirmation_text, reply_markup=get_checkout_keyboard("confirm"))

async def handle_confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cart_items = db.get_cart(user_id)

    if not cart_items:
        await query.edit_message_text("❌ Ошибка: корзина пуста!", reply_markup=get_cart_keyboard(user_id))
        return

    details = user_order_details.get(user_id, {})

    # ФТ-1: повторная проверка доступности даты перед созданием заказа
    if not details.get('date') or not db.is_date_available(details['date']):
        await query.edit_message_text(
            "❌ На выбранную дату уже нет свободных мест (или дата не указана).\nПожалуйста, выберите другую дату.",
            reply_markup=get_checkout_keyboard("date")
        )
        return

    total = sum(item['price'] * item['quantity'] for item in cart_items)

    order_id = db.create_order(user_id, cart_items, total, details)
    db.log_event(user_id, "checkout_finish", order_id)

    db.clear_cart(user_id)
    if user_id in user_order_details:
        del user_order_details[user_id]

    await notify_admin_new_order(context, order_id, total)

    success_text = (
        f"🎉 Заказ успешно оформлен!\n\n"
        f"📦 Номер заказа: {order_id}\n"
        f"💰 Сумма: {total} ₽\n"
        f"📅 Дата получения: {details.get('date', 'не указана')}\n"
        f"⏰ Время получения: {details.get('time', 'не указано')}\n"
        f"💳 Способ оплаты: {details.get('payment', 'не выбран')}\n\n"
        f"Мы свяжемся с вами для подтверждения заказа!\n\n"
        f"👤 Администратор: @moonylupin\n"
        f"📞 Телефон: +375 (33) 142 1425"
    )
    await query.edit_message_text(
        success_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Мои заказы", callback_data='order_history')],
            [InlineKeyboardButton("🍰 Новый заказ", callback_data='catalog')],
            [InlineKeyboardButton("🏠 Главная", callback_data='welcome')]
        ])
    )

async def notify_admin_new_order(context: ContextTypes.DEFAULT_TYPE, order_id, total):
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🆕 Новый заказ!\n\n📦 ID: {order_id}\n💰 Сумма: {total} ₽\n\n/admin - для управления"
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления админа {admin_id}: {e}")

async def render_history(query, user_id):
    orders = db.get_user_orders(user_id)
    keyboard = []

    if not orders:
        history_text = "📦 У вас пока нет заказов.\n\nСделайте первый заказ!"
    else:
        history_text = "📦 Ваши заказы:\n\n"
        for order in orders[:5]:
            status_text = ORDER_STATUSES.get(order['status'], order['status'])
            history_text += (
                f"Заказ {order['order_id']}\n"
                f"📅 {order['created_at']}\n"
                f"💰 {order['total']} ₽\n"
                f"📊 {status_text}\n"
                f"━━━━━━━━━━━━\n"
            )
            # ФТ-7: повтор заказа; отмена доступна, пока заказ новый
            keyboard.append([InlineKeyboardButton(f"🔁 Повторить {order['order_id']}",
                                                  callback_data=f"repeat_{order['order_id']}")])
            if order['status'] == 'new':
                keyboard.append([InlineKeyboardButton(f"❌ Отменить {order['order_id']}",
                                                      callback_data=f"cancel_{order['order_id']}")])

    keyboard.append([InlineKeyboardButton("🍰 Новый заказ", callback_data='catalog')])
    keyboard.append([InlineKeyboardButton("🏠 Главная", callback_data='welcome')])
    await safe_edit(query, history_text, InlineKeyboardMarkup(keyboard))

async def handle_order_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await render_history(query, query.from_user.id)

async def handle_repeat_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ФТ-7: копирует позиции прежнего заказа в корзину"""
    query = update.callback_query
    user_id = query.from_user.id
    order_id = query.data.split('_', 1)[1]
    order = db.get_order(order_id)

    if not order or order['user_id'] != user_id:
        await query.answer("Заказ не найден", show_alert=True)
        return

    items = db.get_order_items(order_id)
    if not items:
        await query.answer("Товары из этого заказа больше недоступны", show_alert=True)
        return

    for item in items:
        db.add_to_cart(user_id, item['product_id'], item['quantity'])
    db.log_event(user_id, "repeat_order", order_id)
    await query.answer("Позиции добавлены в корзину")
    await render_cart(query, user_id)

async def handle_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена клиентом заказа в статусе «Новый» (ФТ-2: событие order_cancel)"""
    query = update.callback_query
    user_id = query.from_user.id
    order_id = query.data.split('_', 1)[1]
    order = db.get_order(order_id)

    if not order or order['user_id'] != user_id:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if order['status'] != 'new':
        await query.answer("Заказ уже в работе. Свяжитесь с владельцем.", show_alert=True)
        return

    db.update_order_status(order_id, 'cancelled')
    db.log_event(user_id, "order_cancel", order_id)
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"❌ Клиент отменил заказ {order_id}")
        except Exception as e:
            logging.error(f"Ошибка уведомления админа {admin_id}: {e}")
    await query.answer("Заказ отменён")
    await render_history(query, user_id)

async def handle_promotions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    promo_text = (
        "🎁 Актуальные акции:\n\n"
        "🎂 Торт в подарок!\nПри заказе от 200 руб. дарю торт на выбор.\nДействует до: 31.12.2026\n\n"
        "⭐ Скидка 10% на первый заказ\nДля новых клиентов\nДействует всегда!\n\n"
        "👨‍👩‍👧‍👦 Семейная скидка 15%\nПри заказе от 3 позиций\nДействует до: 25.12.2026"
    )
    keyboard = [
        [InlineKeyboardButton("🍰 Посмотреть меню", callback_data='catalog')],
        [InlineKeyboardButton("🏠 Главная", callback_data='welcome')]
    ]
    await query.edit_message_text(promo_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    faq_text = (
        "❔ Часто задаваемые вопросы:\n\n"
        "❓ Как оформить заказ?\n💡 Выберите товары в меню → добавьте в корзину → оформите заказ\n\n"
        "❓ За сколько времени рекомендуется делать заказ?\n💡 В зависимости от сложности заказа, но в основном допустимо сделать заказ за 2-3 дня\n\n"
        "❓ Есть ли доставка?\n💡 К сожалению нет, только самовывоз\n\n"
        "❓ Как оплатить заказ?\n💡 Картой онлайн или наличными при получении\n\n"
        "❓ Можно ли изменить заказ?\n💡 Да, до начала приготовления. Свяжитесь с нами"
    )
    keyboard = [
        [InlineKeyboardButton("💬 Задать вопрос", callback_data='contact')],
        [InlineKeyboardButton("🍰 Посмотреть меню", callback_data='catalog')],
        [InlineKeyboardButton("🏠 Главная", callback_data='welcome')]
    ]
    await query.edit_message_text(faq_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ФТ-4: передача диалога владельцу"""
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_admin_msg'] = True
    await query.edit_message_text(
        "💬 Напишите ваш вопрос или пожелание (например, индивидуальный дизайн) — я перешлю его владельцу:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='welcome')]])
    )
    return GETTING_ADMIN_MSG

async def handle_admin_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    admin_ids = get_admin_ids()
    sent = 0

    username = f"@{user.username}" if user.username else "без username"
    for aid in admin_ids:
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=(f"💬 Сообщение от клиента\n"
                      f"👤 {user.first_name} ({username})\n"
                      f"🆔 ID: {user.id}\n\n{text}")
            )
            sent += 1
        except Exception as e:
            logging.error(f"Не удалось переслать сообщение админу {aid}: {e}")

    db.log_event(user.id, "contact_owner")
    if sent:
        await update.message.reply_text(
            "✅ Сообщение отправлено владельцу! Он свяжется с вами.",
            reply_markup=get_welcome_keyboard(user.id)
        )
    else:
        await update.message.reply_text(
            "⚠️ Не удалось передать сообщение. Напишите, пожалуйста, администратору: @moonylupin",
            reply_markup=get_welcome_keyboard(user.id)
        )
    context.user_data['awaiting_admin_msg'] = False
    return ConversationHandler.END

async def handle_back_to_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    await query.edit_message_text(
        f"🍰 Привет! Меня зовут Елена Климашевская {user.first_name}!\n\nЗдесь вы можете оформить заказ.",
        reply_markup=get_welcome_keyboard(user.id)
    )

# ============ ФТ-9: ОЦЕНКА ЗАКАЗА ============

def schedule_rating_request(context: ContextTypes.DEFAULT_TYPE, order_id, user_id):
    if context.job_queue is None:
        logging.warning("JobQueue недоступен: установите python-telegram-bot[job-queue]")
        return
    context.job_queue.run_once(
        ask_rating,
        when=RATING_DELAY_MIN * 60,
        data={'order_id': order_id, 'user_id': user_id},
        name=f"rating_{order_id}"
    )

async def ask_rating(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    order_id, user_id = data['order_id'], data['user_id']
    if db.has_rating(order_id):
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{n}⭐", callback_data=f"rate_{order_id}_{n}") for n in range(1, 6)
    ]])
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Спасибо, что выбрали нас! 💛\nОцените, пожалуйста, заказ {order_id} от 1 до 5:",
            reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка запроса оценки для {user_id}: {e}")

async def handle_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    order_id, score = parts[1], int(parts[2])
    order = db.get_order(order_id)

    if not order or order['user_id'] != query.from_user.id or not 1 <= score <= 5:
        await query.answer()
        return

    db.save_rating(order_id, score)
    db.log_event(query.from_user.id, "rating", f"{order_id}:{score}")
    await query.answer()
    await query.edit_message_text(f"Спасибо за оценку: {score}⭐! Нам очень важно ваше мнение 💛")

# ============ ОБРАБОТЧИКИ СООБЩЕНИЙ (Ввод) ============

async def handle_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    date_text = update.message.text.strip()

    try:
        date_obj = datetime.strptime(date_text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ\nПример: 25.12.2026\n\nПопробуйте еще раз:")
        return GETTING_DATE

    if date_obj < datetime.now(TZ).date():
        await update.message.reply_text("❌ Эта дата уже прошла. Введите будущую дату:")
        return GETTING_DATE

    date_text = date_obj.strftime('%d.%m.%Y')  # единый формат для проверки лимитов и напоминаний

    # ФТ-1: проверка загрузки
    if not db.is_date_available(date_text):
        await update.message.reply_text(
            f"❌ На дату {date_text} уже максимальное количество заказов.\n"
            f"Пожалуйста, выберите другую дату:"
        )
        return GETTING_DATE

    user_order_details.setdefault(user_id, {})['date'] = date_text

    await update.message.reply_text(
        f"✅ Дата сохранена: {date_text}\n\nПродолжайте оформление заказа:",
        reply_markup=get_checkout_keyboard("date")
    )
    context.user_data.pop('awaiting_date', None)
    return ConversationHandler.END

async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    time_text = update.message.text.strip()

    try:
        time_obj = datetime.strptime(time_text, '%H:%M').time()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат времени. Используйте ЧЧ:ММ\nПример: 14:00 или 16:30\n\nПопробуйте еще раз:")
        return GETTING_TIME

    if not WORK_START <= time_obj <= WORK_END:
        await update.message.reply_text("❌ Время получения — с 10:00 до 20:00. Попробуйте еще раз:")
        return GETTING_TIME

    time_text = time_obj.strftime('%H:%M')
    user_order_details.setdefault(user_id, {})['time'] = time_text
    await update.message.reply_text(
        f"✅ Время сохранено: {time_text}\n\nПродолжайте оформление заказа:",
        reply_markup=get_checkout_keyboard("date")
    )
    context.user_data.pop('awaiting_time', None)
    return ConversationHandler.END

async def handle_comment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    comment_text = update.message.text.strip()

    user_order_details.setdefault(user_id, {})['comment'] = comment_text

    await update.message.reply_text(
        f"✅ Комментарий сохранен.\n\nПродолжайте оформление заказа:",
        reply_markup=get_checkout_keyboard("date")
    )
    context.user_data.pop('awaiting_comment', None)
    return ConversationHandler.END

# ============ АДМИН-ОБРАБОТЧИКИ ============

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(update, Update) and update.message:
        user = update.effective_user
        message = update.message
    else:
        query = update.callback_query
        await query.answer()
        user = query.from_user
        message = query.message

    if not is_admin(user.id):
        await message.reply_text("❌ У вас нет прав доступа к админ-панели.")
        return

    await message.reply_text(
        f"👑 Админ-панель\n\nПривет, {user.first_name}!\nВыберите действие:",
        reply_markup=get_admin_main_keyboard()
    )

async def handle_admin_new_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    new_orders = get_orders_by_status('new')
    if not new_orders:
        await query.edit_message_text("📭 Новых заказов нет!", reply_markup=get_admin_main_keyboard())
        return
    await show_admin_order_detail(update, context, 0, new_orders)

async def handle_admin_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    all_orders = get_orders_by_status()
    if not all_orders:
        await query.edit_message_text("📭 Заказов еще нет!", reply_markup=get_admin_main_keyboard())
        return
    await show_admin_order_detail(update, context, 0, all_orders)

async def show_admin_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int, orders: list):
    query = update.callback_query
    order_data = orders[index]
    order_text = format_order_for_admin(order_data)
    order_text += f"\n📊 Заказ {index + 1} из {len(orders)}"

    await safe_edit(query, order_text, get_admin_orders_keyboard(orders, index))

async def show_order_card_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
    """Показывает карточку заказа после изменения статуса"""
    query = update.callback_query
    all_orders = get_orders_by_status()
    index = next((i for i, o in enumerate(all_orders) if o['order']['order_id'] == order_id), None)
    if index is None:
        await query.edit_message_text("Заказ не найден.", reply_markup=get_admin_main_keyboard())
        return
    await show_admin_order_detail(update, context, index, all_orders)

async def handle_admin_order_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    index = int(query.data.split('_')[2])
    all_orders = get_orders_by_status()
    if 0 <= index < len(all_orders):
        await show_admin_order_detail(update, context, index, all_orders)

async def handle_admin_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    parts = query.data.split('_')
    order_id = parts[3]
    new_status = parts[4]

    order = db.get_order(order_id)
    if not order:
        await query.edit_message_text("❌ Заказ не найден.", reply_markup=get_admin_main_keyboard())
        return
    old_status = order['status']

    db.update_order_status(order_id, new_status)

    if new_status == 'cancelled' and old_status != 'cancelled':
        db.log_event(order['user_id'], "order_cancel", order_id)
    # ФТ-9: после выдачи заказа — отложенный запрос оценки
    if new_status == 'delivered' and old_status != 'delivered':
        schedule_rating_request(context, order_id, order['user_id'])

    await show_order_card_by_id(update, context, order_id)

async def handle_admin_pay_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    parts = query.data.split('_')
    order_id = parts[2]
    new_pay = PAYMENT_STATUSES[int(parts[3])]

    db.update_payment_status(order_id, new_pay)
    await show_order_card_by_id(update, context, order_id)

async def handle_admin_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    products_text = "🍰 Управление товарами:\n\n"
    if PRODUCTS:
        for product in PRODUCTS:
            mark = " 🎁" if product.get('is_addon') else ""
            products_text += f"{product.get('icon', '🍰')} {product['name']} - {product['price']} ₽{mark}\n"
        products_text += "\n🎁 — сопутствующий товар (предлагается в корзине)\n"
    else:
        products_text += "📭 Товаров пока нет\n"

    products_text += "\nВыберите действие:"
    await query.edit_message_text(products_text, reply_markup=get_admin_products_keyboard())

async def show_admin_product(query, product_id):
    product = next((p for p in PRODUCTS if p['id'] == product_id), None)
    if not product:
        await query.edit_message_text("❌ Товар не найден!", reply_markup=get_admin_products_keyboard())
        return
    product_text = format_product_for_admin(product) + "\n\nВыберите что изменить:"
    await query.edit_message_text(product_text, reply_markup=get_admin_edit_product_keyboard(product_id))

async def handle_admin_edit_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await show_admin_product(query, int(query.data.split('_')[3]))

async def handle_admin_toggle_addon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ФТ-6: отметка товара как сопутствующего"""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    for product in PRODUCTS:
        if product['id'] == product_id:
            product['is_addon'] = 0 if product.get('is_addon') else 1
            db.save_products_to_db(PRODUCTS)
            break
    await show_admin_product(query, product_id)

async def handle_admin_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    await query.edit_message_text(
        "➕ Добавление нового товара\n\nВведите название товара (с эмодзи, например: 🎂 Торт 'Наполеон'):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='admin_products')]])
    )
    context.user_data['adding_product'] = True
    return ADMIN_ADDING_PRODUCT_NAME

async def handle_admin_change_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    context.user_data['editing_product_id'] = product_id
    context.user_data['editing_field'] = 'name'

    await query.edit_message_text(
        "📝 Введите новое название товара:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f'admin_edit_product_{product_id}')]])
    )
    return ADMIN_EDITING_PRODUCT

async def handle_admin_change_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    context.user_data['editing_product_id'] = product_id
    context.user_data['editing_field'] = 'price'

    await query.edit_message_text(
        "💰 Введите новую цену (только цифры, например: 1500):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f'admin_edit_product_{product_id}')]])
    )
    return ADMIN_EDITING_PRODUCT

async def handle_admin_change_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    context.user_data['editing_product_id'] = product_id
    context.user_data['editing_field'] = 'description'

    await query.edit_message_text(
        "📄 Введите новое описание товара:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f'admin_edit_product_{product_id}')]])
    )
    return ADMIN_EDITING_PRODUCT

async def handle_admin_change_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    context.user_data['editing_product_id'] = product_id
    context.user_data['editing_field'] = 'prepTime'

    await query.edit_message_text(
        "⏱ Введите новое время изготовления (например: 2-3 дня):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data=f'admin_edit_product_{product_id}')]])
    )
    return ADMIN_EDITING_PRODUCT

async def handle_admin_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    product = next((p for p in PRODUCTS if p['id'] == product_id), None)

    if product:
        await query.edit_message_text(
            f"🗑 Удалить товар?\n\n{product['name']}\nЦена: {product['price']} ₽\n\nЭто действие нельзя отменить!",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да, удалить", callback_data=f'admin_delete_now_{product_id}'),
                    InlineKeyboardButton("❌ Нет, отмена", callback_data=f'admin_edit_product_{product_id}')
                ]
            ])
        )

async def handle_admin_delete_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    product_id = int(query.data.split('_')[3])
    global PRODUCTS
    PRODUCTS = [p for p in PRODUCTS if p['id'] != product_id]
    db.save_products_to_db(PRODUCTS)

    await query.edit_message_text("✅ Товар удален!", reply_markup=get_admin_products_keyboard())

# --- ФТ-1: лимиты по датам ---

async def handle_admin_capacity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    text = f"📅 Лимиты заказов по датам\n\nЛимит по умолчанию: {db.DEFAULT_CAPACITY} заказа(ов) в день.\n"
    caps = db.get_capacities()
    if caps:
        text += "\nЗаданные вручную:\n" + "\n".join(f"• {d}: {n}" for d, n in caps) + "\n"
    text += "\nВведите дату и лимит через пробел, например:\n25.12.2026 5\n(0 — дата закрыта для заказов)"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='admin_panel')]])
    )
    return ADMIN_SET_CAPACITY

async def handle_admin_capacity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END

    parts = update.message.text.strip().split()
    try:
        if len(parts) != 2:
            raise ValueError
        date_text = datetime.strptime(parts[0], '%d.%m.%Y').strftime('%d.%m.%Y')
        limit = int(parts[1])
        if limit < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Формат: ДД.ММ.ГГГГ число\nПример: 25.12.2026 5\n\nПопробуйте еще раз:")
        return ADMIN_SET_CAPACITY

    db.set_capacity(date_text, limit)
    await update.message.reply_text(
        f"✅ Лимит на {date_text}: {limit}",
        reply_markup=get_admin_main_keyboard()
    )
    return ConversationHandler.END

# --- ФТ-7: рассылка подписчикам ---

async def handle_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    count = len(db.get_subscribers())
    await query.edit_message_text(
        f"📢 Рассылка\n\nПодписчиков (с согласием): {count}\n\nВведите текст сообщения для рассылки:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='admin_panel')]])
    )
    return ADMIN_BROADCAST

async def handle_admin_broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END

    text = update.message.text
    sent = failed = 0
    for uid in db.get_subscribers():
        try:
            await context.bot.send_message(chat_id=uid, text=f"🎁 {text}")
            sent += 1
        except Exception as e:
            failed += 1
            logging.error(f"Рассылка: ошибка для {uid}: {e}")
        await asyncio.sleep(0.05)  # ограничение Telegram по частоте отправки

    db.log_event(update.effective_user.id, "broadcast", f"sent={sent}, failed={failed}")
    await update.message.reply_text(
        f"✅ Рассылка завершена.\nДоставлено: {sent}\nОшибок: {failed}",
        reply_markup=get_admin_main_keyboard()
    )
    return ConversationHandler.END

# --- ФТ-3: аналитика ---

def stats_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 дней", callback_data='admin_stats_7'),
            InlineKeyboardButton("30 дней", callback_data='admin_stats_30'),
            InlineKeyboardButton("Всё время", callback_data='admin_stats_all')
        ],
        [InlineKeyboardButton("🏠 Админ-панель", callback_data='admin_panel')]
    ])

async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return

    suffix = query.data.replace('admin_stats', '').lstrip('_') or '30'
    days = None if suffix == 'all' else int(suffix)
    period_name = "за всё время" if days is None else f"за {days} дн."

    s = db.get_stats(days)

    text = f"📈 Аналитика {period_name}\n\n"
    text += f"👥 Обращений (уникальных пользователей): {s['visitors']}\n"
    text += f"📦 Заказов: {s['orders_total']} (отменено: {s['by_status'].get('cancelled', 0)})\n"
    text += f"🎯 Конверсия в заказ: {s['conversion']}%\n"
    text += f"💰 Выручка: {s['revenue']} ₽\n"
    text += f"🧾 Средний чек: {s['avg_check']} ₽\n"
    text += f"🔁 Доля повторных клиентов: {s['repeat_share']}%\n"
    if s['avg_rating'] is not None:
        text += f"⭐ Средняя оценка: {s['avg_rating']} (оценок: {s['ratings_count']})\n"

    if s['top_items']:
        text += "\n🏆 Популярные позиции:\n"
        for i, (name, qty) in enumerate(s['top_items'], 1):
            text += f"{i}. {name} — {qty} шт.\n"

    if s['by_status']:
        text += "\n📊 По статусам:\n"
        for status, count in s['by_status'].items():
            text += f"{ORDER_STATUSES.get(status, status)}: {count}\n"

    text += f"\n🍰 Позиций в каталоге: {len(PRODUCTS)}"

    await safe_edit(query, text, stats_keyboard())

# ============ АДМИН-ОБРАБОТЧИКИ СООБЩЕНИЙ ============

async def handle_admin_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END

    product_name = update.message.text.strip()
    context.user_data['new_product'] = {'name': product_name}

    await update.message.reply_text(
        f"✅ Название: {product_name}\n\n💰 Введите цену товара (только цифры, например: 1500):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='admin_products')]])
    )
    return ADMIN_ADDING_PRODUCT_PRICE

async def handle_admin_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    try:
        price = int(update.message.text.strip())
        context.user_data['new_product']['price'] = price

        await update.message.reply_text(
            f"✅ Цена: {price} ₽\n\n📄 Введите описание товара:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='admin_products')]])
        )
        return ADMIN_ADDING_PRODUCT_DESC
    except ValueError:
        await update.message.reply_text("❌ Цена должна быть числом!\nПопробуйте еще раз (например: 1500):")
        return ADMIN_ADDING_PRODUCT_PRICE

async def handle_admin_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END

    description = update.message.text.strip()
    context.user_data['new_product']['description'] = description

    await update.message.reply_text(
        f"✅ Описание: {description}\n\n⏱ Введите время изготовления (например: 2-3 дня):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data='admin_products')]])
    )
    return ADMIN_ADDING_PRODUCT_TIME

async def handle_admin_product_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END

    prep_time = update.message.text.strip()
    new_product = context.user_data.get('new_product', {})

    name = new_product.get('name', '')
    icon = '🎂'
    if 'капкейк' in name.lower(): icon = '🧁'
    elif 'пряник' in name.lower(): icon = '🍪'
    elif 'торт' in name.lower(): icon = '🎂'
    elif 'печенье' in name.lower(): icon = '🍪'

    product = {
        'id': max([p['id'] for p in PRODUCTS]) + 1 if PRODUCTS else 1,
        'name': new_product.get('name', 'Новый товар'),
        'price': new_product.get('price', 0),
        'description': new_product.get('description', ''),
        'prepTime': prep_time,
        'icon': icon,
        'is_addon': 0
    }

    PRODUCTS.append(product)
    db.save_products_to_db(PRODUCTS)

    if 'new_product' in context.user_data:
        del context.user_data['new_product']

    await update.message.reply_text(
        f"✅ Товар добавлен!\n\n{format_product_for_admin(product)}",
        reply_markup=get_admin_products_keyboard()
    )
    return ConversationHandler.END

async def handle_admin_editing_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END

    product_id = context.user_data.get('editing_product_id')
    field = context.user_data.get('editing_field')
    new_value = update.message.text.strip()

    if not product_id or not field:
        await update.message.reply_text("❌ Ошибка данных!")
        return ConversationHandler.END

    for product in PRODUCTS:
        if product['id'] == product_id:
            if field == 'price':
                try:
                    product[field] = int(new_value)
                except ValueError:
                    await update.message.reply_text("❌ Цена должна быть числом!\nПопробуйте еще раз:")
                    return ADMIN_EDITING_PRODUCT
            else:
                product[field] = new_value

            db.save_products_to_db(PRODUCTS)
            del context.user_data['editing_product_id']
            del context.user_data['editing_field']

            await update.message.reply_text(
                f"✅ Товар обновлен!\n\n{format_product_for_admin(product)}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад к товарам", callback_data='admin_products')]])
            )
            return ConversationHandler.END

    await update.message.reply_text("❌ Товар не найден!")
    return ConversationHandler.END

# ============ ФТ-5: НАПОМИНАНИЯ ============

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная задача: напоминание о заказах с датой получения «завтра»"""
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).strftime('%d.%m.%Y')
    for order_id, user_id, delivery_time in db.get_orders_for_reminder(tomorrow):
        time_part = f" в {delivery_time}" if delivery_time else ""
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🔔 Напоминаем: завтра{time_part} вы получаете заказ {order_id}."
            )
            db.mark_reminder_sent(order_id)
        except Exception as e:
            logging.error(f"Ошибка напоминания для {user_id}: {e}")

# ============ ОСНОВНАЯ ФУНКЦИЯ ============

def seed_addons():
    """Стартовый набор сопутствующих товаров (цены — примерные, меняются в админ-панели)"""
    global PRODUCTS
    if any(p.get('is_addon') for p in PRODUCTS):
        return
    next_id = max([p['id'] for p in PRODUCTS], default=0) + 1
    addons = [
        ("Свечи для торта", 3, "Набор праздничных свечей", "🕯"),
        ("Открытка с поздравлением", 5, "Подписанная открытка к заказу", "💌"),
        ("Топпер для торта", 8, "Декоративная надпись в торт", "🎀"),
    ]
    for i, (name, price, desc, icon) in enumerate(addons):
        PRODUCTS.append({
            "id": next_id + i, "name": name, "price": price, "description": desc,
            "prepTime": "в день заказа", "icon": icon, "is_addon": 1
        })
    db.save_products_to_db(PRODUCTS)

def main():
    """Запуск бота"""
    global PRODUCTS

    db.init_db()
    PRODUCTS = db.load_products_from_db()

    if not PRODUCTS:
        initial_products = [
            {"id": 1, "name": "Торт 'Чёрный лес'", "price": 42, "description": "Сливочно-творожный крем, Шоколадный бисквит", "prepTime": "1 день", "icon": "🎂"},
            {"id": 2, "name": "Торт 'Шоколадно-ореховый'", "price": 42, "description": "Ванильный бисквит, Кофейный крем", "prepTime": "1 день", "icon": "🎂"},
            {"id": 3, "name": "Торт 'Бабл-гам'", "price": 48, "description": "Клубничный бисквит, Банановый мусс", "prepTime": "2 дня", "icon": "🎂"},
            {"id": 4, "name": "Торт 'Шоколадный с клубникой'", "price": 48, "description": "Шоколадный бисквит, Клубничный мусс", "prepTime": "1 день", "icon": "🎂"},
            {"id": 5, "name": "Торт 'Медовик'", "price": 50, "description": "Медовые коржи, Сливочно-сметанный крем", "prepTime": "2 дня", "icon": "🎂"},
            {"id": 6, "name": "Капкейки ванильные с клубникой, 6 шт", "price": 20, "description": "Клубничная начинка, Сливочный крем", "prepTime": "2 дня", "icon": "🧁"},
            {"id": 7, "name": "Капкейки шоколадные с домашней карамелью, 6 шт", "price": 20, "description": "Нежный шоколадный бисквит, Солёная карамель", "prepTime": "2 дня", "icon": "🧁"},
            {"id": 8, "name": "Капкейки шоколадные с вишнёвой начинкой, 6 шт", "price": 20, "description": "Нежный шоколадный бисквит, Вишня", "prepTime": "2 дня", "icon": "🧁"}
        ]
        db.save_products_to_db(initial_products)
        PRODUCTS = initial_products

    seed_addons()

    for aid in ADMIN_ID.split(','):
        if aid.strip().isdigit():
            db.add_admin(int(aid.strip()))

    application = Application.builder().token(TOKEN).build()

    # Ежедневные напоминания (ФТ-5). Требуется: pip install "python-telegram-bot[job-queue]"
    if application.job_queue is None:
        logging.error("JobQueue недоступен — напоминания и запрос оценки работать не будут. "
                      "Установите python-telegram-bot[job-queue].")
    else:
        application.job_queue.run_daily(send_reminders, time=dt.time(hour=10, minute=0, tzinfo=TZ))

    client_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_set_date, pattern='^set_date$'),
            CallbackQueryHandler(handle_set_time, pattern='^set_time$'),
            CallbackQueryHandler(handle_set_comment, pattern='^set_comment$'),
            CallbackQueryHandler(handle_contact, pattern='^contact$')
        ],
        states={
            GETTING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_input)],
            GETTING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_input)],
            GETTING_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_comment_input)],
            GETTING_ADMIN_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message_input)],
        },
        # Любая навигационная кнопка или /start завершает ожидание ввода
        fallbacks=[
            CommandHandler('start', ends_conv(start)),
            CallbackQueryHandler(ends_conv(handle_checkout_start), pattern='^checkout_start$'),
            CallbackQueryHandler(ends_conv(handle_back_to_welcome), pattern='^welcome$'),
            CallbackQueryHandler(ends_conv(handle_cart), pattern='^cart$'),
            CallbackQueryHandler(ends_conv(handle_catalog), pattern='^catalog$'),
        ],
        allow_reentry=True
    )

    admin_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_admin_add_product, pattern='^admin_add_product$'),
            CallbackQueryHandler(handle_admin_change_name, pattern='^admin_change_name_'),
            CallbackQueryHandler(handle_admin_change_price, pattern='^admin_change_price_'),
            CallbackQueryHandler(handle_admin_change_desc, pattern='^admin_change_desc_'),
            CallbackQueryHandler(handle_admin_change_time, pattern='^admin_change_time_'),
            CallbackQueryHandler(handle_admin_capacity, pattern='^admin_capacity$'),
            CallbackQueryHandler(handle_admin_broadcast, pattern='^admin_broadcast$'),
        ],
        states={
            ADMIN_ADDING_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_product_name)],
            ADMIN_ADDING_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_product_price)],
            ADMIN_ADDING_PRODUCT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_product_desc)],
            ADMIN_ADDING_PRODUCT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_product_time)],
            ADMIN_EDITING_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_editing_product)],
            ADMIN_SET_CAPACITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_capacity_input)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_broadcast_input)],
        },
        fallbacks=[
            CommandHandler('admin', ends_conv(show_admin_panel)),
            CommandHandler('start', ends_conv(start)),
            CallbackQueryHandler(ends_conv(show_admin_panel), pattern='^admin_panel$'),
            CallbackQueryHandler(ends_conv(handle_admin_products), pattern='^admin_products$'),
            CallbackQueryHandler(ends_conv(handle_admin_edit_product), pattern='^admin_edit_product_'),
        ],
        allow_reentry=True
    )

    # Диалоги регистрируются первыми, чтобы их fallbacks имели приоритет над общими обработчиками
    application.add_handler(client_conv_handler)
    application.add_handler(admin_conv_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", show_admin_panel))

    # Согласие и подписка
    application.add_handler(CallbackQueryHandler(handle_consent_yes, pattern='^consent_yes$'))
    application.add_handler(CallbackQueryHandler(handle_consent_no, pattern='^consent_no$'))
    application.add_handler(CallbackQueryHandler(handle_subscribe_choice, pattern='^sub_(yes|no)$'))
    application.add_handler(CallbackQueryHandler(handle_toggle_subscription, pattern='^toggle_sub$'))

    # Клиентская часть
    application.add_handler(CallbackQueryHandler(handle_catalog, pattern='^catalog$'))
    application.add_handler(CallbackQueryHandler(handle_product, pattern='^product_'))
    application.add_handler(CallbackQueryHandler(handle_add_to_cart, pattern='^add_to_cart$'))
    application.add_handler(CallbackQueryHandler(handle_addon, pattern='^addon_'))
    application.add_handler(CallbackQueryHandler(handle_cart, pattern='^cart$'))
    application.add_handler(CallbackQueryHandler(handle_clear_cart, pattern='^clear_cart$'))
    application.add_handler(CallbackQueryHandler(handle_checkout_start, pattern='^checkout_start$'))
    application.add_handler(CallbackQueryHandler(handle_choose_payment, pattern='^choose_payment$'))
    application.add_handler(CallbackQueryHandler(handle_payment_card, pattern='^payment_card$'))
    application.add_handler(CallbackQueryHandler(handle_payment_cash, pattern='^payment_cash$'))
    application.add_handler(CallbackQueryHandler(handle_confirm_order, pattern='^confirm_order$'))
    application.add_handler(CallbackQueryHandler(handle_order_history, pattern='^order_history$'))
    application.add_handler(CallbackQueryHandler(handle_repeat_order, pattern='^repeat_'))
    application.add_handler(CallbackQueryHandler(handle_cancel_order, pattern='^cancel_'))
    application.add_handler(CallbackQueryHandler(handle_rating, pattern='^rate_'))
    application.add_handler(CallbackQueryHandler(handle_promotions, pattern='^promotions$'))
    application.add_handler(CallbackQueryHandler(handle_faq, pattern='^faq$'))
    application.add_handler(CallbackQueryHandler(handle_back_to_welcome, pattern='^welcome$'))

    # Админ-панель
    application.add_handler(CallbackQueryHandler(show_admin_panel, pattern='^admin_panel$'))
    application.add_handler(CallbackQueryHandler(handle_admin_new_orders, pattern='^admin_new_orders$'))
    application.add_handler(CallbackQueryHandler(handle_admin_all_orders, pattern='^admin_all_orders$'))
    application.add_handler(CallbackQueryHandler(handle_admin_order_nav, pattern='^admin_order_\\d+$'))
    application.add_handler(CallbackQueryHandler(handle_admin_set_status, pattern='^admin_set_status_'))
    application.add_handler(CallbackQueryHandler(handle_admin_pay_status, pattern='^admin_pay_'))
    application.add_handler(CallbackQueryHandler(handle_admin_products, pattern='^admin_products$'))
    application.add_handler(CallbackQueryHandler(handle_admin_edit_product, pattern='^admin_edit_product_'))
    application.add_handler(CallbackQueryHandler(handle_admin_toggle_addon, pattern='^admin_toggle_addon_'))
    application.add_handler(CallbackQueryHandler(handle_admin_confirm_delete, pattern='^admin_confirm_delete_'))
    application.add_handler(CallbackQueryHandler(handle_admin_delete_now, pattern='^admin_delete_now_'))
    application.add_handler(CallbackQueryHandler(handle_admin_stats, pattern='^admin_stats(_\\w+)?$'))

    print("✅ Бот 'Сладкие мечты' запущен!")
    print(f"👉 Админ ID: {ADMIN_ID or 'не настроен'}")
    print("👉 Клиенты: /start")
    print("👉 Админ: /admin")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
