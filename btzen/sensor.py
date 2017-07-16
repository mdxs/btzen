#
# BTZen - Bluetooth Smart sensor reading library.
#
# Copyright (C) 2015-2017 by Artur Wroblewski <wrobell@riseup.net>
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

"""
The identificators for specific sensors can be found at

CC2541DK
    http://processors.wiki.ti.com/index.php/SensorTag_User_Guide
CC2650STK
    http://processors.wiki.ti.com/index.php/CC2650_SensorTag_User's_Guide
"""

import asyncio
import logging
import struct
from collections import deque, namedtuple
from functools import partial

from . import _btzen
from .import converter
from .bus import BUS
from .error import ConfigurationError, DataReadError
from .util import dev_uuid

# default length of buffer for notifying sensors
BUFFER_LEN = 100

logger = logging.getLogger(__name__)

Parameters = namedtuple('Parameters', [
    'name', 'path_data', 'path_conf', 'path_period', 'config_on',
    'config_on_notify', 'config_off',
])


class Sensor:
    """
    :var _system_bus: System D-Bus reference (not thread safe).
    """
    def __init__(self, mac, notifying=False, loop=None):
        self._mac = mac
        self._notifying = notifying
        self._loop = loop if loop else asyncio.get_event_loop()
        self._future = None
        self._error = None

        if self._notifying:
            self._buffer = deque([], maxlen=BUFFER_LEN)
        else:
            self._buffer = None

        self._params = None
        self._data = bytearray(self.DATA_LEN)
        self._device_ref = None
        self._device = None
        self._system_bus = None

        self._notification = None

    async def connect(self):
        """
        Connect to sensor Bluetooth device.
        """
        assert isinstance(self.UUID_DATA, str)
        assert isinstance(self.UUID_CONF, str) or self.UUID_CONF is None
        assert isinstance(self.UUID_PERIOD, str) or self.UUID_PERIOD is None

        self._system_bus = BUS.get_bus()

        name = await BUS.connect(self._mac)
        self._set_parameters(name)
        self._enable()

    def set_interval(self, interval):
        if self._params.path_period:
            value = int(interval * 100)
            assert value < 256
            path = self._params.path_period
            bus = self._system_bus
            r = _btzen.bt_write(bus, path, bytes([value]))
            if r < 0:
                msg = 'Cannot set sensor interval value: {}'.format(r)
                raise ConfigurationError(msg)

    def read(self):
        """
        Read and return sensor data.

        Method is thread safe.
        """
        r = lib.bt_device_read(Sensor.BUS.get_bus(), self._device, ffi.from_buffer(self._data))
        if r < 0:
            raise DataReadError('Sensor data read error: {}'.format(r))
        return self._converter(self._data)

    async def read_async(self):
        """
        Read and return sensor data.

        This method is a coroutine and is *not* thread safe.
        """
        if self._notifying:
            task = self._notification.get()
        else:
            task = self._loop.create_future()
            _btzen.bt_read(self._system_bus, self._params.path_data, task)
        return self._converter(await task)

    def close(self):
        """
        Disable sensor and stop reading sensor data.

        Pending, asynchronous coroutines are cancelled.
        """
        if self._notifying and self._device:
            # ignore any errors when closing sensor
            lib.bt_device_stop_notify(Sensor.BUS.get_bus(), self._device)

        # disable switched on sensor; some sensors stay always on,
        # i.e. button
        if self._params and self._params.config_off and self._device:
            r = lib.bt_device_write(
                Sensor.BUS.get_bus(),
                self._device.chr_conf,
                self._params.config_off,
                len(self._params.config_off)
            )
        future = self._future
        if future and not future.done():
            ex = asyncio.CancelledError('Sensor coroutine closed')
            future.set_exception(ex)

        self._system_bus = None

        logger.info('{} sensor closed'.format(self._mac))

    def _process_event(self):
        """
        Set sensor data as result of current asynchronous call.

        .. seealso:: :py:meth:`Sensor.read_async`
        """
        buffer = self._buffer
        future = self._future
        awaited  = future and not future.done()

        r = lib.bt_device_async_error_no()
        if r < 0:
            ex = DataReadError('Sensor data read error: {}'.format(r))
            if awaited:
                future.set_exception(ex)
            elif self._notifying:
                self._error = ex
            else:
                raise ex
            return

        value = self._converter(self._data)

        if self._notifying:
            # if buffer is non-empty, process data through buffer
            if awaited and buffer:
                # pop item first, so we do not add value to already full
                # buffer
                item = buffer.popleft()
                buffer.append(value)
                future.set_result(item)
            elif awaited and not buffer:
                # no data in buffer, so put value as result of awaited
                # future immediately
                future.set_result(value)
            elif len(buffer) == BUFFER_LEN:
                assert not awaited
                self._error = DataReadError('Data buffer full')
            else:
                assert not awaited
                buffer.append(value)
        elif awaited:
            future.set_result(value)
        else:
            assert not awaited and not self._notifying
            # for non-notifying sensors _process_event method should be
            # called only after lib.bt_device_read_async is executed; if
            # current future is not ready, then it looks like internal
            # programming error and we can only raise an exception
            raise DataReadError('Sensor coroutine not awaited')

    def _set_parameters(self, name):
        get_path = partial(BUS.sensor_path, self._mac)
        mac = self._mac
        self._params = params = Parameters(
            name,
            get_path(self.UUID_DATA),
            get_path(self.UUID_CONF),
            get_path(self.UUID_PERIOD),
            self.CONFIG_ON,
            self.CONFIG_ON_NOTIFY,
            self.CONFIG_OFF,
        )

        # ceate data converter
        name = params.name
        factory = converter.data_converter(name, self.UUID_DATA)
        # TODO: fix for CC2541DK
        self._converter = factory(name, None)

    def _enable(self):
        bus = self._system_bus
        if self._notifying:
            config_on = self._params.config_on_notify
            self._notification = _btzen.ValueChange()
            _btzen.bt_notify(bus, self._params.path_data, self._notification)
        else:
            config_on = self._params.config_on

        # enabled switched off sensor; some sensors are always on,
        # i.e. button
        if config_on:
            _btzen.bt_write( bus, self._params.path_conf, config_on)


