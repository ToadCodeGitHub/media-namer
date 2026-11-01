import os

# GUI tests run without opening windows (works on Windows, macOS and Linux).
if not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
