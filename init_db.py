#!/usr/bin/env python3
"""
Скрипт инициализации базы данных для Railway.
Создает все необходимые таблицы.
"""

import os
import sys
import logging

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from db import init_db
    
    logger.info("Starting database initialization...")
    init_db()
    logger.info("Database initialization completed successfully!")
    
except ImportError as e:
    logger.error(f"Failed to import db module: {e}")
    sys.exit(1)
except Exception as e:
    logger.error(f"Database initialization failed: {e}")
    sys.exit(1)