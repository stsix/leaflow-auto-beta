#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LeafLow Auto Check-in Control Panel
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
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import jwt
import logging
from urllib.parse import urlparse, parse_qs
import random

# Configuration
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', secrets.token_hex(32))
CORS(app)

# Environment variables
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
PORT = int(os.getenv('PORT', '8181'))

# Parse MySQL DSN if provided
MYSQL_DSN = os.getenv('MYSQL_DSN', '')
DB_TYPE = 'sqlite'
DB_HOST = 'localhost'
DB_PORT = 3306
DB_NAME = 'leaflow_checkin'
DB_USER = 'root'
DB_PASSWORD = ''

if MYSQL_DSN:
    try:
        # Parse DSN format: mysql://user:password@host:port/database
        parsed = urlparse(MYSQL_DSN)
        if parsed.scheme == 'mysql':
            DB_TYPE = 'mysql'
            DB_HOST = parsed.hostname or 'localhost'
            DB_PORT = parsed.port or 3306
            DB_NAME = parsed.path.lstrip('/') if parsed.path else 'leaflow_checkin'
            
            # Handle username with special characters (e.g., 4CLAMfGH5AQqJym.root)
            if parsed.username:
                # Split by '.' to handle format like "4CLAMfGH5AQqJym.root"
                username_parts = parsed.username.split('.')
                if len(username_parts) > 1:
                    DB_USER = username_parts[-1]  # Take the last part as actual username
                else:
                    DB_USER = parsed.username
            
            DB_PASSWORD = parsed.password or ''
    except Exception as e:
        print(f"Error parsing MYSQL_DSN: {e}, falling back to SQLite")
        DB_TYPE = 'sqlite'
