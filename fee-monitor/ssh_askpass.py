#!/usr/bin/env python3
"""OpenSSH password prompt helper; the password is inherited in memory only."""
import os
import sys

sys.stdout.write(os.environ.get("FEE_MONITOR_SSH_PASSWORD", "") + "\n")
