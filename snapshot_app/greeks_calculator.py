"""Black-Scholes Greeks Calculator for Options.

This module provides calculations for all option Greeks:
- Delta: Price sensitivity to underlying price change
- Gamma: Rate of change of delta
- Theta: Time decay
- Vega: Volatility sensitivity
- Rho: Interest rate sensitivity
"""

import math
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _norm_cdf(x: float) -> float:
    """Calculate standard normal CDF using error function.
    
    Uses approximation: Φ(x) = 0.5 * (1 + erf(x / sqrt(2)))
    """
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    """Calculate standard normal PDF.
    
    PDF(x) = (1/sqrt(2π)) * exp(-x²/2)
    """
    return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)


class GreeksCalculator:
    """Calculate Black-Scholes Greeks for options and Implied Volatility (IV)."""
    
    # Default risk-free rate for India (7%)
    DEFAULT_RISK_FREE_RATE = 0.07
    
    # Constants for IV calculation
    MAX_ITERATIONS = 100
    IV_TOLERANCE = 1e-6  # 0.0001% accuracy
    IV_MAX = 5.0  # 500% max volatility (safety limit)
    IV_MIN = 0.001  # 0.1% min volatility
    
    @staticmethod
    def calculate_greeks(
        spot_price: float,
        strike: float,
        time_to_expiry: float,  # in years
        volatility: float,  # implied volatility as decimal (e.g., 0.20 for 20%)
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        option_type: str = 'CE'  # 'CE' (Call) or 'PE' (Put)
    ) -> Dict[str, float]:
        """Calculate all Greeks for an option.
        
        Args:
            spot_price: Current underlying spot price
            strike: Option strike price
            time_to_expiry: Time to expiry in years (must be > 0)
            volatility: Implied volatility as decimal (e.g., 0.20 for 20%)
            risk_free_rate: Risk-free interest rate (default: 0.07 for 7% in India)
            option_type: 'CE' for Call option, 'PE' for Put option
        
        Returns:
            Dictionary with Greeks:
            - delta: Price sensitivity
            - gamma: Delta sensitivity (same for calls and puts)
            - theta: Time decay (per day)
            - vega: Volatility sensitivity (per 1% change)
            - rho: Interest rate sensitivity (per 1% change)
        
        Raises:
            ValueError: If time_to_expiry <= 0 or other invalid inputs
        """
        
        if time_to_expiry <= 0:
            logger.warning(f"Invalid time_to_expiry: {time_to_expiry}, returning zero Greeks")
            return {
                'delta': 0.0,
                'gamma': 0.0,
                'theta': 0.0,
                'vega': 0.0,
                'rho': 0.0
            }
        
        if spot_price <= 0 or strike <= 0:
            raise ValueError(f"Invalid prices: spot={spot_price}, strike={strike}")
        
        if volatility < 0:
            raise ValueError(f"Volatility cannot be negative: {volatility}")
        
        # Calculate d1 and d2 for Black-Scholes
        sqrt_t = math.sqrt(time_to_expiry)
        
        if sqrt_t == 0 or volatility == 0:
            # Edge case: no time or no volatility
            if spot_price > strike:
                delta = 1.0 if option_type == 'CE' else -1.0
            elif spot_price < strike:
                delta = 0.0
            else:
                delta = 0.5 if option_type == 'CE' else -0.5
            
            return {
                'delta': delta,
                'gamma': 0.0,
                'theta': 0.0,
                'vega': 0.0,
                'rho': 0.0
            }
        
        # Calculate d1 and d2
        d1 = (math.log(spot_price / strike) + 
              (risk_free_rate + 0.5 * volatility**2) * time_to_expiry) / \
             (volatility * sqrt_t)
        
        d2 = d1 - volatility * sqrt_t
        
        # Calculate standard normal CDF and PDF
        nd1 = _norm_cdf(d1)
        nd2 = _norm_cdf(d2)
        n_minus_d1 = _norm_cdf(-d1)
        n_minus_d2 = _norm_cdf(-d2)
        pdf_d1 = _norm_pdf(d1)
        
        # Calculate Greeks
        if option_type == 'CE':
            # Call option Greeks
            delta = nd1
            
            theta = (-(spot_price * pdf_d1 * volatility) / (2 * sqrt_t) -
                    risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry) * nd2) / 365.0
            
            rho = (strike * time_to_expiry * 
                  math.exp(-risk_free_rate * time_to_expiry) * nd2) / 100.0
        else:
            # Put option Greeks
            delta = -n_minus_d1  # or nd1 - 1
            
            theta = (-(spot_price * pdf_d1 * volatility) / (2 * sqrt_t) +
                    risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry) * n_minus_d2) / 365.0
            
            rho = (-strike * time_to_expiry * 
                  math.exp(-risk_free_rate * time_to_expiry) * n_minus_d2) / 100.0
        
        # Gamma is same for calls and puts
        gamma = pdf_d1 / (spot_price * volatility * sqrt_t)
        
        # Vega is same for calls and puts (per 1% change in volatility)
        vega = (spot_price * pdf_d1 * sqrt_t) / 100.0
        
        return {
            'delta': float(delta),
            'gamma': float(gamma),
            'theta': float(theta),
            'vega': float(vega),
            'rho': float(rho)
        }
    
    @staticmethod
    def calculate_delta(
        spot_price: float,
        strike: float,
        time_to_expiry: float,
        volatility: float,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        option_type: str = 'CE'
    ) -> float:
        """Calculate delta only (faster for single Greek)."""
        greeks = GreeksCalculator.calculate_greeks(
            spot_price, strike, time_to_expiry, volatility, risk_free_rate, option_type
        )
        return greeks['delta']
    
    @staticmethod
    def validate_inputs(
        spot_price: float,
        strike: float,
        time_to_expiry: float,
        volatility: float
    ) -> tuple[bool, Optional[str]]:
        """Validate inputs for Greeks calculation.
        
        Returns:
            Tuple of (is_valid: bool, error_message: Optional[str])
        """
        if spot_price <= 0:
            return False, f"Spot price must be positive: {spot_price}"
        
        if strike <= 0:
            return False, f"Strike price must be positive: {strike}"
        
        if time_to_expiry < 0:
            return False, f"Time to expiry cannot be negative: {time_to_expiry}"
        
        if volatility < 0:
            return False, f"Volatility cannot be negative: {volatility}"
        
        return True, None
    
    # Constants for IV calculation
    MAX_ITERATIONS = 100
    IV_TOLERANCE = 1e-6  # 0.0001% accuracy
    IV_MAX = 5.0  # 500% max volatility (safety limit)
    IV_MIN = 0.001  # 0.1% min volatility
    
    @staticmethod
    def calculate_option_price(
        spot_price: float,
        strike: float,
        time_to_expiry: float,
        volatility: float,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        option_type: str = 'CE'
    ) -> float:
        """Calculate Black-Scholes option price.
        
        Args:
            spot_price: Current underlying spot price
            strike: Option strike price
            time_to_expiry: Time to expiry in years
            volatility: Volatility as decimal (e.g., 0.20 for 20%)
            risk_free_rate: Risk-free interest rate (default: 0.07)
            option_type: 'CE' for Call, 'PE' for Put
        
        Returns:
            Option price
        """
        if time_to_expiry <= 0:
            # At expiry: intrinsic value
            if option_type == 'CE':
                return max(0, spot_price - strike)
            else:
                return max(0, strike - spot_price)
        
        if volatility <= 0:
            # No volatility: intrinsic value discounted
            if option_type == 'CE':
                return max(0, spot_price * math.exp(-risk_free_rate * time_to_expiry) - strike * math.exp(-risk_free_rate * time_to_expiry))
            else:
                return max(0, strike * math.exp(-risk_free_rate * time_to_expiry) - spot_price * math.exp(-risk_free_rate * time_to_expiry))
        
        sqrt_t = math.sqrt(time_to_expiry)
        d1 = (math.log(spot_price / strike) + (risk_free_rate + 0.5 * volatility**2) * time_to_expiry) / (volatility * sqrt_t)
        d2 = d1 - volatility * sqrt_t
        
        if option_type == 'CE':
            price = spot_price * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * time_to_expiry) * _norm_cdf(d2)
        else:
            price = strike * math.exp(-risk_free_rate * time_to_expiry) * _norm_cdf(-d2) - spot_price * _norm_cdf(-d1)
        
        return max(0, price)
    
    @staticmethod
    def calculate_implied_volatility(
        market_price: float,
        spot_price: float,
        strike: float,
        time_to_expiry: float,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        option_type: str = 'CE',
        initial_guess: float = 0.20  # 20% initial guess
    ) -> Optional[float]:
        """Calculate Implied Volatility (IV) using Newton-Raphson method with bisection fallback.
        
        This inverts the Black-Scholes formula to find the volatility that matches the market price.
        
        Args:
            market_price: Current market price of the option
            spot_price: Current underlying spot price
            strike: Option strike price
            time_to_expiry: Time to expiry in years
            risk_free_rate: Risk-free interest rate (default: 0.07)
            option_type: 'CE' for Call, 'PE' for Put
            initial_guess: Initial volatility guess (default: 0.20 = 20%)
        
        Returns:
            Implied volatility as decimal (e.g., 0.20 for 20%), or None if calculation fails
        """
        # Validate inputs
        if market_price <= 0:
            logger.debug(f"Market price must be positive: {market_price}")
            return None
        
        if spot_price <= 0 or strike <= 0:
            logger.debug(f"Invalid prices: spot={spot_price}, strike={strike}")
            return None
        
        if time_to_expiry <= 0:
            logger.debug(f"Time to expiry must be positive: {time_to_expiry}")
            return None
        
        # Calculate intrinsic value
        if option_type == 'CE':
            intrinsic = max(0, spot_price - strike)
        else:
            intrinsic = max(0, strike - spot_price)
        
        if market_price < intrinsic:
            logger.debug(f"Market price {market_price} below intrinsic value {intrinsic}")
            return None
        
        # Check if option is extremely OTM (price very close to 0)
        if market_price < 0.01:
            logger.debug(f"Market price too low for reliable IV calculation: {market_price}")
            return None
        
        # Try Newton-Raphson first (faster, more accurate)
        iv = GreeksCalculator._newton_raphson_iv(
            market_price, spot_price, strike, time_to_expiry, risk_free_rate, option_type, initial_guess
        )
        
        if iv is not None:
            return iv
        
        # Fallback to bisection (more stable, slower)
        logger.debug("Newton-Raphson failed, trying bisection method")
        iv = GreeksCalculator._bisection_iv(
            market_price, spot_price, strike, time_to_expiry, risk_free_rate, option_type
        )
        
        return iv
    
    @staticmethod
    def _newton_raphson_iv(
        market_price: float,
        spot_price: float,
        strike: float,
        time_to_expiry: float,
        risk_free_rate: float,
        option_type: str,
        initial_guess: float
    ) -> Optional[float]:
        """Calculate IV using Newton-Raphson method.
        
        Uses vega (volatility sensitivity) as the derivative for faster convergence.
        """
        vol = initial_guess
        
        for iteration in range(GreeksCalculator.MAX_ITERATIONS):
            # Calculate current price and vega
            price = GreeksCalculator.calculate_option_price(
                spot_price, strike, time_to_expiry, vol, risk_free_rate, option_type
            )
            
            # Calculate vega (volatility sensitivity) - derivative w.r.t. volatility
            sqrt_t = math.sqrt(time_to_expiry)
            d1 = (math.log(spot_price / strike) + (risk_free_rate + 0.5 * vol**2) * time_to_expiry) / (vol * sqrt_t)
            vega = spot_price * _norm_pdf(d1) * sqrt_t  # Vega in price units per 1.0 volatility
            
            # Check convergence
            price_error = price - market_price
            if abs(price_error) < GreeksCalculator.IV_TOLERANCE * market_price:
                if GreeksCalculator.IV_MIN <= vol <= GreeksCalculator.IV_MAX:
                    return vol
            
            # Check if vega is too small (numerical issues)
            if abs(vega) < 1e-10:
                break
            
            # Newton-Raphson update: vol_new = vol - (price - market_price) / vega
            vol_new = vol - price_error / vega
            
            # Keep within bounds
            vol_new = max(GreeksCalculator.IV_MIN, min(GreeksCalculator.IV_MAX, vol_new))
            
            # Check if stuck (no progress)
            if abs(vol_new - vol) < 1e-10:
                break
            
            vol = vol_new
        
        # If converged but outside bounds, return None to try bisection
        if GreeksCalculator.IV_MIN <= vol <= GreeksCalculator.IV_MAX:
            final_price = GreeksCalculator.calculate_option_price(
                spot_price, strike, time_to_expiry, vol, risk_free_rate, option_type
            )
            if abs(final_price - market_price) < GreeksCalculator.IV_TOLERANCE * market_price:
                return vol
        
        return None
    
    @staticmethod
    def _bisection_iv(
        market_price: float,
        spot_price: float,
        strike: float,
        time_to_expiry: float,
        risk_free_rate: float,
        option_type: str
    ) -> Optional[float]:
        """Calculate IV using bisection method (more stable fallback).
        
        Finds root by bisecting the interval where IV must lie.
        """
        # Find bounds where IV must lie
        vol_low = GreeksCalculator.IV_MIN
        vol_high = GreeksCalculator.IV_MAX
        
        # Check if solution exists in range
        price_low = GreeksCalculator.calculate_option_price(
            spot_price, strike, time_to_expiry, vol_low, risk_free_rate, option_type
        )
        price_high = GreeksCalculator.calculate_option_price(
            spot_price, strike, time_to_expiry, vol_high, risk_free_rate, option_type
        )
        
        # Market price must be between price_low and price_high
        if not (price_low <= market_price <= price_high):
            logger.debug(f"Market price {market_price} outside range [{price_low}, {price_high}]")
            return None
        
        # Bisect until convergence
        for iteration in range(GreeksCalculator.MAX_ITERATIONS):
            vol_mid = (vol_low + vol_high) / 2
            price_mid = GreeksCalculator.calculate_option_price(
                spot_price, strike, time_to_expiry, vol_mid, risk_free_rate, option_type
            )
            
            # Check convergence
            if abs(price_mid - market_price) < GreeksCalculator.IV_TOLERANCE * market_price:
                return vol_mid
            
            # Update bounds
            if price_mid < market_price:
                vol_low = vol_mid
            else:
                vol_high = vol_mid
            
            # Check if interval is too small
            if (vol_high - vol_low) < 1e-10:
                break
        
        # Return midpoint if converged
        vol_final = (vol_low + vol_high) / 2
        final_price = GreeksCalculator.calculate_option_price(
            spot_price, strike, time_to_expiry, vol_final, risk_free_rate, option_type
        )
        
        if abs(final_price - market_price) < GreeksCalculator.IV_TOLERANCE * market_price:
            return vol_final
        
        return None