else:
    # Fallback to individual environment variables
    DB_TYPE = os.getenv('DB_TYPE', 'sqlite')
    if DB_TYPE == 'mysql':
        DB_HOST = os.getenv('DB_HOST', 'localhost')
        DB_PORT = int(os.getenv('DB_PORT', '3306'))
        DB_NAME = os.getenv('DB_NAME', 'leaflow_checkin')
        DB_USER = os.getenv('DB_USER', 'root')
        DB_PASSWORD = os.getenv('DB_PASSWORD', '')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('control_panel.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.lock = threading.Lock()
        if DB_TYPE == 'mysql':
            import pymysql
            logger.info(f"Connecting to MySQL: {DB_HOST}:{DB_PORT}/{DB_NAME} as {DB_USER}")
            self.conn = pymysql.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                charset='utf8mb4',
                autocommit=True
            )
            self.db_type = 'mysql'
        else:
            logger.info("Using SQLite database")
            self.conn = sqlite3.connect('leaflow_checkin.db', check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.db_type = 'sqlite'
        self.init_tables()
    
    def init_tables(self):
        with self.lock:
            cursor = self.conn.cursor()
            
            # Adjust SQL syntax based on database type
            if self.db_type == 'mysql':
                # MySQL syntax
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(255) UNIQUE NOT NULL,
                        token_data TEXT NOT NULL,
                        enabled BOOLEAN DEFAULT TRUE,
                        checkin_time VARCHAR(5) DEFAULT '01:00',
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                    )
                ''')
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS notification_settings (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        enabled BOOLEAN DEFAULT TRUE,
                        telegram_bot_token TEXT,
                        telegram_user_id TEXT,
                        wechat_webhook_key TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                ''')
                
                # Check if notification settings exist
                cursor.execute('SELECT COUNT(*) FROM notification_settings')
                count = cursor.fetchone()[0]
            else:
                # SQLite syntax
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(255) UNIQUE NOT NULL,
                        token_data TEXT NOT NULL,
                        enabled BOOLEAN DEFAULT 1,
                        checkin_time VARCHAR(5) DEFAULT '01:00',
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                    )
                ''')
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS notification_settings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        enabled BOOLEAN DEFAULT 1,
                        telegram_bot_token TEXT,
                        telegram_user_id TEXT,
                        wechat_webhook_key TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Check if notification settings exist
                cursor.execute('SELECT COUNT(*) as count FROM notification_settings')
                result = cursor.fetchone()
                count = result['count'] if self.db_type == 'sqlite' else result[0]
            
            # Initialize notification settings if not exists
            if count == 0:
                cursor.execute('''
                    INSERT INTO notification_settings (enabled, telegram_bot_token, telegram_user_id, wechat_webhook_key)
                    VALUES (%s, %s, %s, %s)
                ''' if self.db_type == 'mysql' else '''
                    INSERT INTO notification_settings (enabled, telegram_bot_token, telegram_user_id, wechat_webhook_key)
                    VALUES (?, ?, ?, ?)
                ''', (1, os.getenv('TG_BOT_TOKEN', ''), os.getenv('TG_USER_ID', ''), os.getenv('QYWX_KEY', '')))
            
            if self.db_type == 'sqlite':
                self.conn.commit()
    
    def execute(self, query, params=None):
        with self.lock:
            cursor = self.conn.cursor()
            # Convert ? to %s for MySQL
            if self.db_type == 'mysql' and query:
                query = query.replace('?', '%s')
            
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            
            if self.db_type == 'sqlite':
                self.conn.commit()
            return cursor
    
    def fetchone(self, query, params=None):
        cursor = self.execute(query, params)
        result = cursor.fetchone()
        if result and self.db_type == 'mysql':
            # Convert MySQL result to dict-like object
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, result))
        return result
    
    def fetchall(self, query, params=None):
        cursor = self.execute(query, params)
        results = cursor.fetchall()
        if results and self.db_type == 'mysql':
            # Convert MySQL results to list of dicts
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in results]
        return results

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
                # Wrap in cookies object if not already
                return {'cookies': data}
        except json.JSONDecodeError:
            pass
    
    # Parse as semicolon-separated cookie string
    cookies = {}
    # Split by semicolon and handle each cookie pair
    cookie_pairs = re.split(r';\s*', cookie_input)
    
    for pair in cookie_pairs:
        if '=' in pair:
            key, value = pair.split('=', 1)
            key = key.strip()
            value = value.strip()
            if key:  # Only add non-empty keys
                cookies[key] = value
    
    if cookies:
        return {'cookies': cookies}
    
    raise ValueError("Invalid cookie format")

db = Database()

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
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token has expired!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Token is invalid!'}), 401
        
        return f(*args, **kwargs)
    
    return decorated

# Scheduler for automatic check-ins
class CheckinScheduler:
    def __init__(self):
        self.scheduler_thread = None
        self.running = False
        
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
        while self.running:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
    
    def schedule_checkins(self):
        schedule.clear()
        accounts = db.fetchall('SELECT * FROM accounts WHERE enabled = 1')
        
        for account in accounts:
            checkin_time = account['checkin_time'] if 'checkin_time' in account else '01:00'
            schedule.every().day.at(checkin_time).do(self.perform_checkin, account['id'])
            logger.info(f"Scheduled check-in for account {account['name']} at {checkin_time}")
    
    def perform_checkin(self, account_id):
        account = db.fetchone('SELECT * FROM accounts WHERE id = ?', (account_id,))
        if not account or not account['enabled']:
            return
        
        try:
            # Add random delay for multiple accounts
            delay = random.randint(30, 60)
            time.sleep(delay)
            
            # Import checkin module
            try:
                from checkin_token import LeafLowTokenCheckin
            except ImportError:
                logger.error("checkin_token module not found")
                return
            
            # Prepare account data for check-in
            token_data = json.loads(account['token_data'])
            account_data = {'token_data': token_data, 'enabled': True}
            
            # Create temporary config
            temp_config = {
                'settings': {
                    'log_level': 'INFO',
                    'retry_delay': 3,
                    'timeout': 30,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                },
                'accounts': [account_data]
            }
            
            # Save temporary config
            with open('temp_config.json', 'w') as f:
                json.dump(temp_config, f)
            
            # Perform check-in
            checkin = LeafLowTokenCheckin('temp_config.json')
            success, message = checkin.perform_token_checkin(account_data, account['name'])
            
            # Record history
            db.execute('''
                INSERT INTO checkin_history (account_id, success, message, checkin_date)
                VALUES (?, ?, ?, ?)
            ''', (account_id, success, message, datetime.now().date()))
            
            # Send notification if enabled
            self.send_notification(account['name'], success, message)
            
            logger.info(f"Check-in for {account['name']}: {'Success' if success else 'Failed'} - {message}")
            
        except Exception as e:
            logger.error(f"Check-in error for account {account_id}: {str(e)}")
            db.execute('''
                INSERT INTO checkin_history (account_id, success, message, checkin_date)
                VALUES (?, ?, ?, ?)
            ''', (account_id, False, str(e), datetime.now().date()))
    
    def send_notification(self, account_name, success, message):
        settings = db.fetchone('SELECT * FROM notification_settings WHERE id = 1')
        if not settings or not settings['enabled']:
            return
        
        try:
            from notify import send
            title = f"LeafLow Check-in: {account_name}"
            content = f"{'✅ Success' if success else '❌ Failed'}: {message}"
            
            config = {}
            if settings['telegram_bot_token'] and settings['telegram_user_id']:
                config['TG_BOT_TOKEN'] = settings['telegram_bot_token']
                config['TG_USER_ID'] = settings['telegram_user_id']
            if settings['wechat_webhook_key']:
                config['QYWX_KEY'] = settings['wechat_webhook_key']
            
            if config:
                send(title, content, **config)
        except Exception as e:
            logger.error(f"Notification error: {str(e)}")

scheduler = CheckinScheduler()

# Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = jwt.encode({
            'user': username,
            'exp': datetime.utcnow() + timedelta(days=7)
        }, app.config['SECRET_KEY'], algorithm='HS256')
        
        return jsonify({'token': token, 'message': 'Login successful'})
    
    return jsonify({'message': 'Invalid credentials'}), 401

@app.route('/api/dashboard', methods=['GET'])
@token_required
def dashboard():
    # Get statistics
    total_accounts = db.fetchone('SELECT COUNT(*) as count FROM accounts')['count']
    enabled_accounts = db.fetchone('SELECT COUNT(*) as count FROM accounts WHERE enabled = 1')['count']
    
    # Today's check-ins
    today = datetime.now().date()
    today_checkins = db.fetchall('''
        SELECT a.name, ch.success, ch.message, ch.created_at
        FROM checkin_history ch
        JOIN accounts a ON ch.account_id = a.id
        WHERE ch.checkin_date = ?
        ORDER BY ch.created_at DESC
    ''', (today,))
    
    # Overall statistics
    total_checkins = db.fetchone('SELECT COUNT(*) as count FROM checkin_history')['count']
    successful_checkins = db.fetchone('SELECT COUNT(*) as count FROM checkin_history WHERE success = 1')['count']
    
    # Recent history (last 7 days)
    recent_history = db.fetchall('''
        SELECT checkin_date, 
               COUNT(*) as total,
               SUM(success) as successful
        FROM checkin_history
        WHERE checkin_date >= date('now', '-7 days')
        GROUP BY checkin_date
        ORDER BY checkin_date DESC
    ''')
    
    return jsonify({
        'total_accounts': total_accounts,
        'enabled_accounts': enabled_accounts,
        'today_checkins': today_checkins,
        'total_checkins': total_checkins,
        'successful_checkins': successful_checkins,
        'success_rate': round(successful_checkins / total_checkins * 100, 2) if total_checkins > 0 else 0,
        'recent_history': recent_history
    })

@app.route('/api/accounts', methods=['GET'])
@token_required
def get_accounts():
    accounts = db.fetchall('SELECT id, name, enabled, checkin_time, created_at FROM accounts')
    return jsonify(accounts)

@app.route('/api/accounts', methods=['POST'])
@token_required
def add_account():
    data = request.json
    name = data.get('name')
    cookie_input = data.get('token_data', data.get('cookie_data', ''))
    checkin_time = data.get('checkin_time', '01:00')
    
    if not name or not cookie_input:
        return jsonify({'message': 'Name and cookie data are required'}), 400
    
    try:
        # Parse cookie input (supports both JSON and string format)
        if isinstance(cookie_input, str):
            token_data = parse_cookie_string(cookie_input)
        else:
            token_data = cookie_input
        
        db.execute('''
            INSERT INTO accounts (name, token_data, checkin_time)
            VALUES (?, ?, ?)
        ''', (name, json.dumps(token_data), checkin_time))
        
        scheduler.schedule_checkins()
        return jsonify({'message': 'Account added successfully'})
    except ValueError as e:
        return jsonify({'message': f'Invalid cookie format: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'}), 400

@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
@token_required
def update_account(account_id):
    data = request.json
    
    updates = []
    params = []
    
    if 'enabled' in data:
        updates.append('enabled = ?')
        params.append(data['enabled'])
    
    if 'checkin_time' in data:
        updates.append('checkin_time = ?')
        params.append(data['checkin_time'])
    
    if 'token_data' in data or 'cookie_data' in data:
        cookie_input = data.get('token_data', data.get('cookie_data', ''))
        try:
            if isinstance(cookie_input, str):
                token_data = parse_cookie_string(cookie_input)
            else:
                token_data = cookie_input
            updates.append('token_data = ?')
            params.append(json.dumps(token_data))
        except ValueError as e:
            return jsonify({'message': f'Invalid cookie format: {str(e)}'}), 400
    
    if updates:
        params.append(account_id)
        if db.db_type == 'mysql':
            db.execute(f'''
                UPDATE accounts 
                SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', params)
        else:
            db.execute(f'''
                UPDATE accounts 
                SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', params)
        
        scheduler.schedule_checkins()
        return jsonify({'message': 'Account updated successfully'})
    
    return jsonify({'message': 'No updates provided'}), 400

@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@token_required
def delete_account(account_id):
    db.execute('DELETE FROM checkin_history WHERE account_id = ?', (account_id,))
    db.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
    scheduler.schedule_checkins()
    return jsonify({'message': 'Account deleted successfully'})

@app.route('/api/notification', methods=['GET'])
@token_required
def get_notification_settings():
    settings = db.fetchone('SELECT * FROM notification_settings WHERE id = 1')
    if settings:
        return jsonify(settings)
    return jsonify({})

@app.route('/api/notification', methods=['PUT'])
@token_required
def update_notification_settings():
    data = request.json
    
    db.execute('''
        UPDATE notification_settings
        SET enabled = ?, telegram_bot_token = ?, telegram_user_id = ?, 
            wechat_webhook_key = ?
        WHERE id = 1
    ''', (
        data.get('enabled', True),
        data.get('telegram_bot_token', ''),
        data.get('telegram_user_id', ''),
        data.get('wechat_webhook_key', '')
    ))
    
    return jsonify({'message': 'Notification settings updated'})

@app.route('/api/checkin/manual/<int:account_id>', methods=['POST'])
@token_required
def manual_checkin(account_id):
    threading.Thread(target=scheduler.perform_checkin, args=(account_id,), daemon=True).start()
    return jsonify({'message': 'Manual check-in triggered'})

# HTML Template with bilingual support and mobile optimization
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>LeafLow Auto Check-in Control Panel</title>
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
        
        /* Language Switcher */
        .lang-switcher {
            display: flex;
            background: #f0f0f0;
            border-radius: 6px;
            overflow: hidden;
        }
        .lang-btn {
            padding: 8px 16px;
            background: transparent;
            border: none;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.3s;
        }
        .lang-btn.active {
            background: white;
            color: #667eea;
            font-weight: 600;
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
        .badge-warning { 
            background: #feebc8; 
            color: #744210; 
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
        
        /* Form Row */
        .form-row { 
            display: grid; 
            grid-template-columns: 1fr 1fr; 
            gap: 15px; 
        }
        
        /* Cookie format hint */
        .format-hint {
            background: #f7fafc;
            border-left: 3px solid #667eea;
            padding: 12px;
            margin-top: 10px;
            border-radius: 6px;
            font-size: 13px;
            color: #4a5568;
        }
        .format-hint code {
            background: #e2e8f0;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'Courier New', monospace;
        }
        
        /* Action Buttons Container */
        .action-buttons {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        
        /* Responsive Design */
        @media (max-width: 768px) {
            .login-box {
                padding: 30px 20px;
            }
            
            .dashboard {
                padding: 15px;
            }
            
            .header {
                padding: 20px;
            }
            
            .header h1 {
                font-size: 20px;
            }
            
            .stats-grid {
                grid-template-columns: 1fr;
            }
            
            .section {
                padding: 20px;
            }
            
            .section h2 {
                font-size: 18px;
            }
            
            .table {
                font-size: 14px;
            }
            
            .table th, .table td {
                padding: 10px 8px;
            }
            
            .form-row {
                grid-template-columns: 1fr;
            }
            
            .modal-content {
                padding: 20px;
            }
            
            .action-buttons {
                flex-direction: column;
            }
            
            .action-buttons .btn {
                width: 100%;
            }
            
            /* Hide less important columns on mobile */
            .hide-mobile {
                display: none;
            }
        }
        
        @media (max-width: 480px) {
            .header-content {
                flex-direction: column;
                align-items: stretch;
            }
            
            .header-actions {
                justify-content: space-between;
            }
            
            .stat-card {
                padding: 20px;
            }
            
            .stat-card .value {
                font-size: 28px;
            }
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
    </style>
</head>
<body>
    <!-- Language Data -->
    <script>
        const translations = {
            zh: {
                login: {
                    title: '🔐 管理员登录',
                    username: '用户名',
                    password: '密码',
                    button: '登录',
                    error: '登录失败'
                },
                dashboard: {
                    title: '📊 LeafLow 自动签到控制面板',
                    logout: '退出',
                    stats: {
                        totalAccounts: '账号总数',
                        activeAccounts: '活跃账号',
                        totalCheckins: '签到总数',
                        successRate: '成功率'
                    },
                    todayCheckins: {
                        title: '📅 今日签到记录',
                        account: '账号',
                        status: '状态',
                        message: '消息',
                        time: '时间',
                        success: '成功',
                        failed: '失败'
                    },
                    accounts: {
                        title: '👥 账号管理',
                        addButton: '+ 添加账号',
                        name: '名称',
                        status: '状态',
                        checkinTime: '签到时间',
                        actions: '操作',
                        checkinNow: '立即签到',
                        delete: '删除',
                        confirmDelete: '确定删除此账号吗？',
                        confirmCheckin: '确定立即执行签到吗？'
                    },
                    notifications: {
                        title: '🔔 通知设置',
                        enable: '启用通知',
                        telegramBot: 'Telegram Bot Token',
                        telegramUser: 'Telegram User ID',
                        wechatKey: '企业微信 Webhook Key',
                        save: '保存设置'
                    }
                },
                modal: {
                    addAccount: '添加新账号',
                    accountName: '账号名称',
                    checkinTime: '签到时间',
                    cookieData: 'Cookie 数据',
                    cookieHint: '支持两种格式：',
                    format1: '1. JSON格式：{"cookies": {"key": "value"}}',
                    format2: '2. 字符串格式：key1=value1; key2=value2',
                    addButton: '添加账号',
                    cancel: '取消'
                },
                messages: {
                    loginSuccess: '登录成功',
                    loginFailed: '用户名或密码错误',
                    accountAdded: '账号添加成功',
                    accountDeleted: '账号删除成功',
                    settingsSaved: '设置保存成功',
                    checkinTriggered: '签到任务已触发',
                    invalidFormat: '格式无效',
                    error: '操作失败'
                }
            },
            en: {
                login: {
                    title: '🔐 Admin Login',
                    username: 'Username',
                    password: 'Password',
                    button: 'Login',
                    error: 'Login failed'
                },
                dashboard: {
                    title: '📊 LeafLow Auto Check-in Panel',
                    logout: 'Logout',
                    stats: {
                        totalAccounts: 'Total Accounts',
                        activeAccounts: 'Active Accounts',
                        totalCheckins: 'Total Check-ins',
                        successRate: 'Success Rate'
                    },
                    todayCheckins: {
                        title: '📅 Today\'s Check-ins',
                        account: 'Account',
                        status: 'Status',
                        message: 'Message',
                        time: 'Time',
                        success: 'Success',
                        failed: 'Failed'
                    },
                    accounts: {
                        title: '👥 Account Management',
                        addButton: '+ Add Account',
                        name: 'Name',
                        status: 'Status',
                        checkinTime: 'Check-in Time',
                        actions: 'Actions',
                        checkinNow: 'Check-in Now',
                        delete: 'Delete',
                        confirmDelete: 'Delete this account?',
                        confirmCheckin: 'Perform check-in now?'
                    },
                    notifications: {
                        title: '🔔 Notification Settings',
                        enable: 'Enable Notifications',
                        telegramBot: 'Telegram Bot Token',
                        telegramUser: 'Telegram User ID',
                        wechatKey: 'WeChat Webhook Key',
                        save: 'Save Settings'
                    }
                },
                modal: {
                    addAccount: 'Add New Account',
                    accountName: 'Account Name',
                    checkinTime: 'Check-in Time',
                    cookieData: 'Cookie Data',
                    cookieHint: 'Supports two formats:',
                    format1: '1. JSON: {"cookies": {"key": "value"}}',
                    format2: '2. String: key1=value1; key2=value2',
                    addButton: 'Add Account',
                    cancel: 'Cancel'
                },
                messages: {
                    loginSuccess: 'Login successful',
                    loginFailed: 'Invalid credentials',
                    accountAdded: 'Account added successfully',
                    accountDeleted: 'Account deleted successfully',
                    settingsSaved: 'Settings saved successfully',
                    checkinTriggered: 'Check-in task triggered',
                    invalidFormat: 'Invalid format',
                    error: 'Operation failed'
                }
            }
        };
        
        let currentLang = localStorage.getItem('language') || 'zh';
        let authToken = localStorage.getItem('authToken');
        
        function t(key) {
            const keys = key.split('.');
            let value = translations[currentLang];
            for (const k of keys) {
                value = value[k];
            }
            return value || key;
        }
        
        function setLanguage(lang) {
            currentLang = lang;
            localStorage.setItem('language', lang);
            updateUILanguage();
        }
        
        function updateUILanguage() {
            // Update all elements with data-i18n attribute
            document.querySelectorAll('[data-i18n]').forEach(element => {
                const key = element.getAttribute('data-i18n');
                element.textContent = t(key);
            });
            
            // Update placeholders
            document.querySelectorAll('[data-i18n-placeholder]').forEach(element => {
                const key = element.getAttribute('data-i18n-placeholder');
                element.placeholder = t(key);
            });
            
            // Update language switcher
            document.querySelectorAll('.lang-btn').forEach(btn => {
                btn.classList.toggle('active', btn.getAttribute('data-lang') === currentLang);
            });
        }
    </script>

    <!-- Toast Notification -->
    <div id="toast" class="toast"></div>

    <!-- Login Container -->
    <div class="login-container" id="loginContainer">
        <div class="login-box">
            <h2 data-i18n="login.title">🔐 管理员登录</h2>
            <form id="loginForm">
                <div class="form-group">
                    <label data-i18n="login.username">用户名</label>
                    <input type="text" id="username" required>
                </div>
                <div class="form-group">
                    <label data-i18n="login.password">密码</label>
                    <input type="password" id="password" required>
                </div>
                <button type="submit" class="btn btn-full" data-i18n="login.button">登录</button>
            </form>
        </div>
    </div>

    <!-- Dashboard -->
    <div class="dashboard" id="dashboard">
        <div class="container">
            <div class="header">
                <div class="header-content">
                    <h1 data-i18n="dashboard.title">📊 LeafLow 自动签到控制面板</h1>
                    <div class="header-actions">
                        <div class="lang-switcher">
                            <button class="lang-btn" data-lang="zh" onclick="setLanguage('zh')">中文</button>
                            <button class="lang-btn" data-lang="en" onclick="setLanguage('en')">English</button>
                        </div>
                        <button class="btn btn-danger btn-sm" onclick="logout()" data-i18n="dashboard.logout">退出</button>
                    </div>
                </div>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <h3 data-i18n="dashboard.stats.totalAccounts">账号总数</h3>
                    <div class="value" id="totalAccounts">0</div>
                </div>
                <div class="stat-card">
                    <h3 data-i18n="dashboard.stats.activeAccounts">活跃账号</h3>
                    <div class="value" id="activeAccounts">0</div>
                </div>
                <div class="stat-card">
                    <h3 data-i18n="dashboard.stats.totalCheckins">签到总数</h3>
                    <div class="value" id="totalCheckins">0</div>
                </div>
                <div class="stat-card">
                    <h3 data-i18n="dashboard.stats.successRate">成功率</h3>
                    <div class="value" id="successRate">0%</div>
                </div>
            </div>

            <div class="section">
                <h2 data-i18n="dashboard.todayCheckins.title">📅 今日签到记录</h2>
                <div class="table-wrapper">
                    <table class="table">
                        <thead>
                            <tr>
                                <th data-i18n="dashboard.todayCheckins.account">账号</th>
                                <th data-i18n="dashboard.todayCheckins.status">状态</th>
                                <th class="hide-mobile" data-i18n="dashboard.todayCheckins.message">消息</th>
                                <th data-i18n="dashboard.todayCheckins.time">时间</th>
                            </tr>
                        </thead>
                        <tbody id="todayCheckins">
                            <tr>
                                <td colspan="4" style="text-align: center; color: #a0aec0;">
                                    <div class="spinner"></div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <div class="section-header">
                    <h2 data-i18n="dashboard.accounts.title">👥 账号管理</h2>
                    <button class="btn btn-success btn-sm" onclick="showAddAccountModal()" data-i18n="dashboard.accounts.addButton">+ 添加账号</button>
                </div>
                <div class="table-wrapper">
                    <table class="table">
                        <thead>
                            <tr>
                                <th data-i18n="dashboard.accounts.name">名称</th>
                                <th data-i18n="dashboard.accounts.status">状态</th>
                                <th class="hide-mobile" data-i18n="dashboard.accounts.checkinTime">签到时间</th>
                                <th data-i18n="dashboard.accounts.actions">操作</th>
                            </tr>
                        </thead>
                        <tbody id="accountsList">
                            <tr>
                                <td colspan="4" style="text-align: center; color: #a0aec0;">
                                    <div class="spinner"></div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="section">
                <h2 data-i18n="dashboard.notifications.title">🔔 通知设置</h2>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="notifyEnabled"> 
                        <span data-i18n="dashboard.notifications.enable">启用通知</span>
                    </label>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label data-i18n="dashboard.notifications.telegramBot">Telegram Bot Token</label>
                        <input type="text" id="tgBotToken" placeholder="Bot token">
                    </div>
                    <div class="form-group">
                        <label data-i18n="dashboard.notifications.telegramUser">Telegram User ID</label>
                        <input type="text" id="tgUserId" placeholder="User ID">
                    </div>
                </div>
                <div class="form-group">
                    <label data-i18n="dashboard.notifications.wechatKey">企业微信 Webhook Key</label>
                    <input type="text" id="wechatKey" placeholder="Webhook key">
                </div>
                <button class="btn btn-sm" onclick="saveNotificationSettings()" data-i18n="dashboard.notifications.save">保存设置</button>
            </div>
        </div>
    </div>

    <!-- Add Account Modal -->
    <div class="modal" id="addAccountModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 data-i18n="modal.addAccount">添加新账号</h3>
                <button class="close" onclick="closeModal()">&times;</button>
            </div>
            <form id="addAccountForm">
                <div class="form-group">
                    <label data-i18n="modal.accountName">账号名称</label>
                    <input type="text" id="accountName" required>
                </div>
                <div class="form-group">
                    <label data-i18n="modal.checkinTime">签到时间</label>
                    <input type="time" id="checkinTime" value="01:00" required>
                </div>
                <div class="form-group">
                    <label data-i18n="modal.cookieData">Cookie 数据</label>
                    <textarea id="tokenData" rows="6" placeholder='{"cookies": {"key": "value"}} or key1=value1; key2=value2' required></textarea>
                    <div class="format-hint">
                        <div data-i18n="modal.cookieHint">支持两种格式：</div>
                        <div style="margin-top: 8px;">
                            <div data-i18n="modal.format1">1. JSON格式：{"cookies": {"key": "value"}}</div>
                            <div data-i18n="modal.format2">2. 字符串格式：key1=value1; key2=value2</div>
                        </div>
                    </div>
                </div>
                <div style="display: flex; gap: 10px; margin-top: 20px;">
                    <button type="submit" class="btn btn-full" data-i18n="modal.addButton">添加账号</button>
                    <button type="button" class="btn btn-danger" onclick="closeModal()" data-i18n="modal.cancel">取消</button>
                </div>
            </form>
        </div>
    </div>

    <script>
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

        // Check authentication
        if (authToken) {
            showDashboard();
        }

        // Initialize language
        document.addEventListener('DOMContentLoaded', () => {
            updateUILanguage();
        });

        // Login form
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;

            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });

                const data = await response.json();
                if (response.ok) {
                    authToken = data.token;
                    localStorage.setItem('authToken', authToken);
                    showToast(t('messages.loginSuccess'), 'success');
                    setTimeout(() => showDashboard(), 500);
                } else {
                    showToast(t('messages.loginFailed'), 'error');
                }
            } catch (error) {
                showToast(t('messages.error') + ': ' + error.message, 'error');
            }
        });

        function showDashboard() {
            document.getElementById('loginContainer').style.display = 'none';
            document.getElementById('dashboard').style.display = 'block';
            updateUILanguage();
            loadDashboard();
            loadAccounts();
            loadNotificationSettings();
            // Refresh every 30 seconds
            setInterval(loadDashboard, 30000);
        }

        function logout() {
            localStorage.removeItem('authToken');
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

                document.getElementById('totalAccounts').textContent = data.total_accounts;
                document.getElementById('activeAccounts').textContent = data.enabled_accounts;
                document.getElementById('totalCheckins').textContent = data.total_checkins;
                document.getElementById('successRate').textContent = data.success_rate + '%';

                // Today's check-ins
                const tbody = document.getElementById('todayCheckins');
                tbody.innerHTML = '';
                
                if (data.today_checkins && data.today_checkins.length > 0) {
                    data.today_checkins.forEach(checkin => {
                        const tr = document.createElement('tr');
                        const statusText = checkin.success ? t('dashboard.todayCheckins.success') : t('dashboard.todayCheckins.failed');
                        const statusClass = checkin.success ? 'badge-success' : 'badge-danger';
                        tr.innerHTML = `
                            <td>${checkin.name}</td>
                            <td><span class="badge ${statusClass}">${statusText}</span></td>
                            <td class="hide-mobile">${checkin.message || '-'}</td>
                            <td>${new Date(checkin.created_at).toLocaleTimeString()}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                } else {
                    tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #a0aec0;">暂无记录</td></tr>';
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
                        tr.innerHTML = `
                            <td>${account.name}</td>
                            <td>
                                <label class="switch">
                                    <input type="checkbox" ${account.enabled ? 'checked' : ''} onchange="toggleAccount(${account.id}, this.checked)">
                                    <span class="slider"></span>
                                </label>
                            </td>
                            <td class="hide-mobile">
                                <input type="time" value="${account.checkin_time}" onchange="updateCheckinTime(${account.id}, this.value)" style="border: 2px solid #e0e0e0; padding: 6px; border-radius: 6px;">
                            </td>
                            <td>
                                <div class="action-buttons">
                                    <button class="btn btn-success btn-sm" onclick="manualCheckin(${account.id})">${t('dashboard.accounts.checkinNow')}</button>
                                    <button class="btn btn-danger btn-sm" onclick="deleteAccount(${account.id})">${t('dashboard.accounts.delete')}</button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                } else {
                    tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #a0aec0;">暂无账号</td></tr>';
                }
            } catch (error) {
                console.error('Failed to load accounts:', error);
            }
        }

        async function loadNotificationSettings() {
            try {
                const settings = await apiCall('/api/notification');
                if (!settings) return;

                document.getElementById('notifyEnabled').checked = settings.enabled;
                document.getElementById('tgBotToken').value = settings.telegram_bot_token || '';
                document.getElementById('tgUserId').value = settings.telegram_user_id || '';
                document.getElementById('wechatKey').value = settings.wechat_webhook_key || '';
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
                showToast(t('messages.error'), 'error');
            }
        }

        async function updateCheckinTime(id, checkin_time) {
            try {
                await apiCall(`/api/accounts/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ checkin_time })
                });
            } catch (error) {
                showToast(t('messages.error'), 'error');
            }
        }

        async function manualCheckin(id) {
            if (confirm(t('dashboard.accounts.confirmCheckin'))) {
                try {
                    const result = await apiCall(`/api/checkin/manual/${id}`, { method: 'POST' });
                    showToast(t('messages.checkinTriggered'), 'success');
                    setTimeout(loadDashboard, 2000);
                } catch (error) {
                    showToast(t('messages.error'), 'error');
                }
            }
        }

        async function deleteAccount(id) {
            if (confirm(t('dashboard.accounts.confirmDelete'))) {
                try {
                    await apiCall(`/api/accounts/${id}`, { method: 'DELETE' });
                    showToast(t('messages.accountDeleted'), 'success');
                    loadAccounts();
                } catch (error) {
                    showToast(t('messages.error'), 'error');
                }
            }
        }

        async function saveNotificationSettings() {
            try {
                const settings = {
                    enabled: document.getElementById('notifyEnabled').checked,
                    telegram_bot_token: document.getElementById('tgBotToken').value,
                    telegram_user_id: document.getElementById('tgUserId').value,
                    wechat_webhook_key: document.getElementById('wechatKey').value
                };

                await apiCall('/api/notification', {
                    method: 'PUT',
                    body: JSON.stringify(settings)
                });
                showToast(t('messages.settingsSaved'), 'success');
            } catch (error) {
                showToast(t('messages.error'), 'error');
            }
        }

        function showAddAccountModal() {
            document.getElementById('addAccountModal').style.display = 'flex';
            updateUILanguage();
        }

        function closeModal() {
            document.getElementById('addAccountModal').style.display = 'none';
            document.getElementById('addAccountForm').reset();
        }

        document.getElementById('addAccountForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            try {
                const account = {
                    name: document.getElementById('accountName').value,
                    checkin_time: document.getElementById('checkinTime').value,
                    token_data: document.getElementById('tokenData').value
                };

                await apiCall('/api/accounts', {
                    method: 'POST',
                    body: JSON.stringify(account)
                });
                
                showToast(t('messages.accountAdded'), 'success');
                closeModal();
                loadAccounts();
            } catch (error) {
                showToast(t('messages.invalidFormat') + ': ' + error.message, 'error');
            }
        });

        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('addAccountModal');
            if (event.target == modal) {
                closeModal();
            }
        }
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    # Start scheduler
    scheduler.start()
    scheduler.schedule_checkins()
    
    # Start Flask app
    logger.info(f"Starting control panel on port {PORT}")
    logger.info(f"Database type: {DB_TYPE}")
    if DB_TYPE == 'mysql':
        logger.info(f"MySQL connection: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)
