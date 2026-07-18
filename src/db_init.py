import psycopg2
import sys

# Конфигурация подключения (используйте учетную запись суперадмина для создания таблиц)
DB_CONFIG = {
    "host": "127.0.0.1",
    "database": "clinic",
    "user": "postgres",
    "password": "postgres",
    "port": "5435",
    "sslmode": "disable"
}

SCHEMA_FILE = "../database/init_schema.sql"
TEST_DATA_FILE = "../database/init_schema.sql"


def run_sql_file(conn, filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        sql_commands = f.read()

    if not sql_commands.strip():
        print(f"Файл {filepath} пуст, пропускаем.")
        return

    cursor = conn.cursor()
    try:
        cursor.execute(sql_commands)
        print(f"Файл {filepath} успешно выполнен.")
    except psycopg2.errors.Error as e:
        print(f"Ошибка при выполнении {filepath}: {e}")
        conn.rollback()
        raise
    finally:
        cursor.close()


def main():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        print(f"Успешное подключение к БД")
    except Exception as e:
        print(f"Ошибка подключения к БД: {e}")
        sys.exit(1)

    try:
        run_sql_file(conn, SCHEMA_FILE)
        print("Развёртывание базы данных завершено.")
        run_sql_file(conn, TEST_DATA_FILE)
        print("Успешное заполнение БД тестовыми данными.")
    except Exception as e:
        print(f"[Ошибка] Не удалось инициализировать базу данных: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
