import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
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
from openstack_service import openstack_service
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

    def test_attached_recycle_deletes_created_port_by_port_id(self):
        with app.app_context():
            vm = VirtualMachine(
                uuid='vm-port-recycle',
                status=VirtualMachine.STATUS_ATTACHED_PENDING,
                vm_deleted_date=datetime.now().date() - timedelta(days=60),
            )
            vm.set_networks([{
                'network_id': 'net-1',
                'network_name': 'private',
                'port_id': 'port-created',
                'ip': '10.0.0.30',
                'mac': 'fa:16:3e:00:00:05',
            }])
            vm.attached_resources.append(AttachedResource(
                resource_type=AttachedResource.TYPE_PORT,
                resource_id='port-created',
                status=AttachedResource.STATUS_PENDING,
            ))
            db.session.add(vm)
            db.session.commit()
            vm_id = vm.id

        with patch('app.openstack_service.delete_port', return_value=(True, 'deleted')) as delete_port:
            response = self.client.post(f'/vms/{vm_id}/recycle-attached')

        self.assertEqual(response.status_code, 302)
        delete_port.assert_called_once_with('port-created')
        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            self.assertEqual(vm.status, VirtualMachine.STATUS_RECYCLED)
            self.assertEqual(vm.attached_resources.one().status, AttachedResource.STATUS_RECYCLED)
            content = vm.change_logs.one().get_change_content()
            self.assertEqual(content['results'][0]['id'], 'port-created')
            self.assertTrue(content['results'][0]['success'])

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

    def test_create_image_boot_vm_with_disabled_port_records_audit(self):
        detail = {
            'uuid': 'server-image',
            'name': 'image-vm',
            'status': 'BUILD',
            'project_id': 'project-1',
            'host': None,
            'image': {'id': 'image-1', 'name': 'Ubuntu'},
            'flavor': 'small',
            'flavor_detail': {'vcpus': 1, 'ram': 1024, 'disk': 20},
            'networks': [{
                'network_id': 'net-1',
                'network_name': 'private',
                'port_id': 'port-1',
                'port_name': 'image-vm-port',
                'ip': '10.0.0.10',
                'mac': 'fa:16:3e:00:00:01',
                'port_security_enabled': False,
            }],
            'volumes': [],
        }
        port_info = {
            'id': 'port-1',
            'name': 'image-vm-port',
            'network_id': 'net-1',
            'mac': 'fa:16:3e:00:00:01',
            'fixed_ips': [{'ip_address': '10.0.0.10'}],
            'port_security_enabled': False,
            'security_groups': [],
        }

        with patch('app.openstack_service.create_port', return_value=port_info) as create_port, \
                patch('app.openstack_service.create_server', return_value=SimpleNamespace(id='server-image')) as create_server, \
                patch('app.openstack_service.get_server_detail', return_value=detail):
            response = self.client.post('/vms/add', data={
                'add_mode': 'create',
                'create_name': 'image-vm',
                'boot_source': 'image',
                'image_id': 'image-1',
                'flavor_id': 'flavor-1',
                'network_id': 'net-1',
                'create_port': 'on',
                'disable_port_security': 'on',
                'applicant': 'alice',
                'purpose': 'integration test',
            })

        self.assertEqual(response.status_code, 302)
        create_port.assert_called_once()
        self.assertTrue(create_port.call_args.kwargs['disable_port_security'])
        create_server.assert_called_once()
        self.assertEqual(create_server.call_args.kwargs['image_id'], 'image-1')
        self.assertEqual(create_server.call_args.kwargs['port_id'], 'port-1')

        with app.app_context():
            vm = VirtualMachine.query.filter_by(uuid='server-image').one()
            self.assertEqual(vm.name, 'image-vm')
            self.assertEqual(vm.get_networks()[0]['port_id'], 'port-1')
            resources = vm.attached_resources.all()
            self.assertEqual(resources[0].resource_type, AttachedResource.TYPE_PORT)
            self.assertEqual(resources[0].resource_id, 'port-1')
            contents = [log.get_change_content() for log in vm.change_logs.all()]
            self.assertTrue(any(content.get('action') == 'create_port' for content in contents))
            self.assertTrue(any(content.get('boot_source') == 'image' for content in contents))

    def test_vm_add_page_queries_platform_resources_for_create_mode(self):
        with patch('app.openstack_service.list_servers', return_value=[]), \
                patch('app.openstack_service.list_images', return_value=[{'id': 'image-1', 'name': 'Ubuntu'}]) as images, \
                patch('app.openstack_service.list_flavors', return_value=[{
                    'id': 'flavor-1',
                    'name': 'small',
                    'vcpus': 1,
                    'ram': 1024,
                    'disk': 20,
                }]) as flavors, \
                patch('app.openstack_service.list_networks', return_value=[{'id': 'net-1', 'name': 'private'}]) as networks, \
                patch('app.openstack_service.list_volumes', return_value=[{
                    'id': 'vol-boot',
                    'name': 'boot-volume',
                    'size': 40,
                    'status': 'available',
                    'bootable': True,
                }]) as volumes:
            response = self.client.get('/vms/add')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('创建虚机', html)
        self.assertIn('Ubuntu', html)
        self.assertIn('small', html)
        self.assertIn('private', html)
        self.assertIn('boot-volume', html)
        self.assertIn('禁用端口安全', html)
        images.assert_called_once()
        flavors.assert_called_once()
        networks.assert_called_once()
        volumes.assert_called_once()

    def test_create_volume_boot_vm_records_boot_volume_resource(self):
        detail = {
            'uuid': 'server-volume',
            'name': 'volume-vm',
            'status': 'BUILD',
            'project_id': 'project-1',
            'host': None,
            'image': None,
            'flavor': 'small',
            'flavor_detail': {'vcpus': 1, 'ram': 1024, 'disk': 20},
            'networks': [{
                'network_id': 'net-1',
                'network_name': 'private',
                'ip': '10.0.0.11',
                'mac': 'fa:16:3e:00:00:02',
            }],
            'volumes': [{
                'id': 'vol-boot',
                'name': 'boot-volume',
                'size': 40,
                'bootable': True,
            }],
        }

        with patch('app.openstack_service.create_server', return_value=SimpleNamespace(id='server-volume')) as create_server, \
                patch('app.openstack_service.get_server_detail', return_value=detail):
            response = self.client.post('/vms/add', data={
                'add_mode': 'create',
                'create_name': 'volume-vm',
                'boot_source': 'volume',
                'boot_volume_id': 'vol-boot',
                'flavor_id': 'flavor-1',
                'network_id': 'net-1',
                'applicant': 'alice',
                'purpose': 'volume boot test',
            })

        self.assertEqual(response.status_code, 302)
        create_server.assert_called_once()
        self.assertEqual(create_server.call_args.kwargs['boot_volume_id'], 'vol-boot')
        self.assertIsNone(create_server.call_args.kwargs['image_id'])

        with app.app_context():
            vm = VirtualMachine.query.filter_by(uuid='server-volume').one()
            resources = {resource.resource_id: resource for resource in vm.attached_resources.all()}
            self.assertIn('vol-boot', resources)
            self.assertEqual(resources['vol-boot'].resource_type, AttachedResource.TYPE_VOLUME)
            create_log = [
                log.get_change_content() for log in vm.change_logs.all()
                if log.get_change_content().get('action') == 'create_server'
            ][0]
            self.assertEqual(create_log['boot_source'], 'volume')

    def test_failed_create_after_port_keeps_auditable_cleanup_record(self):
        port_info = {
            'id': 'port-orphan',
            'name': 'failed-vm-port',
            'network_id': 'net-1',
            'mac': 'fa:16:3e:00:00:03',
            'fixed_ips': [{'ip_address': '10.0.0.12'}],
            'port_security_enabled': False,
            'security_groups': [],
        }

        with patch('app.openstack_service.create_port', return_value=port_info), \
                patch('app.openstack_service.create_server', side_effect=RuntimeError('quota exceeded')):
            response = self.client.post('/vms/add', data={
                'add_mode': 'create',
                'create_name': 'failed-vm',
                'boot_source': 'image',
                'image_id': 'image-1',
                'flavor_id': 'flavor-1',
                'network_id': 'net-1',
                'create_port': 'on',
                'disable_port_security': 'on',
                'applicant': 'alice',
            })

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            vm = VirtualMachine.query.filter(VirtualMachine.uuid.like('create-failed-%')).one()
            self.assertEqual(vm.status, VirtualMachine.STATUS_VM_PENDING)
            self.assertEqual(vm.openstack_status, 'CREATE_FAILED')
            self.assertEqual(vm.attached_resources.one().resource_id, 'port-orphan')
            contents = [log.get_change_content() for log in vm.change_logs.all()]
            self.assertTrue(any(content.get('action') == 'create_port' for content in contents))
            failure_log = [
                content for content in contents
                if content.get('action') == 'create_server_failed'
            ][0]
            self.assertIn('quota exceeded', failure_log['error'])

    def test_created_vm_lifecycle_recycles_server_and_attached_resources_with_audit(self):
        detail = {
            'uuid': 'server-lifecycle',
            'name': 'lifecycle-vm',
            'status': 'BUILD',
            'project_id': 'project-1',
            'host': None,
            'image': None,
            'flavor': 'small',
            'flavor_detail': {'vcpus': 1, 'ram': 1024, 'disk': 20},
            'networks': [{
                'network_id': 'net-1',
                'network_name': 'private',
                'port_id': 'port-lifecycle',
                'port_name': 'lifecycle-vm-port',
                'ip': '10.0.0.21',
                'mac': 'fa:16:3e:00:00:06',
                'port_security_enabled': False,
            }],
            'volumes': [{
                'id': 'vol-lifecycle',
                'name': 'boot-volume',
                'size': 40,
                'bootable': True,
            }],
        }
        port_info = {
            'id': 'port-lifecycle',
            'name': 'lifecycle-vm-port',
            'network_id': 'net-1',
            'mac': 'fa:16:3e:00:00:06',
            'fixed_ips': [{'ip_address': '10.0.0.21'}],
            'port_security_enabled': False,
            'security_groups': [],
        }

        with patch('app.openstack_service.create_port', return_value=port_info), \
                patch('app.openstack_service.create_server', return_value=SimpleNamespace(id='server-lifecycle')), \
                patch('app.openstack_service.get_server_detail', return_value=detail):
            create_response = self.client.post('/vms/add', data={
                'add_mode': 'create',
                'create_name': 'lifecycle-vm',
                'boot_source': 'volume',
                'boot_volume_id': 'vol-lifecycle',
                'flavor_id': 'flavor-1',
                'network_id': 'net-1',
                'create_port': 'on',
                'disable_port_security': 'on',
                'applicant': 'alice',
            })

        self.assertEqual(create_response.status_code, 302)
        with app.app_context():
            vm = VirtualMachine.query.filter_by(uuid='server-lifecycle').one()
            vm_id = vm.id
            self.assertEqual(vm.status, VirtualMachine.STATUS_IN_USE)
            self.assertEqual(vm.attached_resources.count(), 2)

        with patch('app.openstack_service.delete_server', return_value=(True, 'server deleted')):
            recycle_vm_response = self.client.post(f'/vms/{vm_id}/recycle', data={'operator': 'alice'})

        self.assertEqual(recycle_vm_response.status_code, 302)
        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            self.assertEqual(vm.status, VirtualMachine.STATUS_VM_PENDING)
            self.assertEqual(
                {resource.status for resource in vm.attached_resources.all()},
                {AttachedResource.STATUS_PENDING},
            )

        with patch('app.openstack_service.delete_volume', return_value=(True, 'volume deleted')) as delete_volume, \
                patch('app.openstack_service.delete_port', return_value=(True, 'port deleted')) as delete_port:
            recycle_attached_response = self.client.post(
                f'/vms/{vm_id}/recycle-attached',
                data={'operator': 'alice'},
            )

        self.assertEqual(recycle_attached_response.status_code, 302)
        delete_volume.assert_called_once_with('vol-lifecycle', force=True)
        delete_port.assert_called_once_with('port-lifecycle')
        with app.app_context():
            vm = db.session.get(VirtualMachine, vm_id)
            self.assertEqual(vm.status, VirtualMachine.STATUS_RECYCLED)
            self.assertEqual(
                {resource.status for resource in vm.attached_resources.all()},
                {AttachedResource.STATUS_RECYCLED},
            )
            logs = vm.change_logs.all()
            actions = [log.get_change_content().get('action') for log in logs]
            change_types = {log.change_type for log in logs}
            self.assertIn('create_port', actions)
            self.assertIn('create_server', actions)
            self.assertIn(ChangeLog.TYPE_RECYCLE_VM, change_types)
            self.assertIn(ChangeLog.TYPE_RECYCLE_ATTACHED, change_types)

    def test_openstack_service_create_port_disables_security(self):
        class FakeNetwork:
            def __init__(self):
                self.attrs = None

            def create_port(self, **attrs):
                self.attrs = attrs
                return SimpleNamespace(
                    id='port-1',
                    name=attrs.get('name'),
                    network_id=attrs.get('network_id'),
                    mac_address='fa:16:3e:00:00:04',
                    fixed_ips=attrs.get('fixed_ips', []),
                    port_security_enabled=attrs.get('port_security_enabled'),
                    security_groups=attrs.get('security_groups', []),
                )

        fake_network = FakeNetwork()
        with patch.object(openstack_service, 'get_connection', return_value=SimpleNamespace(network=fake_network)):
            port = openstack_service.create_port(
                network_id='net-1',
                name='unsafe-port',
                fixed_ip='10.0.0.20',
                disable_port_security=True,
            )

        self.assertFalse(fake_network.attrs['port_security_enabled'])
        self.assertEqual(fake_network.attrs['security_groups'], [])
        self.assertEqual(fake_network.attrs['fixed_ips'], [{'ip_address': '10.0.0.20'}])
        self.assertEqual(port['id'], 'port-1')

    def test_openstack_service_create_server_supports_image_and_volume_boot(self):
        class FakeCompute:
            def __init__(self):
                self.calls = []

            def create_server(self, **attrs):
                self.calls.append(attrs)
                return SimpleNamespace(id=f"server-{len(self.calls)}")

        fake_compute = FakeCompute()
        with patch.object(openstack_service, 'get_connection', return_value=SimpleNamespace(compute=fake_compute)):
            openstack_service.create_server(
                name='image-vm',
                flavor_id='flavor-1',
                image_id='image-1',
                port_id='port-1',
            )
            openstack_service.create_server(
                name='volume-vm',
                flavor_id='flavor-1',
                boot_volume_id='vol-boot',
                network_id='net-1',
                delete_volume_on_termination=True,
            )

        image_call, volume_call = fake_compute.calls
        self.assertEqual(image_call['image_id'], 'image-1')
        self.assertEqual(image_call['networks'], [{'port': 'port-1'}])
        self.assertNotIn('block_device_mapping_v2', image_call)

        self.assertEqual(volume_call['networks'], [{'uuid': 'net-1'}])
        self.assertNotIn('image_id', volume_call)
        self.assertEqual(volume_call['block_device_mapping_v2'][0]['uuid'], 'vol-boot')
        self.assertEqual(volume_call['block_device_mapping_v2'][0]['source_type'], 'volume')
        self.assertTrue(volume_call['block_device_mapping_v2'][0]['delete_on_termination'])


if __name__ == '__main__':
    unittest.main()
