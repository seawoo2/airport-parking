import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

with psycopg.connect(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=os.getenv("POSTGRES_PORT", "5432"),
    dbname=os.environ["POSTGRES_DB"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, version()")
        database, user, version = cursor.fetchone()

print(f"연결 성공: database={database}, user={user}")
print(version)