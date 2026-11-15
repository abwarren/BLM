import unittest

from candles import aggregate_kpi_candles


class KpiCandlesTests(unittest.TestCase):
    def test_default_one_minute_and_under_green(self):
        rows = [
            {"timestamp": 1, "market_line": 150, "final_score": 140},
            {"timestamp": 20, "market_line": 148, "final_score": 140},
            {"timestamp": 59, "market_line": 145, "final_score": 140},
        ]
        candle = aggregate_kpi_candles(rows)[0]
        self.assertEqual(candle.time, 0)
        self.assertEqual((candle.open, candle.high, candle.low, candle.close), (10, 10, 5, 5))
        self.assertEqual(candle.color, "green")
        self.assertEqual(candle.observations, 3)

    def test_over_is_red_even_if_value_rises_inside_candle(self):
        rows = [
            {"timestamp": 61, "market_line": 130, "final_score": 140},
            {"timestamp": 90, "market_line": 135, "final_score": 140},
        ]
        candle = aggregate_kpi_candles(rows)[0]
        self.assertEqual(candle.time, 60)
        self.assertEqual((candle.open, candle.high, candle.low, candle.close), (-10, -5, -10, -5))
        self.assertEqual(candle.color, "red")

    def test_push_is_neutral(self):
        candle = aggregate_kpi_candles([
            {"timestamp": 1, "market_line": 140, "final_score": 140}
        ])[0]
        self.assertEqual(candle.color, "neutral")
        self.assertEqual(candle.close, 0)

    def test_interval_is_adjustable(self):
        rows = [
            {"timestamp": 10, "market_line": 150, "final_score": 140},
            {"timestamp": 70, "market_line": 130, "final_score": 140},
        ]
        self.assertEqual(len(aggregate_kpi_candles(rows, 60)), 2)
        self.assertEqual(len(aggregate_kpi_candles(rows, 300)), 1)

    def test_missing_values_are_not_zero(self):
        rows = [
            {"timestamp": 1, "market_line": None, "final_score": 140},
            {"timestamp": 2, "market_line": 150, "final_score": None},
            {"timestamp": 3, "market_line": 150, "final_score": 140},
        ]
        candles = aggregate_kpi_candles(rows)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].observations, 1)
        self.assertEqual(candles[0].close, 10)

    def test_naive_datetime_is_rejected(self):
        rows = [
            {"timestamp": "2026-11-15T12:00:00", "market_line": 150, "final_score": 140}
        ]
        self.assertEqual(aggregate_kpi_candles(rows), [])

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_kpi_candles([], 0)


if __name__ == "__main__":
    unittest.main()
