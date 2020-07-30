from caproto.server import PVGroup, SubGroup, pvproperty
from caproto.server.records import MotorFields


def broadcast_precision_to_fields(record):
    precision = record.precision
    for field, prop in record.field_inst.pvdb.items():
        # HACK: this shouldn't be done normally
        if 'precision' in prop._data:
            prop._data['precision'] = precision


class FakeMotor(PVGroup):
    tick_rate_hz = 10
    motor = pvproperty(value=0.0, name='', record='motor',
                       precision=3)

    @motor.startup
    async def motor(self, instance, async_lib):
        self.async_lib = async_lib

        broadcast_precision_to_fields(self.motor)

        fields = self.motor.field_inst  # type: MotorFields
        await fields.velocity.write(0.1)

        while True:
            dwell = 1. / self.tick_rate_hz
            target_pos = self.motor.value
            diff = (target_pos - fields.user_readback_value.value)
            # compute the total movement time based an velocity
            total_time = abs(diff / fields.velocity.value)
            # compute how many steps, should come up short as there will
            # be a final write of the return value outside of this call
            num_steps = int(total_time // dwell)

            if abs(diff) < 1e-9:
                await async_lib.library.sleep(dwell)
                continue

            await fields.done_moving_to_value.write(1)
            await fields.done_moving_to_value.write(0)
            await fields.motor_is_moving.write(1)

            readback = fields.user_readback_value.value
            step_size = diff / num_steps if num_steps > 0 else 0.0
            for j in range(num_steps):
                readback += step_size
                await fields.user_readback_value.write(readback)
                await async_lib.library.sleep(dwell)

            await fields.user_readback_value.write(target_pos)
            await fields.done_moving_to_value.write(1)


class FakeBladeGroup(PVGroup):
    """
    PV group for fake photon energy readback
    """
    blade1 = SubGroup(FakeMotor, prefix='Blade1:Mtr')