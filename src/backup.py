import os
import time
import threading
import pandas as pd
from datetime import datetime
from src.database import _engine
from src import config

def run_backup():
    backup_dir = config.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = backup_dir / f"database_backup_{timestamp}.xlsx"
    
    try:
        invoices_df = pd.read_sql("SELECT * FROM invoices", con=_engine)
        line_items_df = pd.read_sql("SELECT * FROM line_items", con=_engine)
        logs_df = pd.read_sql("SELECT * FROM processing_log", con=_engine)
    except Exception as e:
        print(f"Error reading from database for backup: {e}")
        return None
    
    # Excel does not support datetime with timezone natively, strip timezone
    for df in [invoices_df, line_items_df, logs_df]:
        for col in df.select_dtypes(include=['datetimetz']).columns:
            try:
                df[col] = df[col].dt.tz_localize(None)
            except Exception:
                pass
            
    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        invoices_df.to_excel(writer, sheet_name='Invoices', index=False)
        line_items_df.to_excel(writer, sheet_name='Line Items', index=False)
        logs_df.to_excel(writer, sheet_name='Logs', index=False)
        
    print(f"Backup successfully created at: {filepath}")
    return filepath

def _backup_worker():
    backup_dir = config.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    last_backup_file = backup_dir / "last_backup.txt"
    
    while True:
        should_run = True
        if last_backup_file.exists():
            try:
                with open(last_backup_file, "r") as f:
                    last_time = float(f.read().strip() or "0")
                # 28 days interval (to safely beat the 29-day limit)
                if time.time() - last_time < 28 * 24 * 60 * 60:
                    should_run = False
            except Exception:
                should_run = True
                
        if should_run:
            print("Running scheduled 28-day database backup to Excel...")
            try:
                success_path = run_backup()
                if success_path:
                    with open(last_backup_file, "w") as f:
                        f.write(str(time.time()))
            except Exception as e:
                print(f"Backup failed: {e}")
                
        # Sleep for 1 hour before checking again
        time.sleep(60 * 60)

def start_backup_scheduler():
    t = threading.Thread(target=_backup_worker, daemon=True)
    t.start()
