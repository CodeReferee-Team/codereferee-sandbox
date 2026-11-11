import os
import time

from fastapi import FastAPI, HTTPException
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field


app = FastAPI()
PREFIX = os.getenv('API_PREFIX', '/api')


class Item(BaseModel):
    label: str = Field(min_length=1, max_length=128)


def connect():
    return psycopg.connect(host=os.environ['DB_HOST'], port=int(os.getenv('DB_PORT', '5432')),
        user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'],
        connect_timeout=1, row_factory=dict_row)


@app.on_event('startup')
def prepare_database():
    for attempt in range(60):
        try:
            with connect() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS items (id SERIAL PRIMARY KEY, label TEXT NOT NULL)')
            return
        except psycopg.OperationalError:
            if attempt == 59:
                raise
            time.sleep(.5)


@app.get('/health')
def health():
    with connect() as connection:
        connection.execute('SELECT 1')
    return {'ready': True}


@app.post(PREFIX + '/items', status_code=201)
def create(item: Item):
    with connect() as connection:
        row = connection.execute('INSERT INTO items (label) VALUES (%s) RETURNING id, label', (item.label,)).fetchone()
    return {**row, 'saved': True}


@app.get(PREFIX + '/items')
def listing():
    with connect() as connection:
        rows = connection.execute('SELECT id, label FROM items ORDER BY id LIMIT 100').fetchall()
    return {'items': rows}


@app.get(PREFIX + '/items/{item_id}')
def detail(item_id: int):
    with connect() as connection:
        row = connection.execute('SELECT id, label FROM items WHERE id=%s', (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    return {**row, 'saved': True}
