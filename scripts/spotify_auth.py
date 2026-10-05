"""Autorización OAuth de Spotify para Jarvis (UNA sola vez, en el ordenador).

Abre el navegador para que autorices la app de Spotify con los scopes de control
de reproducción, y guarda el token cacheado en `SPOTIFY_CACHE`. A partir de ahí,
la capacidad (`src/capabilities/spotify.py`) lo lee y lo auto-refresca sola.

Requiere SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REDIRECT_URI en el
`.env`. El redirect URI debe estar dado de alta en la app de Spotify Developer
(http://127.0.0.1:8888/callback).

Uso:
    python -m scripts.spotify_auth

Tras autorizar, copia el token al servidor y de ahí al volumen jarvis-data:
    scp <SPOTIFY_CACHE local> <servidor>:/tmp/
    docker cp /tmp/spotify-cache jarvis:/app/data/spotify-cache
"""

from __future__ import annotations

import sys

import spotipy
from dotenv import load_dotenv

from src.capabilities import spotify

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main() -> None:
    print("== Autorización de Spotify para Jarvis ==\n")
    print("Se abrirá el navegador para que inicies sesión y autorices la app.")
    # open_browser=True: flujo interactivo en el PC. Crea/actualiza el cache.
    sp = spotipy.Spotify(auth_manager=spotify.make_auth(open_browser=True))
    me = sp.me()
    print(f"\n✅ Autorizado como: {me.get('display_name')} (id: {me.get('id')})")
    print(f"Token cacheado en: {spotify._CACHE_PATH}")

    devices = sp.devices().get("devices", [])
    if devices:
        print("\nDispositivos Spotify visibles ahora:")
        for d in devices:
            print(f"  - {d['name']}{' (activo)' if d.get('is_active') else ''}")
    else:
        print("\n(No hay dispositivos activos; abre Spotify en el altavoz para verlos.)")


if __name__ == "__main__":
    main()
