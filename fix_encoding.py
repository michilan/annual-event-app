from pathlib import Path
path = Path("annual_event_app/templates/admin/raffle.html")
text = path.read_text(encoding="cp950")
path.write_text(text, encoding="utf-8")
