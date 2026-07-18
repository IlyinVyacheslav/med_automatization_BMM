# med_automatization_BMM
репозиторий для разработки сервиса автоматизации медицинских процессов в крупной многопрофильной медицинской сети

# локальный запуск
1. поднять БД: `docker run --name clinic-db -e POSTGRES_USER=postgres -e POSTGRES_DB=clinic -e POSTGRES_PASSWORD=postgres -p 5435:5432 -d postgres:16`
2. запустить db_init.py

# запуск через Docker
## тестирование
Проверяется корректность составленных запросов к БД, права доступа и многопоточность. Тесты вынесены в отдельный сервис tests под профилем test, поэтому при обычном  `docker compose up --build` они не запустятся.
Запуск на общей сети вместе с БД: `docker compose --profile test up --build tests`