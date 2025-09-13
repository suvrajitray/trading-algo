import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from logger import logger
from datetime import datetime
import time

class ThetaDecayStrategy:
    """
    Nifty Weekly Options - Theta Decay Strangle Strategy

    This strategy implements a theta decay strangle on NIFTY weekly options.
    It is designed to be run on expiry day to capture the time decay of options.

    STRATEGY OVERVIEW:
    ==================
    1. **Initial Setup**:
       - Sell a strangle (1 CE and 1 PE) with premiums around a configured value.
       - Buy far OTM options for hedging to reduce margin.

    2. **Stop Loss Rules**:
       - A stop-loss is placed on the total premium of the strangle.
       - If the SL is breached, only the loss-making leg is exited.
       - A new strangle can be opened up to a configured number of times.

    3. **Trailing Stop Loss**:
       - The profitable leg is trailed with a TSL based on premium decay.

    4. **Profit Booking**:
       - Individual legs are exited if they decay by a certain percentage.
       - The entire position is closed if a total profit target is reached.

    5. **Capital Protection**:
       - If the total loss reaches a configured limit, all positions are exited for the day.
    """

    def __init__(self, broker, config, order_manager):
        # Assign config values as instance variables with 'strat_var_' prefix
        for k, v in config.items():
            setattr(self, f'strat_var_{k}', v)

        # External dependencies
        self.broker = broker
        self.order_manager = order_manager
        self.symbol_initials = self.strat_var_symbol_initials

        # Load instruments
        self.broker.download_instruments()
        self.instruments = self.broker.instruments_df[self.broker.instruments_df['tradingsymbol'].str.startswith(self.symbol_initials)]
        if self.instruments.shape[0] == 0:
            logger.error(f"No instruments found for {self.symbol_initials}")
            return

        self._initialize_state()

    def _initialize_state(self):
        """Initializes the state of the strategy."""
        self.active_strangle = None
        self.reentries_count = 0
        self.total_pnl = 0
        self.positions = {}
        self.stop_loss_level = 0
        self.is_trading_stopped = False
        self.trailing_overall_profit_sl = None
        self.highest_profit_seen = 0
        self.last_log_time = time.time()
        logger.info("ThetaDecayStrategy initialized.")
        logger.info(f"Capital Protection Limit: {-self.strat_var_capital_protection_limit}")
        logger.info(f"Overall Profit Target: {self.strat_var_overall_profit_target}")
        logger.info(f"Trailing Profit Target Activation: {self.strat_var_trailing_profit_target}")

    def on_ticks_update(self, ticks):
        """Main strategy execution method called on each tick update."""
        if self.is_trading_stopped:
            return

        current_time = datetime.now().strftime("%H:%M")
        if current_time < self.strat_var_entry_time:
            logger.info(f"Waiting for entry time {self.strat_var_entry_time}. Current time is {current_time}.")
            return

        if not self.active_strangle:
            self._handle_entry()
        else:
            self._update_pnl(ticks)
            self._handle_stop_loss()
            self._handle_trailing_sl()
            self._handle_profit_booking()
            self._handle_capital_protection()

            # Log status periodically
            current_time_now = time.time()
            if current_time_now - self.last_log_time >= 60: # Log every 60 seconds
                self._log_status()
                self.last_log_time = current_time_now

    def _find_option_by_premium(self, option_type, target_premium):
        """Finds the option with the premium closest to the target premium."""
        best_match = None
        min_diff = float('inf')

        options = self.instruments[self.instruments['instrument_type'] == option_type]

        for _, instrument in options.iterrows():
            try:
                symbol = f"{self.strat_var_exchange}:{instrument['tradingsymbol']}"
                quote = self.broker.get_quote(symbol)
                if not quote or symbol not in quote:
                    continue

                last_price = quote[symbol]['last_price']
                diff = abs(last_price - target_premium)

                if diff < min_diff:
                    min_diff = diff
                    best_match = instrument.to_dict()
                    best_match['last_price'] = last_price

            except Exception as e:
                logger.error(f"Error getting quote for {instrument['tradingsymbol']}: {e}")
                continue

        return best_match

    def _handle_entry(self):
        """Handles the initial entry of the strangle."""
        if self.active_strangle:
            return

        logger.info("Attempting to enter strangle...")

        # 1. Find best call and put options to sell
        call_option = self._find_option_by_premium("CE", self.strat_var_call_premium)
        put_option = self._find_option_by_premium("PE", self.strat_var_put_premium)

        if not call_option or not put_option:
            logger.error("Could not find suitable call or put option for the strangle. Retrying on next tick.")
            return

        # 2. Find best call and put options for hedge
        call_hedge = self._find_option_by_premium("CE", self.strat_var_hedge_premium)
        put_hedge = self._find_option_by_premium("PE", self.strat_var_hedge_premium)

        if not call_hedge or not put_hedge:
            logger.error("Could not find suitable call or put option for hedge. Retrying on next tick.")
            return

        # 3. Place orders
        logger.info(f"Selling strangle: {call_option['tradingsymbol']} and {put_option['tradingsymbol']}")
        logger.info(f"Buying hedge: {call_hedge['tradingsymbol']} and {put_hedge['tradingsymbol']}")

        # Sell strangle
        sell_call_order_id = self._place_order(call_option['tradingsymbol'], self.strat_var_quantity, "SELL")
        sell_put_order_id = self._place_order(put_option['tradingsymbol'], self.strat_var_quantity, "SELL")

        # Buy hedge
        buy_call_hedge_order_id = self._place_order(call_hedge['tradingsymbol'], self.strat_var_quantity, "BUY")
        buy_put_hedge_order_id = self._place_order(put_hedge['tradingsymbol'], self.strat_var_quantity, "BUY")

        if not all([sell_call_order_id, sell_put_order_id, buy_call_hedge_order_id, buy_put_hedge_order_id]):
            logger.error("Failed to place all orders for the strangle. Will not proceed.")
            # A more robust implementation would cancel any successful orders.
            return

        # 4. Update state
        total_premium = call_option['last_price'] + put_option['last_price']
        sl_percentage_premium = total_premium * (1 + self.strat_var_stop_loss_percentage)
        sl_double_premium = 2 * total_premium
        self.stop_loss_level = min(sl_percentage_premium, sl_double_premium)

        self.active_strangle = {
            "call_option": call_option,
            "put_option": put_option,
            "call_hedge": call_hedge,
            "put_hedge": put_hedge,
            "entry_premium": total_premium,
            "stop_loss_level": self.stop_loss_level,
            "status": "active"
        }

        self.positions = {
            call_option['tradingsymbol']: {'entry_price': call_option['last_price'], 'type': 'CE', 'leg': 'sell'},
            put_option['tradingsymbol']: {'entry_price': put_option['last_price'], 'type': 'PE', 'leg': 'sell'},
            call_hedge['tradingsymbol']: {'entry_price': call_hedge['last_price'], 'type': 'CE', 'leg': 'hedge'},
            put_hedge['tradingsymbol']: {'entry_price': put_hedge['last_price'], 'type': 'PE', 'leg': 'hedge'},
        }

        logger.info(f"Strangle entered successfully. Total premium: {total_premium}, SL level: {self.stop_loss_level}")

    def _update_pnl(self, ticks):
        """Updates the PnL of the current positions."""
        if not self.positions:
            return

        symbols_to_quote = [s for s, p in self.positions.items() if p.get('status') != 'exited']
        if not symbols_to_quote:
            self.total_pnl = sum(p.get('pnl', 0) for p in self.positions.values())
            logger.info(f"All positions exited. Final PnL: {self.total_pnl:.2f}")
            return

        exchange_symbols = [f"{self.strat_var_exchange}:{s}" for s in symbols_to_quote]

        try:
            quotes = self.broker.get_quote(exchange_symbols)
            if not quotes:
                logger.warning("Could not get quotes for positions.")
                return
        except Exception as e:
            logger.error(f"Error getting quotes for positions: {e}")
            return

        current_pnl = 0
        for symbol, pos in self.positions.items():
            if pos.get('status') == 'exited':
                current_pnl += pos.get('pnl', 0)
                continue

            exchange_symbol = f"{self.strat_var_exchange}:{symbol}"
            if exchange_symbol in quotes:
                pos['current_price'] = quotes[exchange_symbol]['last_price']
                price_diff = pos['current_price'] - pos['entry_price']

                if pos['leg'] == 'sell':
                    pnl = -price_diff * self.strat_var_quantity
                else: # hedge
                    pnl = price_diff * self.strat_var_quantity
                pos['pnl'] = pnl

            current_pnl += pos.get('pnl', 0)

        self.total_pnl = current_pnl
        logger.info(f"Total PnL updated: {self.total_pnl:.2f}")

    def _handle_stop_loss(self):
        """Handles the stop-loss logic for the active strangle."""
        if not self.active_strangle or self.active_strangle.get('sl_triggered'):
            return

        call_symbol = self.active_strangle['call_option']['tradingsymbol']
        put_symbol = self.active_strangle['put_option']['tradingsymbol']

        if self.positions.get(call_symbol, {}).get('status') == 'exited' or \
           self.positions.get(put_symbol, {}).get('status') == 'exited':
            return

        current_call_price = self.positions[call_symbol].get('current_price')
        current_put_price = self.positions[put_symbol].get('current_price')

        if current_call_price is None or current_put_price is None:
            return

        current_total_premium = current_call_price + current_put_price

        if current_total_premium >= self.stop_loss_level:
            logger.warning(f"Stop-loss breached! Current premium: {current_total_premium}, SL level: {self.stop_loss_level}")

            # Identify loss-making and profitable legs
            call_pnl = self.positions[call_symbol].get('pnl', 0)
            put_pnl = self.positions[put_symbol].get('pnl', 0)

            if call_pnl < put_pnl:
                loss_making_leg_symbol = call_symbol
                profitable_leg_symbol = put_symbol
            else:
                loss_making_leg_symbol = put_symbol
                profitable_leg_symbol = call_symbol

            # Exit the loss-making leg
            logger.info(f"Exiting loss-making leg: {loss_making_leg_symbol}")
            self._place_order(loss_making_leg_symbol, self.strat_var_quantity, "BUY")
            self.positions[loss_making_leg_symbol]['status'] = 'exited'

            # Flag the profitable leg for trailing
            if profitable_leg_symbol in self.positions:
                self.positions[profitable_leg_symbol]['trail_sl'] = True
                logger.info(f"Flagging profitable leg {profitable_leg_symbol} for trailing stop-loss.")

            # Check if we can re-enter with a new strangle
            if self.reentries_count < self.strat_var_max_reentries:
                self.reentries_count += 1
                logger.info(f"Re-entry #{self.reentries_count} will be attempted. A new strangle will be opened.")
            else:
                logger.info("Max re-entries reached. No more new strangles will be opened.")

            # Mark the current strangle as broken. The main loop will attempt a new entry.
            self.active_strangle = None


    def _handle_trailing_sl(self):
        """Handles the trailing stop-loss for profitable legs."""
        for symbol, pos in self.positions.items():
            if pos.get('trail_sl') and pos.get('status') == 'active':

                entry_price = pos['entry_price']
                current_price = pos.get('current_price')

                if current_price is None:
                    continue

                decay = entry_price - current_price
                if decay <= 0:
                    continue

                if self.reentries_count <= 1:
                     decay_factor = self.strat_var_trailing_sl_decay_factor_1
                else:
                     decay_factor = self.strat_var_trailing_sl_decay_factor_2

                tsl_trigger_price = current_price + (decay / decay_factor)
                tsl_trigger_price = round(tsl_trigger_price * 20) / 20

                if 'tsl_order_id' not in pos:
                    logger.info(f"Placing TSL for {symbol} at {tsl_trigger_price}")
                    tsl_order_id = self.broker.place_gtt_order(
                        symbol=symbol,
                        quantity=self.strat_var_quantity,
                        trigger_price=tsl_trigger_price,
                        transaction_type="BUY",
                        order_type="LIMIT",
                        exchange=self.strat_var_exchange,
                        product=self.strat_var_product_type
                    )
                    if tsl_order_id:
                        pos['tsl_order_id'] = tsl_order_id
                        pos['tsl_price'] = tsl_trigger_price
                else:
                    if tsl_trigger_price < pos.get('tsl_price', float('inf')):
                        logger.info(f"Modifying TSL for {symbol} from {pos.get('tsl_price')} to {tsl_trigger_price}")
                        new_tsl_order_id = self.broker.modify_gtt_order(
                            trigger_id=pos['tsl_order_id'],
                            symbol=symbol,
                            quantity=self.strat_var_quantity,
                            trigger_price=tsl_trigger_price,
                            transaction_type="BUY",
                            order_type="LIMIT",
                            exchange=self.strat_var_exchange,
                            product=self.strat_var_product_type
                        )
                        if new_tsl_order_id:
                            pos['tsl_order_id'] = new_tsl_order_id
                            pos['tsl_price'] = tsl_trigger_price

    def _exit_all_positions(self, reason=""):
        """Exits all active positions and stops trading."""
        logger.info(f"Exiting all positions. Reason: {reason}")

        for symbol, pos in self.positions.items():
            if pos.get('status') == 'active':
                transaction_type = "BUY" if pos['leg'] == 'sell' else "SELL"
                self._place_order(symbol, self.strat_var_quantity, transaction_type)
                pos['status'] = 'exited'

            if 'tsl_order_id' in pos:
                self.broker.cancel_gtt_order(pos['tsl_order_id'])
                del pos['tsl_order_id']

        self.is_trading_stopped = True
        logger.info("Trading stopped for the day.")

    def _handle_profit_booking(self):
        """Handles the profit booking logic."""
        if self.is_trading_stopped:
            return

        # 1. Individual Leg Profit Booking
        for symbol, pos in list(self.positions.items()):
            if pos.get('leg') == 'sell' and pos.get('status') == 'active':
                entry_price = pos['entry_price']
                current_price = pos.get('current_price')
                if current_price is None:
                    continue

                if entry_price == 0: # Avoid division by zero
                    continue

                decay_percentage = (entry_price - current_price) / entry_price
                if decay_percentage > self.strat_var_profit_booking_percentage:
                    logger.info(f"Booking profit for leg {symbol}. Decay: {decay_percentage:.2%}")
                    self._place_order(symbol, self.strat_var_quantity, "BUY")
                    pos['status'] = 'exited'

                    if self.active_strangle:
                        call_symbol = self.active_strangle['call_option']['tradingsymbol']
                        put_symbol = self.active_strangle['put_option']['tradingsymbol']

                        if symbol == call_symbol or symbol == put_symbol:
                            other_leg_symbol = put_symbol if symbol == call_symbol else call_symbol
                            if other_leg_symbol in self.positions:
                                self.positions[other_leg_symbol]['trail_sl'] = True
                                logger.info(f"Flagging {other_leg_symbol} for TSL after profit booking on counterpart.")
                            self.active_strangle = None

        # 2. Overall Profit Target
        if self.total_pnl >= self.strat_var_overall_profit_target:
            self._exit_all_positions(reason=f"Overall profit target of {self.strat_var_overall_profit_target} reached.")
            return

        # 3. Trailing Overall Profit
        self.highest_profit_seen = max(self.highest_profit_seen, self.total_pnl)

        if self.trailing_overall_profit_sl is None and self.highest_profit_seen >= self.strat_var_trailing_profit_target:
            lock_in_profit = self.strat_var_trailing_profit_target * 0.5
            self.trailing_overall_profit_sl = lock_in_profit
            logger.info(f"Trailing overall profit activated. Highest profit: {self.highest_profit_seen}, SL set at: {self.trailing_overall_profit_sl}")

        if self.trailing_overall_profit_sl is not None:
            profit_above_target = self.highest_profit_seen - self.strat_var_trailing_profit_target
            if profit_above_target > 0:
                initial_lock_in = self.strat_var_trailing_profit_target * 0.5
                new_trailing_sl = initial_lock_in + profit_above_target

                if new_trailing_sl > self.trailing_overall_profit_sl:
                    self.trailing_overall_profit_sl = new_trailing_sl
                    logger.info(f"Trailing overall profit SL adjusted to: {self.trailing_overall_profit_sl}")

            if self.total_pnl <= self.trailing_overall_profit_sl:
                self._exit_all_positions(reason=f"Trailing overall profit SL hit. PnL: {self.total_pnl}, SL: {self.trailing_overall_profit_sl}")
                return

    def _handle_capital_protection(self):
        """Handles the capital protection logic."""
        if self.is_trading_stopped:
            return

        # Capital protection based on total loss
        if self.total_pnl <= -self.strat_var_capital_protection_limit:
            self._exit_all_positions(reason=f"Capital protection limit of {self.strat_var_capital_protection_limit} reached. Total PnL: {self.total_pnl}")

    def _log_status(self):
        """Logs the current status of the strategy."""
        if self.is_trading_stopped:
            logger.info(f"Strategy is stopped. Final PnL: {self.total_pnl:.2f}")
            return

        status_report = []
        status_report.append(f"Current PnL: {self.total_pnl:.2f}")
        status_report.append(f"Highest Profit Seen: {self.highest_profit_seen:.2f}")

        if self.trailing_overall_profit_sl is not None:
            status_report.append(f"Trailing Overall Profit SL: {self.trailing_overall_profit_sl:.2f}")

        if self.active_strangle:
            status_report.append(f"Active Strangle SL Level: {self.active_strangle['stop_loss_level']:.2f}")

        active_positions_report = []
        for symbol, pos in self.positions.items():
            if pos.get('status') == 'active':
                pos_pnl = pos.get('pnl', 0)
                pos_report = f"  - {symbol}: Entry={pos['entry_price']}, Current={pos.get('current_price', 'N/A')}, PnL={pos_pnl:.2f}"
                if pos.get('trail_sl'):
                    pos_report += f", TSL Price={pos.get('tsl_price', 'N/A')}"
                active_positions_report.append(pos_report)

        if active_positions_report:
            status_report.append("Active Positions:")
            status_report.extend(active_positions_report)

        logger.info("===== STRATEGY STATUS =====")
        for line in status_report:
            logger.info(line)
        logger.info("==========================")

    def _place_order(self, symbol, quantity, transaction_type):
        """Places an order."""
        order_id = self.broker.place_order(
            symbol,
            quantity,
            price=None,
            transaction_type=transaction_type,
            order_type=self.strat_var_order_type,
            variety="REGULAR",
            exchange=self.strat_var_exchange,
            product=self.strat_var_product_type,
            tag="ThetaDecay"
        )

        if order_id == -1:
            logger.error(f"Order placement failed for {symbol}")
            return None

        logger.info(f"Order placed for {symbol}: {order_id}")
        order_details = {
            "order_id": order_id,
            "symbol": symbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "price": None,
            "timestamp": datetime.now().isoformat(),
        }
        self.order_manager.add_order(order_details)
        return order_id


