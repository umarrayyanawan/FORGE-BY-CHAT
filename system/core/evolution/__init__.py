"""FORGE Evolution Engine — safe iterative software updates with regression protection."""
from system.core.evolution.engine import EvolutionEngine
from system.core.evolution.diff_analyzer import DiffAnalyzer
from system.core.evolution.patch_planner import PatchPlanner
from system.core.evolution.regression_guard import RegressionGuard

__all__ = ["EvolutionEngine", "DiffAnalyzer", "PatchPlanner", "RegressionGuard"]
