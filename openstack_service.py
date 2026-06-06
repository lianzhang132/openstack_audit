"""
OpenStack服务接口
"""
import openstack
from openstack.exceptions import ResourceNotFound, SDKException, HttpException
from config import Config
import json
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def _resource_get(resource, key, default=None):
    if isinstance(resource, dict):
        return resource.get(key, default)
    return getattr(resource, key, default)


class OpenStackService:
    """OpenStack服务类"""

    _instance = None
    _conn = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_connection(self):
        """获取OpenStack连接"""
        if self._conn is None:
            try:
                self._conn = openstack.connect(
                    auth_url=Config.OS_AUTH_URL,
                    username=Config.OS_USERNAME,
                    password=Config.OS_PASSWORD,
                    project_name=Config.OS_PROJECT_NAME,
                    user_domain_name=Config.OS_USER_DOMAIN_NAME,
                    project_domain_name=Config.OS_PROJECT_DOMAIN_NAME,
                    region_name=Config.OS_REGION_NAME,
                )
                logger.info("OpenStack connection established successfully")
            except Exception as e:
                logger.error(f"Failed to connect to OpenStack: {e}")
                raise
        return self._conn

    def refresh_connection(self):
        """刷新连接"""
        self._conn = None
        return self.get_connection()

    def list_projects(self):
        """获取所有项目列表"""
        conn = self.get_connection()
        try:
            projects = list(conn.identity.projects())
            return [{'id': p.id, 'name': p.name, 'description': p.description} for p in projects]
        except Exception as e:
            logger.error(f"Failed to list projects: {e}")
            return []

    def list_servers(self, project_id=None, all_projects=True):
        """获取云主机列表"""
        conn = self.get_connection()
        try:
            if all_projects:
                servers = list(conn.compute.servers(details=True, all_projects=True))
            else:
                servers = list(conn.compute.servers(details=True, project_id=project_id))
            return servers
        except Exception as e:
            logger.error(f"Failed to list servers: {e}")
            raise

    def get_server(self, server_id):
        """获取单个云主机详情"""
        conn = self.get_connection()
        try:
            server = conn.compute.get_server(server_id)
            return server
        except ResourceNotFound:
            logger.warning(f"Server {server_id} not found")
            return None
        except Exception as e:
            logger.error(f"Failed to get server {server_id}: {e}")
            raise

    def get_server_detail(self, server_id):
        """获取云主机完整详情，包括网络、卷等"""
        conn = self.get_connection()
        try:
            server = conn.compute.get_server(server_id)
            if not server:
                return None

            result = {
                'uuid': server.id,
                'name': server.name,
                'status': server.status,
                'project_id': server.project_id,
                'host': getattr(server, 'hypervisor_hostname', None) or getattr(server, 'host', None),
                'created': server.created_at,
                'image': None,
                'flavor': None,
                'flavor_detail': {},
                'networks': [],
                'volumes': [],
            }

            ports_by_mac = {}
            ports_by_ip = {}
            try:
                ports = list(conn.network.ports(device_id=server.id))
                for port in ports:
                    mac = _resource_get(port, 'mac_address')
                    if mac:
                        ports_by_mac[mac] = port
                    for fixed_ip in _resource_get(port, 'fixed_ips', []) or []:
                        ip_address = fixed_ip.get('ip_address')
                        if ip_address:
                            ports_by_ip[ip_address] = port
            except Exception as e:
                logger.warning(f"Failed to get ports for server {server_id}: {e}")

            # 获取镜像信息
            if server.image:
                image_id = server.image.get('id') if isinstance(server.image, dict) else server.image
                if image_id:
                    try:
                        image = conn.image.get_image(image_id)
                        if image:
                            result['image'] = {
                                'id': image.id,
                                'name': image.name,
                            }
                    except:
                        result['image'] = {'id': image_id, 'name': 'Unknown'}

            # 获取规格信息
            if server.flavor:
                flavor_id = server.flavor.get('id') if isinstance(server.flavor, dict) else server.flavor
                if flavor_id:
                    try:
                        flavor = conn.compute.get_flavor(flavor_id)
                        if flavor:
                            result['flavor'] = flavor.name
                            result['flavor_detail'] = {
                                'vcpus': flavor.vcpus,
                                'ram': flavor.ram,
                                'disk': flavor.disk,
                            }
                    except:
                        result['flavor'] = flavor_id

            # 获取网络信息
            if server.addresses:
                for network_name, addresses in server.addresses.items():
                    for addr in addresses:
                        port_info = {
                            'network_name': network_name,
                            'ip': addr.get('addr'),
                            'mac': addr.get('OS-EXT-IPS-MAC:mac_addr'),
                            'type': addr.get('OS-EXT-IPS:type'),
                        }
                        port = ports_by_mac.get(port_info['mac']) or ports_by_ip.get(port_info['ip'])
                        if port:
                            port_info.update({
                                'port_id': port.id,
                                'port_name': _resource_get(port, 'name'),
                                'port_security_enabled': _resource_get(port, 'port_security_enabled'),
                            })
                        result['networks'].append(port_info)

            # 获取挂载的卷
            try:
                volume_attachments = list(conn.compute.volume_attachments(server))
                for va in volume_attachments:
                    vol_id = va.volume_id
                    try:
                        volume = conn.block_storage.get_volume(vol_id)
                        if volume:
                            result['volumes'].append({
                                'id': volume.id,
                                'name': volume.name,
                                'size': volume.size,
                                'status': volume.status,
                                'bootable': volume.is_bootable,
                            })
                    except:
                        result['volumes'].append({
                            'id': vol_id,
                            'name': 'Unknown',
                        })
            except Exception as e:
                logger.warning(f"Failed to get volume attachments: {e}")

            return result

        except ResourceNotFound:
            return None
        except Exception as e:
            logger.error(f"Failed to get server detail {server_id}: {e}")
            return None

    def create_port(self, network_id, name=None, fixed_ip=None, disable_port_security=False):
        """创建网络端口，可选择禁用端口安全"""
        conn = self.get_connection()
        attrs = {
            'network_id': network_id,
        }
        if name:
            attrs['name'] = name
        if fixed_ip:
            attrs['fixed_ips'] = [{'ip_address': fixed_ip}]
        if disable_port_security:
            attrs['port_security_enabled'] = False
            attrs['security_groups'] = []

        port = conn.network.create_port(**attrs)
        return {
            'id': port.id,
            'name': _resource_get(port, 'name'),
            'network_id': _resource_get(port, 'network_id') or network_id,
            'mac': _resource_get(port, 'mac_address'),
            'fixed_ips': _resource_get(port, 'fixed_ips', []) or [],
            'port_security_enabled': _resource_get(port, 'port_security_enabled'),
            'security_groups': _resource_get(port, 'security_groups', []) or [],
        }

    def create_server(self, name, flavor_id, image_id=None, boot_volume_id=None,
                      network_id=None, port_id=None, delete_volume_on_termination=False):
        """创建云主机，支持镜像启动或已有卷启动"""
        conn = self.get_connection()
        networks = []
        if port_id:
            networks.append({'port': port_id})
        elif network_id:
            networks.append({'uuid': network_id})

        attrs = {
            'name': name,
            'flavor_id': flavor_id,
            'networks': networks,
        }

        if boot_volume_id:
            attrs['block_device_mapping_v2'] = [{
                'uuid': boot_volume_id,
                'source_type': 'volume',
                'destination_type': 'volume',
                'boot_index': 0,
                'delete_on_termination': bool(delete_volume_on_termination),
            }]
        else:
            attrs['image_id'] = image_id

        return conn.compute.create_server(**attrs)

    def delete_server(self, server_id):
        """删除云主机"""
        conn = self.get_connection()
        try:
            # 尝试直接删除
            conn.compute.delete_server(server_id)
            logger.info(f"Server {server_id} deleted successfully")
            return True, "删除成功"
        except ResourceNotFound:
            logger.warning(f"Server {server_id} not found, maybe already deleted")
            return True, "云主机不存在或已删除"
        except HttpException as e:
            logger.error(f"HTTP error deleting server {server_id}: {e}")
            return False, f"HTTP错误: {str(e)}"
        except Exception as e:
            logger.error(f"Failed to delete server {server_id}: {e}")
            return False, str(e)

    def delete_port(self, port_id):
        """删除网络端口"""
        conn = self.get_connection()
        try:
            # 先尝试通过MAC地址查找端口
            port = None
            if ':' in str(port_id):  # 看起来像MAC地址
                try:
                    ports = list(conn.network.ports(mac_address=port_id))
                    if ports:
                        port = ports[0]
                        port_id = port.id
                except:
                    pass

            # 尝试删除
            conn.network.delete_port(port_id, ignore_missing=True)
            logger.info(f"Port {port_id} deleted successfully")
            return True, "删除成功"
        except ResourceNotFound:
            logger.warning(f"Port {port_id} not found")
            return True, "端口不存在或已删除"
        except HttpException as e:
            error_msg = str(e)
            logger.error(f"HTTP error deleting port {port_id}: {error_msg}")
            # 如果是因为端口正在使用
            if 'in use' in error_msg.lower() or 'still in use' in error_msg.lower():
                return False, "端口仍在使用中"
            return False, f"删除失败: {error_msg}"
        except Exception as e:
            logger.error(f"Failed to delete port {port_id}: {e}")
            return False, str(e)

    def delete_volume(self, volume_id, force=True):
        """删除存储卷"""
        conn = self.get_connection()
        try:
            # 先获取卷信息
            volume = None
            try:
                volume = conn.block_storage.get_volume(volume_id)
            except ResourceNotFound:
                logger.warning(f"Volume {volume_id} not found")
                return True, "存储卷不存在或已删除"
            except Exception as e:
                logger.error(f"Failed to get volume {volume_id}: {e}")
                return False, f"无法确认存储卷状态，已取消删除: {str(e)}"

            if volume:
                # 检查卷状态
                logger.info(f"Volume {volume_id} status: {volume.status}")

                # 如果卷还在使用中，先尝试分离
                if volume.status == 'in-use':
                    try:
                        # 获取卷的附加信息
                        attachments = getattr(volume, 'attachments', []) or []
                        for attachment in attachments:
                            server_id = attachment.get('server_id')
                            if server_id:
                                logger.info(f"Trying to detach volume {volume_id} from server {server_id}")
                                try:
                                    conn.compute.delete_volume_attachment(volume_id, server_id, ignore_missing=True)
                                except Exception as detach_err:
                                    logger.warning(f"Failed to detach: {detach_err}")
                    except Exception as e:
                        logger.warning(f"Failed to detach volume: {e}")

                    # 如果设置了强制删除
                    if force:
                        try:
                            # 使用 force_delete
                            conn.block_storage.delete_volume(volume_id, force=True)
                            logger.info(f"Volume {volume_id} force deleted")
                            return True, "强制删除成功"
                        except Exception as e:
                            logger.error(f"Force delete failed: {e}")
                            return False, f"卷仍在使用中，强制删除失败: {str(e)}"
                    else:
                        return False, "存储卷仍在使用中，请先分离"

                # 如果是 available 或 error 状态，直接删除
                elif volume.status in ['available', 'error', 'error_deleting']:
                    conn.block_storage.delete_volume(volume_id)
                    logger.info(f"Volume {volume_id} deleted successfully")
                    return True, "删除成功"

                else:
                    # 其他状态尝试删除
                    try:
                        conn.block_storage.delete_volume(volume_id, force=force)
                        logger.info(f"Volume {volume_id} deleted (status was {volume.status})")
                        return True, f"删除成功（原状态: {volume.status}）"
                    except Exception as e:
                        return False, f"卷状态为 {volume.status}，删除失败: {str(e)}"
            else:
                # 直接尝试删除
                conn.block_storage.delete_volume(volume_id, ignore_missing=True)
                return True, "删除成功"

        except ResourceNotFound:
            return True, "存储卷不存在或已删除"
        except HttpException as e:
            error_msg = str(e)
            logger.error(f"HTTP error deleting volume {volume_id}: {error_msg}")
            return False, f"删除失败: {error_msg}"
        except Exception as e:
            logger.error(f"Failed to delete volume {volume_id}: {e}")
            return False, str(e)

    def get_volume(self, volume_id):
        """获取存储卷信息"""
        conn = self.get_connection()
        try:
            # 查询所有项目的卷
            volume = conn.block_storage.get_volume(volume_id)
            return volume
        except ResourceNotFound:
            return None
        except Exception as e:
            logger.error(f"Failed to get volume {volume_id}: {e}")
            return None

    def get_port(self, port_id):
        """获取端口信息"""
        conn = self.get_connection()
        try:
            port = conn.network.get_port(port_id)
            return port
        except ResourceNotFound:
            return None
        except Exception as e:
            logger.error(f"Failed to get port {port_id}: {e}")
            return None

    def find_port_by_mac(self, mac_address):
        """通过MAC地址查找端口"""
        conn = self.get_connection()
        try:
            ports = list(conn.network.ports(mac_address=mac_address))
            if ports:
                return ports[0]
            return None
        except Exception as e:
            logger.error(f"Failed to find port by MAC {mac_address}: {e}")
            raise

    def list_ports_by_device(self, device_id):
        """根据设备ID获取端口列表"""
        conn = self.get_connection()
        try:
            ports = list(conn.network.ports(device_id=device_id))
            return ports
        except Exception as e:
            logger.error(f"Failed to list ports for device {device_id}: {e}")
            return []

    def list_volumes(self, all_projects=True):
        """获取所有存储卷"""
        conn = self.get_connection()
        try:
            volumes = list(conn.block_storage.volumes(all_projects=all_projects))
            return [{
                'id': volume.id,
                'name': _resource_get(volume, 'name') or volume.id,
                'size': _resource_get(volume, 'size'),
                'status': _resource_get(volume, 'status'),
                'bootable': _resource_get(volume, 'is_bootable') or _resource_get(volume, 'bootable'),
            } for volume in volumes]
        except Exception as e:
            logger.error(f"Failed to list volumes: {e}")
            return []

    def list_images(self):
        """获取镜像列表"""
        conn = self.get_connection()
        try:
            images = list(conn.image.images())
            return [{'id': img.id, 'name': img.name} for img in images]
        except Exception as e:
            logger.error(f"Failed to list images: {e}")
            return []

    def list_flavors(self):
        """获取规格列表"""
        conn = self.get_connection()
        try:
            flavors = list(conn.compute.flavors())
            return [{'id': f.id, 'name': f.name, 'vcpus': f.vcpus, 'ram': f.ram, 'disk': f.disk} for f in flavors]
        except Exception as e:
            logger.error(f"Failed to list flavors: {e}")
            return []

    def list_networks(self):
        """获取网络列表"""
        conn = self.get_connection()
        try:
            networks = list(conn.network.networks())
            return [{'id': n.id, 'name': n.name} for n in networks]
        except Exception as e:
            logger.error(f"Failed to list networks: {e}")
            return []


# 全局单例
openstack_service = OpenStackService()