# =============================================================================
# MAIN SCRIPT EXECUTION
# =============================================================================

if __name__ == "__main__":
    import time
    import yaml
    import sys
    import argparse
    from dispatcher import DataDispatcher
    from orders import OrderTracker
    from brokers.zerodha import ZerodhaBroker
    from logger import logger
    from queue import Queue
    import traceback
    import warnings
    warnings.filterwarnings("ignore")

    import logging
    logger.setLevel(logging.INFO)

    # Load default configuration from YAML file
    config_file = os.path.join(os.path.dirname(__file__), "configs/theta_decay.yml")
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)['default']

    def create_argument_parser():
        """Create and configure argument parser."""
        parser = argparse.ArgumentParser(description="Theta Decay Strangle Strategy")

        # Add arguments from config file to allow command-line overrides
        for key, value in config.items():
            parser.add_argument(f'--{key}', type=type(value), default=value, help=f'Default: {value}')

        parser.add_argument('--show-config', action='store_true', help='Display current configuration and exit.')
        parser.add_argument('--config-file', type=str, default=config_file, help='Path to YAML configuration file.')

        return parser

    def show_config(config):
        """Display current configuration."""
        print("\n" + "="*80)
        print("THETA DECAY STRATEGY CONFIGURATION")
        print("="*80)
        for key, value in config.items():
            print(f"  {key:30}: {value}")
        print("="*80)

    # Parse command line arguments
    parser = create_argument_parser()
    args = parser.parse_args()

    # Apply command line overrides to configuration
    for key, value in config.items():
        if hasattr(args, key):
            config[key] = getattr(args, key)

    if args.show_config:
        show_config(config)
        sys.exit(0)

    # Setup broker, order tracker, and dispatcher
    if os.getenv("BROKER_TOTP_ENABLE") == "true":
        broker = ZerodhaBroker(without_totp=False)
    else:
        broker = ZerodhaBroker(without_totp=True)

    order_tracker = OrderTracker()

    try:
        quote_data = broker.get_quote(config['index_symbol'])
        instrument_token = quote_data[config['index_symbol']]['instrument_token']
        logger.info(f"Index instrument token obtained: {instrument_token}")
    except Exception as e:
        logger.error(f"Failed to get instrument token for {config['index_symbol']}: {e}")
        sys.exit(1)

    dispatcher = DataDispatcher()
    dispatcher.register_main_queue(Queue())

    # Websocket callbacks
    def on_ticks(ws, ticks):
        dispatcher.dispatch(ticks)

    def on_connect(ws, response):
        logger.info("Websocket connected successfully.")
        ws.subscribe([instrument_token])
        ws.set_mode(ws.MODE_FULL, [instrument_token])

    def on_order_update(ws, data):
        logger.info(f"Order update: {data}")

    broker.on_ticks = on_ticks
    broker.on_connect = on_connect
    broker.on_order_update = on_order_update

    broker.connect_websocket()

    # Initialize and run the strategy
    strategy = ThetaDecayStrategy(broker, config, order_tracker)

    try:
        while True:
            try:
                tick_data = dispatcher._main_queue.get()
                if tick_data:
                    strategy.on_ticks_update(tick_data[0])
            except KeyboardInterrupt:
                logger.info("Shutdown requested. Stopping strategy...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                traceback.print_exc()
                time.sleep(1)

    finally:
        logger.info("Strategy shutdown complete.")
