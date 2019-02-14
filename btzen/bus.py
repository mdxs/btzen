#
# BTZen - Bluetooth Smart sensor reading library.
#
# Copyright (C) 2015-2018 by Artur Wroblewski <wrobell@riseup.net>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import asyncio
import contextvars
import logging
import threading
from functools import lru_cache, partial
from weakref import WeakValueDictionary

from . import _btzen
from .error import ConnectionError

logger = logging.getLogger(__name__)

INTERFACE_DEVICE = 'org.bluez.Device1'
INTERFACE_GATT_CHR = 'org.bluez.GattCharacteristic1'

def _mac(mac):
    return mac.replace(':', '_').upper()

def _device_path(mac):
    return '/org/bluez/hci0/dev_{}'.format(_mac(mac))

class Bus:
    bus = contextvars.ContextVar('bus', default=None)

    def __init__(self, system_bus):
        self.system_bus = system_bus

        loop = asyncio.get_event_loop()
        process = partial(_btzen.bt_process, system_bus)
        loop.add_reader(system_bus.fileno, process)

        # cache of connection locks; lock is used to perform single
        # connection to a given bluetooth device; once a lock is deleted,
        # it will be removed from the dictionary
        self._lock = WeakValueDictionary()
        self._notifications = Notifications(self)

    @staticmethod
    def get_bus():
        """
        Get system bus reference.

        The reference is local to current thread.
        """
        bus = Bus.bus.get()
        if bus is None:
            system_bus = _btzen.default_bus()
            bus = Bus(system_bus)
            Bus.bus.set(bus)
        return bus

    async def connect(self, mac):
        """
        Connect to Bluetooth device.

        If connected, the method does nothing.

        :param mac: MAC address of Bluetooth device.
        """
        path = _device_path(mac)

        lock = self._lock.get(mac)
        if lock is None:
            self._lock[mac] = lock = asyncio.Lock()

        try:
            async with lock:
                await self._connect_and_resolve(path)
        finally:
            # destroy lock, so it is removed from the cache when no longer
            # in use
            del lock
            logger.debug('number of connection locks: {}'.format(len(self._lock)))

        name = self._get_name(mac)
        logger.info('connected to {}'.format(name))

        return name

    def sensor_path(self, mac, uuid):
        if uuid is None:
            return None
        by_uuid = self._get_sensor_paths(mac)
        return by_uuid[uuid]

    def _gatt_start(self, path):
        # TODO: creates notification session; if another session started,
        # then we get notifications twice; this needs to be fixed
        self._notifications.start(path, INTERFACE_GATT_CHR, 'Value')
        _btzen.bt_notify_start(self.system_bus, path)

    async def _gatt_get(self, path):
        task = self._notifications.get(path, INTERFACE_GATT_CHR, 'Value')
        return (await task)

    def _gatt_stop(self, path):
        _btzen.bt_notify_stop(self.system_bus, path)
        self._notifications.stop(path, INTERFACE_GATT_CHR)

    def _gatt_size(self, path) -> int:
        return self._notifications.size(path, INTERFACE_GATT_CHR, 'Value')

    def _dev_property_start(self, path, name):
        self._notifications.start(path, INTERFACE_DEVICE, name)

    async def _dev_property_get(self, path, name):
        value = await self._notifications.get(path, INTERFACE_DEVICE, name)
        return value

    def _dev_property_stop(self, path, name):
        self._notifications.stop(path, INTERFACE_DEVICE)

    async def _dev_property(self, path, name):
        self._dev_property_start(path, name)
        try:
            return (await self._dev_property_get(path, name))
        finally:
            self._dev_property_stop(path, name)

    async def _connect_and_resolve(self, path):
        logger.info('connecting to {}'.format(path))
        await self._connect(path)

        # first create task
        task_sr = self._dev_property(path, 'ServicesResolved')
        # then check the property
        resolved = self._property_bool(path, 'ServicesResolved')
        try:
            if not resolved:
                logger.info('resolving services for {}'.format(path))
                # and wait for services to be resolved
                value = await task_sr
                logger.info('{} services resolved {}'.format(path, value))
        finally:
            # destroy the notification
            task_sr.close()

    async def _connect(self, path):
        assert self.system_bus is not None
        try:
            task = _btzen.bt_connect(self.system_bus, path)
            await task
        except Exception as ex:
            # exception might be raised if device is already connected, so
            # check if errors has to be raised
            logger.debug('connection error: {}'.format(ex))
            # FIXME: if no scan on, then this fails
            connected = self._property_bool(path, 'Connected')
            if not connected:
                raise
        else:
            connected = self._property_bool(path, 'Connected')
            logger.info('connected to {}: {}'.format(path, connected))

    def _property_bool(self, path, name):
        bus = self.system_bus
        value = _btzen.bt_property_bool(bus, path, INTERFACE_DEVICE, name)
        return value

    @lru_cache()
    def _get_sensor_paths(self, mac):
        path = _device_path(mac)
        by_uuid = _btzen.bt_characteristic(self.system_bus, path)
        return by_uuid

    @lru_cache()
    def _get_name(self, mac):
        path = _device_path(mac)
        bus = self.system_bus
        return _btzen.bt_property_str(bus, path, INTERFACE_DEVICE, 'Name')

class Notifications:
    def __init__(self, bus):
        self._data = {}
        self._bus = bus

    def start(self, path, iface, name):
        key = path, iface
        data = self._data.get(key)
        if data is None:
            bus = self._bus.system_bus
            data = _btzen.bt_property_monitor_start(bus, path, iface)
            self._data[key] = data

        assert key in self._data
        if not data.is_registered(name):
            data.register(name)

    async def get(self, path, iface, name):
        key = path, iface
        data = self._data[key]
        return (await data.get(name))

    def size(self, path, iface, name):
        key = path, iface
        return self._data[key].size(name)

    def stop(self, path, iface):
        # TODO: add name and call PropertyNotification.stop when no
        # properties monitored
        key = path, iface
        data = self._data[key].stop()

# vim: sw=4:et:ai
