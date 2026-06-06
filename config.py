"""
配置文件 - 支持Docker环境变量
"""
import os
import secrets


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


class Config:
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    DEBUG = env_bool('DEBUG', False)

    # 数据库配置
    DATABASE_PATH = os.environ.get('DATABASE_PATH', '/app/data/openstack_audit.db')

    # 确保数据目录存在
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    SQLALCHEMY_DATABASE_URI = f'sqlite:///{DATABASE_PATH}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # OpenStack认证配置
    OS_AUTH_URL = os.environ.get('OS_AUTH_URL', 'http://controller:5000/v3')
    OS_USERNAME = os.environ.get('OS_USERNAME', 'admin')
    OS_PASSWORD = os.environ.get('OS_PASSWORD', '')
    OS_PROJECT_NAME = os.environ.get('OS_PROJECT_NAME', 'admin')
    OS_USER_DOMAIN_NAME = os.environ.get('OS_USER_DOMAIN_NAME', 'Default')
    OS_PROJECT_DOMAIN_NAME = os.environ.get('OS_PROJECT_DOMAIN_NAME', 'Default')
    OS_REGION_NAME = os.environ.get('OS_REGION_NAME', 'RegionOne')

    # 回收策略配置
    VM_DEFAULT_EXPIRE_DAYS = int(os.environ.get('VM_DEFAULT_EXPIRE_DAYS', 90))
    ATTACHED_RESOURCE_EXPIRE_DAYS = int(os.environ.get('ATTACHED_RESOURCE_EXPIRE_DAYS', 30))

    # 定时任务配置
    SYNC_INTERVAL_HOURS = int(os.environ.get('SYNC_INTERVAL_HOURS', 6))
    ENABLE_SCHEDULER = env_bool('ENABLE_SCHEDULER', True)
