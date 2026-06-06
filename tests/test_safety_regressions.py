import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


os.environ['DATABASE_PATH'] = ':memory:'
os.environ['ENABLE_SCHEDULER'] = 'false'
os.environ['DEBUG'] = 'false'

try:
    import openstack
except ImportError:
    openstack = types.ModuleType('openstack')
    openstack.connect = lambda **kwargs: None
    openstack_exceptions = types.ModuleType('openstack.exceptions')

    class ResourceNotFound(Exception):
        pass

    class SDKException(Exception):
        pass

    class HttpException(Exception):
        pass

    openstack_exceptions.ResourceNotFound = ResourceNotFound
    openstack_exceptions.SDKException = SDKException
    openstack_exceptions.HttpException = HttpException
    sys.modules['openstack'] = openstack
    sys.modules['openstack.exceptions'] = openstack_exceptions

from app import app
from models import AttachedResource, ChangeLog, VirtualMachine, db
from scheduler import check_expire_status, sync_openstack_data


class SafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        with app.app_context():
            db.drop_all()
            db.create_all()

    def add_vm(self, **kwargs):
        with app.app_context():
            vm = VirtualMachine(uuid=kwargs.pop('uuid', 'vm-1'), **kwargs)
            db.session.add(vm)
            db.session.commit()
            return vm.id

    def test_sync_list_failure_does_not_mark_vm_deleted(self):
        vm_id = self.add_vm(status=VirtualMachine.STATUS_IN_USE)

        with patch('openstack_service.openstack_service.list_servers', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                sync_openstack_data(app)

        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            self.assertEqual(vm.status, VirtualMachine.STATUS_IN_USE)
            self.assertIsNone(vm.vm_deleted_date)

    def test_sync_verification_failure_does_not_mark_vm_deleted(self):
        vm_id = self.add_vm(status=VirtualMachine.STATUS_IN_USE)

        with patch('openstack_service.openstack_service.list_servers', return_value=[]), \
                patch('openstack_service.openstack_service.get_server', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                sync_openstack_data(app)

        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            self.assertEqual(vm.status, VirtualMachine.STATUS_IN_USE)
            self.assertIsNone(vm.vm_deleted_date)

    def test_failed_vm_recycle_preserves_state(self):
        vm_id = self.add_vm(status=VirtualMachine.STATUS_IN_USE)

        with patch('app.openstack_service.delete_server', return_value=(False, 'delete failed')):
            response = self.client.post(f'/vms/{vm_id}/recycle')

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            self.assertEqual(vm.status, VirtualMachine.STATUS_IN_USE)
            self.assertIsNone(vm.vm_deleted_date)
            log = ChangeLog.query.filter_by(vm_id=vm_id).one()
            self.assertFalse(log.get_change_content()['success'])

    def test_partial_attached_recycle_preserves_pending_state(self):
        with app.app_context():
            vm = VirtualMachine(
                uuid='vm-attached',
                status=VirtualMachine.STATUS_ATTACHED_PENDING,
                vm_deleted_date=datetime.now().date() - timedelta(days=60),
            )
            vm.set_volumes([
                {'id': 'vol-ok', 'name': 'ok'},
                {'id': 'vol-fail', 'name': 'fail'},
            ])
            vm.attached_resources.append(AttachedResource(
                resource_type=AttachedResource.TYPE_VOLUME,
                resource_id='vol-ok',
                status=AttachedResource.STATUS_PENDING,
            ))
            vm.attached_resources.append(AttachedResource(
                resource_type=AttachedResource.TYPE_VOLUME,
                resource_id='vol-fail',
                status=AttachedResource.STATUS_PENDING,
            ))
            db.session.add(vm)
            db.session.commit()
            vm_id = vm.id

        with patch('app.openstack_service.delete_volume', side_effect=[
            (True, 'deleted'),
            (False, 'failed'),
        ]):
            response = self.client.post(f'/vms/{vm_id}/recycle-attached')

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            resources = {resource.resource_id: resource for resource in vm.attached_resources.all()}
            self.assertEqual(vm.status, VirtualMachine.STATUS_ATTACHED_PENDING)
            self.assertEqual(resources['vol-ok'].status, AttachedResource.STATUS_RECYCLED)
            self.assertEqual(resources['vol-fail'].status, AttachedResource.STATUS_PENDING)

    def test_expire_check_respects_long_term_and_custom_expire_date(self):
        old_delete_date = datetime.now().date() - timedelta(days=90)
        future_expire_date = datetime.now().date() + timedelta(days=10)
        past_expire_date = datetime.now().date() - timedelta(days=1)

        long_term_id = self.add_vm(
            uuid='vm-long-term',
            status=VirtualMachine.STATUS_VM_PENDING,
            vm_deleted_date=old_delete_date,
            is_attached_long_term=True,
        )
        extended_id = self.add_vm(
            uuid='vm-extended',
            status=VirtualMachine.STATUS_VM_PENDING,
            vm_deleted_date=old_delete_date,
            attached_expire_date=future_expire_date,
        )
        expired_id = self.add_vm(
            uuid='vm-expired',
            status=VirtualMachine.STATUS_VM_PENDING,
            vm_deleted_date=old_delete_date,
            attached_expire_date=past_expire_date,
        )

        check_expire_status(app)

        with app.app_context():
            self.assertEqual(
                db.session.get(VirtualMachine, long_term_id).status,
                VirtualMachine.STATUS_VM_PENDING,
            )
            self.assertEqual(
                db.session.get(VirtualMachine, extended_id).status,
                VirtualMachine.STATUS_VM_PENDING,
            )
            self.assertEqual(
                db.session.get(VirtualMachine, expired_id).status,
                VirtualMachine.STATUS_ATTACHED_PENDING,
            )

    def test_audit_log_delete_route_is_not_available(self):
        response = self.client.post('/logs/1/delete')
        self.assertEqual(response.status_code, 404)

    def test_audit_log_cannot_be_deleted_through_orm(self):
        vm_id = self.add_vm(status=VirtualMachine.STATUS_IN_USE)
        with app.app_context():
            log = ChangeLog(vm_id=vm_id, change_type=ChangeLog.TYPE_CREATE)
            db.session.add(log)
            db.session.commit()
            log_id = log.id

            db.session.delete(log)
            with self.assertRaises(ValueError):
                db.session.commit()
            db.session.rollback()

            self.assertIsNotNone(db.session.get(ChangeLog, log_id))


if __name__ == '__main__':
    unittest.main()
