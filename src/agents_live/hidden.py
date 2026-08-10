"""Legacy entry point for persisted Windows tasks; removed in 7.0."""
from .runtime.hosts.hidden import main

if __name__ == "__main__":
    raise SystemExit(main())