class Temperature(Sensor):
    DATA_LEN = 4
    UUID_DATA = dev_uuid(0xaa01)
    UUID_CONF = dev_uuid(0xaa02)
    UUID_PERIOD = dev_uuid(0xaa03)
    CONFIG_ON = b'\x01'
    CONFIG_ON_NOTIFY = b'\x01'
    CONFIG_OFF = b'\x00'


class Pressure(Sensor):
    DATA_LEN = 6
    UUID_DATA = dev_uuid(0xaa41)
    UUID_CONF = dev_uuid(0xaa42)
    UUID_PERIOD = dev_uuid(0xaa44)
    CONFIG_ON = b'\x01'
    CONFIG_ON_NOTIFY = b'\x01'
    CONFIG_OFF = b'\x00'


class Humidity(Sensor):
    DATA_LEN = 4
    UUID_DATA = dev_uuid(0xaa21)
    UUID_CONF = dev_uuid(0xaa22)
    UUID_PERIOD = dev_uuid(0xaa23)
    CONFIG_ON = b'\x01'
    CONFIG_ON_NOTIFY = b'\x01'
    CONFIG_OFF = b'\x00'


class Light(Sensor):
    DATA_LEN = 2
    UUID_DATA = dev_uuid(0xaa71)
    UUID_CONF = dev_uuid(0xaa72)
    UUID_PERIOD = dev_uuid(0xaa73)
    CONFIG_ON = b'\x01'
    CONFIG_ON_NOTIFY = b'\x01'
    CONFIG_OFF = b'\x00'


class Accelerometer(Sensor):
    DATA_LEN = 18
    UUID_DATA = dev_uuid(0xaa81)
    UUID_CONF = dev_uuid(0xaa82)
    UUID_PERIOD = dev_uuid(0xaa83)

    ACCEL_Z = 0x08
    ACCEL_Y = 0x10
    ACCEL_X = 0x20
    WAKE_ON_MOTION = 0x80
    CONFIG_ON = struct.pack('<H', ACCEL_X | ACCEL_Y | ACCEL_Z)
    CONFIG_ON_NOTIFY = struct.pack('<H', ACCEL_X | ACCEL_Y | ACCEL_Z | WAKE_ON_MOTION)
    CONFIG_OFF = '\x00\x00'


class Button(Sensor):
    DATA_LEN = 1
    UUID_DATA = '0000ffe1-0000-1000-8000-00805f9b34fb'
    UUID_CONF = None
    UUID_PERIOD = None

    CONFIG_ON = None
    CONFIG_ON_NOTIFY = None
    CONFIG_OFF = None

class Weight(Sensor):
    DATA_LEN = 9
    UUID_DATA = '00002a9d-0000-1000-8000-00805f9b34fb'

    UUID_CONF = None
    UUID_PERIOD = None

    CONFIG_ON = None
    CONFIG_ON_NOTIFY = None
    CONFIG_OFF = None

# vim: sw=4:et:ai
