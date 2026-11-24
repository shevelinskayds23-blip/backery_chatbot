import os
import sqlite3
import logging
from datetime import datetime, timedelta

# Путь к БД можно переопределить (например, для Docker-тома): DB_PATH=/data/bakery_bot.db
DB_NAME = os.getenv("DB_PATH", "bakery_bot.db")

# Лимит заказов на дату по умолчанию (ФТ-1)
DEFAULT_CAPACITY = 3


def _add_column(cur, table, ddl):
    """Безопасно добавляет колонку, если её ещё нет"""
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError:
        pass


def init_db():
    """Создаёт таблицы, если их нет, и добавляет новые поля"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Таблица товаров
    cur.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            description TEXT,
            prepTime TEXT,
            icon TEXT
        )
    ''')
    _add_column(cur, "products", "is_addon INTEGER DEFAULT 0")  # ФТ-6

    # Таблица пользователей
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT
        )
    ''')
    _add_column(cur, "users", "consent INTEGER DEFAULT 0")     # согласие на обработку ПД
    _add_column(cur, "users", "subscribed INTEGER DEFAULT 0")  # ФТ-7

    # Таблица корзин
    cur.execute('''
        CREATE TABLE IF NOT EXISTS cart (
            user_id INTEGER,
            product_id INTEGER,
            quantity INTEGER,
            added_at TEXT,
            PRIMARY KEY (user_id, product_id)
        )
    ''')

    # Таблица заказов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            total INTEGER,
            status TEXT,
            delivery_date TEXT,
            delivery_time TEXT,
            payment_method TEXT,
            comment TEXT,
            created_at TEXT
        )
    ''')
    _add_column(cur, "orders", "payment_status TEXT DEFAULT 'не оплачен'")  # ФТ-8
    _add_column(cur, "orders", "reminder_sent INTEGER DEFAULT 0")           # ФТ-5

    # Таблица позиций заказа
    cur.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            product_id INTEGER,
            product_name TEXT,
            quantity INTEGER,
            price INTEGER,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        )
    ''')

    # Таблица администраторов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    ''')

    # Лимиты заказов по датам (ФТ-1)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS capacity (
            date TEXT PRIMARY KEY,
            max_orders INTEGER NOT NULL
        )
    ''')
    # Журнал событий (ФТ-2, ФТ-3)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT NOT NULL,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Оценки заказов (ФТ-9)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS ratings (
            order_id TEXT PRIMARY KEY,
            score INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    logging.info("База данных инициализирована")


# ============ ТОВАРЫ ============

def load_products_from_db():
    """Загружает товары из БД"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM products ORDER BY id")
    products = [dict(row) for row in cur.fetchall()]
    conn.close()
    return products


def save_products_to_db(products):
    """Сохраняет товары в БД"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM products")
    for p in products:
        cur.execute('''
            INSERT INTO products (id, name, price, description, prepTime, icon, is_addon)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (p['id'], p['name'], p['price'], p['description'], p['prepTime'], p['icon'],
              int(p.get('is_addon') or 0)))
    conn.commit()
    conn.close()


# ============ ПОЛЬЗОВАТЕЛИ ============

def upsert_user(user_id, username, first_name):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO users (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,
                                           first_name = excluded.first_name
    ''', (user_id, username, first_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def has_consent(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT consent FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


def set_consent(user_id, value=1):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE users SET consent = ? WHERE user_id = ?", (int(value), user_id))
    conn.commit()
    conn.close()


def is_subscribed(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT subscribed FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


def set_subscribed(user_id, value=1):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE users SET subscribed = ? WHERE user_id = ?", (int(value), user_id))
    conn.commit()
    conn.close()


def get_subscribers():
    """Пользователи, давшие согласие и подписавшиеся на рассылки (ФТ-7)"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE subscribed = 1 AND consent = 1")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ============ КОРЗИНА ============

