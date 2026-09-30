import unittest
from datetime import datetime

from riskreport import metrics
from riskreport.model import Trade

D = datetime(2024, 1, 2)
T = [Trade(D, "A", "BUY", 4, 10.0),
     Trade(D.replace(hour=1), "A", "SELL", 4, 12.0)]


class MetricsTest(unittest.TestCase):
    def test_flat_position_dropped(self):
        self.assertEqual(metrics.positions(T), {})

    def test_avg_cost(self):
        self.assertEqual(metrics.avg_cost(T, "A"), 10.0)

    def test_pnl(self):
        self.assertEqual(metrics.realized_pnl(T, "A"), 8.0)
