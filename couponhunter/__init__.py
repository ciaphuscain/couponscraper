"""CouponHunter: a personal savings intelligence engine for Amazon India.

All money is held as integer paise (1 INR = 100 paise). All percentages are
held as integer basis points (1% = 100 bp). Floats never touch money.
"""

__version__ = "0.1.0"

# Bumped whenever the extension <-> backend JSON contract changes shape.
API_VERSION = 1