def add_to_cart(user_id, product_id, quantity=1):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO cart (user_id, product_id, quantity, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, product_id) DO UPDATE SET quantity = quantity + ?
    ''', (user_id, product_id, quantity, datetime.now().isoformat(), quantity))
    conn.commit()
    conn.close()


def get_cart(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        SELECT c.product_id, c.quantity, p.name, p.price, p.icon
        FROM cart c
        JOIN products p ON c.product_id = p.id
        WHERE c.user_id = ?
    ''', (user_id,))
    cart_items = [{
        'id': row['product_id'],
        'name': row['name'],
        'price': row['price'],
        'quantity': row['quantity'],
        'icon': row['icon']
    } for row in cur.fetchall()]
    conn.close()
    return cart_items


def clear_cart(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ============ ЗАКАЗЫ ============

def create_order(user_id, cart_items, total, details):
    order_id = f"#{datetime.now().strftime('%Y%m%d%H%M%S')}"
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO orders (order_id, user_id, total, status, delivery_date, delivery_time, payment_method, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (order_id, user_id, total, 'new', details.get('date', ''), details.get('time', ''),
          details.get('payment', ''), details.get('comment', ''), datetime.now().isoformat()))

    for item in cart_items:
        cur.execute('''
            INSERT INTO order_items (order_id, product_id, product_name, quantity, price)
            VALUES (?, ?, ?, ?, ?)
        ''', (order_id, item['id'], item['name'], item['quantity'], item['price']))

    conn.commit()
    conn.close()
    return order_id


def get_order(order_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_order_items(order_id):
    """Позиции заказа (только те товары, которые ещё есть в каталоге) — для повтора заказа"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
        SELECT oi.product_id, oi.quantity
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
    ''', (order_id,))
    rows = cur.fetchall()
    conn.close()
    return [{'product_id': r[0], 'quantity': r[1]} for r in rows]


def get_user_orders(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        SELECT order_id, total, status, delivery_date, created_at
        FROM orders
        WHERE user_id = ?
        ORDER BY created_at DESC
    ''', (user_id,))
    orders = [dict(row) for row in cur.fetchall()]
    conn.close()
    return orders


def get_all_orders(status=None):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if status:
        cur.execute('SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC', (status,))
    else:
        cur.execute('SELECT * FROM orders ORDER BY created_at DESC')
    rows = cur.fetchall()
    orders = []
    for row in rows:
        order = dict(row)
        cur2 = conn.cursor()
        cur2.execute('SELECT product_name, quantity, price FROM order_items WHERE order_id = ?', (order['order_id'],))
        items = cur2.fetchall()
        order['items'] = [f"{item[0]} ({item[1]} шт.)" for item in items]
        orders.append({'user_id': order['user_id'], 'order': order})
    conn.close()
    return orders


def update_order_status(order_id, new_status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ? WHERE order_id = ?", (new_status, order_id))
    conn.commit()
    conn.close()


def update_payment_status(order_id, new_status):
    """ФТ-8: статус оплаты заказа"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET payment_status = ? WHERE order_id = ?", (new_status, order_id))
    conn.commit()
    conn.close()


# ============ АДМИНИСТРАТОРЫ ============

def add_admin(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def get_admins():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admins")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


def is_admin(user_id):
    return user_id in get_admins()


# ============ РАСШИРЕННАЯ ВЕРСИЯ ============

def log_event(user_id, event, details=''):
    """ФТ-2: журнал событий"""
    conn = sqlite3.connect(DB_NAME)
    conn.execute('INSERT INTO events (user_id, event, details) VALUES (?, ?, ?)',
                 (user_id, event, details))
    conn.commit()
    conn.close()


# --- ФТ-1: учёт загрузки ---

def is_date_available(date_str, default_limit=DEFAULT_CAPACITY):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT max_orders FROM capacity WHERE date = ?', (date_str,))
    row = cur.fetchone()
    limit = row[0] if row else default_limit

    cur.execute('SELECT COUNT(*) FROM orders WHERE delivery_date = ? AND status != ?',
                (date_str, 'cancelled'))
    busy = cur.fetchone()[0]
    conn.close()
    return busy < limit


def set_capacity(date_str, max_orders):
    conn = sqlite3.connect(DB_NAME)
    conn.execute('''
        INSERT INTO capacity (date, max_orders) VALUES (?, ?)
        ON CONFLICT(date) DO UPDATE SET max_orders = excluded.max_orders
    ''', (date_str, max_orders))
    conn.commit()
    conn.close()


def get_capacities(limit=10):
    """Заданные вручную лимиты (последние добавленные)"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT date, max_orders FROM capacity ORDER BY rowid DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- ФТ-5: напоминания ---

def get_orders_for_reminder(date_str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
        SELECT order_id, user_id, delivery_time FROM orders
        WHERE delivery_date = ? AND reminder_sent = 0
          AND status NOT IN ('cancelled', 'delivered')
    ''', (date_str,))
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_reminder_sent(order_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('UPDATE orders SET reminder_sent = 1 WHERE order_id = ?', (order_id,))
    conn.commit()
    conn.close()


# --- ФТ-9: оценки ---

def save_rating(order_id, score):
    conn = sqlite3.connect(DB_NAME)
    conn.execute('INSERT OR REPLACE INTO ratings (order_id, score) VALUES (?, ?)', (order_id, score))
    conn.commit()
    conn.close()


def has_rating(order_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM ratings WHERE order_id = ?', (order_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


# --- ФТ-3: аналитика ---

def get_stats(days=None):
    """
    Аналитика за период (days дней) или за всё время (days=None).
    created_at сравнивается по дате (YYYY-MM-DD) — оба формата (isoformat и CURRENT_TIMESTAMP)
    начинаются с неё.
    """
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d') if days else '0000-00-00'
    not_admin = "user_id NOT IN (SELECT user_id FROM admins)"

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Обращения: уникальные пользователи в журнале событий (без администраторов)
    cur.execute(f"SELECT COUNT(DISTINCT user_id) FROM events WHERE created_at >= ? AND {not_admin}", (since,))
    visitors = cur.fetchone()[0]

    # Пользователи, дошедшие до оформления заказа
    cur.execute(f"""SELECT COUNT(DISTINCT user_id) FROM events
                    WHERE event = 'checkout_finish' AND created_at >= ? AND {not_admin}""", (since,))
    buyers_events = cur.fetchone()[0]

    # Заказы
    cur.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (since,))
    orders_total = cur.fetchone()[0]

    cur.execute("""SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders
                   WHERE created_at >= ? AND status != 'cancelled'""", (since,))
    orders_active, revenue = cur.fetchone()

    cur.execute("SELECT status, COUNT(*) FROM orders WHERE created_at >= ? GROUP BY status", (since,))
    by_status = {row[0]: row[1] for row in cur.fetchall()}

    # Популярные позиции
    cur.execute('''
        SELECT oi.product_name, SUM(oi.quantity) AS qty
        FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        WHERE o.created_at >= ? AND o.status != 'cancelled'
        GROUP BY oi.product_name
        ORDER BY qty DESC
        LIMIT 5
    ''', (since,))
    top_items = cur.fetchall()

    # Доля повторных клиентов: среди покупателей периода — те, у кого 2+ заказа за всё время
    cur.execute("""SELECT COUNT(DISTINCT user_id) FROM orders
                   WHERE created_at >= ? AND status != 'cancelled'""", (since,))
    buyers = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT user_id FROM orders WHERE status != 'cancelled' GROUP BY user_id HAVING COUNT(*) > 1
        ) WHERE user_id IN (
            SELECT user_id FROM orders WHERE created_at >= ? AND status != 'cancelled'
        )
    """, (since,))
    repeat_buyers = cur.fetchone()[0]

    # Оценки
    cur.execute('''
        SELECT AVG(r.score), COUNT(*) FROM ratings r
        JOIN orders o ON o.order_id = r.order_id
        WHERE o.created_at >= ?
    ''', (since,))
    avg_rating, ratings_count = cur.fetchone()

    conn.close()

    return {
        'visitors': visitors,
        'orders_total': orders_total,
        'orders_active': orders_active,
        'revenue': revenue,
        'avg_check': round(revenue / orders_active) if orders_active else 0,
        'conversion': round(min(buyers_events, visitors) / visitors * 100, 1) if visitors else 0,
        'by_status': by_status,
        'top_items': top_items,
        'buyers': buyers,
        'repeat_share': round(repeat_buyers / buyers * 100, 1) if buyers else 0,
        'avg_rating': round(avg_rating, 2) if avg_rating else None,
        'ratings_count': ratings_count,
    }