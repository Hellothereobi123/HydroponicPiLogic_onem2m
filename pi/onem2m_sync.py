"""Entry point for hydro-onem2m.service: keep the oneM2M CSE up to date with the local database.

Only for HYDRO_BACKEND=onem2m. Run from the pi/ directory:  python3 onem2m_sync.py
"""

from config import BACKEND
from onem2m.uploader import run

if __name__ == "__main__":
    if BACKEND != "onem2m":
        raise SystemExit(f'HYDRO_BACKEND is "{BACKEND}"; set HYDRO_BACKEND=onem2m to use the CSE')
    run()
