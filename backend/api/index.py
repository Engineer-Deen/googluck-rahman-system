# Vercel entrypoint. Vercel's Python runtime looks for a WSGI-callable
# named `app` inside api/*.py. run.py already builds one at import time
# and safely skips starting the local-only background sync thread when
# GLR_MODE != "local" -- so importing it here is safe.
from run import app
