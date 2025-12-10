# 使用完整版Python镜像（包含更多工具）
FROM python:3.11

WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装系统依赖和常用调试工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 编译依赖
    gcc \
    g++ \
    make \
    libffi-dev \
    libssl-dev \
    python3-dev \
    libc-dev \
    # 常用调试工具
    curl \
    wget \
    vim \
    net-tools \
    iputils-ping \
    telnet \
    dnsutils \
    procps \
    htop \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# 升级pip
RUN pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir gunicorn -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 5432

# 启动命令
CMD ["sh", "-c", "python -c 'from app import app; from models import db; app.app_context().push(); db.create_all(); print(\"Database initialized\")' && gunicorn --bind 0.0.0.0:5432 --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile - app:app"]
