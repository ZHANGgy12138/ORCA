"""Provide writable application data and standard streams to frozen workers."""
import os
from pathlib import Path
import sys

data = Path(os.environ.get("PDFMT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ORCA")
os.environ["PDFMT_DATA_DIR"] = str(data)
os.environ["PDFMT_CACHE_DIR"] = str(data / "cache" / "babeldoc")
(data / "logs").mkdir(parents=True, exist_ok=True)
if sys.stdout is None:
    sys.stdout = (data / "logs" / "runtime.log").open("a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout
import truststore
truststore.inject_into_ssl()
sys._orca_truststore_ready = True
