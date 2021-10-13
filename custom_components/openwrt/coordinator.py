from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .ubus import Ubus
from .constants import DOMAIN

import logging
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

class DeviceCoordinator:


    def __init__(self, hass, config: dict, ubus: Ubus, all_devices: dict):
        self._config = config
        self._ubus = ubus
        self._all_devices = all_devices
        self._id = config["id"]
        self._apis = None
        
        self._coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name='openwrt',
            update_method=self.make_async_update_data(),
            update_interval=timedelta(seconds=30)
        )

    @property
    def coordinator(self) -> DataUpdateCoordinator:
        return self._coordinator

    async def discover_wireless(self) -> dict:
        result = dict(ap=[], mesh=[])
        if not self.is_api_supported("network.wireless"):
            return result
        try:
            response = await self._ubus.api_call('network.wireless', 'status', {})
            for radio, item in response.items():
                for iface in item['interfaces']:
                    conf = dict(ifname=iface['ifname'], network=iface['config']['network'][0])
                    if iface['config']['mode'] == 'ap':
                        result['ap'].append(conf)
                    if iface['config']['mode'] == 'mesh':
                        conf['mesh_id'] = iface['config']['mesh_id']
                        result['mesh'].append(conf)
        except NameError as err:
            _LOGGER.warning(f"Device [{self._id}] doesn't support wireless: {err}")
        return result

    def find_mesh_peers(self, mesh_id: str):
        result = []
        for _, device in self._all_devices.items():
            data = device.coordinator.data
            for _, mesh in data['mesh'].items():
                if mesh['id'] == mesh_id:
                    result.append(mesh['mac'])
        return result

    async def update_mesh(self, configs) -> dict:
        result = dict()
        if not self.is_api_supported("iwinfo"):
            return result
        try:
            for conf in configs:
                info = await self._ubus.api_call(
                    'iwinfo',
                    'info',
                    dict(device=conf['ifname'])
                )
                peers = {}
                result[conf['ifname']] = dict(
                    mac=info['bssid'].lower(),
                    signal=info.get("signal", -100),
                    id=conf['mesh_id'],
                    noise=info.get("noise", 0),
                    bitrate=info.get("bitrate", -1),
                    peers=peers,
                )
                for mac in self.find_mesh_peers(conf['mesh_id']):
                    try:
                        assoc = await self._ubus.api_call(
                            'iwinfo',
                            'assoclist',
                            dict(device=conf['ifname'], mac=mac)
                        )
                        peers[mac] = dict(
                            active=assoc.get("mesh plink") == "ESTAB",
                            signal=assoc.get("signal", -100), 
                            noise=assoc.get("noise", 0)
                        )
                    except ConnectionError:
                        pass
        except ConnectionError as err:
            _LOGGER.warning(f"Device [{self._id}] doesn't support iwinfo: {err}")
        return result

    async def update_hostapd_clients(self, interface_id: str) -> dict:
        response = await self._ubus.api_call(
            f"hostapd.{interface_id}", 
            'get_clients', 
            dict()
        )
        macs = list(map(lambda x: x.lower(), response['clients'].keys()))
        response = await self._ubus.api_call(
            f"hostapd.{interface_id}", 
            'wps_status', 
            dict()
        )
        return dict(
            clients=len(macs), 
            macs=macs, 
            wps=response["pbc_status"] == "Active"
        )

    async def set_wps(self, interface_id: str, enable: bool):
        await self._ubus.api_call(
            f"hostapd.{interface_id}",
            "wps_start" if enable else "wps_cancel", 
            dict()
        )
        await self.coordinator.async_request_refresh()

    async def do_reboot(self):
        await self._ubus.api_call(
            "system",
            "reboot",
            dict()
        )

    async def update_ap(self, configs) -> dict:
        result = dict()
        for item in configs:
            result[item['ifname']] = await self.update_hostapd_clients(item['ifname'])
        return result

    async def update_info(self) -> dict:
        result = dict()
        response = await self._ubus.api_call("system", "board", {})
        return {
            "model": response["model"],
            "manufacturer": response["release"]["distribution"],
            "sw_version": "%s %s" % (
                response["release"]["version"],
                response["release"]["revision"]
            ),
        }

    async def discover_mwan3(self):
        if not self.is_api_supported("mwan3"):
            return dict()
        result = dict()
        response = await self._ubus.api_call(
            "mwan3", 
            "status",
            dict(section="interfaces")
        )
        for key, iface in response["interfaces"].items():
            if not iface.get("enabled", False):
                continue
            result[key] = {
                "offline_sec": iface.get("offline", 0),
                "online_sec": iface.get("online", 0),
                "uptime_sec": iface.get("uptime", 0),
                "online": iface.get("status") == "online",
                "status": iface.get("status"),
                "up": iface.get("up")
            }
        return result
    
    async def load_ubus(self):
        return await self._ubus.api_call("*", None, None, "list")

    def is_api_supported(self, name: str) -> bool:
        if self._apis and name in self._apis:
            return True
        return False

    def make_async_update_data(self):
        async def async_update_data():
            try:
                if not self._apis:
                    self._apis = await self.load_ubus()
                result = dict()
                result["info"] = await self.update_info()
                wireless_config = await self.discover_wireless()
                result['wireless'] = await self.update_ap(wireless_config['ap'])
                result['mesh'] = await self.update_mesh(wireless_config['mesh'])
                result["mwan3"] = await self.discover_mwan3()
                _LOGGER.debug(f"Full update [{self._id}]: {result}")
                return result
            except PermissionError as err:
                raise ConfigEntryAuthFailed from err
            except Exception as err:
                _LOGGER.exception(f"Device [{self._id}] async_update_data error: {err}")
                raise UpdateFailed(f"OpenWrt communication error: {err}")
        return async_update_data

def new_coordinator(hass, config: dict, all_devices: dict) -> DeviceCoordinator:
    _LOGGER.debug(f"new_coordinator: {config}")
    schema = "https" if config["https"] else "http"
    port = ":%d" % (config["port"]) if config["port"] > 0 else ''
    url = "%s://%s%s%s" % (schema, config["address"], port, config["path"])
    connection = Ubus(
        hass.async_add_executor_job, 
        url,
        config["username"], 
        config.get("password", "")
    )
    device = DeviceCoordinator(hass, config, connection, all_devices)
    return device