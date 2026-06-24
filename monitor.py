"""
Bot Monitor - logs health checks every 2 hours.
Run this in a separate terminal alongside upgainpulse.py
"""
import time
import os
import subprocess
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("monitor.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL = 2 * 60 * 60  # 2 hours

def check_bot():
    """Check if upgainpulse.py process is running."""
    try:
        result = subprocess.run(
            ['tasklist', '/fi', 'imagename eq python.exe', '/nh'],
            capture_output=True, text=True, timeout=10
        )
        if 'python.exe' in result.stdout:
            return True, "upgainpulse.py process is running"
        else:
            return False, "No python.exe process found"
    except Exception as e:
        return False, f"Check failed: {e}"

def check_log_recent_activity():
    """Check if the bot log has recent entries (last 15 min)."""
    try:
        if not os.path.exists("upgainpulse.log"):
            return "No upgainpulse.log found"
        
        mtime = os.path.getmtime("upgainpulse.log")
        age_min = (time.time() - mtime) / 60
        
        if age_min < 15:
            return f"Log updated {age_min:.0f} min ago - ACTIVE"
        else:
            return f"Log last updated {age_min:.0f} min ago - STALE"
    except Exception as e:
        return f"Log check error: {e}"

def main():
    logger.info("=" * 50)
    logger.info("Bot Monitor started")
    logger.info(f"Check interval: {CHECK_INTERVAL // 3600} hours")
    logger.info("=" * 50)
    
    while True:
        running, status = check_bot()
        log_status = check_log_recent_activity()
        
        if running:
            logger.info(f"[OK] BOT OK - {status} | {log_status}")
        else:
            logger.error(f"[FAIL] BOT ISSUE - {status} | {log_status}")
        
        next_check = datetime.fromtimestamp(time.time() + CHECK_INTERVAL).strftime('%H:%M:%S')
        logger.info(f"Next check at: {next_check}")
        
        # Save a simple status file for quick review
        with open("monitor_status.txt", "w") as f:
            f.write(f"Last check: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Bot running: {running}\n")
            f.write(f"Status: {status}\n")
            f.write(f"Log: {log_status}\n")
            f.write(f"Next check at: {next_check}\n")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")