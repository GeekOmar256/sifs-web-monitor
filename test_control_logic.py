import unittest

from control_logic import light_should_run, parse_co2_packet, pump_should_run


class ControlLogicTests(unittest.TestCase):
    def test_mhz19b_packet_and_checksum(self):
        self.assertEqual(parse_co2_packet(bytes.fromhex("FF 86 01 F4 00 00 00 00 85")), 500)
        with self.assertRaises(ValueError):
            parse_co2_packet(bytes.fromhex("FF 86 01 F4 00 00 00 00 00"))

    def test_pump_stops_for_wet_or_missing_soil_and_times_out(self):
        self.assertTrue(pump_should_run("auto", False, "dry", False, None, 0, 100))
        self.assertFalse(pump_should_run("manual", True, "wet", True, 90, 0, 100))
        self.assertFalse(pump_should_run("manual", True, None, False, None, 0, 100))
        self.assertFalse(pump_should_run("auto", False, "dry", True, 70, 0, 100))
        self.assertFalse(pump_should_run("auto", False, "dry", False, None, 95, 100))

    def test_light_hysteresis(self):
        self.assertTrue(light_should_run("auto", False, 1000, False, 1000, 100))
        self.assertTrue(light_should_run("auto", False, 950, True, 1000, 100))
        self.assertFalse(light_should_run("auto", False, 900, True, 1000, 100))
        self.assertFalse(light_should_run("auto", False, None, True, 1000, 100))


if __name__ == "__main__":
    unittest.main()
