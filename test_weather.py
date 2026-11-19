"""weather.py without the network: splitting GRIB2 messages and making the pictures."""

import unittest

import numpy as np

import weather


class WeatherTest(unittest.TestCase):
    def test_messages(self):
        def message(body):
            total = 16 + len(body) + 4
            return b"GRIB" + b"\0\0\0\x02" + total.to_bytes(8, "big") + body + b"7777"

        a, b = message(b"first"), message(b"second one")
        self.assertEqual([a, b], list(weather.messages(b"junk" + a + b)))
        self.assertEqual([], list(weather.messages(b"")))

    def test_pictures(self):
        shape = (361, 720)
        fields = {
            "10u:instant": np.full(shape, 40.0),
            "10v:instant": np.full(shape, -40.0),
            "2t:instant": np.full(shape, 273.15),  # 0 °C
            "tcc:instant": np.full(shape, 50.0),
            "prate:avg": np.full(shape, 20 / 3600),  # 20 mm/h
            "prmsl:instant": np.full(shape, 100_000.0),  # 1000 hPa
        }
        wt, cp = weather.pictures(fields)
        self.assertEqual((720, 361), wt.size)
        self.assertEqual((255, 0, 128), wt.getpixel((0, 0)))
        self.assertEqual((128, 255, 128), cp.getpixel((10, 10)))
        # The averaged rain rate when there is one, else the instant one.
        del fields["prate:avg"]
        fields["prate:instant"] = np.zeros(shape)
        self.assertEqual(0, weather.pictures(fields)[1].getpixel((0, 0))[1])


if __name__ == "__main__":
    unittest.main()
