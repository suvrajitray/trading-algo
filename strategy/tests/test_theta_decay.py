import unittest
from unittest.mock import Mock, patch
import os, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategy.theta_decay import ThetaDecayStrategy
import pandas as pd

class TestThetaDecayStrategy(unittest.TestCase):

    def setUp(self):
        """Set up a mock environment for testing the strategy."""
        self.mock_broker = Mock()
        self.mock_order_manager = Mock()

        # Mock the instruments dataframe
        self.mock_broker.instruments_df = pd.DataFrame({
            'tradingsymbol': ['NIFTY24OCT20000CE', 'NIFTY24OCT19000PE', 'NIFTY24OCT21000CE', 'NIFTY24OCT18000PE'],
            'instrument_type': ['CE', 'PE', 'CE', 'PE'],
            'strike': [20000, 19000, 21000, 18000]
        })
        self.mock_broker.download_instruments.return_value = None

        self.config = {
            'symbol_initials': 'NIFTY24OCT',
            'call_premium': 50,
            'put_premium': 50,
            'hedge_premium': 2.0,
            'stop_loss_percentage': 0.25,
            'max_reentries': 2,
            'trailing_sl_decay_factor_1': 2,
            'trailing_sl_decay_factor_2': 3,
            'capital_protection_limit': 30000,
            'profit_booking_percentage': 0.70,
            'overall_profit_target': 20000,
            'trailing_profit_target': 10000,
            'trailing_profit_increment': 10,
            'entry_time': "09:20",
            'exchange': "NFO",
            'product_type': "NRML",
            'order_type': "MARKET",
            'quantity': 50
        }

        self.strategy = ThetaDecayStrategy(self.mock_broker, self.config, self.mock_order_manager)

    def test_stop_loss_level_calculation(self):
        """Test if the stop-loss level is calculated correctly on entry."""

        # Mock the return values for finding options
        self.strategy._find_option_by_premium = Mock()
        self.strategy._find_option_by_premium.side_effect = [
            {'tradingsymbol': 'NIFTY24OCT20000CE', 'last_price': 55}, # Call
            {'tradingsymbol': 'NIFTY24OCT19000PE', 'last_price': 45}, # Put
            {'tradingsymbol': 'NIFTY24OCT21000CE', 'last_price': 2.0}, # Call Hedge
            {'tradingsymbol': 'NIFTY24OCT18000PE', 'last_price': 1.5}  # Put Hedge
        ]

        self.strategy._place_order = Mock(return_value="12345")

        # Call the entry handler
        self.strategy._handle_entry()

        # Total premium = 55 + 45 = 100
        # SL percentage premium = 100 * (1 + 0.25) = 125
        # SL double premium = 2 * 100 = 200
        # Expected SL should be min(125, 200) = 125
        self.assertEqual(self.strategy.stop_loss_level, 125)
        self.assertEqual(self.strategy.active_strangle['stop_loss_level'], 125)

    def test_individual_profit_booking(self):
        """Test if a leg is exited when profit booking percentage is reached."""

        # Setup initial state
        self.strategy.positions = {
            'NIFTY24OCT20000CE': {
                'leg': 'sell',
                'status': 'active',
                'entry_price': 50,
                'current_price': 14.9 # 70.2% decay
            }
        }
        self.strategy.strat_var_profit_booking_percentage = 0.70
        self.strategy.is_trading_stopped = False
        self.strategy._place_order = Mock()

        # Call profit booking handler
        self.strategy._handle_profit_booking()

        # Assert that the place_order was called to exit the position
        self.strategy._place_order.assert_called_once_with('NIFTY24OCT20000CE', 50, 'BUY')
        self.assertEqual(self.strategy.positions['NIFTY24OCT20000CE']['status'], 'exited')

    def test_no_individual_profit_booking(self):
        """Test that a leg is not exited if profit booking percentage is not reached."""

        # Setup initial state
        self.strategy.positions = {
            'NIFTY24OCT20000CE': {
                'leg': 'sell',
                'status': 'active',
                'entry_price': 50,
                'current_price': 20 # 60% decay
            }
        }
        self.strategy.strat_var_profit_booking_percentage = 0.70
        self.strategy.is_trading_stopped = False
        self.strategy._place_order = Mock()

        # Call profit booking handler
        self.strategy._handle_profit_booking()

        # Assert that place_order was not called
        self.strategy._place_order.assert_not_called()
        self.assertEqual(self.strategy.positions['NIFTY24OCT20000CE']['status'], 'active')

if __name__ == '__main__':
    unittest.main()
