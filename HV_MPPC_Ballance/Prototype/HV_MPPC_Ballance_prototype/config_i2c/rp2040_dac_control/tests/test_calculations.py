"""Standard-library tests for the desktop application's DAC calculations."""

import importlib.util
from pathlib import Path
import sys
import unittest


APP_PATH = Path(__file__).resolve().parents[1] / "desktop" / "dac_control_app.py"


class CalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("dac_control_app", APP_PATH)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def test_one_volt_code_at_3v3(self):
        code = self.module.voltage_to_code(1.0, 0, 0, 0, 3.3, 3.3, 3.3, 1.22)
        self.assertEqual(code, 0x04D9)

    def test_code_to_voltage_at_3v3(self):
        voltage = self.module.code_to_voltage(0x04D9, 0, 0, 0, 3.3, 3.3, 3.3, 1.22)
        self.assertAlmostEqual(voltage, 0.9998291015625)

    def test_external_reference_uses_channel_bank(self):
        even = self.module.output_scale(0, 2, 0, 3.3, 2.5, 2.0, 1.22)
        odd = self.module.output_scale(1, 2, 0, 3.3, 2.5, 2.0, 1.22)
        self.assertEqual(even, 2.5)
        self.assertEqual(odd, 2.0)

    def test_bandgap_effective_scale(self):
        gain_1 = self.module.output_scale(0, 1, 0, 3.3, 3.3, 3.3, 1.22)
        gain_2 = self.module.output_scale(0, 1, 1, 5.5, 3.3, 3.3, 1.22)
        self.assertAlmostEqual(gain_1, 2.44)
        self.assertAlmostEqual(gain_2, 4.88)

    def test_full_scale_is_clamped(self):
        code = self.module.voltage_to_code(9.0, 0, 0, 0, 3.3, 3.3, 3.3, 1.22)
        self.assertEqual(code, 0x0FFF)


if __name__ == "__main__":
    unittest.main()
