"""
数据库模型
"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import json
from sqlalchemy import event

db = SQLAlchemy()


class Project(db.Model):
    """公司内部项目表（手动管理）"""
    __tablename__ = 'projects'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, comment='项目名称')
    description = db.Column(db.Text, comment='项目描述')
    owner = db.Column(db.String(64), comment='项目负责人')
    department = db.Column(db.String(128), comment='所属部门')
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 关联云主机
    virtual_machines = db.relationship('VirtualMachine', backref='project', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'owner': self.owner,
            'department': self.department,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None,
            'vm_count': self.virtual_machines.count()
        }


class VirtualMachine(db.Model):
    """云主机记录表"""
    __tablename__ = 'virtual_machines'

    # 状态常量
    STATUS_IN_USE = 'in_use'  # 使用期内
    STATUS_VM_PENDING = 'vm_pending'  # 虚机待回收
    STATUS_ATTACHED_PENDING = 'attached_pending'  # 磁盘网卡待回收
    STATUS_RECYCLED = 'recycled'  # 完全回收

    STATUS_CHOICES = [
        (STATUS_IN_USE, '使用期内'),
        (STATUS_VM_PENDING, '虚机待回收'),
        (STATUS_ATTACHED_PENDING, '磁盘网卡待回收'),
        (STATUS_RECYCLED, '完全回收'),
    ]

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(64), unique=True, nullable=False, comment='OpenStack VM UUID')
    name = db.Column(db.String(128), comment='云主机名称')

    # 项目关联（公司内部项目）
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), comment='关联项目')

    # OpenStack项目ID（仅用于标识来源）
    openstack_project_id = db.Column(db.String(64), comment='OpenStack项目ID')
    openstack_project_name = db.Column(db.String(128), comment='OpenStack项目名称')

    # 手动输入字段
    purpose = db.Column(db.Text, comment='用途说明')
    is_long_term = db.Column(db.Boolean, default=False, comment='是否长期使用')
    long_term_reason = db.Column(db.Text, comment='长期使用理由')
    applicant = db.Column(db.String(64), comment='申请人')

    # 可查询字段（从OpenStack获取）
    image_info = db.Column(db.Text, comment='镜像信息(JSON)')
    networks = db.Column(db.Text, comment='网络信息(JSON)')
    flavor = db.Column(db.String(128), comment='规格名称')
    flavor_detail = db.Column(db.Text, comment='规格详情(JSON): cpu, memory, disk')
    host = db.Column(db.String(128), comment='宿主机')
    volumes = db.Column(db.Text, comment='磁盘卷信息(JSON)')

    # 日期相关
    created_date = db.Column(db.Date, comment='创建日期')
    expire_date = db.Column(db.Date, comment='到期日期')
    vm_deleted_date = db.Column(db.Date, comment='虚机删除日期')

    # 状态
    status = db.Column(db.String(32), default=STATUS_IN_USE, comment='当前状态')
    openstack_status = db.Column(db.String(32), comment='OpenStack中的状态')
    # ========== 新增：附属资源控制字段 ==========
    is_attached_long_term = db.Column(db.Boolean, default=False, comment='附属资源是否长期保留')
    attached_long_term_reason = db.Column(db.Text, comment='附属资源长期保留理由')
    attached_expire_date = db.Column(db.Date, comment='附属资源到期日期')
    # ============================================
    # 记录时间
    record_created_at = db.Column(db.DateTime, default=datetime.now, comment='记录创建时间')
    last_sync_at = db.Column(db.DateTime, comment='最后同步时间')
    last_sync_data = db.Column(db.Text, comment='上次同步的数据快照(JSON)')

    # 关联
    change_logs = db.relationship('ChangeLog', backref='virtual_machine', lazy='dynamic',
                                   order_by='desc(ChangeLog.changed_at)')
    attached_resources = db.relationship('AttachedResource', backref='virtual_machine', lazy='dynamic')

    def get_networks(self):
        """获取网络信息列表"""
        if self.networks:
            try:
                return json.loads(self.networks)
            except:
                return []
        return []

    def set_networks(self, networks_list):
        """设置网络信息"""
        self.networks = json.dumps(networks_list, ensure_ascii=False)

    def get_networks_display(self):
        """获取网络信息显示文本"""
        networks = self.get_networks()
        if not networks:
            return '-'
        parts = []
        for net in networks:
            part = f"{net.get('ip', '-')}"
            if net.get('network_name'):
                part = f"{net['network_name']}: {part}"
            parts.append(part)
        return '\n'.join(parts)

    def get_volumes(self):
        """获取磁盘卷列表"""
        if self.volumes:
            try:
                return json.loads(self.volumes)
            except:
                return []
        return []

    def set_volumes(self, volumes_list):
        """设置磁盘卷信息"""
        self.volumes = json.dumps(volumes_list, ensure_ascii=False)

    def get_volumes_display(self):
        """获取存储卷显示文本"""
        volumes = self.get_volumes()
        if not volumes:
            return '-'
        parts = []
        for vol in volumes:
            name = vol.get('name') or vol.get('id', '-')[:8]
            size = vol.get('size', '?')
            parts.append(f"{name} ({size}GB)")
        return '\n'.join(parts)

    def get_image_info(self):
        """获取镜像信息"""
        if self.image_info:
            try:
                return json.loads(self.image_info)
            except:
                return {}
        return {}

    def get_image_display(self):
        """获取镜像显示文本"""
        info = self.get_image_info()
        return info.get('name', '-')

    def get_flavor_detail(self):
        """获取规格详情"""
        if self.flavor_detail:
            try:
                return json.loads(self.flavor_detail)
            except:
                return {}
        return {}

    def get_flavor_display(self):
        """获取规格显示文本"""
        fd = self.get_flavor_detail()
        if fd:
            return f"{self.flavor or '-'} ({fd.get('vcpus', '?')}核/{fd.get('ram', '?')}MB/{fd.get('disk', '?')}GB)"
        return self.flavor or '-'

    def get_status_display(self):
        """获取状态显示文本"""
        status_dict = dict(self.STATUS_CHOICES)
        return status_dict.get(self.status, self.status)

    def is_expired(self):
        """检查是否过期"""
        if self.is_long_term:
            return False
        if self.expire_date:
            return datetime.now().date() > self.expire_date
        return False

    def days_until_expire(self):
        """距离过期的天数"""
        if self.is_long_term:
            return None
        if self.expire_date:
            delta = self.expire_date - datetime.now().date()
            return delta.days
        return None

    # ========== 新增：附属资源相关方法 ==========
    def is_attached_expired(self):
        """检查附属资源是否过期"""
        if self.is_attached_long_term:
            return False
        if self.attached_expire_date:
            return datetime.now().date() > self.attached_expire_date
        # 如果没有设置附属资源到期日期，使用 vm_deleted_date + 30天
        if self.vm_deleted_date:
            from config import Config
            default_expire = self.vm_deleted_date + timedelta(days=Config.ATTACHED_RESOURCE_EXPIRE_DAYS)
            return datetime.now().date() > default_expire
        return False

    def attached_days_until_expire(self):
        """附属资源距离过期的天数"""
        if self.is_attached_long_term:
            return None
        if self.attached_expire_date:
            delta = self.attached_expire_date - datetime.now().date()
            return delta.days
        # 使用默认计算
        if self.vm_deleted_date:
            from config import Config
            default_expire = self.vm_deleted_date + timedelta(days=Config.ATTACHED_RESOURCE_EXPIRE_DAYS)
            delta = default_expire - datetime.now().date()
            return delta.days
        return None

    def get_attached_expire_date(self):
        """获取附属资源到期日期（显示用）"""
        if self.is_attached_long_term:
            return None
        if self.attached_expire_date:
            return self.attached_expire_date
        # 使用默认计算
        if self.vm_deleted_date:
            from config import Config
            return self.vm_deleted_date + timedelta(days=Config.ATTACHED_RESOURCE_EXPIRE_DAYS)
        return None

    def to_dict(self):
        return {
            'id': self.id,
            'uuid': self.uuid,
            'name': self.name,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'openstack_project_id': self.openstack_project_id,
            'openstack_project_name': self.openstack_project_name,
            'purpose': self.purpose,
            'is_long_term': self.is_long_term,
            'long_term_reason': self.long_term_reason,
            'applicant': self.applicant,
            'image_info': self.get_image_info(),
            'networks': self.get_networks(),
            'flavor': self.flavor,
            'flavor_detail': self.get_flavor_detail(),
            'host': self.host,
            'volumes': self.get_volumes(),
            'created_date': self.created_date.strftime('%Y-%m-%d') if self.created_date else None,
            'expire_date': self.expire_date.strftime('%Y-%m-%d') if self.expire_date else None,
            'status': self.status,
            'status_display': self.get_status_display(),
            'openstack_status': self.openstack_status,
            'record_created_at': self.record_created_at.strftime('%Y-%m-%d %H:%M:%S') if self.record_created_at else None,
            'last_sync_at': self.last_sync_at.strftime('%Y-%m-%d %H:%M:%S') if self.last_sync_at else None,
            'is_expired': self.is_expired(),
            'days_until_expire': self.days_until_expire(),
        }

    def to_export_dict(self):
        """导出用的字典"""
        fd = self.get_flavor_detail()
        attached_expire = self.get_attached_expire_date()

        return {
            '云主机名称': self.name or '-',
            'UUID': self.uuid,
            '所属项目': self.project.name if self.project else '-',
            '用途说明': self.purpose or '-',
            '申请人': self.applicant or '-',
            '是否长期使用': '是' if self.is_long_term else '否',
            '长期使用理由': self.long_term_reason or '-',
            '镜像': self.get_image_display(),
            '规格': self.flavor or '-',
            'CPU(核)': fd.get('vcpus', '-') if fd else '-',
            '内存(MB)': fd.get('ram', '-') if fd else '-',
            '系统盘(GB)': fd.get('disk', '-') if fd else '-',
            '网络信息': self.get_networks_display(),
            '存储卷': self.get_volumes_display(),
            '宿主机': self.host or '-',
            '创建日期': self.created_date.strftime('%Y-%m-%d') if self.created_date else '-',
            '到期日期': self.expire_date.strftime('%Y-%m-%d') if self.expire_date else (
                '-' if not self.is_long_term else '长期'),
            '当前状态': self.get_status_display(),
            'OpenStack状态': self.openstack_status or '-',
            '虚机删除日期': self.vm_deleted_date.strftime('%Y-%m-%d') if self.vm_deleted_date else '-',
            '附属资源长期保留': '是' if self.is_attached_long_term else '否',
            '附属资源长期理由': self.attached_long_term_reason or '-',
            '附属资源到期日期': attached_expire.strftime('%Y-%m-%d') if attached_expire else (
                '-' if not self.is_attached_long_term else '长期'),
            'OpenStack项目': self.openstack_project_name or self.openstack_project_id or '-',
            '记录创建时间': self.record_created_at.strftime('%Y-%m-%d %H:%M:%S') if self.record_created_at else '-',
            '最后同步时间': self.last_sync_at.strftime('%Y-%m-%d %H:%M:%S') if self.last_sync_at else '-',
        }


class ChangeLog(db.Model):
    """变更记录表"""
    __tablename__ = 'change_logs'

    # 变更类型
    TYPE_CREATE = 'create'
    TYPE_UPDATE = 'update'
    TYPE_SYNC = 'sync'
    TYPE_STATUS_CHANGE = 'status_change'
    TYPE_RECYCLE_VM = 'recycle_vm'
    TYPE_RECYCLE_ATTACHED = 'recycle_attached'
    TYPE_EXTEND = 'extend'
    TYPE_MANUAL = 'manual'  # 新增：手动记录
    TYPE_OTHER = 'other'  # 新增：其他

    id = db.Column(db.Integer, primary_key=True)
    vm_id = db.Column(db.Integer, db.ForeignKey('virtual_machines.id'), nullable=False)
    change_type = db.Column(db.String(32), nullable=False, comment='变更类型')
    change_content = db.Column(db.Text, comment='变更内容(JSON)')
    changed_at = db.Column(db.DateTime, default=datetime.now, comment='变更时间')
    operator = db.Column(db.String(64), comment='操作人')

    def get_change_content(self):
        if self.change_content:
            try:
                return json.loads(self.change_content)
            except:
                return {'message': self.change_content}
        return {}

    def set_change_content(self, content):
        if isinstance(content, dict):
            self.change_content = json.dumps(content, ensure_ascii=False)
        else:
            self.change_content = json.dumps({'message': str(content)}, ensure_ascii=False)

    def get_type_display(self):
        type_names = {
            self.TYPE_CREATE: '创建记录',
            self.TYPE_UPDATE: '信息更新',
            self.TYPE_SYNC: '同步变更',
            self.TYPE_STATUS_CHANGE: '状态变更',
            self.TYPE_RECYCLE_VM: '回收虚机',
            self.TYPE_RECYCLE_ATTACHED: '回收附属资源',
            self.TYPE_EXTEND: '延期',
            self.TYPE_MANUAL: '手动记录',
            self.TYPE_OTHER: '其他',
        }
        return type_names.get(self.change_type, self.change_type)

    def to_dict(self):
        return {
            'id': self.id,
            'vm_id': self.vm_id,
            'change_type': self.change_type,
            'type_display': self.get_type_display(),
            'change_content': self.get_change_content(),
            'changed_at': self.changed_at.strftime('%Y-%m-%d %H:%M:%S') if self.changed_at else None,
            'operator': self.operator,
        }


@event.listens_for(ChangeLog, 'before_delete')
def prevent_change_log_delete(mapper, connection, target):
    raise ValueError('审计记录不可删除')


class AttachedResource(db.Model):
    """附属资源表（网卡、存储卷）"""
    __tablename__ = 'attached_resources'

    TYPE_PORT = 'port'
    TYPE_VOLUME = 'volume'

    STATUS_ACTIVE = 'active'
    STATUS_PENDING = 'pending_recycle'
    STATUS_RECYCLED = 'recycled'

    id = db.Column(db.Integer, primary_key=True)
    vm_id = db.Column(db.Integer, db.ForeignKey('virtual_machines.id'), nullable=False)
    resource_type = db.Column(db.String(32), nullable=False, comment='资源类型: port/volume')
    resource_id = db.Column(db.String(64), nullable=False, comment='资源ID')
    resource_name = db.Column(db.String(128), comment='资源名称')
    resource_info = db.Column(db.Text, comment='资源详情(JSON)')
    status = db.Column(db.String(32), default=STATUS_ACTIVE, comment='状态')
    created_at = db.Column(db.DateTime, default=datetime.now)
    pending_at = db.Column(db.DateTime, comment='进入待回收状态时间')
    recycled_at = db.Column(db.DateTime, comment='回收时间')

    def get_resource_info(self):
        if self.resource_info:
            try:
                return json.loads(self.resource_info)
            except:
                return {}
        return {}

    def set_resource_info(self, info):
        self.resource_info = json.dumps(info, ensure_ascii=False)

    def get_status_display(self):
        status_names = {
            self.STATUS_ACTIVE: '使用中',
            self.STATUS_PENDING: '待回收',
            self.STATUS_RECYCLED: '已回收',
        }
        return status_names.get(self.status, self.status)

    def to_dict(self):
        return {
            'id': self.id,
            'vm_id': self.vm_id,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'resource_name': self.resource_name,
            'resource_info': self.get_resource_info(),
            'status': self.status,
            'status_display': self.get_status_display(),
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None,
            'pending_at': self.pending_at.strftime('%Y-%m-%d %H:%M:%S') if self.pending_at else None,
            'recycled_at': self.recycled_at.strftime('%Y-%m-%d %H:%M:%S') if self.recycled_at else None,
        }


def init_db(app):
    """初始化数据库"""
    db.init_app(app)
    with app.app_context():
        db.create_all()
