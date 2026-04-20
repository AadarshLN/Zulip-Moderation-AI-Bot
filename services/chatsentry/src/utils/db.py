import logging

import psycopg2

from .config import config

logger = logging.getLogger(__name__)


def get_db_connection():
    conn = psycopg2.connect(config.DATABASE_URL)
    logger.info("PostgreSQL connection established")
    return conn
