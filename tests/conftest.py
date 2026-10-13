import os, tempfile
from pathlib import Path

# Keep destructive test schema setup away from the user's real application DB.
test_db = Path(tempfile.gettempdir()) / f'mail_sender_pytest_{os.getpid()}.db'
os.environ['MAIL_DB_URL'] = f"sqlite:///{test_db.as_posix()}"
