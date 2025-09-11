#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Leaflow Auto Check-in Control Panel
Web-based management interface for the check-in system
"""

import os
import json
import sqlite3
import hashlib
import secrets
import threading
import schedule
import time
import re
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, make_response
from flask_cors import CORS
import jwt
import logging
from urllib.parse import urlparse, unquote
import random
import pytz
import hmac
import base64
import urllib.parse
import traceback

# Configuration
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', secrets.token_hex(32))
CORS(app, supports_credentials=True)

# Environment variables
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
PORT = int(os.getenv('PORT', '8181'))

# 设置时区为北京时间
TIMEZONE = pytz.timezone('Asia/Shanghai')

# Database configuration
def parse_mysql_dsn(dsn):
    """Parse MySQL DSN string"""
    try:
        parsed = urlparse(dsn)
        
        if parsed.scheme not in ['mysql', 'mysql+pymysql']:
            return None
            
        config = {
            'type': 'mysql',
            'host': parsed.hostname or 'localhost',
            'port': parsed.port or 3306,
            'database': parsed.path.lstrip('/') if parsed.path else 'leaflow_checkin',
            'password': unquote(parsed.password) if parsed.password else ''
        }
        
        username = unquote(parsed.username) if parsed.username else 'root'
        
        if '.' in username:
            username = username.split('.')[-1]
        
        config['user'] = username
        
        return config
    except Exception as e:
        logging.error(f"Error parsing MySQL DSN: {e}")
        return None

# Parse database configuration
MYSQL_DSN = os.getenv('MYSQL_DSN', '')
db_config = None

if MYSQL_DSN:
    db_config = parse_mysql_dsn(MYSQL_DSN)

if db_config:
    DB_TYPE = 'mysql'
    DB_HOST = db_config['host']
    DB_PORT = db_config['port']
    DB_NAME = db_config['database']
    DB_USER = db_config['user']
    DB_PASSWORD = db_config['password']
else:
    DB_TYPE = 'sqlite'
    DB_HOST = 'localhost'
    DB_PORT = 3306
    DB_NAME = 'leaflow_checkin'
    DB_USER = 'root'
    DB_PASSWORD = ''

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 账户缓存
class AccountCache:
    def __init__(self):
        self.cache = {}
        self.last_update = None
        self.cache_duration = 300  # 5分钟缓存
        self.lock = threading.Lock()
    
    def get_accounts(self, force_refresh=False):
        """获取缓存的账户列表"""
        with self.lock:
            now = time.time()
            if force_refresh or not self.last_update or (now - self.last_update) > self.cache_duration:
                return None
            return list(self.cache.values())  # 返回列表而不是字典
    
    def update_cache(self, accounts):
        """更新缓存"""
        with self.lock:
            self.cache = {acc['id']: acc for acc in accounts}
            self.last_update = time.time()
    
    def invalidate(self):
        """使缓存失效"""
        with self.lock:
            self.cache = {}
            self.last_update = None
    
    def refresh_from_db(self, db):
        """从数据库刷新缓存"""
        try:
            accounts_list = db.fetchall('SELECT * FROM accounts WHERE enabled = 1')
            if accounts_list:
                self.update_cache(accounts_list)
                logger.info(f"Account cache refreshed with {len(accounts_list)} accounts")
            else:
                self.invalidate()
        except Exception as e:
            logger.error(f"Error refreshing account cache: {e}")

account_cache = AccountCache()

# 通用数据缓存类
class DataCache:
    def __init__(self, cache_duration=300):
        self.cache = {}
        self.cache_duration = cache_duration
        self.lock = threading.Lock()
    
    def get(self, key):
        """获取缓存数据"""
        with self.lock:
            if key in self.cache:
                data, timestamp = self.cache[key]
                if time.time() - timestamp < self.cache_duration:
                    return data
                else:
                    del self.cache[key]
            return None
    
    def set(self, key, data):
        """设置缓存数据"""
        with self.lock:
            self.cache[key] = (data, time.time())
    
    def invalidate(self, key=None):
        """使缓存失效"""
        with self.lock:
            if key:
                self.cache.pop(key, None)
            else:
                self.cache.clear()
    
    def invalidate_pattern(self, pattern):
        """使匹配模式的缓存失效"""
        with self.lock:
            keys_to_remove = [k for k in self.cache.keys() if pattern in k]
            for key in keys_to_remove:
                self.cache.pop(key, None)

# 初始化数据缓存
data_cache = DataCache(cache_duration=60)  # 1分钟缓存

class Database:
    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None
        self.pool = None
        self.last_ping = time.time()
        self.last_actual_ping = time.time()  # 记录上次实际ping的时间
        self.ping_check_interval = 300  # 每5分钟检查一次
        self.ping_actual_interval = 1800  # 30分钟实际ping间隔
        self.db_type = None  # 初始化db_type
        self.connect()
        self.init_tables()
        # 启动保活线程
        self.start_keepalive()
    
    def start_keepalive(self):
        """启动MySQL保活线程"""
        if self.db_type == 'mysql':
            thread = threading.Thread(target=self._keepalive_worker, daemon=True)
            thread.start()
            logger.info("MySQL intelligent keepalive thread started")
    
    def _keepalive_worker(self):
        """智能保活工作线程"""
        while True:
            try:
                time.sleep(self.ping_check_interval)  # 每5分钟检查一次
                
                with self.lock:
                    if self.conn and self.db_type == 'mysql':
                        current_time = time.time()
                        # 只有在距离上次实际ping超过30分钟时才执行ping
                        if current_time - self.last_actual_ping >= self.ping_actual_interval:
                            try:
                                self.conn.ping(reconnect=True)
                                self.last_actual_ping = current_time
                                logger.debug(f"MySQL keepalive ping executed (30min interval)")
                            except Exception as e:
                                logger.error(f"MySQL ping failed, reconnecting: {e}")
                                self.reconnect()
                                self.last_actual_ping = current_time
                        else:
                            remaining = self.ping_actual_interval - (current_time - self.last_actual_ping)
                            logger.debug(f"Keepalive check: Next ping in {remaining:.0f} seconds")
                            
            except Exception as e:
                logger.error(f"Keepalive worker error: {e}")
    
    def _ensure_connection(self):
        """确保连接可用（智能ping）"""
        if self.db_type == 'mysql':
            current_time = time.time()
            # 如果距离上次ping超过30分钟，执行ping
            if current_time - self.last_actual_ping >= self.ping_actual_interval:
                try:
                    self.conn.ping(reconnect=True)
                    self.last_actual_ping = current_time
                    logger.debug("Connection ping on query execution")
                except Exception as e:
                    logger.error(f"Connection ping failed: {e}")
                    self.reconnect()
                    self.last_actual_ping = current_time
    
    def reconnect(self):
        """重新连接数据库"""
        try:
            if self.conn:
                try:
                    self.conn.close()
                except:
                    pass
            self.connect()
            # 清空所有缓存
            data_cache.invalidate()
            account_cache.invalidate()
            logger.info("Database reconnected successfully, cache cleared")
        except Exception as e:
            logger.error(f"Database reconnection failed: {e}")
            time.sleep(5)  # 等待5秒后重试
            self.reconnect()
    
    def connect(self):
        """Establish database connection with retry mechanism"""
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                if DB_TYPE == 'mysql':
                    import pymysql
                    logger.info(f"Connecting to MySQL: {DB_HOST}:{DB_PORT}/{DB_NAME} as {DB_USER} (attempt {attempt + 1}/{max_retries})")
                    self.conn = pymysql.connect(
                        host=DB_HOST,
                        port=DB_PORT,
                        user=DB_USER,
                        password=DB_PASSWORD,
                        database=DB_NAME,
                        charset='utf8mb4',
                        autocommit=True,
                        connect_timeout=10,
                        read_timeout=30,
                        write_timeout=30,
                        max_allowed_packet=64*1024*1024  # 64MB
                    )
                    self.db_type = 'mysql'
                    self.last_actual_ping = time.time()  # 记录连接时间
                    logger.info("Successfully connected to MySQL database")
                    return
                else:
                    logger.info("Using SQLite database")
                    os.makedirs('/app/data', exist_ok=True)
                    self.conn = sqlite3.connect('/app/data/leaflow_checkin.db', check_same_thread=False)
                    self.conn.row_factory = sqlite3.Row
                    self.db_type = 'sqlite'
                    logger.info("Successfully connected to SQLite database")
                    return
            except Exception as e:
                logger.error(f"Database connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    if DB_TYPE == 'mysql':
                        logger.info("Falling back to SQLite database")
                        os.makedirs('/app/data', exist_ok=True)
                        self.conn = sqlite3.connect('/app/data/leaflow_checkin.db', check_same_thread=False)
                        self.conn.row_factory = sqlite3.Row
                        self.db_type = 'sqlite'
                    else:
                        raise
    
    def init_tables(self):
        """Initialize database tables"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                
                if self.db_type == 'mysql':
                    # MySQL table creation
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS accounts (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            name VARCHAR(255) UNIQUE NOT NULL,
                            token_data TEXT NOT NULL,
                            enabled BOOLEAN DEFAULT TRUE,
                            checkin_time_start VARCHAR(5) DEFAULT '06:30',
                            checkin_time_end VARCHAR(5) DEFAULT '06:40',
                            check_interval INT DEFAULT 60,
                            retry_count INT DEFAULT 2,
                            last_checkin_date DATE DEFAULT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS checkin_history (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            account_id INT NOT NULL,
                            success BOOLEAN NOT NULL,
                            message TEXT,
                            checkin_date DATE NOT NULL,
                            retry_times INT DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
                            INDEX idx_checkin_date (checkin_date),
                            INDEX idx_account_date (account_id, checkin_date)
                        )
                    ''')
                    
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS notification_settings (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            enabled BOOLEAN DEFAULT FALSE,
                            telegram_enabled BOOLEAN DEFAULT FALSE,
                            telegram_bot_token VARCHAR(255) DEFAULT '',
                            telegram_user_id VARCHAR(255) DEFAULT '',
                            wechat_enabled BOOLEAN DEFAULT FALSE,
                            wechat_webhook_key VARCHAR(255) DEFAULT '',
                            wxpusher_enabled BOOLEAN DEFAULT FALSE,
                            wxpusher_app_token VARCHAR(255) DEFAULT '',
                            wxpusher_uid VARCHAR(255) DEFAULT '',
                            dingtalk_enabled BOOLEAN DEFAULT FALSE,
                            dingtalk_access_token VARCHAR(255) DEFAULT '',
                            dingtalk_secret VARCHAR(255) DEFAULT '',
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # 添加新字段（如果不存在）
                    try:
                        cursor.execute("ALTER TABLE accounts ADD COLUMN retry_count INT DEFAULT 2")
                    except:
                        pass
                    
                    try:
                        cursor.execute("ALTER TABLE checkin_history ADD COLUMN retry_times INT DEFAULT 0")
                    except:
                        pass
                    
                    # 添加通知设置新字段
                    notification_fields = [
                        ("telegram_enabled", "BOOLEAN DEFAULT FALSE"),
                        ("wechat_enabled", "BOOLEAN DEFAULT FALSE"),
                        ("wxpusher_enabled", "BOOLEAN DEFAULT FALSE"),
                        ("wxpusher_app_token", "VARCHAR(255) DEFAULT ''"),
                        ("wxpusher_uid", "VARCHAR(255) DEFAULT ''"),
                        ("dingtalk_enabled", "BOOLEAN DEFAULT FALSE"),
                        ("dingtalk_access_token", "VARCHAR(255) DEFAULT ''"),
                        ("dingtalk_secret", "VARCHAR(255) DEFAULT ''")
                    ]
                    
                    for field_name, field_type in notification_fields:
                        try:
                            cursor.execute(f"ALTER TABLE notification_settings ADD COLUMN {field_name} {field_type}")
                        except:
                            pass
                    
                else:
                    # SQLite table creation
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS accounts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(255) UNIQUE NOT NULL,
                            token_data TEXT NOT NULL,
                            enabled BOOLEAN DEFAULT 1,
                            checkin_time_start VARCHAR(5) DEFAULT '06:30',
                            checkin_time_end VARCHAR(5) DEFAULT '06:40',
                            check_interval INTEGER DEFAULT 60,
                            retry_count INTEGER DEFAULT 2,
                            last_checkin_date DATE DEFAULT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS checkin_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            account_id INTEGER NOT NULL,
                            success BOOLEAN NOT NULL,
                            message TEXT,
                            checkin_date DATE NOT NULL,
                            retry_times INTEGER DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                        )
                    ''')
                    
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS notification_settings (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            enabled BOOLEAN DEFAULT 0,
                            telegram_enabled BOOLEAN DEFAULT 0,
                            telegram_bot_token TEXT DEFAULT '',
                            telegram_user_id TEXT DEFAULT '',
                            wechat_enabled BOOLEAN DEFAULT 0,
                            wechat_webhook_key TEXT DEFAULT '',
                            wxpusher_enabled BOOLEAN DEFAULT 0,
                            wxpusher_app_token TEXT DEFAULT '',
                            wxpusher_uid TEXT DEFAULT '',
                            dingtalk_enabled BOOLEAN DEFAULT 0,
                            dingtalk_access_token TEXT DEFAULT '',
                            dingtalk_secret TEXT DEFAULT '',
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                
                # 初始化通知设置 - 修复这里
                cursor.execute('SELECT COUNT(*) as cnt FROM notification_settings')
                result = cursor.fetchone()
                
                # 修复：正确处理不同数据库返回的结果
                if self.db_type == 'mysql':
                    # MySQL返回元组
                    count = result[0] if result else 0
                else:
                    # SQLite返回Row对象
                    count = result['cnt'] if result else 0
                
                if count == 0:
                    if self.db_type == 'mysql':
                        cursor.execute('''
                            INSERT INTO notification_settings 
                            (enabled) VALUES (FALSE)
                        ''')
                    else:
                        cursor.execute('''
                            INSERT INTO notification_settings 
                            (enabled) VALUES (0)
                        ''')
                        self.conn.commit()
                
                logger.info("Database tables initialized successfully")
                
            except Exception as e:
                logger.error(f"Error initializing tables: {e}")
                logger.error(traceback.format_exc())
                raise
    
    def execute(self, query, params=None, use_cache=False, cache_key=None):
        """Execute a database query with connection retry and optional caching"""
        # 尝试从缓存获取数据（仅用于SELECT查询）
        if use_cache and cache_key and query.strip().upper().startswith('SELECT'):
            cached_data = data_cache.get(cache_key)
            if cached_data is not None:
                logger.debug(f"Cache hit for key: {cache_key}")
                return cached_data
        
        with self.lock:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    if self.db_type == 'mysql':
                        # 智能检查连接
                        self._ensure_connection()
                    
                    cursor = self.conn.cursor()
                    
                    if self.db_type == 'mysql' and query:
                        query = query.replace('?', '%s')
                    
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    
                    if self.db_type == 'sqlite':
                        self.conn.commit()
                    
                    # 如果需要缓存且是SELECT查询，缓存结果
                    if use_cache and cache_key and query.strip().upper().startswith('SELECT'):
                        data_cache.set(cache_key, cursor)
                    
                    return cursor
                    
                except Exception as e:
                    logger.error(f"Database execute error (attempt {attempt + 1}): {e}")
                    if attempt < max_retries - 1:
                        if self.db_type == 'mysql':
                            self.reconnect()
                        time.sleep(1)
                    else:
                        raise
    
    def fetchone(self, query, params=None, use_cache=False):
        """Fetch one row from database with optional caching"""
        cache_key = None
        if use_cache:
            # 生成缓存键
            cache_key = f"fetchone_{hash(query)}_{hash(str(params))}"
            cached_data = data_cache.get(cache_key)
            if cached_data is not None:
                return cached_data
        
        cursor = self.execute(query, params)
        result = cursor.fetchone()
        
        if result:
            if self.db_type == 'mysql':
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    if isinstance(result, tuple):
                        result = dict(zip(columns, result))
            elif self.db_type == 'sqlite':
                result = dict(result) if result else None
        
        # 缓存结果
        if use_cache and cache_key:
            data_cache.set(cache_key, result)
        
        return result
    
    def fetchall(self, query, params=None, use_cache=False):
        """Fetch all rows from database with optional caching"""
        cache_key = None
        if use_cache:
            # 生成缓存键
            cache_key = f"fetchall_{hash(query)}_{hash(str(params))}"
            cached_data = data_cache.get(cache_key)
            if cached_data is not None:
                return cached_data
        
        cursor = self.execute(query, params)
        results = cursor.fetchall()
        
        if results:
            if self.db_type == 'mysql':
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    results = [dict(zip(columns, row)) for row in results]
            elif self.db_type == 'sqlite':
                results = [dict(row) for row in results]
        
        results = results or []
        
        # 缓存结果
        if use_cache and cache_key:
            data_cache.set(cache_key, results)
        
        return results
    
    def __del__(self):
        """清理连接"""
        try:
            if self.conn:
                self.conn.close()
        except:
            pass

# Initialize database
try:
    db = Database()
except Exception as e:
    logger.error(f"Failed to initialize database: {e}")
    raise

# Notification class
class NotificationService:
    @staticmethod
    def send_notification(title, content, account_name=None):
        """Send notification through configured channels"""
        try:
            # 不使用缓存，直接从数据库获取最新设置
            settings = db.fetchone('SELECT * FROM notification_settings WHERE id = 1')
            if not settings or not settings.get('enabled'):
                logger.info("Notifications disabled")
                return
            
            # Send Telegram notification
            if settings.get('telegram_enabled') and settings.get('telegram_bot_token') and settings.get('telegram_user_id'):
                NotificationService.send_telegram(
                    settings['telegram_bot_token'],
                    settings['telegram_user_id'],
                    title,
                    content
                )
            
            # Send WeChat Work notification
            if settings.get('wechat_enabled') and settings.get('wechat_webhook_key'):
                NotificationService.send_wechat(
                    settings['wechat_webhook_key'],
                    title,
                    content
                )
            
            # Send WxPusher notification
            if settings.get('wxpusher_enabled') and settings.get('wxpusher_app_token') and settings.get('wxpusher_uid'):
                NotificationService.send_wxpusher(
                    settings['wxpusher_app_token'],
                    settings['wxpusher_uid'],
                    title,
                    content
                )
            
            # Send DingTalk notification
            if settings.get('dingtalk_enabled') and settings.get('dingtalk_access_token') and settings.get('dingtalk_secret'):
                NotificationService.send_dingtalk(
                    settings['dingtalk_access_token'],
                    settings['dingtalk_secret'],
                    title,
                    content
                )
                
        except Exception as e:
            logger.error(f"Notification error: {e}")
    
    @staticmethod
    def send_telegram(token, chat_id, title, content):
        """Send Telegram notification"""
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": f"📢 {title}\n\n{content}",
                "disable_web_page_preview": True
            }
            
            response = requests.post(url=url, data=data, timeout=30)
            result = response.json()
            
            if result.get("ok"):
                logger.info("Telegram notification sent successfully")
            else:
                logger.error(f"Telegram notification failed: {result.get('description')}")
        except Exception as e:
            logger.error(f"Telegram notification error: {e}")
    
    @staticmethod
    def send_wechat(webhook_key, title, content):
        """Send WeChat Work notification"""
        try:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={webhook_key}"
            headers = {"Content-Type": "application/json;charset=utf-8"}
            data = {"msgtype": "text", "text": {"content": f"【{title}】\n\n{content}"}}
            
            response = requests.post(
                url=url, 
                data=json.dumps(data), 
                headers=headers, 
                timeout=15
            ).json()

            if response.get("errcode") == 0:
                logger.info("WeChat Work notification sent successfully")
            else:
                logger.error(f"WeChat Work notification failed: {response.get('errmsg')}")
        except Exception as e:
            logger.error(f"WeChat Work notification error: {e}")
    
    @staticmethod
    def send_wxpusher(app_token, uid, title, content):
        """Send WxPusher notification"""
        try:
            url = "https://wxpusher.zjiecode.com/api/send/message"
            
            # 格式化HTML内容
            html_content = f"""
            <div style="padding: 10px;">
                <h2 style="color: #2c3e50;">{title}</h2>
                <div style="margin-top: 10px; padding: 10px; background: #f8f9fa; border-radius: 5px;">
                    <pre style="white-space: pre-wrap; word-wrap: break-word;">{content}</pre>
                </div>
                <div style="margin-top: 10px; color: #7f8c8d; font-size: 12px;">
                    发送时间: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}
                </div>
            </div>
            """
            
            data = {
                "appToken": app_token,
                "content": html_content,
                "summary": title[:20],  # 摘要限制20字符
                "contentType": 2,  # HTML格式
                "uids": [uid],
                "verifyPayType": 0
            }
            
            response = requests.post(url, json=data, timeout=30)
            result = response.json()
            
            if result.get("code") == 1000:
                logger.info("WxPusher notification sent successfully")
            else:
                logger.error(f"WxPusher notification failed: {result.get('msg')}")
        except Exception as e:
            logger.error(f"WxPusher notification error: {e}")
    
    @staticmethod
    def send_dingtalk(access_token, secret, title, content):
        """Send DingTalk robot notification"""
        try:
            # 生成签名
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f'{timestamp}\n{secret}'
            hmac_code = hmac.new(
                secret.encode('utf-8'), 
                string_to_sign.encode('utf-8'), 
                digestmod=hashlib.sha256
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            
            # 构建URL
            url = f'https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}'
            
            # 构建消息体
            data = {
                "msgtype": "text",
                "text": {
                    "content": f"【{title}】\n{content}"
                },
                "at": {
                    "isAtAll": False
                }
            }
            
            headers = {'Content-Type': 'application/json'}
            response = requests.post(url, json=data, headers=headers, timeout=30)
            result = response.json()
            
            if result.get("errcode") == 0:
                logger.info("DingTalk notification sent successfully")
            else:
                logger.error(f"DingTalk notification failed: {result.get('errmsg')}")
        except Exception as e:
            logger.error(f"DingTalk notification error: {e}")

# Leaflow check-in class
class LeafLowCheckin:
    def __init__(self):
        self.checkin_url = "https://checkin.leaflow.net"
        self.main_site = "https://leaflow.net"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    
    def create_session(self, token_data):
        """Create session with authentication"""
        session = requests.Session()
        
        session.headers.update({
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        
        if 'cookies' in token_data:
            for name, value in token_data['cookies'].items():
                session.cookies.set(name, value)
        
        if 'headers' in token_data:
            session.headers.update(token_data['headers'])
        
        return session
    
    def test_authentication(self, session, account_name):
        """Test if authentication is valid"""
        try:
            test_urls = [
                f"{self.main_site}/dashboard",
                f"{self.main_site}/profile",
                f"{self.main_site}/user",
                self.checkin_url,
            ]
            
            for url in test_urls:
                response = session.get(url, timeout=30)
                
                if response.status_code == 200:
                    content = response.text.lower()
                    if any(indicator in content for indicator in ['dashboard', 'profile', 'user', 'logout', 'welcome']):
                        logger.info(f"✅ [{account_name}] Authentication valid")
                        return True, "Authentication successful"
                elif response.status_code in [301, 302, 303]:
                    location = response.headers.get('location', '')
                    if 'login' not in location.lower():
                        logger.info(f"✅ [{account_name}] Authentication valid (redirect)")
                        return True, "Authentication successful (redirect)"
            
            return False, "Authentication failed - no valid authenticated pages found"
            
        except Exception as e:
            return False, f"Authentication test error: {str(e)}"
    
    def perform_checkin(self, session, account_name):
        """Perform check-in"""
        logger.info(f"🎯 [{account_name}] Performing checkin...")
        
        try:
            # Try direct check-in page
            response = session.get(self.checkin_url, timeout=30)
            
            if response.status_code == 200:
                result = self.analyze_and_checkin(session, response.text, self.checkin_url, account_name)
                if result[0]:
                    return result
            
            # Try API endpoints
            api_endpoints = [
                f"{self.checkin_url}/api/checkin",
                f"{self.checkin_url}/checkin",
                f"{self.main_site}/api/checkin",
                f"{self.main_site}/checkin"
            ]
            
            for endpoint in api_endpoints:
                try:
                    # GET request
                    response = session.get(endpoint, timeout=30)
                    if response.status_code == 200:
                        success, message = self.check_checkin_response(response.text)
                        if success:
                            return True, message
                    
                    # POST request
                    response = session.post(endpoint, data={'checkin': '1'}, timeout=30)
                    if response.status_code == 200:
                        success, message = self.check_checkin_response(response.text)
                        if success:
                            return True, message
                            
                except Exception as e:
                    logger.debug(f"[{account_name}] API endpoint {endpoint} failed: {str(e)}")
                    continue
            
            return False, "All checkin methods failed"
            
        except Exception as e:
            return False, f"Checkin error: {str(e)}"
    
    def analyze_and_checkin(self, session, html_content, page_url, account_name):
        """Analyze page and perform check-in"""
        if self.already_checked_in(html_content):
            return True, "Already checked in today"
        
        if not self.is_checkin_page(html_content):
            return False, "Not a checkin page"
        
        try:
            checkin_data = {'checkin': '1', 'action': 'checkin', 'daily': '1'}
            
            csrf_token = self.extract_csrf_token(html_content)
            if csrf_token:
                checkin_data['_token'] = csrf_token
                checkin_data['csrf_token'] = csrf_token
            
            response = session.post(page_url, data=checkin_data, timeout=30)
            
            if response.status_code == 200:
                return self.check_checkin_response(response.text)
                
        except Exception as e:
            logger.debug(f"[{account_name}] POST checkin failed: {str(e)}")
        
        return False, "Failed to perform checkin"
    
    def already_checked_in(self, html_content):
        """Check if already checked in"""
        content_lower = html_content.lower()
        indicators = [
            'already checked in', '今日已签到', 'checked in today',
            'attendance recorded', '已完成签到', 'completed today'
        ]
        return any(indicator in content_lower for indicator in indicators)
    
    def is_checkin_page(self, html_content):
        """Check if it's a check-in page"""
        content_lower = html_content.lower()
        indicators = ['check-in', 'checkin', '签到', 'attendance', 'daily']
        return any(indicator in content_lower for indicator in indicators)
    
    def extract_csrf_token(self, html_content):
        """Extract CSRF token"""
        patterns = [
            r'name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
            r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
            r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                return match.group(1)
        
        return None
    
    def check_checkin_response(self, html_content):
        """Check check-in response"""
        content_lower = html_content.lower()
        
        success_indicators = [
            'check-in successful', 'checkin successful', '签到成功',
            'attendance recorded', 'earned reward', '获得奖励',
            'success', '成功', 'completed'
        ]
        
        if any(indicator in content_lower for indicator in success_indicators):
            reward_patterns = [
                r'获得奖励[^\d]*(\d+\.?\d*)\s*元',
                r'earned.*?(\d+\.?\d*)\s*(credits?|points?)',
                r'(\d+\.?\d*)\s*(credits?|points?|元)'
            ]
            
            for pattern in reward_patterns:
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    reward = match.group(1)
                    return True, f"Check-in successful! Earned {reward} credits"
            
            return True, "Check-in successful!"
        
        return False, "Checkin response indicates failure"

# Helper function to parse cookie string
def parse_cookie_string(cookie_input):
    """Parse cookie string in various formats"""
    cookie_input = cookie_input.strip()
    
    # Try to parse as JSON first
    if cookie_input.startswith('{'):
        try:
            data = json.loads(cookie_input)
            if 'cookies' in data:
                return data
            else:
                return {'cookies': data}
        except json.JSONDecodeError:
            pass
    
    # Parse as semicolon-separated cookie string
    cookies = {}
    cookie_pairs = re.split(r';\s*', cookie_input)
    
    for pair in cookie_pairs:
        if '=' in pair:
            key, value = pair.split('=', 1)
            key = key.strip()
            value = value.strip()
            if key:
                cookies[key] = value
    
    if cookies:
        return {'cookies': cookies}
    
    raise ValueError("Invalid cookie format")

# JWT authentication decorator
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        
        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        
        try:
            if token.startswith('Bearer '):
                token = token[7:]
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            return f(*args, **kwargs)
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token has expired!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Token is invalid!'}), 401
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return jsonify({'message': 'Token validation failed!'}), 401
    
    return decorated

# Scheduler class
class CheckinScheduler:
    def __init__(self):
        self.scheduler_thread = None
        self.running = False
        self.leaflow_checkin = LeafLowCheckin()
        self.checkin_tasks = {}  # 存储每个账户的签到任务
    
    def start(self):
        if not self.running:
            self.running = True
            self.scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True)
            self.scheduler_thread.start()
            logger.info("Scheduler started")
    
    def stop(self):
        self.running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        logger.info("Scheduler stopped")
    
    def _run_scheduler(self):
        """调度器主循环"""
        while self.running:
            try:
                # 获取当前北京时间
                now = datetime.now(TIMEZONE)
                current_date = now.date()
                
                # 尝试从缓存获取账户
                accounts = account_cache.get_accounts()
                if accounts is None:
                    # 缓存失效，从数据库获取
                    accounts_list = db.fetchall('SELECT * FROM accounts WHERE enabled = 1')
                    if accounts_list:
                        account_cache.update_cache(accounts_list)
                        accounts = accounts_list  # 直接使用列表
                    else:
                        accounts = []
                
                for account in accounts:
                    try:
                        account_id = account['id']
                        
                        # 检查今天是否已经签到
                        last_checkin_date = account.get('last_checkin_date')
                        if last_checkin_date:
                            if isinstance(last_checkin_date, str):
                                last_checkin_date = datetime.strptime(last_checkin_date, '%Y-%m-%d').date()
                            if last_checkin_date == current_date:
                                continue  # 今天已经签到，跳过
                        
                        # 获取签到时间范围
                        start_time_str = account.get('checkin_time_start', '06:30')
                        end_time_str = account.get('checkin_time_end', '06:40')
                        check_interval = account.get('check_interval', 60)
                        
                        # 解析时间
                        start_hour, start_minute = map(int, start_time_str.split(':'))
                        end_hour, end_minute = map(int, end_time_str.split(':'))
                        
                        # 创建今天的开始和结束时间
                        start_time = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
                        end_time = now.replace(hour=end_hour, minute=end_minute, second=59, microsecond=999999)
                        
                        # 检查是否在签到时间范围内
                        if start_time <= now <= end_time:
                            # 检查是否需要执行签到
                            task_key = f"{account_id}_{current_date}"
                            
                            if task_key not in self.checkin_tasks:
                                self.checkin_tasks[task_key] = {
                                    'last_check': None,
                                    'completed': False,
                                    'retry_count': 0
                                }
                            
                            task = self.checkin_tasks[task_key]
                            
                            # 如果还没完成签到，且距离上次检查超过了间隔时间
                            if not task['completed']:
                                if task['last_check'] is None or \
                                   (now - task['last_check']).total_seconds() >= check_interval:
                                    # 执行签到
                                    task['last_check'] = now
                                    threading.Thread(
                                        target=self.perform_checkin_with_delay,
                                        args=(account_id, task_key),
                                        daemon=True
                                    ).start()
                    except Exception as e:
                        logger.error(f"Error processing account {account.get('id', 'unknown')}: {e}")
                        continue
                
                # 清理过期的任务记录
                expired_keys = []
                for key in self.checkin_tasks:
                    if not key.endswith(str(current_date)):
                        expired_keys.append(key)
                for key in expired_keys:
                    del self.checkin_tasks[key]
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                logger.error(traceback.format_exc())
            
            # 等待一段时间再检查
            time.sleep(30)  # 每30秒检查一次
    
    def perform_checkin_with_delay(self, account_id, task_key):
        """带随机延迟的签到执行"""
        try:
            # 添加随机延迟（0-30秒）
            delay = random.randint(0, 30)
            time.sleep(delay)
            
            # 执行签到
            success = self.perform_checkin(account_id)
            
            # 标记任务完成
            if task_key in self.checkin_tasks:
                self.checkin_tasks[task_key]['completed'] = success
                
        except Exception as e:
            logger.error(f"Checkin with delay error: {e}")
            logger.error(traceback.format_exc())
    
    def perform_checkin(self, account_id, retry_attempt=0):
        """Perform check-in for an account with retry mechanism"""
        try:
            account = db.fetchone('SELECT * FROM accounts WHERE id = ?', (account_id,))
            if not account or not account.get('enabled'):
                return False
            
            # 获取当前北京时间日期
            current_date = datetime.now(TIMEZONE).date()
            
            # 检查今天是否已经签到
            existing_checkin = db.fetchone('''
                SELECT id FROM checkin_history 
                WHERE account_id = ? AND checkin_date = ?
            ''', (account_id, current_date))
            
            if existing_checkin:
                logger.info(f"Account {account['name']} already checked in today")
                return True
            
            # Parse token data
            token_data = json.loads(account['token_data'])
            
            # Create session and perform check-in
            session = self.leaflow_checkin.create_session(token_data)
            
            # Test authentication
            auth_result = self.leaflow_checkin.test_authentication(session, account['name'])
            if not auth_result[0]:
                success = False
                message = f"Authentication failed: {auth_result[1]}"
            else:
                # Perform check-in
                success, message = self.leaflow_checkin.perform_checkin(session, account['name'])
            
            # 如果失败且还有重试次数
            retry_count = account.get('retry_count', 2)
            if not success and retry_attempt < retry_count:
                logger.info(f"Retrying checkin for {account['name']} (attempt {retry_attempt + 1}/{retry_count})")
                time.sleep(5)  # 等待5秒后重试
                return self.perform_checkin(account_id, retry_attempt + 1)
            
            # Record check-in result
            db.execute('''
                INSERT INTO checkin_history (account_id, success, message, checkin_date, retry_times)
                VALUES (?, ?, ?, ?, ?)
            ''', (account_id, success, message, current_date, retry_attempt))
            
            # 更新最后签到日期
            if success:
                db.execute('''
                    UPDATE accounts SET last_checkin_date = ?
                    WHERE id = ?
                ''', (current_date, account_id))
                # 刷新账户缓存
                account_cache.refresh_from_db(db)
            
            logger.info(f"Check-in for {account['name']}: {'Success' if success else 'Failed'} - {message}")
            
            # Send notification
            notification_title = f"Leaflow签到结果 - {account['name']}"
            status_emoji = '✅' if success else '❌'
            notification_content = f"状态: {status_emoji} {'成功' if success else '失败'}\n消息: {message}\n重试次数: {retry_attempt}"
            NotificationService.send_notification(notification_title, notification_content, account['name'])
            
            return success
            
        except Exception as e:
            logger.error(f"Check-in error for account {account_id}: {e}")
            logger.error(traceback.format_exc())
            
            # Send error notification
            try:
                account = db.fetchone('SELECT name FROM accounts WHERE id = ?', (account_id,))
                if account:
                    NotificationService.send_notification(
                        f"Leaflow签到错误 - {account['name']}",
                        f"错误: {str(e)}",
                        account['name']
                    )
            except:
                pass
            
            return False

scheduler = CheckinScheduler()

# Routes
@app.route('/')
def index():
    """Serve the main HTML page"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    """Handle login requests"""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'message': 'No data provided'}), 400
        
        username = data.get('username')
        password = data.get('password')
        
        logger.info(f"Login attempt for user: {username}")
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            token = jwt.encode({
                'user': username,
                'exp': datetime.utcnow() + timedelta(days=7)
            }, app.config['SECRET_KEY'], algorithm='HS256')
            
            logger.info(f"Login successful for user: {username}")
            return jsonify({'token': token, 'message': 'Login successful'})
        
        logger.warning(f"Login failed for user: {username}")
        return jsonify({'message': 'Invalid credentials'}), 401
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'message': 'Login error'}), 500

@app.route('/api/dashboard', methods=['GET'])
@token_required
def dashboard():
    """Get dashboard statistics"""
    try:
        # 使用缓存获取统计数据
        total_accounts = db.fetchone('SELECT COUNT(*) as count FROM accounts', use_cache=True)
        enabled_accounts = db.fetchone('SELECT COUNT(*) as count FROM accounts WHERE enabled = 1', use_cache=True)
        
        # 获取今天的日期（北京时间）
        today = datetime.now(TIMEZONE).date()
        
        # 获取今日签到记录（不使用缓存，保证实时性）
        today_checkins = db.fetchall('''
            SELECT a.name, ch.success, ch.message, ch.created_at, ch.retry_times
            FROM checkin_history ch
            JOIN accounts a ON ch.account_id = a.id
            WHERE DATE(ch.checkin_date) = DATE(?)
            ORDER BY ch.created_at DESC
            LIMIT 20
        ''', (today,))
        
        total_checkins = db.fetchone('SELECT COUNT(*) as count FROM checkin_history', use_cache=True)
        successful_checkins = db.fetchone('SELECT COUNT(*) as count FROM checkin_history WHERE success = 1', use_cache=True)
        
        total_count = total_checkins['count'] if total_checkins else 0
        success_count = successful_checkins['count'] if successful_checkins else 0
        success_rate = round(success_count / total_count * 100, 2) if total_count > 0 else 0
        
        return jsonify({
            'total_accounts': total_accounts['count'] if total_accounts else 0,
            'enabled_accounts': enabled_accounts['count'] if enabled_accounts else 0,
            'today_checkins': today_checkins or [],
            'total_checkins': total_count,
            'successful_checkins': success_count,
            'success_rate': success_rate
        })
        
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return jsonify({'error': 'Failed to load dashboard data'}), 500

@app.route('/api/accounts', methods=['GET'])
@token_required
def get_accounts():
    """Get all accounts"""
    try:
        accounts = db.fetchall('''
            SELECT id, name, enabled, checkin_time_start, checkin_time_end, 
                   check_interval, retry_count, created_at 
            FROM accounts
        ''')
        return jsonify(accounts or [])
    except Exception as e:
        logger.error(f"Get accounts error: {e}")
        return jsonify({'error': 'Failed to load accounts'}), 500

@app.route('/api/accounts', methods=['POST'])
@token_required
def add_account():
    """Add a new account"""
    try:
        data = request.get_json()
        name = data.get('name')
        cookie_input = data.get('token_data', data.get('cookie_data', ''))
        checkin_time_start = data.get('checkin_time_start', '06:30')
        checkin_time_end = data.get('checkin_time_end', '06:40')
        check_interval = data.get('check_interval', 60)
        retry_count = data.get('retry_count', 2)
        
        if not name or not cookie_input:
            return jsonify({'message': 'Name and cookie data are required'}), 400
        
        # Parse cookie input
        if isinstance(cookie_input, str):
            token_data = parse_cookie_string(cookie_input)
        else:
            token_data = cookie_input
        
        db.execute('''
            INSERT INTO accounts (name, token_data, checkin_time_start, checkin_time_end, check_interval, retry_count)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, json.dumps(token_data), checkin_time_start, checkin_time_end, check_interval, retry_count))
        
        # 立即刷新账户缓存
        account_cache.refresh_from_db(db)
        # 清除相关数据缓存
        data_cache.invalidate()
        
        logger.info(f"Account '{name}' added and cache refreshed")
        
        return jsonify({'message': 'Account added successfully'})
        
    except ValueError as e:
        return jsonify({'message': f'Invalid cookie format: {str(e)}'}), 400
    except Exception as e:
        logger.error(f"Add account error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
@token_required
def update_account(account_id):
    """Update an account"""
    try:
        data = request.get_json()
        
        updates = []
        params = []
        
        if 'enabled' in data:
            updates.append('enabled = ?')
            params.append(1 if data['enabled'] else 0)
        
        if 'checkin_time_start' in data:
            updates.append('checkin_time_start = ?')
            params.append(data['checkin_time_start'])
        
        if 'checkin_time_end' in data:
            updates.append('checkin_time_end = ?')
            params.append(data['checkin_time_end'])
        
        if 'check_interval' in data:
            updates.append('check_interval = ?')
            params.append(data['check_interval'])
        
        if 'retry_count' in data:
            updates.append('retry_count = ?')
            params.append(data['retry_count'])
        
        if 'token_data' in data or 'cookie_data' in data:
            cookie_input = data.get('token_data', data.get('cookie_data', ''))
            if isinstance(cookie_input, str):
                token_data = parse_cookie_string(cookie_input)
            else:
                token_data = cookie_input
            updates.append('token_data = ?')
            params.append(json.dumps(token_data))
        
        if updates:
            params.append(account_id)
            query = f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?"
            db.execute(query, params)
            
            # 立即刷新账户缓存
            account_cache.refresh_from_db(db)
            # 清除相关数据缓存
            data_cache.invalidate()
            
            logger.info(f"Account {account_id} updated and cache refreshed")
            
            return jsonify({'message': 'Account updated successfully'})
        
        return jsonify({'message': 'No updates provided'}), 400
        
    except Exception as e:
        logger.error(f"Update account error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@token_required
def delete_account(account_id):
    """Delete an account"""
    try:
        db.execute('DELETE FROM checkin_history WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
        
        # 立即刷新账户缓存
        account_cache.refresh_from_db(db)
        # 清除相关数据缓存
        data_cache.invalidate()
        
        logger.info(f"Account {account_id} deleted and cache refreshed")
        
        return jsonify({'message': 'Account deleted successfully'})
    except Exception as e:
        logger.error(f"Delete account error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/checkin/clear', methods=['POST'])
@token_required
def clear_checkin_history():
    """Clear checkin history"""
    try:
        data = request.get_json()
        clear_type = data.get('type', 'today')
        
        if clear_type == 'today':
            # 清空今日签到记录
            today = datetime.now(TIMEZONE).date()
            db.execute('DELETE FROM checkin_history WHERE DATE(checkin_date) = DATE(?)', (today,))
            # 重置今日的最后签到日期
            db.execute('UPDATE accounts SET last_checkin_date = NULL WHERE DATE(last_checkin_date) = DATE(?)', (today,))
            message = 'Today\'s checkin history cleared'
        elif clear_type == 'all':
            # 清空所有签到记录
            db.execute('DELETE FROM checkin_history')
            db.execute('UPDATE accounts SET last_checkin_date = NULL')
            message = 'All checkin history cleared'
        else:
            return jsonify({'message': 'Invalid clear type'}), 400
        
        # 立即刷新账户缓存
        account_cache.refresh_from_db(db)
        # 清除相关数据缓存
        data_cache.invalidate()
        
        logger.info(f"Checkin history cleared ({clear_type}) and cache refreshed")
        
        return jsonify({'message': message})
    except Exception as e:
        logger.error(f"Clear checkin history error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/notification', methods=['GET'])
@token_required
def get_notification_settings():
    """Get notification settings"""
    try:
        # 直接从数据库获取最新设置，不使用缓存
        settings = db.fetchone('SELECT * FROM notification_settings WHERE id = 1')
        if settings:
            # 转换布尔值
            for key in ['enabled', 'telegram_enabled', 'wechat_enabled', 'wxpusher_enabled', 'dingtalk_enabled']:
                if key in settings:
                    settings[key] = bool(settings.get(key, 0))
            
            # 确保字符串字段不为None
            string_fields = [
                'telegram_bot_token', 'telegram_user_id', 'wechat_webhook_key',
                'wxpusher_app_token', 'wxpusher_uid', 'dingtalk_access_token', 'dingtalk_secret'
            ]
            for field in string_fields:
                settings[field] = settings.get(field, '') or ''
            
            logger.info(f"Loaded notification settings: {settings}")
            return jsonify(settings)
        else:
            default_settings = {
                'id': 1,
                'enabled': False,
                'telegram_enabled': False,
                'telegram_bot_token': '',
                'telegram_user_id': '',
                'wechat_enabled': False,
                'wechat_webhook_key': '',
                'wxpusher_enabled': False,
                'wxpusher_app_token': '',
                'wxpusher_uid': '',
                'dingtalk_enabled': False,
                'dingtalk_access_token': '',
                'dingtalk_secret': ''
            }
            return jsonify(default_settings)
    except Exception as e:
        logger.error(f"Get notification settings error: {e}")
        return jsonify({'error': 'Failed to load settings'}), 500

@app.route('/api/notification', methods=['PUT'])
@token_required
def update_notification_settings():
    """Update notification settings"""
    try:
        data = request.get_json()
        logger.info(f"Updating notification settings with data: {data}")
        
        # 准备更新数据
        enabled = 1 if data.get('enabled', False) else 0
        telegram_enabled = 1 if data.get('telegram_enabled', False) else 0
        telegram_bot_token = data.get('telegram_bot_token', '') or ''
        telegram_user_id = data.get('telegram_user_id', '') or ''
        wechat_enabled = 1 if data.get('wechat_enabled', False) else 0
        wechat_webhook_key = data.get('wechat_webhook_key', '') or ''
        wxpusher_enabled = 1 if data.get('wxpusher_enabled', False) else 0
        wxpusher_app_token = data.get('wxpusher_app_token', '') or ''
        wxpusher_uid = data.get('wxpusher_uid', '') or ''
        dingtalk_enabled = 1 if data.get('dingtalk_enabled', False) else 0
        dingtalk_access_token = data.get('dingtalk_access_token', '') or ''
        dingtalk_secret = data.get('dingtalk_secret', '') or ''
        
        existing = db.fetchone('SELECT id FROM notification_settings WHERE id = 1')
        
        if existing:
            db.execute('''
                UPDATE notification_settings
                SET enabled = ?, telegram_enabled = ?, telegram_bot_token = ?, telegram_user_id = ?, 
                    wechat_enabled = ?, wechat_webhook_key = ?, wxpusher_enabled = ?, 
                    wxpusher_app_token = ?, wxpusher_uid = ?, dingtalk_enabled = ?,
                    dingtalk_access_token = ?, dingtalk_secret = ?, updated_at = ?
                WHERE id = 1
            ''', (
                enabled, telegram_enabled, telegram_bot_token, telegram_user_id,
                wechat_enabled, wechat_webhook_key, wxpusher_enabled,
                wxpusher_app_token, wxpusher_uid, dingtalk_enabled,
                dingtalk_access_token, dingtalk_secret, datetime.now()
            ))
            logger.info("Notification settings updated successfully")
        else:
            db.execute('''
                INSERT INTO notification_settings 
                (id, enabled, telegram_enabled, telegram_bot_token, telegram_user_id, 
                 wechat_enabled, wechat_webhook_key, wxpusher_enabled, wxpusher_app_token, 
                 wxpusher_uid, dingtalk_enabled, dingtalk_access_token, dingtalk_secret)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                enabled, telegram_enabled, telegram_bot_token, telegram_user_id,
                wechat_enabled, wechat_webhook_key, wxpusher_enabled,
                wxpusher_app_token, wxpusher_uid, dingtalk_enabled,
                dingtalk_access_token, dingtalk_secret
            ))
            logger.info("Notification settings created successfully")
        
        # 清除所有通知相关缓存
        data_cache.invalidate_pattern('notification')
        
        # 立即从数据库获取最新设置并验证
        updated_settings = db.fetchone('SELECT * FROM notification_settings WHERE id = 1')
        logger.info(f"Verified settings after update: {updated_settings}")
        
        return jsonify({'message': 'Notification settings updated successfully'})
    except Exception as e:
        logger.error(f"Update notification settings error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/checkin/manual/<int:account_id>', methods=['POST'])
@token_required
def manual_checkin(account_id):
    """Trigger manual check-in"""
    try:
        threading.Thread(target=scheduler.perform_checkin, args=(account_id,), daemon=True).start()
        return jsonify({'message': 'Manual check-in triggered'})
    except Exception as e:
        logger.error(f"Manual checkin error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/test/notification', methods=['POST'])
@token_required
def test_notification():
    """Test notification settings"""
    try:
        NotificationService.send_notification(
            "测试通知",
            "这是来自Leaflow自动签到系统的测试通知。如果您收到此消息，说明您的通知设置正常工作！",
            "系统测试"
        )
        return jsonify({'message': 'Test notification sent'})
    except Exception as e:
        logger.error(f"Test notification error: {e}")
        return jsonify({'message': f'Error: {str(e)}'}), 400

# HTML Template
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Leaflow Auto Check-in Control Panel</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif; 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
            min-height: 100vh;
        }
        
        /* Login Styles */
        .login-container { 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            min-height: 100vh; 
            padding: 20px;
        }
        .login-box { 
            background: white; 
            padding: 40px; 
            border-radius: 15px; 
            box-shadow: 0 20px 60px rgba(0,0,0,0.2); 
            width: 100%;
            max-width: 400px;
        }
        .login-box h2 { 
            margin-bottom: 30px; 
            color: #333; 
            text-align: center;
            font-size: 24px;
        }
        
        /* Form Styles */
        .form-group { 
            margin-bottom: 20px; 
        }
        .form-group label { 
            display: block; 
            margin-bottom: 8px; 
            color: #555; 
            font-weight: 500;
        }
        .form-group input, .form-group textarea, .form-group select { 
            width: 100%; 
            padding: 12px; 
            border: 2px solid #e0e0e0; 
            border-radius: 8px; 
            font-size: 14px;
            transition: all 0.3s;
        }
        .form-group input:focus, .form-group textarea:focus, .form-group select:focus { 
            border-color: #667eea;
            outline: none;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .form-group-inline {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .form-group-inline input[type="checkbox"] {
            width: auto;
            margin: 0;
        }
        
        /* Notification Settings Styles */
        .notification-channel {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
        }
        
        .notification-channel h4 {
            color: #2d3748;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .channel-toggle {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 15px;
        }
        
        /* Button Styles */
        .btn { 
            padding: 12px 24px; 
            background: linear-gradient(135deg, #667eea, #764ba2); 
            color: white; 
            border: none; 
            border-radius: 8px; 
            cursor: pointer; 
            font-size: 14px; 
            font-weight: 600;
            transition: all 0.3s;
            display: inline-block;
            text-align: center;
        }
        .btn:hover { 
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }
        .btn-full { width: 100%; }
        .btn-sm { 
            padding: 8px 16px; 
            font-size: 13px; 
        }
        .btn-danger { 
            background: linear-gradient(135deg, #f56565, #e53e3e); 
        }
        .btn-danger:hover { 
            box-shadow: 0 5px 15px rgba(245, 101, 101, 0.4);
        }
        .btn-success {
            background: linear-gradient(135deg, #48bb78, #38a169);
        }
        .btn-success:hover {
            box-shadow: 0 5px 15px rgba(72, 187, 120, 0.4);
        }
        .btn-info {
            background: linear-gradient(135deg, #4299e1, #3182ce);
        }
        .btn-info:hover {
            box-shadow: 0 5px 15px rgba(66, 153, 225, 0.4);
        }
        .btn-warning {
            background: linear-gradient(135deg, #ed8936, #dd6b20);
        }
        .btn-warning:hover {
            box-shadow: 0 5px 15px rgba(237, 137, 54, 0.4);
        }
        
        /* Dashboard Styles */
        .dashboard { 
            display: none; 
            padding: 20px; 
            background: #f7fafc; 
            min-height: 100vh; 
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .header { 
            background: white; 
            padding: 20px 30px; 
            border-radius: 15px; 
            margin-bottom: 30px; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
        }
        .header-content {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
        }
        .header h1 { 
            color: #2d3748;
            font-size: 24px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .header-actions {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        
        /* Stats Grid */
        .stats-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); 
            gap: 20px; 
            margin-bottom: 30px; 
        }
        .stat-card { 
            background: white; 
            padding: 25px; 
            border-radius: 15px; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            transition: all 0.3s;
        }
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 5px 20px rgba(0,0,0,0.12);
        }
        .stat-card h3 { 
            color: #718096; 
            font-size: 14px; 
            margin-bottom: 12px;
            font-weight: 500;
        }
        .stat-card .value { 
            font-size: 32px; 
            font-weight: bold; 
            color: #2d3748; 
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        /* Section Styles */
        .section { 
            background: white; 
            padding: 30px; 
            border-radius: 15px; 
            margin-bottom: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 25px;
            flex-wrap: wrap;
            gap: 15px;
        }
        .section h2 { 
            color: #2d3748;
            font-size: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .button-group {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        
        /* Table Styles */
        .table-wrapper {
            overflow-x: auto;
            margin: -10px;
            padding: 10px;
        }
        .table { 
            width: 100%; 
            border-collapse: separate;
            border-spacing: 0;
        }
        .table th, .table td { 
            padding: 14px; 
            text-align: left; 
            border-bottom: 1px solid #e2e8f0;
        }
        .table th { 
            background: #f7fafc; 
            font-weight: 600;
            color: #4a5568;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .table tbody tr {
            transition: background 0.2s;
        }
        .table tbody tr:hover {
            background: #f7fafc;
        }
        
        /* Badge Styles */
        .badge { 
            padding: 6px 12px; 
            border-radius: 6px; 
            font-size: 12px;
            font-weight: 600;
            display: inline-block;
        }
        .badge-success { 
            background: #c6f6d5; 
            color: #22543d; 
        }
        .badge-danger { 
            background: #fed7d7; 
            color: #742a2a; 
        }
        .badge-info {
            background: #bee3f8;
            color: #2c5282;
        }
        
        /* Switch Styles */
        .switch { 
            position: relative; 
            display: inline-block; 
            width: 50px; 
            height: 26px; 
        }
        .switch input { 
            opacity: 0; 
            width: 0; 
            height: 0; 
        }
        .slider { 
            position: absolute; 
            cursor: pointer; 
            top: 0; 
            left: 0; 
            right: 0; 
            bottom: 0; 
            background-color: #cbd5e0; 
            transition: .4s; 
            border-radius: 26px; 
        }
        .slider:before { 
            position: absolute; 
            content: ""; 
            height: 20px; 
            width: 20px; 
            left: 3px; 
            bottom: 3px; 
            background-color: white; 
            transition: .4s; 
            border-radius: 50%; 
        }
        input:checked + .slider { 
            background: linear-gradient(135deg, #667eea, #764ba2); 
        }
        input:checked + .slider:before { 
            transform: translateX(24px); 
        }
        
        /* Time Range Input */
        .time-range-input {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .time-range-input input[type="time"] {
            border: 2px solid #e0e0e0;
            padding: 6px;
            border-radius: 6px;
            font-size: 13px;
        }
        
        .interval-input {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .interval-input input[type="number"] {
            width: 80px;
            border: 2px solid #e0e0e0;
            padding: 6px;
            border-radius: 6px;
            font-size: 13px;
        }
        
        /* Modal Styles */
        .modal { 
            display: none; 
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            background: rgba(0,0,0,0.6); 
            justify-content: center; 
            align-items: center;
            padding: 20px;
            z-index: 1000;
        }
        .modal-content { 
            background: white; 
            padding: 30px; 
            border-radius: 15px; 
            width: 100%;
            max-width: 600px;
            max-height: 90vh;
            overflow-y: auto;
            animation: modalSlideIn 0.3s ease;
        }
        @keyframes modalSlideIn {
            from {
                transform: translateY(-50px);
                opacity: 0;
            }
            to {
                transform: translateY(0);
                opacity: 1;
            }
        }
        .modal-header { 
            margin-bottom: 25px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-header h3 { 
            color: #2d3748;
            font-size: 20px;
        }
        .close { 
            font-size: 28px; 
            cursor: pointer; 
            color: #a0aec0;
            background: none;
            border: none;
            padding: 0;
            width: 30px;
            height: 30px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            transition: all 0.3s;
        }
        .close:hover { 
            background: #f7fafc;
            color: #4a5568;
        }
        
        /* Loading Spinner */
        .spinner {
            border: 3px solid #f3f3f3;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 20px auto;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        /* Toast Notification */
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: white;
            padding: 16px 24px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: none;
            animation: slideInUp 0.3s ease;
            z-index: 2000;
            max-width: 350px;
        }
        
        @keyframes slideInUp {
            from {
                transform: translateY(100px);
                opacity: 0;
            }
            to {
                transform: translateY(0);
                opacity: 1;
            }
        }
        
        .toast.success {
            border-left: 4px solid #48bb78;
        }
        
        .toast.error {
            border-left: 4px solid #f56565;
        }
        
        .toast.info {
            border-left: 4px solid #4299e1;
        }
        
        /* Error message */
        .error-message {
            color: #e53e3e;
            font-size: 14px;
            margin-top: 10px;
            display: none;
        }
        
        /* Cookie format hint */
        .format-hint {
            font-size: 12px;
            color: #718096;
            margin-top: 5px;
        }
        
        .help-link {
            color: #667eea;
            text-decoration: none;
            font-size: 12px;
        }
        
        .help-link:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <!-- Toast Notification -->
    <div id="toast" class="toast"></div>

    <!-- Login Container -->
    <div class="login-container" id="loginContainer">
        <div class="login-box">
            <h2>🔐 管理员登录</h2>
            <div id="loginForm">
                <div class="form-group">
                    <label>用户名</label>
                    <input type="text" id="username" required autocomplete="username">
                </div>
                <div class="form-group">
                    <label>密码</label>
                    <input type="password" id="password" required autocomplete="current-password">
                </div>
                <button type="button" class="btn btn-full" id="loginBtn" onclick="handleLogin()">登录</button>
                <div class="error-message" id="loginError"></div>
            </div>
        </div>
    </div>

    <!-- Dashboard -->
    <div class="dashboard" id="dashboard">
        <div class="container">
            <div class="header">
                <div class="header-content">
                    <h1>📊 Leaflow 自动签到控制面板</h1>
                    <div class="header-actions">
                        <button class="btn btn-danger btn-sm" onclick="logout()">退出</button>
                    </div>
                </div>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <h3>账号总数</h3>
                    <div class="value" id="totalAccounts">0</div>
                </div>
                <div class="stat-card">
                    <h3>活跃账号</h3>
                    <div class="value" id="activeAccounts">0</div>
                </div>
                <div class="stat-card">
                    <h3>签到总数</h3>
                    <div class="value" id="totalCheckins">0</div>
                </div>
                <div class="stat-card">
                    <h3>成功率</h3>
                    <div class="value" id="successRate">0%</div>
                </div>
            </div>

            <div class="section">
                <div class="section-header">
                    <h2>📅 今日签到记录</h2>
                    <div class="button-group">
                        <button class="btn btn-warning btn-sm" onclick="clearCheckinHistory('today')">清空今日记录</button>
                        <button class="btn btn-danger btn-sm" onclick="clearCheckinHistory('all')">清空所有记录</button>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>账号</th>
                                <th>状态</th>
                                <th>消息</th>
                                <th>重试次数</th>
                                <th>时间</th>
                            </tr>
                        </thead>
                        <tbody id="todayCheckins">
                            <tr>
                                <td colspan="5" style="text-align: center; color: #a0aec0;">
                                    <div class="spinner"></div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <div class="section-header">
                    <h2>👥 账号管理</h2>
                    <button class="btn btn-success btn-sm" onclick="showAddAccountModal()">+ 添加账号</button>
                </div>
                <div class="table-wrapper">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>名称</th>
                                <th>状态</th>
                                <th>签到时间段</th>
                                <th>检查间隔</th>
                                <th>重试次数</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody id="accountsList">
                            <tr>
                                <td colspan="6" style="text-align: center; color: #a0aec0;">
                                    <div class="spinner"></div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <div class="section-header">
                    <h2>🔔 通知设置</h2>
                    <button class="btn btn-info btn-sm" onclick="testNotification()">测试通知</button>
                </div>
                
                <div class="form-group">
                    <div class="form-group-inline">
                        <input type="checkbox" id="notifyEnabled">
                        <label for="notifyEnabled" style="margin-bottom: 0;">启用通知功能</label>
                    </div>
                </div>
                
                <!-- Telegram通知设置 -->
                <div class="notification-channel">
                    <h4>📱 Telegram 通知设置</h4>
                    <div class="channel-toggle">
                        <input type="checkbox" id="telegramEnabled">
                        <label for="telegramEnabled">启用 Telegram 通知</label>
                    </div>
                    <div class="form-group">
                        <label>Bot Token</label>
                        <input type="text" id="tgBotToken" placeholder="从 @BotFather 获取的 Bot Token">
                    </div>
                    <div class="form-group">
                        <label>User ID</label>
                        <input type="text" id="tgUserId" placeholder="接收通知的用户ID">
                    </div>
                </div>
                
                <!-- 企业微信通知设置 -->
                <div class="notification-channel">
                    <h4>💼 企业微信通知设置</h4>
                    <div class="channel-toggle">
                        <input type="checkbox" id="wechatEnabled">
                        <label for="wechatEnabled">启用企业微信通知</label>
                    </div>
                    <div class="form-group">
                        <label>Webhook Key</label>
                        <input type="text" id="wechatKey" placeholder="企业微信机器人的 Webhook Key">
                    </div>
                </div>
                
                <!-- WxPusher通知设置 -->
                <div class="notification-channel">
                    <h4>📨 WxPusher 消息通知设置</h4>
                    <div class="channel-toggle">
                        <input type="checkbox" id="wxpusherEnabled">
                        <label for="wxpusherEnabled">启用 WxPusher 通知</label>
                    </div>
                    <div class="form-group">
                        <label>APP Token</label>
                        <input type="text" id="wxpusherAppToken" placeholder="AT_xxx">
                        <div class="format-hint">
                            <a href="https://wxpusher.zjiecode.com/docs/#/" target="_blank" class="help-link">
                                访问 WxPusher 文档获取 Token 和 UID
                            </a>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>UID</label>
                        <input type="text" id="wxpusherUid" placeholder="UID_xxx">
                    </div>
                </div>
                
                <!-- 钉钉机器人通知设置 -->
                <div class="notification-channel">
                    <h4>🤖 钉钉机器人通知设置</h4>
                    <div class="channel-toggle">
                        <input type="checkbox" id="dingtalkEnabled">
                        <label for="dingtalkEnabled">启用钉钉机器人通知</label>
                    </div>
                    <div class="form-group">
                        <label>Access Token</label>
                        <input type="text" id="dingtalkAccessToken" placeholder="机器人的 Access Token">
                        <div class="format-hint">
                            <a href="https://open.dingtalk.com/document/orgapp/obtain-the-webhook-address-of-a-custom-robot" target="_blank" class="help-link">
                                获取钉钉机器人配置
                            </a>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>加签密钥</label>
                        <input type="text" id="dingtalkSecret" placeholder="安全设置中的加签密钥">
                    </div>
                </div>
                
                <button class="btn" onclick="saveNotificationSettings()">保存通知设置</button>
            </div>
        </div>
    </div>

    <!-- Add Account Modal -->
    <div class="modal" id="addAccountModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>添加新账号</h3>
                <button class="close" onclick="closeModal('addAccountModal')">&times;</button>
            </div>
            <div id="addAccountForm">
                <div class="form-group">
                    <label>账号名称</label>
                    <input type="text" id="accountName" required>
                </div>
                <div class="form-group">
                    <label>签到时间段（北京时间）</label>
                    <div class="time-range-input">
                        <input type="time" id="checkinTimeStart" value="06:30" required>
                        <span>至</span>
                        <input type="time" id="checkinTimeEnd" value="06:40" required>
                    </div>
                    <div class="format-hint">将在此时间段内随机执行签到</div>
                </div>
                <div class="form-group">
                    <label>检查间隔（秒）</label>
                    <input type="number" id="checkInterval" value="60" min="30" max="3600" required>
                    <div class="format-hint">在时间段内每隔多少秒检查一次是否需要签到</div>
                </div>
                <div class="form-group">
                    <label>重试次数</label>
                    <input type="number" id="retryCount" value="2" min="0" max="5" required>
                    <div class="format-hint">签到失败时的重试次数（0表示不重试）</div>
                </div>
                <div class="form-group">
                    <label>Cookie 数据</label>
                    <textarea id="tokenData" rows="6" placeholder='支持格式：
1. JSON格式: {"cookies": {"key": "value"}}
2. 分号分隔: key1=value1; key2=value2
3. 完整cookie: leaflow_session=xxx; remember_xxx=xxx; XSRF-TOKEN=xxx' required></textarea>
                    <div class="format-hint">从浏览器开发者工具(F12) → Network → 请求头 → Cookie 复制</div>
                </div>
                <div style="display: flex; gap: 10px; margin-top: 20px;">
                    <button type="button" class="btn btn-full" onclick="addAccount()">添加账号</button>
                    <button type="button" class="btn btn-danger" onclick="closeModal('addAccountModal')">取消</button>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Edit Account Modal -->
    <div class="modal" id="editAccountModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>修改账号</h3>
                <button class="close" onclick="closeModal('editAccountModal')">&times;</button>
            </div>
            <div id="editAccountForm">
                <input type="hidden" id="editAccountId">
                <div class="form-group">
                    <label>Cookie 数据</label>
                    <textarea id="editTokenData" rows="6" placeholder='支持格式：
1. JSON格式: {"cookies": {"key": "value"}}
2. 分号分隔: key1=value1; key2=value2
3. 完整cookie: leaflow_session=xxx; remember_xxx=xxx; XSRF-TOKEN=xxx' required></textarea>
                    <div class="format-hint">从浏览器开发者工具(F12) → Network → 请求头 → Cookie 复制</div>
                </div>
                <div style="display: flex; gap: 10px; margin-top: 20px;">
                    <button type="button" class="btn btn-full" onclick="updateAccountCookie()">保存修改</button>
                    <button type="button" class="btn btn-danger" onclick="closeModal('editAccountModal')">取消</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        // 全局变量
        let authToken = localStorage.getItem('authToken');
        
        // Toast notification function
        function showToast(message, type = 'info') {
            const toast = document.getElementById('toast');
            toast.className = `toast ${type}`;
            toast.textContent = message;
            toast.style.display = 'block';
            
            setTimeout(() => {
                toast.style.display = 'none';
            }, 3000);
        }

        // 显示登录错误
        function showLoginError(message) {
            const errorDiv = document.getElementById('loginError');
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
            setTimeout(() => {
                errorDiv.style.display = 'none';
            }, 5000);
        }

        // 处理登录
        async function handleLogin() {
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            
            if (!username || !password) {
                showLoginError('请输入用户名和密码');
                return;
            }
            
            const loginBtn = document.getElementById('loginBtn');
            loginBtn.disabled = true;
            loginBtn.textContent = '登录中...';

            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ username, password })
                });

                const data = await response.json();
                
                if (response.ok && data.token) {
                    authToken = data.token;
                    localStorage.setItem('authToken', authToken);
                    showToast('登录成功', 'success');
                    
                    document.getElementById('loginContainer').style.display = 'none';
                    document.getElementById('dashboard').style.display = 'block';
                    
                    loadDashboard();
                    loadAccounts();
                    loadNotificationSettings();
                } else {
                    showLoginError(data.message || '用户名或密码错误');
                }
            } catch (error) {
                console.error('Login error:', error);
                showLoginError('登录失败：' + error.message);
            } finally {
                loginBtn.disabled = false;
                loginBtn.textContent = '登录';
            }
        }

        // 监听回车键
        document.addEventListener('DOMContentLoaded', function() {
            document.getElementById('username').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    handleLogin();
                }
            });
            
            document.getElementById('password').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    handleLogin();
                }
            });
            
            // 检查是否已登录
            if (authToken) {
                fetch('/api/dashboard', {
                    headers: {
                        'Authorization': 'Bearer ' + authToken
                    }
                }).then(response => {
                    if (response.ok) {
                        document.getElementById('loginContainer').style.display = 'none';
                        document.getElementById('dashboard').style.display = 'block';
                        loadDashboard();
                        loadAccounts();
                        loadNotificationSettings();
                    } else {
                        localStorage.removeItem('authToken');
                        authToken = null;
                    }
                }).catch(error => {
                    console.error('Token check error:', error);
                    localStorage.removeItem('authToken');
                    authToken = null;
                });
            }
        });

        function logout() {
            localStorage.removeItem('authToken');
            authToken = null;
            location.reload();
        }

        async function apiCall(url, options = {}) {
            try {
                const response = await fetch(url, {
                    ...options,
                    headers: {
                        'Authorization': 'Bearer ' + authToken,
                        'Content-Type': 'application/json',
                        ...options.headers
                    }
                });

                if (response.status === 401) {
                    logout();
                    return;
                }

                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.message || 'Request failed');
                }
                return data;
            } catch (error) {
                console.error('API call error:', error);
                throw error;
            }
        }

        async function loadDashboard() {
            try {
                const data = await apiCall('/api/dashboard');
                if (!data) return;

                document.getElementById('totalAccounts').textContent = data.total_accounts || 0;
                document.getElementById('activeAccounts').textContent = data.enabled_accounts || 0;
                document.getElementById('totalCheckins').textContent = data.total_checkins || 0;
                document.getElementById('successRate').textContent = (data.success_rate || 0) + '%';

                const tbody = document.getElementById('todayCheckins');
                tbody.innerHTML = '';
                
                if (data.today_checkins && data.today_checkins.length > 0) {
                    data.today_checkins.forEach(checkin => {
                        const tr = document.createElement('tr');
                        const statusText = checkin.success ? '成功' : '失败';
                        const statusClass = checkin.success ? 'badge-success' : 'badge-danger';
                        const time = checkin.created_at ? new Date(checkin.created_at).toLocaleTimeString() : '-';
                        const retryTimes = checkin.retry_times || 0;
                        const retryBadge = retryTimes > 0 ? `<span class="badge badge-info">${retryTimes}</span>` : '-';
                        
                        tr.innerHTML = `
                            <td>${checkin.name || '-'}</td>
                            <td><span class="badge ${statusClass}">${statusText}</span></td>
                            <td>${checkin.message || '-'}</td>
                            <td>${retryBadge}</td>
                            <td>${time}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                } else {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #a0aec0;">暂无记录</td></tr>';
                }
            } catch (error) {
                console.error('Failed to load dashboard:', error);
            }
        }

        async function loadAccounts() {
            try {
                const accounts = await apiCall('/api/accounts');
                if (!accounts) return;

                const tbody = document.getElementById('accountsList');
                tbody.innerHTML = '';
                
                if (accounts && accounts.length > 0) {
                    accounts.forEach(account => {
                        const tr = document.createElement('tr');
                        const interval = account.check_interval || 60;
                        const retryCount = account.retry_count || 2;
                        
                        tr.innerHTML = `
                            <td>${account.name}</td>
                            <td>
                                <label class="switch">
                                    <input type="checkbox" ${account.enabled ? 'checked' : ''} onchange="toggleAccount(${account.id}, this.checked)">
                                    <span class="slider"></span>
                                </label>
                            </td>
                            <td>
                                <div class="time-range-input">
                                    <input type="time" value="${account.checkin_time_start || '06:30'}" onchange="updateAccountTime(${account.id}, 'start', this.value)">
                                    <span>-</span>
                                    <input type="time" value="${account.checkin_time_end || '06:40'}" onchange="updateAccountTime(${account.id}, 'end', this.value)">
                                </div>
                            </td>
                            <td>
                                <div class="interval-input">
                                    <input type="number" value="${interval}" min="30" max="3600" onchange="updateAccountInterval(${account.id}, this.value)">
                                    <span>秒</span>
                                </div>
                            </td>
                            <td>
                                <div class="interval-input">
                                    <input type="number" value="${retryCount}" min="0" max="5" onchange="updateAccountRetry(${account.id}, this.value)">
                                    <span>次</span>
                                </div>
                            </td>
                            <td>
                                <button class="btn btn-success btn-sm" onclick="manualCheckin(${account.id})">立即签到</button>
                                <button class="btn btn-info btn-sm" onclick="showEditAccountModal(${account.id}, '${account.name}')">修改</button>
                                <button class="btn btn-danger btn-sm" onclick="deleteAccount(${account.id})">删除</button>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                } else {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #a0aec0;">暂无账号</td></tr>';
                }
            } catch (error) {
                console.error('Failed to load accounts:', error);
            }
        }

        async function loadNotificationSettings() {
            try {
                const settings = await apiCall('/api/notification');
                if (!settings) return;

                // 主开关
                document.getElementById('notifyEnabled').checked = settings.enabled === true || settings.enabled === 1;
                
                // Telegram设置
                document.getElementById('telegramEnabled').checked = settings.telegram_enabled === true || settings.telegram_enabled === 1;
                document.getElementById('tgBotToken').value = settings.telegram_bot_token || '';
                document.getElementById('tgUserId').value = settings.telegram_user_id || '';
                
                // 企业微信设置
                document.getElementById('wechatEnabled').checked = settings.wechat_enabled === true || settings.wechat_enabled === 1;
                document.getElementById('wechatKey').value = settings.wechat_webhook_key || '';
                
                // WxPusher设置
                document.getElementById('wxpusherEnabled').checked = settings.wxpusher_enabled === true || settings.wxpusher_enabled === 1;
                document.getElementById('wxpusherAppToken').value = settings.wxpusher_app_token || '';
                document.getElementById('wxpusherUid').value = settings.wxpusher_uid || '';
                
                // 钉钉设置
                document.getElementById('dingtalkEnabled').checked = settings.dingtalk_enabled === true || settings.dingtalk_enabled === 1;
                document.getElementById('dingtalkAccessToken').value = settings.dingtalk_access_token || '';
                document.getElementById('dingtalkSecret').value = settings.dingtalk_secret || '';
            } catch (error) {
                console.error('Failed to load notification settings:', error);
            }
        }

        async function toggleAccount(id, enabled) {
            try {
                await apiCall(`/api/accounts/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ enabled })
                });
                loadAccounts();
            } catch (error) {
                showToast('操作失败', 'error');
            }
        }

        async function updateAccountTime(id, type, value) {
            try {
                const data = {};
                if (type === 'start') {
                    data.checkin_time_start = value;
                } else {
                    data.checkin_time_end = value;
                }
                
                await apiCall(`/api/accounts/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify(data)
                });
            } catch (error) {
                showToast('操作失败', 'error');
            }
        }

        async function updateAccountInterval(id, value) {
            try {
                await apiCall(`/api/accounts/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ check_interval: parseInt(value) })
                });
            } catch (error) {
                showToast('操作失败', 'error');
            }
        }
        
        async function updateAccountRetry(id, value) {
            try {
                await apiCall(`/api/accounts/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ retry_count: parseInt(value) })
                });
            } catch (error) {
                showToast('操作失败', 'error');
            }
        }

        async function manualCheckin(id) {
            if (confirm('确定立即执行签到吗？')) {
                try {
                    await apiCall(`/api/checkin/manual/${id}`, { method: 'POST' });
                    showToast('签到任务已触发', 'success');
                    setTimeout(loadDashboard, 2000);
                } catch (error) {
                    showToast('操作失败', 'error');
                }
            }
        }

        async function deleteAccount(id) {
            if (confirm('确定删除此账号吗？')) {
                try {
                    await apiCall(`/api/accounts/${id}`, { method: 'DELETE' });
                    showToast('账号删除成功', 'success');
                    loadAccounts();
                } catch (error) {
                    showToast('操作失败', 'error');
                }
            }
        }

        async function clearCheckinHistory(type) {
            const message = type === 'today' ? '确定清空今日签到记录吗？' : '确定清空所有签到记录吗？';
            if (confirm(message)) {
                try {
                    await apiCall('/api/checkin/clear', {
                        method: 'POST',
                        body: JSON.stringify({ type })
                    });
                    showToast('清空成功', 'success');
                    loadDashboard();
                } catch (error) {
                    showToast('操作失败: ' + error.message, 'error');
                }
            }
        }

        async function saveNotificationSettings() {
            try {
                const settings = {
                    enabled: document.getElementById('notifyEnabled').checked,
                    telegram_enabled: document.getElementById('telegramEnabled').checked,
                    telegram_bot_token: document.getElementById('tgBotToken').value,
                    telegram_user_id: document.getElementById('tgUserId').value,
                    wechat_enabled: document.getElementById('wechatEnabled').checked,
                    wechat_webhook_key: document.getElementById('wechatKey').value,
                    wxpusher_enabled: document.getElementById('wxpusherEnabled').checked,
                    wxpusher_app_token: document.getElementById('wxpusherAppToken').value,
                    wxpusher_uid: document.getElementById('wxpusherUid').value,
                    dingtalk_enabled: document.getElementById('dingtalkEnabled').checked,
                    dingtalk_access_token: document.getElementById('dingtalkAccessToken').value,
                    dingtalk_secret: document.getElementById('dingtalkSecret').value
                };

                await apiCall('/api/notification', {
                    method: 'PUT',
                    body: JSON.stringify(settings)
                });
                showToast('设置保存成功', 'success');
                
                setTimeout(loadNotificationSettings, 500);
            } catch (error) {
                showToast('操作失败: ' + error.message, 'error');
            }
        }

        async function testNotification() {
            try {
                await apiCall('/api/test/notification', { method: 'POST' });
                showToast('测试通知已发送', 'info');
            } catch (error) {
                showToast('发送失败: ' + error.message, 'error');
            }
        }

        function showAddAccountModal() {
            document.getElementById('addAccountModal').style.display = 'flex';
        }
        
        function showEditAccountModal(accountId, accountName) {
            document.getElementById('editAccountId').value = accountId;
            document.getElementById('editAccountModal').style.display = 'flex';
        }

        function closeModal(modalId) {
            document.getElementById(modalId).style.display = 'none';
            
            if (modalId === 'addAccountModal') {
                document.getElementById('accountName').value = '';
                document.getElementById('checkinTimeStart').value = '06:30';
                document.getElementById('checkinTimeEnd').value = '06:40';
                document.getElementById('checkInterval').value = '60';
                document.getElementById('retryCount').value = '2';
                document.getElementById('tokenData').value = '';
            } else if (modalId === 'editAccountModal') {
                document.getElementById('editAccountId').value = '';
                document.getElementById('editTokenData').value = '';
            }
        }

        async function addAccount() {
            try {
                const account = {
                    name: document.getElementById('accountName').value,
                    checkin_time_start: document.getElementById('checkinTimeStart').value,
                    checkin_time_end: document.getElementById('checkinTimeEnd').value,
                    check_interval: parseInt(document.getElementById('checkInterval').value),
                    retry_count: parseInt(document.getElementById('retryCount').value),
                    token_data: document.getElementById('tokenData').value
                };

                if (!account.name || !account.token_data) {
                    showToast('请填写完整信息', 'error');
                    return;
                }

                await apiCall('/api/accounts', {
                    method: 'POST',
                    body: JSON.stringify(account)
                });
                
                showToast('账号添加成功', 'success');
                closeModal('addAccountModal');
                loadAccounts();
            } catch (error) {
                showToast('格式无效: ' + error.message, 'error');
            }
        }
        
        async function updateAccountCookie() {
            try {
                const accountId = document.getElementById('editAccountId').value;
                const tokenData = document.getElementById('editTokenData').value;
                
                if (!tokenData) {
                    showToast('请输入Cookie数据', 'error');
                    return;
                }
                
                await apiCall(`/api/accounts/${accountId}`, {
                    method: 'PUT',
                    body: JSON.stringify({ token_data: tokenData })
                });
                
                showToast('账号修改成功', 'success');
                closeModal('editAccountModal');
                loadAccounts();
            } catch (error) {
                showToast('修改失败: ' + error.message, 'error');
            }
        }

        // Close modal when clicking outside
        window.onclick = function(event) {
            const modals = ['addAccountModal', 'editAccountModal'];
            modals.forEach(modalId => {
                const modal = document.getElementById(modalId);
                if (event.target == modal) {
                    closeModal(modalId);
                }
            });
        }
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    try:
        # Start scheduler
        scheduler.start()
        
        # Log startup information
        logger.info(f"Starting Leaflow Control Panel on port {PORT}")
        logger.info(f"Database type: {DB_TYPE}")
        if DB_TYPE == 'mysql':
            logger.info(f"MySQL connection: {DB_HOST}:{DB_PORT}/{DB_NAME} as {DB_USER}")
        logger.info(f"Admin username: {ADMIN_USERNAME}")
        logger.info(f"Access the panel at: http://localhost:{PORT}")
        logger.info(f"Timezone: Asia/Shanghai (UTC+8)")
        logger.info(f"MySQL keepalive strategy: Check every 5 minutes, ping every 30 minutes")
        logger.info(f"Data caching enabled: Account cache refreshes on data changes")
        
        # Start Flask app
        app.run(host='0.0.0.0', port=PORT, debug=False)
        
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        raise
