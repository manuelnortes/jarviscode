"""Prueba de la capacidad de música por YouTube (Cast).

Por defecto hace comprobaciones SIN sonido (no castea nada):
  1. Resuelve la semilla de una consulta y expande el Mix → imprime la cola
     (debe haber variedad real: distintos temas, no la misma canción repetida).
  2. Resuelve la URL de audio de la primera pista (formato/codec).

Con --play (¡SUENA en el salón!): reproduce de verdad y deja el watcher
encadenando un par de pistas para validar el paso de una a otra.

Uso:
    python -m scripts.test_youtube                 # seco, sin sonido
    python -m scripts.test_youtube "rumba catalana"
    python -m scripts.test_youtube --play          # castea al Salón de verdad
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from src.capabilities import youtube

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    play = "--play" in args
    args = [a for a in args if a != "--play"]
    query = args[0] if args else "Estopa - Pastillas de freno"

    print(f"== Test capacidad · YouTube ==\nConsulta: {query!r}\n")

    # 1) Semilla + Mix (sin audio).
    print("→ Resolviendo semilla…")
    seed = await asyncio.to_thread(youtube._resolve_seed, query)
    print(f"  semilla: {seed['title']}  ({seed['id']})")

    print("\n→ Expandiendo el Mix…")
    queue = await asyncio.to_thread(youtube._expand_mix, seed["id"], seed["title"])
    print(f"  cola: {len(queue)} pistas")
    for i, q in enumerate(queue[:12]):
        print(f"   {i + 1:>2}. {q['title']}")
    uniq = len({q["id"] for q in queue})
    print(f"  únicas: {uniq}/{len(queue)}")
    if uniq < len(queue):
        print("  ⚠️ hay pistas repetidas en la cola (revisar dedupe).")

    # 2) Audio de la primera pista.
    print("\n→ Resolviendo audio de la 1ª pista…")
    audio = await asyncio.to_thread(youtube._resolve_audio, queue[0]["id"])
    print(f"  {audio['title']} → {audio['content_type']}")
    print(f"  url ok: {str(audio['url'])[:70]}…")

    if not play:
        print("\n✅ Pruebas secas OK. Lanza con --play para castear al Salón.")
        return

    # 3) Reproducción real + watcher (SUENA).
    print("\n→ youtube_play (¡suena en el Salón!)…")
    res = await youtube.youtube_play.handler({"query": query})
    print("  ", res["content"][0]["text"])
    print("  Dejando sonar 20 s y luego youtube_stop…")
    await asyncio.sleep(20)
    res = await youtube.youtube_stop.handler({})
    print("  ", res["content"][0]["text"])
    print("\n✅ Test con sonido terminado.")


if __name__ == "__main__":
    asyncio.run(main())
