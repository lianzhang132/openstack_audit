"""
定时任务模块
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
import json
import logging
from config import Config

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def sync_openstack_data(app):
    """同步OpenStack数据并检测变更"""
    with app.app_context():
        from models import db, VirtualMachine, ChangeLog, AttachedResource, Project
        from openstack_service import openstack_service

        logger.info("Starting OpenStack data sync...")

        try:
            # 获取所有服务器
            servers = openstack_service.list_servers(all_projects=True)
            server_uuids = set()

            for server in servers:
                server_uuids.add(server.id)

                # 获取详细信息
                detail = openstack_service.get_server_detail(server.id)
                if not detail:
                    continue

                # 查找现有记录
                vm = VirtualMachine.query.filter_by(uuid=server.id).first()

                if vm:
                    # 比较并检测变更
                    changes = detect_changes(vm, detail)

                    if changes:
                        # 记录变更
                        log = ChangeLog(
                            vm_id=vm.id,
                            change_type=ChangeLog.TYPE_SYNC,
                            operator='system_sync'
                        )
                        log.set_change_content({
                            'message': '定时同步检测到变更',
                            'changes': changes
                        })
                        db.session.add(log)

                        # 更新VM信息
                        update_vm_from_detail(vm, detail)

                    vm.last_sync_at = datetime.now()
                    vm.last_sync_data = json.dumps(detail, ensure_ascii=False, default=str)
                    vm.openstack_status = detail.get('status')

            # 检查已删除的VM
            all_vms = VirtualMachine.query.filter(
                VirtualMachine.status.in_([VirtualMachine.STATUS_IN_USE, VirtualMachine.STATUS_VM_PENDING])
            ).all()

            for vm in all_vms:
                if vm.uuid not in server_uuids:
                    if vm.status == VirtualMachine.STATUS_IN_USE:
                        # VM已在OpenStack中删除，更新状态
                        vm.status = VirtualMachine.STATUS_VM_PENDING
                        vm.vm_deleted_date = datetime.now().date()
                        vm.openstack_status = 'DELETED'

                        # 更新附属资源状态
                        for resource in vm.attached_resources.filter_by(status=AttachedResource.STATUS_ACTIVE):
                            resource.status = AttachedResource.STATUS_PENDING
                            resource.pending_at = datetime.now()

                        log = ChangeLog(
                            vm_id=vm.id,
                            change_type=ChangeLog.TYPE_STATUS_CHANGE,
                            operator='system_sync'
                        )
                        log.set_change_content({
                            'message': '检测到云主机已在OpenStack中删除',
                            'old_status': VirtualMachine.STATUS_IN_USE,
                            'new_status': VirtualMachine.STATUS_VM_PENDING
                        })
                        db.session.add(log)

            db.session.commit()
            logger.info("OpenStack data sync completed successfully")

        except Exception as e:
            logger.error(f"Error during OpenStack data sync: {e}")
            db.session.rollback()


def detect_changes(vm, detail):
    """检测云主机变更"""
    changes = []

    # 检查名称变更
    if vm.name != detail.get('name'):
        changes.append({
            'field': 'name',
            'old': vm.name,
            'new': detail.get('name')
        })

    # 检查宿主机变更
    if vm.host != detail.get('host'):
        changes.append({
            'field': 'host',
            'old': vm.host,
            'new': detail.get('host')
        })

    # 检查规格变更
    if vm.flavor != detail.get('flavor'):
        changes.append({
            'field': 'flavor',
            'old': vm.flavor,
            'new': detail.get('flavor')
        })

    # 检查网络变更
    old_networks = vm.get_networks()
    new_networks = detail.get('networks', [])
    if json.dumps(old_networks, sort_keys=True) != json.dumps(new_networks, sort_keys=True):
        changes.append({
            'field': 'networks',
            'old': old_networks,
            'new': new_networks
        })

    # 检查存储卷变更
    old_volumes = vm.get_volumes()
    new_volumes = detail.get('volumes', [])
    if json.dumps(old_volumes, sort_keys=True) != json.dumps(new_volumes, sort_keys=True):
        changes.append({
            'field': 'volumes',
            'old': old_volumes,
            'new': new_volumes
        })

    return changes


def update_vm_from_detail(vm, detail):
    """从OpenStack详情更新VM记录"""
    vm.name = detail.get('name')
    vm.host = detail.get('host')
    vm.flavor = detail.get('flavor')
    vm.openstack_status = detail.get('status')

    if detail.get('image'):
        vm.image_info = json.dumps(detail['image'], ensure_ascii=False)

    if detail.get('flavor_detail'):
        vm.flavor_detail = json.dumps(detail['flavor_detail'], ensure_ascii=False)

    vm.set_networks(detail.get('networks', []))
    vm.set_volumes(detail.get('volumes', []))


def check_expire_status(app):
    """检查并更新过期状态"""
    with app.app_context():
        from models import db, VirtualMachine, AttachedResource, ChangeLog

        logger.info("Checking expire status...")
        today = datetime.now().date()

        try:
            # 检查使用期内但已过期的VM
            expired_vms = VirtualMachine.query.filter(
                VirtualMachine.status == VirtualMachine.STATUS_IN_USE,
                VirtualMachine.is_long_term == False,
                VirtualMachine.expire_date < today
            ).all()

            for vm in expired_vms:
                vm.status = VirtualMachine.STATUS_VM_PENDING
                log = ChangeLog(
                    vm_id=vm.id,
                    change_type=ChangeLog.TYPE_STATUS_CHANGE,
                    operator='system'
                )
                log.set_change_content({
                    'message': '云主机已过期，进入待回收状态',
                    'expire_date': vm.expire_date.strftime('%Y-%m-%d'),
                    'old_status': VirtualMachine.STATUS_IN_USE,
                    'new_status': VirtualMachine.STATUS_VM_PENDING
                })
                db.session.add(log)

            # 检查待回收附属资源（VM删除超过30天）
            threshold_date = today - timedelta(days=Config.ATTACHED_RESOURCE_EXPIRE_DAYS)
            pending_attached_vms = VirtualMachine.query.filter(
                VirtualMachine.status == VirtualMachine.STATUS_VM_PENDING,
                VirtualMachine.vm_deleted_date <= threshold_date
            ).all()

            for vm in pending_attached_vms:
                vm.status = VirtualMachine.STATUS_ATTACHED_PENDING
                log = ChangeLog(
                    vm_id=vm.id,
                    change_type=ChangeLog.TYPE_STATUS_CHANGE,
                    operator='system'
                )
                log.set_change_content({
                    'message': f'云主机删除已超过{Config.ATTACHED_RESOURCE_EXPIRE_DAYS}天，附属资源进入待回收状态',
                    'vm_deleted_date': vm.vm_deleted_date.strftime('%Y-%m-%d'),
                    'old_status': VirtualMachine.STATUS_VM_PENDING,
                    'new_status': VirtualMachine.STATUS_ATTACHED_PENDING
                })
                db.session.add(log)

            db.session.commit()
            logger.info("Expire status check completed")

        except Exception as e:
            logger.error(f"Error checking expire status: {e}")
            db.session.rollback()


def init_scheduler(app):
    """初始化定时任务"""
    # 每6小时同步一次OpenStack数据
    scheduler.add_job(
        func=lambda: sync_openstack_data(app),
        trigger=IntervalTrigger(hours=Config.SYNC_INTERVAL_HOURS),
        id='sync_openstack_data',
        name='Sync OpenStack Data',
        replace_existing=True
    )

    # 每天检查一次过期状态
    scheduler.add_job(
        func=lambda: check_expire_status(app),
        trigger=IntervalTrigger(hours=24),
        id='check_expire_status',
        name='Check Expire Status',
        replace_existing=True
    )

    scheduler.start()
    logger.info("Scheduler initialized")