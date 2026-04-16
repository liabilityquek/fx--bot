"""Trade conflict checker to prevent hedging and opposing positions.

This module checks for conflicting positions (hedging) and provides logic
for position consolidation when multiple trades exist on the same pair.
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from config.settings import settings


class ConflictType(Enum):
    """Types of trade conflicts."""
    NONE = "none"
    HEDGING = "hedging"  # Long and short on same pair
    DUPLICATE = "duplicate"  # Same direction on same pair
    OVER_LEVERAGE = "over_leverage"  # Too much exposure on one pair


@dataclass
class ConflictCheckResult:
    """Result of conflict check."""
    has_conflict: bool
    conflict_type: Optional[ConflictType]
    conflicting_positions: List[Dict]
    can_consolidate: bool
    consolidation_plan: Optional[Dict]
    recommendation: str
    allow_trade: bool


class TradeConflictChecker:
    """Check for and manage trade conflicts."""
    
    def __init__(
        self,
        allow_hedging: bool = False,
        allow_duplicate_positions: bool = True,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize conflict checker.
        
        Args:
            allow_hedging: Whether to allow hedging (long + short on same pair)
            allow_duplicate_positions: Allow multiple positions same direction
            logger: Logger instance (optional)
        """
        self.logger = logger or logging.getLogger('conflict_checker')
        self.allow_hedging = allow_hedging
        self.allow_duplicate_positions = allow_duplicate_positions
    
    def check_conflicts(
        self,
        pair: str,
        units: int,
        open_positions: List[Dict]
    ) -> ConflictCheckResult:
        """
        Check for conflicts with existing positions.
        
        Args:
            pair: Trading pair (e.g., 'EUR_USD')
            units: Proposed position size (positive = long, negative = short)
            open_positions: List of current open positions from broker
        
        Returns:
            ConflictCheckResult with detailed conflict analysis
        """
        is_long = units > 0
        conflicting_positions = []
        conflict_type = ConflictType.NONE
        can_consolidate = False
        consolidation_plan = None
        recommendation = "Trade is allowed"
        allow_trade = True
        
        # Find positions on the same pair
        same_pair_positions = [
            pos for pos in open_positions
            if pos.get('instrument', '').replace('-', '_') == pair
        ]
        
        if not same_pair_positions:
            # No conflicts - no positions on this pair
            return ConflictCheckResult(
                has_conflict=False,
                conflict_type=ConflictType.NONE,
                conflicting_positions=[],
                can_consolidate=False,
                consolidation_plan=None,
                recommendation="No existing positions on this pair",
                allow_trade=True
            )
        
        # Analyze existing positions
        existing_long = []
        existing_short = []
        
        for pos in same_pair_positions:
            long_units = float(pos.get('long', {}).get('units', 0))
            short_units = float(pos.get('short', {}).get('units', 0))
            
            if long_units > 0:
                existing_long.append(pos)
            if short_units < 0:
                existing_short.append(pos)
        
        # Check for hedging
        if (is_long and existing_short) or (not is_long and existing_long):
            conflict_type = ConflictType.HEDGING
            conflicting_positions = existing_short if is_long else existing_long
            
            if not self.allow_hedging:
                allow_trade = False
                recommendation = (
                    f"REJECTED: Hedging not allowed. "
                    f"Trying to open {'LONG' if is_long else 'SHORT'} but "
                    f"{'SHORT' if is_long else 'LONG'} position exists on {pair}"
                )
                
                self.logger.warning(recommendation)
            else:
                recommendation = (
                    f"WARNING: Opening hedge position on {pair}. "
                    f"Both long and short will be active."
                )
                self.logger.warning(recommendation)
        
        # Check for duplicate positions (same direction)
        elif (is_long and existing_long) or (not is_long and existing_short):
            conflict_type = ConflictType.DUPLICATE
            conflicting_positions = existing_long if is_long else existing_short
            
            if not self.allow_duplicate_positions:
                # Suggest consolidation instead
                can_consolidate = True
                
                total_existing_units = sum(
                    abs(float(pos.get('long' if is_long else 'short', {}).get('units', 0)))
                    for pos in conflicting_positions
                )
                
                consolidation_plan = {
                    'action': 'close_and_reopen',
                    'positions_to_close': [
                        pos.get('id') for pos in conflicting_positions
                    ],
                    'new_position_units': total_existing_units + abs(units),
                    'direction': 'long' if is_long else 'short'
                }
                
                allow_trade = False
                recommendation = (
                    f"REJECTED: Duplicate position not allowed. "
                    f"Consider consolidating {len(conflicting_positions)} existing "
                    f"{'LONG' if is_long else 'SHORT'} positions on {pair}. "
                    f"Total units: {total_existing_units:,.0f}"
                )
                
                self.logger.warning(recommendation)
            else:
                recommendation = (
                    f"INFO: Adding to existing {'LONG' if is_long else 'SHORT'} "
                    f"position on {pair} ({len(conflicting_positions)} existing)"
                )
                self.logger.info(recommendation)
        
        # Check total exposure on this pair
        total_units_after = abs(units)
        for pos in same_pair_positions:
            long_units = abs(float(pos.get('long', {}).get('units', 0)))
            short_units = abs(float(pos.get('short', {}).get('units', 0)))
            total_units_after += long_units + short_units
        
        # Rough check: if total units > 500k, might be over-leveraged
        if total_units_after > 500000:
            if conflict_type == ConflictType.NONE:
                conflict_type = ConflictType.OVER_LEVERAGE
            
            recommendation += (
                f" WARNING: High exposure on {pair}: {total_units_after:,.0f} units total"
            )
            self.logger.warning(f"High exposure on {pair}: {total_units_after:,.0f} units")
        
        has_conflict = conflict_type != ConflictType.NONE
        
        return ConflictCheckResult(
            has_conflict=has_conflict,
            conflict_type=conflict_type,
            conflicting_positions=conflicting_positions,
            can_consolidate=can_consolidate,
            consolidation_plan=consolidation_plan,
            recommendation=recommendation,
            allow_trade=allow_trade
        )
    
    def suggest_consolidation(
        self,
        pair: str,
        open_positions: List[Dict]
    ) -> Optional[Dict]:
        """
        Suggest how to consolidate multiple positions on the same pair.
        
        Args:
            pair: Trading pair
            open_positions: List of open positions
        
        Returns:
            Consolidation plan dict or None if not applicable
        """
        same_pair_positions = [
            pos for pos in open_positions
            if pos.get('instrument', '').replace('-', '_') == pair
        ]
        
        if len(same_pair_positions) <= 1:
            return None  # Nothing to consolidate
        
        # Separate by direction
        long_positions = []
        short_positions = []
        
        for pos in same_pair_positions:
            long_units = float(pos.get('long', {}).get('units', 0))
            short_units = float(pos.get('short', {}).get('units', 0))
            
            if long_units > 0:
                long_positions.append({
                    'id': pos.get('id'),
                    'units': long_units,
                    'avg_price': float(pos.get('long', {}).get('averagePrice', 0))
                })
            
            if short_units < 0:
                short_positions.append({
                    'id': pos.get('id'),
                    'units': abs(short_units),
                    'avg_price': float(pos.get('short', {}).get('averagePrice', 0))
                })
        
        plans = []
        
        # Plan for long positions
        if len(long_positions) > 1:
            total_units = sum(p['units'] for p in long_positions)
            weighted_avg_price = sum(
                p['units'] * p['avg_price'] for p in long_positions
            ) / total_units
            
            plans.append({
                'direction': 'long',
                'positions_to_close': [p['id'] for p in long_positions],
                'new_position_units': total_units,
                'estimated_avg_price': weighted_avg_price,
                'count': len(long_positions)
            })
        
        # Plan for short positions
        if len(short_positions) > 1:
            total_units = sum(p['units'] for p in short_positions)
            weighted_avg_price = sum(
                p['units'] * p['avg_price'] for p in short_positions
            ) / total_units
            
            plans.append({
                'direction': 'short',
                'positions_to_close': [p['id'] for p in short_positions],
                'new_position_units': -total_units,
                'estimated_avg_price': weighted_avg_price,
                'count': len(short_positions)
            })
        
        if not plans:
            return None
        
        return {
            'pair': pair,
            'consolidation_plans': plans,
            'total_positions': len(same_pair_positions)
        }
    
    def get_conflict_summary(self, result: ConflictCheckResult) -> str:
        """
        Get human-readable conflict check summary.
        
        Args:
            result: Conflict check result
        
        Returns:
            Formatted summary string
        """
        lines = [
            f"\n🔍 Trade Conflict Check",
            f"  Conflict: {'⚠️  YES' if result.has_conflict else '✅ NONE'}",
        ]
        
        if result.conflict_type and result.conflict_type != ConflictType.NONE:
            lines.append(f"  Type: {result.conflict_type.value.upper()}")
        
        lines.append(f"  Allow Trade: {'✅ YES' if result.allow_trade else '❌ NO'}")
        
        if result.conflicting_positions:
            lines.append(
                f"  Conflicting Positions: {len(result.conflicting_positions)}"
            )
        
        if result.can_consolidate and result.consolidation_plan:
            lines.append(f"\n  💡 Consolidation Available:")
            plan = result.consolidation_plan
            lines.append(f"    Action: {plan.get('action', 'N/A')}")
            lines.append(
                f"    Positions to close: {len(plan.get('positions_to_close', []))}"
            )
            lines.append(
                f"    New position: {plan.get('new_position_units', 0):,.0f} units "
                f"({plan.get('direction', 'N/A')})"
            )
        
        lines.append(f"\n  Recommendation:")
        lines.append(f"    {result.recommendation}")
        
        return "\n".join(lines)
    
    def execute_consolidation(
        self,
        broker_client,
        consolidation_plan: Dict
    ) -> bool:
        """
        Execute a consolidation plan (close multiple positions, open one).
        
        Args:
            broker_client: Broker client with close/open methods
            consolidation_plan: Consolidation plan from suggest_consolidation
        
        Returns:
            True if successful, False otherwise
        """
        if not consolidation_plan:
            self.logger.error("No consolidation plan provided")
            return False
        
        try:
            pair = consolidation_plan.get('pair')
            plans = consolidation_plan.get('consolidation_plans', [])
            
            for plan in plans:
                positions_to_close = plan.get('positions_to_close', [])
                
                # Close all positions
                for pos_id in positions_to_close:
                    try:
                        broker_client.close_position(pos_id)
                        self.logger.info(f"Closed position {pos_id} for consolidation")
                    except Exception as e:
                        self.logger.error(f"Failed to close position {pos_id}: {e}")
                        return False
                
                # Open new consolidated position
                new_units = plan.get('new_position_units')
                
                try:
                    broker_client.place_market_order(
                        pair=pair,
                        units=int(new_units)
                    )
                    self.logger.info(
                        f"Opened consolidated position: {new_units:,.0f} units on {pair}"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to open consolidated position: {e}")
                    return False
            
            self.logger.info(f"✅ Consolidation complete for {pair}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error executing consolidation: {e}")
            return False
