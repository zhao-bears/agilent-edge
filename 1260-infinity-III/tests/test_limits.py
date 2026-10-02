import unittest

from driver import Driver


def _gradient(**overrides):
    gradient = {
        "name": "10min",
        "eluents": {
            "solvent_a": "Water (0.1% Formic Acid)",
            "solvent_b": "Acetonitrile (0.1% Formic Acid)",
        },
        "run_time": 10.0,
        "equilibration_time": 1.0,
        "header_gradient_table": [
            "time",
            "flow_rate",
            "Acetonitrile (0.1% Formic Acid)",
        ],
        "gradient_table": [[0.0, 1.0, 0.05], [10.0, 1.0, 0.95]],
    }
    gradient.update(overrides)
    return gradient


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.driver = Driver.__new__(Driver)
        self.driver._max_flow_ml_min = 5.0
        self.driver._max_injection_ul = 100.0
        self.driver._max_run_time_min = 120.0
        self.driver._instrument_config = {
            "eluents": {
                "A1": "Water (0.1% Formic Acid)",
                "B1": "Acetonitrile (0.1% Formic Acid)",
            }
        }
        self.driver._software_settings = {}
        self.driver._controller = None

    def test_accepts_binary_pump_flow(self):
        self.driver._check_gradient_limits(_gradient())

    def test_rejects_flow_above_binary_pump_maximum(self):
        with self.assertRaises(ValueError):
            self.driver._check_gradient_limits(
                _gradient(gradient_table=[[0.0, 5.1, 0.05], [10.0, 1.0, 0.95]])
            )

    def test_rejects_percent_instead_of_fraction(self):
        with self.assertRaisesRegex(ValueError, "fraction"):
            self.driver._check_gradient_limits(
                _gradient(gradient_table=[[0.0, 1.0, 20], [10.0, 1.0, 95]])
            )

    def test_rejects_unknown_solvent(self):
        gradient = _gradient()
        gradient["eluents"] = {"solvent_a": "Methanol", "solvent_b": "Water"}
        with self.assertRaises(ValueError):
            self.driver._check_gradient_limits(gradient)

    def test_rejects_injection_above_metering_head(self):
        with self.assertRaises(ValueError):
            self.driver._check_injection("sample", "D1F-A1", 101, "C:/results")

    def test_rejects_path_in_sample_name(self):
        with self.assertRaises(ValueError):
            self.driver._check_injection("a/b", "D1F-A1", 1, "C:/results")

    def test_lc_mode_skips_mass_spec_template(self):
        self.assertIsNone(self.driver._ms_settings(""))
        self.assertIsNone(self.driver._ms_settings("lc"))


if __name__ == "__main__":
    unittest.main()
