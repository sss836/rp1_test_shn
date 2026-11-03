"""Account-bound resource downloads independent of controller/CAN leases."""
import os
from pathlib import Path

from factory_hmi.motor_firmware_resources import stage_resource,verify_resource


class MotorFirmwareResourceDownloads:
    def __init__(self,software_updates,data_root):
        self.sessions = software_updates
        self.cache_root = Path(data_root)/'motor-firmware-resources'

    @staticmethod
    def key():
        # This is the existing administrator-provisioned trust path, not a key
        # supplied by the catalog, a user form, or the downloaded resource.
        value = os.environ.get('RP1_MOTOR_FIRMWARE_PUBLIC_KEY','').strip()
        if not value:
            raise ValueError('尚未配置可信电机固件公钥')
        return Path(value).expanduser()

    def _current(self,client,identity):
        current,current_identity = self.sessions._session()
        if current is not client or current_identity != identity:
            raise ValueError('资源查询或下载期间账号已变化；文件仅为缓存，请重新登录查询')

    def check(self):
        client,identity = self.sessions._session()
        result = client.motor_firmware_resource_releases()
        for release in result.get('releases',[]):
            verify_resource(release,self.key())
        self._current(client,identity)
        return {**result,'artifact_type':'motor_firmware_resource','flash_eligible':False,
                'scope_notice':'管理员确认适用范围，仅下载；实际软件/硬件/协议映射未知，不进入OTA刷写列表。'}

    def download(self,release_id):
        client,identity = self.sessions._session()
        result = self.check()
        self._current(client,identity)
        release = next((row for row in result.get('releases',[]) if row['manifest']['release_id']==release_id),None)
        if release is None:
            raise ValueError('电机资源目录已变化，请重新查询')
        receipt = stage_resource(release,key=self.key(),cache_root=self.cache_root,
            open_stream=lambda:client.motor_firmware_resource_package(release_id),downloaded_by=identity,
            check_session=lambda:self._current(client,identity))
        self._current(client,identity)
        return receipt
