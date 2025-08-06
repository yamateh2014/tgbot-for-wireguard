import sqlite3
import logging
from datetime import datetime
from dateutil.relativedelta import relativedelta

# Убираем глобальный DB_FILE

# Настройка логгирования (лучше делать в основном файле, но можно и здесь для модуля)
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def init_db(db_path: str):
    """Инициализирует БД по указанному пути, создает таблицу, если её нет."""
    try:
        # Убедимся, что директория существует (если db_path включает директорию)
        import os
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logging.info(f"Создана директория для БД: {db_dir}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Схема: name - уникальный ключ, expiry_date - текст (ISO), status - текст
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                name TEXT PRIMARY KEY,
                expiry_date TEXT,
                status TEXT CHECK(status IN ('enabled', 'disabled')) DEFAULT 'enabled',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Можно добавить индексы для ускорения поиска, если клиентов много
        # cursor.execute("CREATE INDEX IF NOT EXISTS idx_clients_name ON clients (name);")
        conn.commit()
        logging.info(f"База данных '{db_path}' успешно инициализирована/проверена.")
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при инициализации '{db_path}': {e}")
        raise  # Пробрасываем ошибку выше
    except OSError as e:
        logging.error(f"Ошибка ОС при создании директории/файла БД '{db_path}': {e}")
        raise
    finally:
        if conn:
            conn.close()

def save_client(db_path: str, name: str, expiry_date_str: str | None) -> bool:
    """
    Сохраняет нового клиента или заменяет существующего (из-за PRIMARY KEY).
    Использует переданную строку expiry_date_str (может быть None).
    Устанавливает статус 'enabled'. Возвращает True при успехе.
    """
    # Расчет даты убран отсюда, она передается готовой строкой
    status = "enabled" # Всегда enabled при создании/замене

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # INSERT OR REPLACE заменит строку, если name уже существует
        cursor.execute("INSERT OR REPLACE INTO clients (name, expiry_date, status) VALUES (?, ?, ?)",
                       (name, expiry_date_str, status)) # Передаем expiry_date_str
        conn.commit()
        logging.info(f"Клиент '{name}' сохранен/заменен в '{db_path}'. Срок: {expiry_date_str or 'не указан'}, Статус: {status}")
        return True
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при сохранении '{name}' в '{db_path}': {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()



def get_all_clients(db_path: str) -> list:
    """Возвращает список кортежей (name, expiry_date, status) всех клиентов."""
    clients = []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Выбираем только нужные столбцы
        cursor.execute("SELECT name, expiry_date, status FROM clients ORDER BY name")
        clients = cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при получении всех клиентов из '{db_path}': {e}")
        # Возвращаем пустой список или можно пробросить ошибку
    finally:
        if conn: conn.close()
    return clients

def get_client_by_name(db_path: str, name: str) -> tuple | None:
    """Возвращает кортеж (name, expiry_date, status) для клиента по имени или None."""
    row = None
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Исправлена опечатка 'c lients' -> 'clients'
        cursor.execute("SELECT name, expiry_date, status FROM clients WHERE name = ?", (name,))
        row = cursor.fetchone()
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при получении '{name}' из '{db_path}': {e}")
    finally:
        if conn: conn.close()
    return row

def delete_client_from_db(db_path: str, name: str) -> bool:
    """Удаляет клиента по имени. Возвращает True, если строка была удалена."""
    deleted = False
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM clients WHERE name = ?", (name,))
        conn.commit()
        # cursor.rowcount показывает количество измененных/удаленных строк
        if cursor.rowcount > 0:
            logging.info(f"Клиент '{name}' удален из '{db_path}'.")
            deleted = True
        else:
             logging.info(f"Клиент '{name}' не найден в '{db_path}' для удаления.")
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при удалении '{name}' из '{db_path}': {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()
    return deleted

def update_client_status(db_path: str, name: str, status: str) -> bool:
    """Обновляет статус клиента. Возвращает True, если строка была обновлена."""
    if status not in ['enabled', 'disabled']:
        logging.error(f"Попытка установить неверный статус '{status}' для '{name}' в '{db_path}'")
        return False
    updated = False
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE clients SET status = ? WHERE name = ?", (status, name))
        conn.commit()
        if cursor.rowcount > 0:
            logging.info(f"Статус клиента '{name}' обновлен на '{status}' в '{db_path}'.")
            updated = True
        else:
             logging.info(f"Клиент '{name}' не найден в '{db_path}' для обновления статуса.")
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при обновлении статуса '{name}' в '{db_path}': {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()
    return updated

def extend_client(db_path: str, name: str, months: int) -> bool:
    """Продлевает срок действия клиента. Возвращает True при успехе."""
    if not isinstance(months, int) or months <= 0:
        logging.error(f"Некорректный срок продления '{months}' для {name} в {db_path}")
        return False

    conn = None
    success = False
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT expiry_date FROM clients WHERE name = ?", (name,))
        row = cursor.fetchone()
        if row and row[0]: # Проверяем, что клиент найден и дата существует
            try:
                # Пытаемся распарсить дату из БД
                current_expiry = datetime.fromisoformat(row[0])
                # Добавляем месяцы
                new_expiry = current_expiry + relativedelta(months=months)
                new_expiry_str = new_expiry.isoformat(timespec='microseconds')
                # Обновляем в БД
                cursor.execute("UPDATE clients SET expiry_date = ? WHERE name = ?", (new_expiry_str, name))
                conn.commit()
                if cursor.rowcount > 0:
                    logging.info(f"Срок клиента '{name}' в '{db_path}' продлен до '{new_expiry_str}'.")
                    success = True
                else:
                    # Это не должно произойти, если select прошел успешно, но на всякий случай
                    logging.warning(f"Не удалось обновить срок для '{name}' в '{db_path}' после SELECT.")
            except ValueError as date_err:
                logging.error(f"Не удалось распарсить дату '{row[0]}' для '{name}' в '{db_path}': {date_err}")
            except Exception as calc_err:
                 logging.error(f"Не удалось рассчитать новую дату для '{name}' в '{db_path}': {calc_err}")
        elif row:
             logging.warning(f"У клиента '{name}' в '{db_path}' пустая дата (expiry_date is NULL). Продление невозможно.")
        else:
            logging.warning(f"Клиент '{name}' не найден в '{db_path}' для продления.")
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при продлении '{name}' в '{db_path}': {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()
    return success

# Функция get_expired_clients() также должна принимать db_path, если она используется
def get_expired_clients(db_path: str) -> list:
    """Возвращает список имен клиентов с истекшим сроком."""
    expired = []
    conn = None
    try:
        now = datetime.now().isoformat(timespec='microseconds')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Сравниваем строки ISO формата
        cursor.execute("SELECT name FROM clients WHERE expiry_date IS NOT NULL AND expiry_date <= ?", (now,))
        rows = cursor.fetchall()
        expired = [row[0] for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Ошибка SQLite при поиске истекших клиентов в '{db_path}': {e}")
    finally:
        if conn: conn.close()
    return expired
