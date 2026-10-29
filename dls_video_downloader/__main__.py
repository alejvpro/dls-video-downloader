from .cli import main
import signal


def interrupted(signum, frame):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, interrupted)

raise SystemExit(main())
